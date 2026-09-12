from __future__ import annotations

import csv
import importlib.util
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOOL_DIR = PROJECT_ROOT / "tools" / "deidentify-member-csv"
JOB_PATH = PROJECT_ROOT / "gcp" / "member-deidentify" / "main.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class StoredValue:
    body: bytes
    generation: int
    metageneration: int
    metadata: dict[str, str]


class FakeStorage:
    def __init__(self, job):
        self.job = job
        self.values: dict[tuple[str, str], StoredValue] = {}
        self.download_count = 0

    def put(self, bucket: str, object_name: str, body: bytes) -> None:
        key = (bucket, object_name)
        previous = self.values.get(key)
        self.values[key] = StoredValue(
            body=body,
            generation=(previous.generation + 1 if previous else 1),
            metageneration=1,
            metadata={},
        )

    def stat(self, bucket: str, object_name: str):
        value = self.values.get((bucket, object_name))
        if value is None:
            return None
        return self.job.ObjectState(
            generation=str(value.generation),
            metageneration=value.metageneration,
            size=len(value.body),
            metadata=dict(value.metadata),
        )

    def download(
        self,
        bucket: str,
        object_name: str,
        destination: Path,
        expected_generation: str,
    ) -> None:
        value = self.values[(bucket, object_name)]
        if str(value.generation) != expected_generation:
            raise self.job.WriteConflictError("source generation changed")
        self.download_count += 1
        destination.write_bytes(value.body)

    def upload(
        self,
        bucket: str,
        object_name: str,
        source: Path,
        content_type: str,
        metadata: dict[str, str],
        expected_generation: str,
    ):
        del content_type
        key = (bucket, object_name)
        previous = self.values.get(key)
        actual_generation = str(previous.generation if previous else 0)
        if actual_generation != expected_generation:
            raise self.job.WriteConflictError("destination generation changed")
        generation = previous.generation + 1 if previous else 1
        self.values[key] = StoredValue(
            body=source.read_bytes(),
            generation=generation,
            metageneration=1,
            metadata=dict(metadata),
        )
        return self.stat(bucket, object_name)

    def patch_metadata(
        self,
        bucket: str,
        object_name: str,
        metadata: dict[str, str],
        expected_metageneration: int,
    ):
        value = self.values[(bucket, object_name)]
        if value.metageneration != expected_metageneration:
            raise self.job.WriteConflictError("destination metadata changed")
        value.metadata = dict(metadata)
        value.metageneration += 1
        return self.stat(bucket, object_name)


class MemberDeidentifyJobTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(TOOL_DIR))
        cls.job = load_module("member_deidentify_job", JOB_PATH)
        cls.secret = b"synthetic-deidentification-secret-32-bytes"

    def config(self):
        return self.job.JobConfig(
            source_bucket="private-source",
            source_object="incoming/member_features.csv",
            destination_bucket="analysis-data",
            destination_object="raw/member_features.csv",
            report_prefix="audit/deidentification",
            key_version="1",
            max_source_bytes=1024 * 1024,
            allow_initial_replacement=False,
        )

    @staticmethod
    def source_csv(invalid: bool = False) -> bytes:
        amount = "not-a-number" if invalid else "3000"
        text = (
            "member_id,total_tx,total_amount,avg_amount,active_years,tx_per_year,"
            "avg_gap_days,sale_ratio,premium_ratio,top_category,top_cat_ratio,"
            "is_loyal,姓名,電話\n"
            f"REAL-001,3,{amount},1000,3,1,180,0.2,0.5,枕頭,0.7,1,測試甲,0900000000\n"
            "REAL-002,1,500,500,1,1,0,0,0,床包組,1,0,測試乙,0911111111\n"
        )
        return text.encode("utf-8-sig")

    def test_publishes_only_deidentified_csv_and_audit_report(self):
        store = FakeStorage(self.job)
        config = self.config()
        store.put(config.source_bucket, config.source_object, self.source_csv())

        result = self.job.run_deidentification(store, config, self.secret)

        self.assertEqual(result["event"], "deidentification_succeeded")
        destination = store.values[(config.destination_bucket, config.destination_object)]
        text = destination.body.decode("utf-8-sig")
        self.assertNotIn("REAL-001", text)
        self.assertNotIn("測試甲", text)
        self.assertNotIn("0900000000", text)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "safe.csv"
            path.write_bytes(destination.body)
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        self.assertTrue(all(row["member_id"].startswith("ANON-M-") for row in rows))
        self.assertEqual(destination.metadata["source_generation"], "1")
        self.assertEqual(destination.metadata["deidentification_key_version"], "1")
        audit_key = (config.destination_bucket, result["audit_report_object"])
        self.assertIn(audit_key, store.values)
        audit_text = store.values[audit_key].body.decode("utf-8")
        self.assertNotIn("REAL-001", audit_text)
        self.assertNotIn("0900000000", audit_text)

    def test_same_generation_skips_without_downloading(self):
        store = FakeStorage(self.job)
        config = self.config()
        store.put(config.source_bucket, config.source_object, self.source_csv())
        self.job.run_deidentification(store, config, self.secret)
        downloads_after_first_run = store.download_count

        result = self.job.run_deidentification(store, config, self.secret)

        self.assertEqual(result["event"], "deidentification_skipped")
        self.assertEqual(result["match_basis"], "generation")
        self.assertEqual(store.download_count, downloads_after_first_run)

    def test_first_migration_only_adds_metadata_when_output_matches(self):
        store = FakeStorage(self.job)
        config = self.config()
        store.put(config.source_bucket, config.source_object, self.source_csv())

        baseline_store = FakeStorage(self.job)
        baseline_store.put(config.source_bucket, config.source_object, self.source_csv())
        baseline_result = self.job.run_deidentification(
            baseline_store, config, self.secret
        )
        baseline = baseline_store.values[
            (config.destination_bucket, config.destination_object)
        ].body
        store.put(config.destination_bucket, config.destination_object, baseline)
        before = store.stat(config.destination_bucket, config.destination_object)

        result = self.job.run_deidentification(store, config, self.secret)
        after = store.stat(config.destination_bucket, config.destination_object)

        self.assertEqual(result["publication_mode"], "metadata_only")
        self.assertEqual(before.generation, after.generation)
        self.assertEqual(after.metadata["source_generation"], "1")

    def test_first_migration_rejects_different_existing_output(self):
        store = FakeStorage(self.job)
        config = self.config()
        store.put(config.source_bucket, config.source_object, self.source_csv())
        store.put(
            config.destination_bucket,
            config.destination_object,
            b"member_id,total_tx\nANON-M-EXISTING,1\n",
        )
        before = store.values[
            (config.destination_bucket, config.destination_object)
        ].body

        with self.assertRaises(self.job.CloudDeidentificationError):
            self.job.run_deidentification(store, config, self.secret)

        after = store.values[(config.destination_bucket, config.destination_object)].body
        self.assertEqual(after, before)
        self.assertFalse(
            any(name.startswith(config.report_prefix) for _, name in store.values)
        )

    def test_same_content_new_generation_does_not_rewrite_output(self):
        store = FakeStorage(self.job)
        config = self.config()
        content = self.source_csv()
        store.put(config.source_bucket, config.source_object, content)
        self.job.run_deidentification(store, config, self.secret)
        first_destination = store.stat(config.destination_bucket, config.destination_object)
        store.put(config.source_bucket, config.source_object, content)

        result = self.job.run_deidentification(store, config, self.secret)
        second_destination = store.stat(config.destination_bucket, config.destination_object)

        self.assertEqual(result["match_basis"], "sha256")
        self.assertEqual(first_destination.generation, second_destination.generation)
        self.assertEqual(second_destination.metadata["source_generation"], "2")

    def test_invalid_source_does_not_publish_destination_or_report(self):
        store = FakeStorage(self.job)
        config = self.config()
        store.put(config.source_bucket, config.source_object, self.source_csv(invalid=True))

        with self.assertRaises(self.job.DeidentificationError):
            self.job.run_deidentification(store, config, self.secret)

        self.assertIsNone(store.stat(config.destination_bucket, config.destination_object))
        self.assertFalse(
            any(name.startswith(config.report_prefix) for _, name in store.values)
        )

    def test_object_paths_are_restricted_to_expected_prefixes(self):
        with self.assertRaises(self.job.CloudDeidentificationError):
            self.job.validate_object_name(
                "processed/member_features.csv", "incoming/", ".csv", "SOURCE_OBJECT"
            )
        with self.assertRaises(self.job.CloudDeidentificationError):
            self.job.validate_object_name(
                "raw/../member_features.csv", "raw/", ".csv", "DESTINATION_OBJECT"
            )


if __name__ == "__main__":
    unittest.main()
