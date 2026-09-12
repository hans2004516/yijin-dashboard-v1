from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


TOOL_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(__file__).resolve().parents[3]
ANALYSIS_PATH = PROJECT_ROOT / "gcp" / "member-analysis" / "main.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class DeidentifyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_module("deidentify_member_csv", TOOL_DIR / "deidentify.py")
        cls.analysis = load_module("member_analysis_for_deid", ANALYSIS_PATH)
        cls.secret = b"synthetic-deidentification-secret-32-bytes"

    def write_source(self, directory: Path, invalid_amount: bool = False) -> Path:
        path = directory / "source.csv"
        rows = [
            [
                "會員編號",
                "訂單編號",
                "交易日期",
                "消費金額",
                "品類",
                "是否促銷",
                "是否高單價",
                "姓名",
                "電話",
                "Email",
                "地址",
            ],
            [
                "RAW-M-001",
                "ORDER-001",
                "2025/01/05",
                "錯誤金額" if invalid_amount else "1,200",
                "枕頭",
                "否",
                "是",
                "測試甲",
                "0900000000",
                "test1@example.com",
                "測試地址一號",
            ],
            [
                "RAW-M-001",
                "ORDER-002",
                "2025-03-08",
                "800",
                "保潔墊",
                "是",
                "否",
                "測試甲",
                "0900000000",
                "test1@example.com",
                "測試地址一號",
            ],
            [
                "RAW-M-002",
                "ORDER-003",
                "2025-04-09",
                "3200",
                "床包組",
                "0",
                "1",
                "測試乙",
                "0911111111",
                "test2@example.com",
                "測試地址二號",
            ],
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows(rows)
        return path

    def write_feature_source(self, directory: Path, conflicting_duplicate: bool = False) -> Path:
        path = directory / "member_features.csv"
        headers = [
            "member_id",
            "total_tx",
            "total_amount",
            "avg_amount",
            "active_years",
            "tx_per_year",
            "avg_gap_days",
            "sale_ratio",
            "premium_ratio",
            "top_category",
            "top_cat_ratio",
            "is_loyal",
            "last_purchase_date",
            "recency_days",
            "max_amount",
            "姓名",
        ]
        rows = [
            [
                "RAW-M-001",
                "4",
                "5450",
                "1362.5",
                "3",
                "1.3",
                "343.3",
                "0.25",
                "0.5",
                "枕頭",
                "0.75",
                "1",
                "2025-11-05",
                "56",
                "1800",
                "測試甲",
            ],
            [
                "RAW-M-001",
                "5" if conflicting_duplicate else "4",
                "5450",
                "1362.5",
                "3",
                "1.3",
                "343.3",
                "0.25",
                "0.5",
                "枕頭",
                "0.75",
                "1",
                "2025-11-05",
                "56",
                "1800",
                "測試甲",
            ],
            [
                "RAW-M-002",
                "1",
                "3200",
                "3200",
                "1",
                "1",
                "0",
                "0",
                "1",
                "床包組",
                "1",
                "0",
                "2025-02-14",
                "320",
                "3200",
                "測試乙",
            ],
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(headers)
            writer.writerows(rows)
        return path

    def convert(self, directory: Path, source: Path, suffix: str, secret: bytes | None = None):
        output = directory / f"safe-{suffix}.csv"
        report = directory / f"safe-{suffix}.report.json"
        result = self.tool.convert_file(
            source,
            output,
            report,
            secret or self.secret,
        )
        return output, report, result

    def test_removes_pii_and_pseudonymizes_identifiers(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = self.write_source(directory)
            output, report_path, report = self.convert(directory, source, "one")

            self.assertTrue(output.read_bytes().startswith(b"\xef\xbb\xbf"))
            with output.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(len(rows), 3)
            self.assertEqual(len({row["member_id"] for row in rows}), 2)
            self.assertTrue(all(row["member_id"].startswith("ANON-M-") for row in rows))
            self.assertTrue(all(row["transaction_id"].startswith("ANON-T-") for row in rows))
            self.assertNotIn("RAW-M-001", output.read_text(encoding="utf-8-sig"))
            self.assertEqual(rows[0]["transaction_date"], "2025-01-05")
            self.assertEqual(rows[0]["amount"], "1200")
            self.assertEqual(rows[0]["is_premium"], "1")
            self.assertEqual(
                set(report["sensitive_columns_removed"]),
                {"姓名", "電話", "Email", "地址"},
            )
            saved_report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_report["stats"]["unique_members"], 2)
            serialized_report = json.dumps(saved_report, ensure_ascii=False)
            self.assertNotIn("RAW-M-001", serialized_report)
            self.assertNotIn("test1@example.com", serialized_report)

    def test_same_secret_is_deterministic_and_different_secret_is_isolated(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = self.write_source(directory)
            first, _, _ = self.convert(directory, source, "first")
            second, _, _ = self.convert(directory, source, "second")
            third, _, _ = self.convert(
                directory,
                source,
                "third",
                b"different-synthetic-secret-for-another-project",
            )

            with first.open("r", encoding="utf-8-sig", newline="") as handle:
                first_ids = [row["member_id"] for row in csv.DictReader(handle)]
            with second.open("r", encoding="utf-8-sig", newline="") as handle:
                second_ids = [row["member_id"] for row in csv.DictReader(handle)]
            with third.open("r", encoding="utf-8-sig", newline="") as handle:
                third_ids = [row["member_id"] for row in csv.DictReader(handle)]

            self.assertEqual(first_ids, second_ids)
            self.assertNotEqual(first_ids, third_ids)

    def test_invalid_source_does_not_create_partial_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = self.write_source(directory, invalid_amount=True)
            output = directory / "safe.csv"
            report = directory / "safe.report.json"

            with self.assertRaises(self.tool.DeidentificationError):
                self.tool.convert_file(source, output, report, self.secret)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())
            self.assertEqual(list(directory.glob("*.tmp")), [])

    def test_pii_hidden_in_category_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "source.csv"
            source.write_text(
                "member_id,amount,category\nRAW-M-001,100,test@example.com\n",
                encoding="utf-8",
            )
            output = directory / "safe.csv"
            report = directory / "safe.report.json"
            with self.assertRaises(self.tool.DeidentificationError):
                self.tool.convert_file(source, output, report, self.secret)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())

    def test_headers_with_outer_spaces_are_supported(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "source.csv"
            source.write_text(
                " member_id , amount , category \nRAW-M-001,100,枕頭\n",
                encoding="utf-8",
            )
            output, _, _ = self.convert(directory, source, "spaces")
            with output.open("r", encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertTrue(row["member_id"].startswith("ANON-M-"))
            self.assertEqual(row["category"], "枕頭")

    def test_short_secret_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = self.write_source(directory)
            output = directory / "safe.csv"
            report = directory / "safe.report.json"
            with self.assertRaises(self.tool.DeidentificationError):
                self.tool.convert_file(source, output, report, b"too-short")
            self.assertFalse(output.exists())

    def test_output_is_compatible_with_existing_analysis(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = self.write_source(directory)
            output, _, _ = self.convert(directory, source, "analysis")
            transactions, stats, has_date = self.analysis.read_transactions(
                output.read_bytes()
            )
            features = self.analysis.calculate_member_features(
                transactions,
                has_date,
                self.analysis.date(2025, 12, 31),
            )
            self.assertEqual(stats.valid_rows, 3)
            self.assertEqual(len(features), 2)
            self.assertTrue(all(row["member_id"].startswith("ANON-M-") for row in features))
            self.assertEqual({row["top_category"] for row in features}, {"枕頭", "床包組"})

    def test_feature_source_is_deidentified_deduplicated_and_analysis_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = self.write_feature_source(directory)
            output, report_path, report = self.convert(directory, source, "features")

            with output.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(report["input_mode"], "member-features")
            self.assertEqual(report["stats"]["input_rows"], 3)
            self.assertEqual(report["stats"]["output_rows"], 2)
            self.assertEqual(report["stats"]["duplicate_member_rows"], 1)
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row["member_id"].startswith("ANON-M-") for row in rows))
            self.assertNotIn("max_amount", rows[0])
            self.assertNotIn("姓名", rows[0])
            self.assertIn("姓名", report["sensitive_columns_removed"])

            mode = self.analysis.detect_csv_source_mode(output.read_bytes())
            features, stats = self.analysis.read_member_features(output.read_bytes())
            self.assertEqual(mode, "member-features")
            self.assertEqual(stats.valid_rows, 2)
            self.assertEqual(len(features), 2)
            pillow_feature = next(row for row in features if row["top_category"] == "枕頭")
            self.assertEqual(pillow_feature["last_purchase_date"], "2025-11-05")
            self.assertEqual(pillow_feature["recency_days"], 56)
            self.assertTrue(report_path.is_file())

    def test_conflicting_feature_duplicate_does_not_create_partial_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = self.write_feature_source(directory, conflicting_duplicate=True)
            output = directory / "safe.csv"
            report = directory / "safe.report.json"
            with self.assertRaises(self.tool.DeidentificationError):
                self.tool.convert_file(source, output, report, self.secret)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())


if __name__ == "__main__":
    unittest.main()
