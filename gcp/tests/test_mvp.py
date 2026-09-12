from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ANALYSIS_DIR = PROJECT_ROOT / "gcp" / "member-analysis"
API_DIR = PROJECT_ROOT / "gcp" / "dashboard-api"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class GcpMvpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_directory = tempfile.TemporaryDirectory()
        cls.storage_root = Path(cls.temp_directory.name)
        raw_directory = cls.storage_root / "raw"
        raw_directory.mkdir(parents=True)

        source_bytes = (
            ANALYSIS_DIR / "testdata" / "member_transactions.csv"
        ).read_bytes()
        # Explicitly verify UTF-8 BOM input handling.
        (raw_directory / "member_transactions.csv").write_bytes(
            b"\xef\xbb\xbf" + source_bytes
        )

        cls.old_environment = {
            name: os.environ.get(name)
            for name in (
                "GCS_BUCKET",
                "GCS_SOURCE_OBJECT",
                "HISTORY_RETENTION_DAYS",
                "HISTORY_MIN_COUNT",
                "LOCAL_STORAGE_ROOT",
                "ALLOWED_ORIGINS",
                "DASHBOARD_AUTH_MODE",
                "DASHBOARD_API_KEY",
            )
        }
        os.environ["GCS_BUCKET"] = "local-test-bucket"
        os.environ["LOCAL_STORAGE_ROOT"] = str(cls.storage_root)
        os.environ.pop("GCS_SOURCE_OBJECT", None)
        os.environ["HISTORY_RETENTION_DAYS"] = "90"
        os.environ["HISTORY_MIN_COUNT"] = "3"
        os.environ["ALLOWED_ORIGINS"] = "http://localhost:3000"
        os.environ["DASHBOARD_AUTH_MODE"] = "api-key"
        os.environ["DASHBOARD_API_KEY"] = "local-test-dashboard-key-32-bytes"

        cls.analysis = load_module("member_analysis_main", ANALYSIS_DIR / "main.py")
        cls.document = cls.analysis.run_analysis()
        cls.api = load_module("dashboard_api_app", API_DIR / "app.py")
        cls.client = cls.api.create_app().test_client()
        cls.auth_headers = {
            "Authorization": "Bearer local-test-dashboard-key-32-bytes"
        }

    @classmethod
    def tearDownClass(cls):
        for name, value in cls.old_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        cls.temp_directory.cleanup()

    def test_analysis_writes_latest_and_history(self):
        latest_path = self.storage_root / "processed" / "member_features.json"
        history_files = list((self.storage_root / "processed" / "history").glob("*.json"))
        self.assertTrue(latest_path.is_file())
        self.assertEqual(len(history_files), 1)

        payload = json.loads(latest_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], "member-features-v1")
        self.assertEqual(len(payload["records"]), 12)
        self.assertTrue(payload["generated_at"])
        self.assertTrue(payload["version"])
        self.assertEqual(len(payload["source"]["sha256"]), 64)

        required = {
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
        }
        self.assertTrue(required.issubset(payload["records"][0]))

    def test_unchanged_source_does_not_create_new_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "raw"
            raw.mkdir()
            source = raw / "member_transactions.csv"
            source.write_bytes(
                (ANALYSIS_DIR / "testdata" / "member_transactions.csv").read_bytes()
            )
            old_root = os.environ.get("LOCAL_STORAGE_ROOT")
            old_source = os.environ.get("GCS_SOURCE_OBJECT")
            os.environ["LOCAL_STORAGE_ROOT"] = str(root)
            os.environ["GCS_SOURCE_OBJECT"] = "raw/member_transactions.csv"
            try:
                first_document = self.analysis.run_analysis()
                first_latest = (
                    root / "processed" / "member_features.json"
                ).read_bytes()

                # Simulate uploading the exact same bytes as a new GCS generation.
                stat = source.stat()
                os.utime(
                    source,
                    ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000),
                )
                second_document = self.analysis.run_analysis()
            finally:
                if old_root is None:
                    os.environ.pop("LOCAL_STORAGE_ROOT", None)
                else:
                    os.environ["LOCAL_STORAGE_ROOT"] = old_root
                if old_source is None:
                    os.environ.pop("GCS_SOURCE_OBJECT", None)
                else:
                    os.environ["GCS_SOURCE_OBJECT"] = old_source

            history_files = list(
                (root / "processed" / "history").glob("member_features_*.json")
            )
            self.assertEqual(len(history_files), 1)
            self.assertEqual(first_document["version"], second_document["version"])
            self.assertEqual(
                first_latest,
                (root / "processed" / "member_features.json").read_bytes(),
            )

    def test_changed_source_creates_new_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "raw"
            raw.mkdir()
            source = raw / "member_transactions.csv"
            source.write_bytes(
                (ANALYSIS_DIR / "testdata" / "member_transactions.csv").read_bytes()
            )
            old_root = os.environ.get("LOCAL_STORAGE_ROOT")
            old_source = os.environ.get("GCS_SOURCE_OBJECT")
            os.environ["LOCAL_STORAGE_ROOT"] = str(root)
            os.environ["GCS_SOURCE_OBJECT"] = "raw/member_transactions.csv"
            try:
                first_document = self.analysis.run_analysis()
                source.write_bytes(source.read_bytes() + b"\n")
                second_document = self.analysis.run_analysis()
            finally:
                if old_root is None:
                    os.environ.pop("LOCAL_STORAGE_ROOT", None)
                else:
                    os.environ["LOCAL_STORAGE_ROOT"] = old_root
                if old_source is None:
                    os.environ.pop("GCS_SOURCE_OBJECT", None)
                else:
                    os.environ["GCS_SOURCE_OBJECT"] = old_source

            history_files = list(
                (root / "processed" / "history").glob("member_features_*.json")
            )
            self.assertEqual(len(history_files), 2)
            self.assertNotEqual(first_document["version"], second_document["version"])
            self.assertNotEqual(
                first_document["source"]["sha256"],
                second_document["source"]["sha256"],
            )

    def test_legacy_latest_uses_generation_for_unchanged_source(self):
        latest_document = {
            "source": {
                "object": "raw/member_features.csv",
                "generation": "12345",
            }
        }
        match_basis = self.analysis.unchanged_source_match(
            latest_document,
            "raw/member_features.csv",
            "12345",
            "a" * 64,
        )
        self.assertEqual(match_basis, "generation")

    def test_history_cleanup_keeps_recent_extra_and_deletes_old_extra(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "processed" / "history"
            history.mkdir(parents=True)
            now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
            ages = (1, 2, 3, 30, 100, 120)
            for age in ages:
                timestamp = now - timedelta(days=age)
                path = history / (
                    f"member_features_{timestamp.strftime('%Y%m%dT%H%M%S%fZ')}.json"
                )
                path.write_text("{}", encoding="utf-8")
                modified = timestamp.timestamp()
                os.utime(path, (modified, modified))

            unrelated = history / "manual_backup.json"
            unrelated.write_text("{}", encoding="utf-8")
            old_modified = (now - timedelta(days=500)).timestamp()
            os.utime(unrelated, (old_modified, old_modified))

            result = self.analysis.cleanup_history(
                self.analysis.LocalObjectStore(str(root)),
                now,
            )
            remaining = list(history.glob("member_features_*.json"))

            self.assertEqual(result["history_count_before"], 6)
            self.assertEqual(result["deleted_count"], 2)
            self.assertEqual(result["history_count_after"], 4)
            self.assertEqual(len(remaining), 4)
            self.assertTrue(unrelated.exists())

    def test_history_cleanup_never_drops_below_minimum_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "processed" / "history"
            history.mkdir(parents=True)
            now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
            ages = (100, 110, 120, 130, 140)
            for age in ages:
                timestamp = now - timedelta(days=age)
                path = history / (
                    f"member_features_{timestamp.strftime('%Y%m%dT%H%M%S%fZ')}.json"
                )
                path.write_text("{}", encoding="utf-8")
                modified = timestamp.timestamp()
                os.utime(path, (modified, modified))

            result = self.analysis.cleanup_history(
                self.analysis.LocalObjectStore(str(root)),
                now,
            )

            self.assertEqual(result["deleted_count"], 2)
            self.assertEqual(result["history_count_after"], 3)
            self.assertEqual(len(list(history.glob("member_features_*.json"))), 3)

    def test_existing_loyalty_rule_is_preserved(self):
        for row in self.document["records"]:
            self.assertEqual(row["is_loyal"], int(row["active_years"] >= 3))

    def test_unfiltered_dashboard_contract(self):
        response = self.client.get("/v1/dashboard", headers=self.auth_headers)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        for key in (
            "meta",
            "summary",
            "distributions",
            "insights",
            "records",
            "pagination",
        ):
            self.assertIn(key, payload)
        self.assertEqual(payload["meta"]["api_contract"], "v1")
        self.assertEqual(payload["meta"]["source"], "processed/member_features.json")
        self.assertEqual(payload["summary"]["member_count"], 12)
        self.assertEqual(payload["pagination"]["page_size"], 12)

    def test_all_dashboard_filters(self):
        cases = [
            ({"category": "枕頭"}, 2),
            ({"loyalty": "loyal"}, 3),
            ({"activeYears": "3"}, 2),
            ({"frequency": "10 筆以上"}, 1),
            ({"search": "m-0003"}, 1),
        ]
        for query, expected_count in cases:
            with self.subTest(query=query):
                response = self.client.get(
                    f"/v1/dashboard?{urlencode(query)}",
                    headers=self.auth_headers,
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.get_json()["summary"]["member_count"],
                    expected_count,
                )

    def test_pagination_and_page_size_limit(self):
        response = self.client.get(
            "/v1/dashboard?page=2&pageSize=10",
            headers=self.auth_headers,
        )
        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["pagination"]["page"], 2)
        self.assertEqual(payload["pagination"]["page_count"], 2)
        self.assertEqual(len(payload["records"]), 2)

        limited = self.client.get(
            "/v1/dashboard?pageSize=999",
            headers=self.auth_headers,
        ).get_json()
        self.assertEqual(limited["pagination"]["page_size"], 50)

    def test_csv_download_has_bom_and_traditional_chinese(self):
        query = urlencode({"category": "枕頭", "download": "csv"})
        response = self.client.get(
            f"/v1/dashboard?{query}",
            headers=self.auth_headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b"\xef\xbb\xbf"))
        text = response.data.decode("utf-8-sig")
        self.assertIn("忠誠會員", text)
        self.assertIn("枕頭", text)
        self.assertIn("attachment;", response.headers["Content-Disposition"])

    def test_invalid_parameters_return_400(self):
        for query in ("page=abc", "loyalty=unknown", "activeYears=zero", "download=xlsx"):
            with self.subTest(query=query):
                response = self.client.get(
                    f"/v1/dashboard?{query}",
                    headers=self.auth_headers,
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.get_json()["error"]["code"],
                    "INVALID_PARAMETER",
                )

    def test_missing_analysis_returns_safe_503(self):
        with tempfile.TemporaryDirectory() as empty_directory:
            repository = self.api.FeatureRepository(
                "local-test-bucket",
                empty_directory,
            )
            client = self.api.create_app(repository).test_client()
            response = client.get("/v1/dashboard", headers=self.auth_headers)
            payload = response.get_json()
            self.assertEqual(response.status_code, 503)
            self.assertEqual(
                payload["error"]["code"],
                "ANALYSIS_DATA_UNAVAILABLE",
            )
            serialized = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn("local-test-bucket", serialized)
            self.assertNotIn(empty_directory, serialized)
            self.assertNotIn("Traceback", serialized)

    def test_cors_only_allows_configured_origin(self):
        response = self.client.get(
            "/v1/dashboard",
            headers={
                **self.auth_headers,
                "Origin": "http://localhost:3000",
            },
        )
        self.assertEqual(
            response.headers["Access-Control-Allow-Origin"],
            "http://localhost:3000",
        )
        blocked = self.client.get(
            "/v1/dashboard",
            headers={
                **self.auth_headers,
                "Origin": "https://not-allowed.example",
            },
        )
        self.assertNotIn("Access-Control-Allow-Origin", blocked.headers)

    def test_api_key_is_required(self):
        missing = self.client.get("/v1/dashboard")
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(missing.get_json()["error"]["code"], "UNAUTHORIZED")

        invalid = self.client.get(
            "/v1/dashboard",
            headers={"Authorization": "Bearer invalid-key"},
        )
        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(invalid.get_json()["error"]["code"], "UNAUTHORIZED")

    def test_cloud_run_iam_mode_trusts_platform_authentication(self):
        os.environ["DASHBOARD_AUTH_MODE"] = "cloud-run-iam"
        try:
            iam_client = self.api.create_app().test_client()
            response = iam_client.get("/v1/dashboard")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["meta"]["api_contract"], "v1")
        finally:
            os.environ["DASHBOARD_AUTH_MODE"] = "api-key"

    def test_cors_preflight_allows_authorization_header(self):
        response = self.client.options(
            "/v1/dashboard",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Authorization", response.headers["Access-Control-Allow-Headers"])

    def test_failed_analysis_does_not_create_latest(self):
        with tempfile.TemporaryDirectory() as empty_directory:
            os.environ["LOCAL_STORAGE_ROOT"] = empty_directory
            try:
                with self.assertRaises(self.analysis.AnalysisError):
                    self.analysis.run_analysis()
                self.assertFalse(
                    (Path(empty_directory) / "processed" / "member_features.json").exists()
                )
            finally:
                os.environ["LOCAL_STORAGE_ROOT"] = str(self.storage_root)

    def test_precomputed_member_features_source_is_supported_and_deduplicated(self):
        headers = (
            "member_id,total_tx,total_amount,avg_amount,active_years,tx_per_year,"
            "avg_gap_days,sale_ratio,premium_ratio,top_category,top_cat_ratio,is_loyal\n"
        )
        rows = (
            "ANON-M-A,4,5450,1362.5,3,1.3,343.3,0.25,0.5,枕頭,0.75,1\n"
            "ANON-M-A,4,5450,1362.5,3,1.3,343.3,0.25,0.5,枕頭,0.75,1\n"
            "ANON-M-B,1,3200,3200,1,1,0,0,1,床包組,1,0\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "raw"
            raw.mkdir()
            (raw / "member_features.csv").write_text(
                headers + rows, encoding="utf-8-sig"
            )
            old_root = os.environ.get("LOCAL_STORAGE_ROOT")
            old_source = os.environ.get("GCS_SOURCE_OBJECT")
            os.environ["LOCAL_STORAGE_ROOT"] = str(root)
            os.environ["GCS_SOURCE_OBJECT"] = "raw/member_features.csv"
            try:
                document = self.analysis.run_analysis()
            finally:
                if old_root is None:
                    os.environ.pop("LOCAL_STORAGE_ROOT", None)
                else:
                    os.environ["LOCAL_STORAGE_ROOT"] = old_root
                if old_source is None:
                    os.environ.pop("GCS_SOURCE_OBJECT", None)
                else:
                    os.environ["GCS_SOURCE_OBJECT"] = old_source

            self.assertEqual(len(document["records"]), 2)
            self.assertEqual(document["source"]["object"], "raw/member_features.csv")
            self.assertEqual(document["source"]["input_mode"], "member-features")
            self.assertEqual(document["source"]["input_rows"], 3)
            self.assertEqual(document["source"]["valid_rows"], 2)
            self.assertEqual(document["source"]["duplicate_rows"], 1)

    def test_conflicting_feature_rows_do_not_create_latest(self):
        content = (
            "member_id,total_tx,total_amount,avg_amount,active_years,tx_per_year,"
            "avg_gap_days,sale_ratio,premium_ratio,top_category,top_cat_ratio,is_loyal\n"
            "ANON-M-A,4,5450,1362.5,3,1.3,343.3,0.25,0.5,枕頭,0.75,1\n"
            "ANON-M-A,5,5450,1362.5,3,1.3,343.3,0.25,0.5,枕頭,0.75,1\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "raw"
            raw.mkdir()
            (raw / "member_features.csv").write_text(content, encoding="utf-8")
            old_root = os.environ.get("LOCAL_STORAGE_ROOT")
            old_source = os.environ.get("GCS_SOURCE_OBJECT")
            os.environ["LOCAL_STORAGE_ROOT"] = str(root)
            os.environ["GCS_SOURCE_OBJECT"] = "raw/member_features.csv"
            try:
                with self.assertRaises(self.analysis.AnalysisError):
                    self.analysis.run_analysis()
            finally:
                if old_root is None:
                    os.environ.pop("LOCAL_STORAGE_ROOT", None)
                else:
                    os.environ["LOCAL_STORAGE_ROOT"] = old_root
                if old_source is None:
                    os.environ.pop("GCS_SOURCE_OBJECT", None)
                else:
                    os.environ["GCS_SOURCE_OBJECT"] = old_source
            self.assertFalse((root / "processed" / "member_features.json").exists())


if __name__ == "__main__":
    unittest.main()
