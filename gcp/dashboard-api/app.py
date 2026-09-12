"""Cloud Run Service exposing the single dashboard v1 business API."""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import secrets
import threading
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request


LATEST_OBJECT = "processed/member_features.json"
FREQUENCY_ORDER = ["1 筆", "2–3 筆", "4–9 筆", "10 筆以上"]
REQUIRED_FIELDS = {
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
}


class DataUnavailable(RuntimeError):
    """Processed data is absent, inaccessible, or malformed."""


class InvalidParameter(ValueError):
    """A request query parameter is invalid."""


def unauthorized_response() -> tuple[Response, int]:
    response = jsonify(
        {
            "error": {
                "code": "UNAUTHORIZED",
                "message": "API Key 無效或尚未提供，請重新輸入。",
            }
        }
    )
    response.headers["WWW-Authenticate"] = 'Bearer realm="dashboard-api"'
    response.headers["Cache-Control"] = "no-store"
    return response, 401


def structured_log(event: str, severity: str = "INFO", **fields: Any) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "severity": severity,
        "event": event,
        **fields,
    }
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False), flush=True)


def safe_local_path(root: Path, object_name: str) -> Path:
    resolved_root = root.resolve()
    candidate = (resolved_root / Path(object_name)).resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise DataUnavailable("分析資料路徑無法使用。")
    return candidate


class FeatureRepository:
    """Loads and caches one processed object, keyed by its storage version."""

    def __init__(self, bucket_name: str, local_root: str = ""):
        self.bucket_name = bucket_name
        self.local_root = Path(local_root).resolve() if local_root else None
        self._lock = threading.RLock()
        self._cache_key = ""
        self._cache_document: dict[str, Any] | None = None
        self._storage_client = None

    def get_document(self) -> dict[str, Any]:
        with self._lock:
            if self.local_root:
                return self._load_local()
            return self._load_gcs()

    def _load_local(self) -> dict[str, Any]:
        path = safe_local_path(self.local_root, LATEST_OBJECT)
        try:
            stat = path.stat()
        except FileNotFoundError as exc:
            raise DataUnavailable("分析結果尚未產生，請先執行會員分析工作。") from exc
        except OSError as exc:
            raise DataUnavailable("目前無法讀取分析資料，請稍後再試。") from exc

        cache_key = f"local:{stat.st_mtime_ns}:{stat.st_size}"
        if cache_key == self._cache_key and self._cache_document is not None:
            return self._cache_document

        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise DataUnavailable("目前無法讀取分析資料，請稍後再試。") from exc

        updated = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
        document = self._parse_document(payload, cache_key, updated, "local-storage")
        self._cache_key = cache_key
        self._cache_document = document
        return document

    def _load_gcs(self) -> dict[str, Any]:
        if not self.bucket_name:
            raise DataUnavailable("Dashboard API 尚未完成資料來源設定。")

        try:
            from google.api_core.exceptions import NotFound
            from google.cloud import storage

            if self._storage_client is None:
                self._storage_client = storage.Client()
            bucket = self._storage_client.bucket(self.bucket_name)
            blob = bucket.blob(LATEST_OBJECT)
            try:
                blob.reload()
            except NotFound as exc:
                raise DataUnavailable(
                    "分析結果尚未產生，請先執行會員分析工作。"
                ) from exc

            generation = str(blob.generation or "")
            updated = blob.updated.isoformat() if blob.updated else ""
            cache_key = f"gcs:{generation}:{updated}"
            if cache_key == self._cache_key and self._cache_document is not None:
                return self._cache_document

            versioned_blob = bucket.blob(LATEST_OBJECT, generation=blob.generation)
            payload = versioned_blob.download_as_bytes()
        except DataUnavailable:
            raise
        except Exception as exc:
            raise DataUnavailable("目前無法讀取分析資料，請稍後再試。") from exc

        document = self._parse_document(payload, cache_key, updated, "gcs")
        self._cache_key = cache_key
        self._cache_document = document
        return document

    def _parse_document(
        self,
        payload: bytes,
        object_version: str,
        object_updated: str,
        storage_mode: str,
    ) -> dict[str, Any]:
        try:
            parsed = json.loads(payload.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataUnavailable("分析資料格式無法使用，請重新執行分析工作。") from exc

        if isinstance(parsed, list):
            records = parsed
            generated_at = object_updated
            version = object_version
            schema_version = "legacy-list"
        elif isinstance(parsed, dict):
            records = parsed.get("records")
            generated_at = str(parsed.get("generated_at") or object_updated)
            version = str(parsed.get("version") or object_version)
            schema_version = str(parsed.get("schema_version") or "unknown")
        else:
            raise DataUnavailable("分析資料格式無法使用，請重新執行分析工作。")

        if not isinstance(records, list):
            raise DataUnavailable("分析資料格式無法使用，請重新執行分析工作。")

        normalized_records = [self._normalize_record(record) for record in records]
        return {
            "records": normalized_records,
            "generated_at": generated_at,
            "version": version,
            "schema_version": schema_version,
            "storage_mode": storage_mode,
        }

    @staticmethod
    def _normalize_record(record: Any) -> dict[str, Any]:
        if not isinstance(record, dict) or not REQUIRED_FIELDS.issubset(record):
            raise DataUnavailable("分析資料缺少必要會員欄位，請重新執行分析工作。")

        try:
            member_id = str(record["member_id"]).strip()
            top_category = str(record["top_category"]).strip()
            normalized = {
                "member_id": member_id,
                "total_tx": int(record["total_tx"]),
                "total_amount": float(record["total_amount"]),
                "avg_amount": float(record["avg_amount"]),
                "active_years": int(record["active_years"]),
                "tx_per_year": float(record["tx_per_year"]),
                "avg_gap_days": float(record["avg_gap_days"]),
                "sale_ratio": float(record["sale_ratio"]),
                "premium_ratio": float(record["premium_ratio"]),
                "top_category": top_category,
                "top_cat_ratio": float(record["top_cat_ratio"]),
                "is_loyal": bool(int(record["is_loyal"])),
            }
        except (TypeError, ValueError) as exc:
            raise DataUnavailable("分析資料欄位型別錯誤，請重新執行分析工作。") from exc

        numeric_values = [
            value
            for key, value in normalized.items()
            if key not in {"member_id", "top_category", "is_loyal"}
        ]
        if (
            not member_id
            or not top_category
            or any(not math.isfinite(value) for value in numeric_values)
        ):
            raise DataUnavailable("分析資料包含無效會員欄位，請重新執行分析工作。")
        return normalized


def js_round(value: float, digits: int = 2) -> float:
    scale = 10**digits
    return math.floor(value * scale + 0.5) / scale


def frequency_band(total_tx: int) -> str:
    if total_tx <= 1:
        return "1 筆"
    if total_tx <= 3:
        return "2–3 筆"
    if total_tx <= 9:
        return "4–9 筆"
    return "10 筆以上"


def parse_positive_integer(name: str, default: int) -> int:
    raw_value = request.args.get(name)
    if raw_value is None:
        return default
    if len(raw_value) > 9 or not re.fullmatch(r"\d+", raw_value):
        raise InvalidParameter(f"{name} 必須是正整數。")
    value = int(raw_value)
    if value < 1:
        raise InvalidParameter(f"{name} 必須是正整數。")
    return value


def parse_filters() -> dict[str, Any]:
    category = request.args.get("category", "all").strip()
    loyalty = request.args.get("loyalty", "all").strip()
    active_years_raw = request.args.get("activeYears", "all").strip()
    frequency = request.args.get("frequency", "all").strip()
    search = request.args.get("search", "").strip().casefold()
    download = request.args.get("download", "").strip()
    page = parse_positive_integer("page", 1)
    page_size = min(50, max(10, parse_positive_integer("pageSize", 12)))

    if not category or len(category) > 100:
        raise InvalidParameter("category 參數無效。")
    if loyalty not in {"all", "loyal", "non-loyal"}:
        raise InvalidParameter("loyalty 必須是 all、loyal 或 non-loyal。")
    if frequency not in {"all", *FREQUENCY_ORDER}:
        raise InvalidParameter("frequency 不是支援的頻次區間。")
    if len(search) > 100:
        raise InvalidParameter("search 最多 100 個字元。")
    if download not in {"", "csv"}:
        raise InvalidParameter("download 目前只支援 csv。")

    active_years: int | None = None
    if active_years_raw != "all":
        if not re.fullmatch(r"\d+", active_years_raw):
            raise InvalidParameter("activeYears 必須是 all 或正整數。")
        active_years = int(active_years_raw)
        if active_years < 1 or active_years > 100:
            raise InvalidParameter("activeYears 必須介於 1 到 100。")

    return {
        "category": category,
        "loyalty": loyalty,
        "activeYears": active_years_raw,
        "active_years_value": active_years,
        "frequency": frequency,
        "search": search,
        "page": page,
        "page_size": page_size,
        "download": download,
    }


def filter_rows(rows: list[dict[str, Any]], filters: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        if filters["category"] != "all" and row["top_category"] != filters["category"]:
            continue
        if filters["loyalty"] == "loyal" and not row["is_loyal"]:
            continue
        if filters["loyalty"] == "non-loyal" and row["is_loyal"]:
            continue
        if (
            filters["active_years_value"] is not None
            and row["active_years"] != filters["active_years_value"]
        ):
            continue
        if (
            filters["frequency"] != "all"
            and frequency_band(row["total_tx"]) != filters["frequency"]
        ):
            continue
        if filters["search"] and filters["search"] not in row["member_id"].casefold():
            continue
        result.append(row)
    return result


def build_csv_response(rows: list[dict[str, Any]]) -> Response:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(
        [
            "Member ID",
            "Loyalty",
            "Total Transactions",
            "Total Amount",
            "Average Amount",
            "Active Years",
            "Transactions Per Year",
            "Average Gap Days",
            "Top Category",
            "Top Category Ratio",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row["member_id"],
                "忠誠會員" if row["is_loyal"] else "一般會員",
                row["total_tx"],
                row["total_amount"],
                row["avg_amount"],
                row["active_years"],
                row["tx_per_year"],
                row["avg_gap_days"],
                row["top_category"],
                row["top_cat_ratio"],
            ]
        )

    response = Response(f"\ufeff{stream.getvalue()}")
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = (
        'attachment; filename="yijin-members-filtered.csv"'
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def build_dashboard_payload(
    source_rows: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    document: dict[str, Any],
    filters: dict[str, Any],
) -> dict[str, Any]:
    category_map: dict[str, dict[str, float]] = defaultdict(
        lambda: {"count": 0, "revenue": 0.0, "loyal": 0}
    )
    year_map: Counter[int] = Counter()
    frequency_map: Counter[str] = Counter()

    for row in rows:
        category_stats = category_map[row["top_category"]]
        category_stats["count"] += 1
        category_stats["revenue"] += row["total_amount"]
        category_stats["loyal"] += int(row["is_loyal"])
        year_map[row["active_years"]] += 1
        frequency_map[frequency_band(row["total_tx"])] += 1

    member_count = len(rows)
    transaction_count = sum(row["total_tx"] for row in rows)
    total_revenue = sum(row["total_amount"] for row in rows)
    loyal_rows = [row for row in rows if row["is_loyal"]]
    loyal_count = len(loyal_rows)
    valid_gap_rows = [row for row in rows if row["avg_gap_days"] > 0]
    repeat_count = sum(1 for row in rows if row["total_tx"] > 1)
    loyal_revenue = sum(row["total_amount"] for row in loyal_rows)

    categories = sorted(
        [
            {
                "label": label,
                "count": int(value["count"]),
                "share": (
                    js_round((value["count"] / member_count) * 100, 1)
                    if member_count
                    else 0
                ),
                "revenue": js_round(value["revenue"], 0),
                "loyal_rate": (
                    js_round((value["loyal"] / value["count"]) * 100, 1)
                    if value["count"]
                    else 0
                ),
            }
            for label, value in category_map.items()
        ],
        key=lambda item: (-item["count"], item["label"]),
    )

    page_count = max(1, math.ceil(member_count / filters["page_size"]))
    page = min(filters["page"], page_count)
    start = (page - 1) * filters["page_size"]
    records = rows[start : start + filters["page_size"]]
    source_category_counts = Counter(row["top_category"] for row in source_rows)

    return {
        "meta": {
            "source": LATEST_OBJECT,
            "mode": "gcp-api" if document["storage_mode"] == "gcs" else "local-gcs-emulator",
            "generated_at": document["generated_at"],
            "version": document["version"],
            "schema_version": document["schema_version"],
            "source_rows": len(source_rows),
            "filtered_rows": member_count,
            "api_contract": "v1",
        },
        "applied_filters": {
            "category": filters["category"],
            "loyalty": filters["loyalty"],
            "activeYears": filters["activeYears"],
            "frequency": filters["frequency"],
            "search": filters["search"],
        },
        "filter_options": {
            "categories": [
                label
                for label, _ in sorted(
                    source_category_counts.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ],
            "active_years": sorted({row["active_years"] for row in source_rows}),
            "frequency_bands": FREQUENCY_ORDER,
        },
        "summary": {
            "member_count": member_count,
            "transaction_count": transaction_count,
            "total_revenue": js_round(total_revenue, 0),
            "avg_transaction_value": (
                js_round(total_revenue / transaction_count, 0)
                if transaction_count
                else 0
            ),
            "loyal_count": loyal_count,
            "loyal_rate": (
                js_round((loyal_count / member_count) * 100, 1)
                if member_count
                else 0
            ),
            "repeat_member_rate": (
                js_round((repeat_count / member_count) * 100, 1)
                if member_count
                else 0
            ),
            "avg_gap_days": (
                js_round(
                    sum(row["avg_gap_days"] for row in valid_gap_rows)
                    / len(valid_gap_rows),
                    1,
                )
                if valid_gap_rows
                else None
            ),
            "avg_gap_sample": len(valid_gap_rows),
        },
        "distributions": {
            "categories": categories,
            "active_years": [
                {
                    "label": f"{label} 年",
                    "count": count,
                    "share": (
                        js_round((count / member_count) * 100, 1)
                        if member_count
                        else 0
                    ),
                }
                for label, count in sorted(year_map.items())
            ],
            "frequency": [
                {
                    "label": label,
                    "count": frequency_map[label],
                    "share": (
                        js_round((frequency_map[label] / member_count) * 100, 1)
                        if member_count
                        else 0
                    ),
                }
                for label in FREQUENCY_ORDER
            ],
            "loyalty": [
                {
                    "label": "忠誠會員",
                    "count": loyal_count,
                    "share": (
                        js_round((loyal_count / member_count) * 100, 1)
                        if member_count
                        else 0
                    ),
                },
                {
                    "label": "一般會員",
                    "count": member_count - loyal_count,
                    "share": (
                        js_round(
                            ((member_count - loyal_count) / member_count) * 100,
                            1,
                        )
                        if member_count
                        else 0
                    ),
                },
            ],
        },
        "insights": {
            "top_category": categories[0] if categories else None,
            "loyal_revenue_share": (
                js_round((loyal_revenue / total_revenue) * 100, 1)
                if total_revenue
                else 0
            ),
            "repeat_member_rate": (
                js_round((repeat_count / member_count) * 100, 1)
                if member_count
                else 0
            ),
            "valid_gap_sample": len(valid_gap_rows),
        },
        "records": [
            {
                "member_id": row["member_id"],
                "is_loyal": row["is_loyal"],
                "total_tx": row["total_tx"],
                "total_amount": row["total_amount"],
                "avg_amount": row["avg_amount"],
                "active_years": row["active_years"],
                "tx_per_year": row["tx_per_year"],
                "avg_gap_days": row["avg_gap_days"],
                "top_category": row["top_category"],
                "top_cat_ratio": row["top_cat_ratio"],
            }
            for row in records
        ],
        "pagination": {
            "page": page,
            "page_size": filters["page_size"],
            "page_count": page_count,
            "total": member_count,
        },
    }


def create_app(repository: FeatureRepository | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.json.ensure_ascii = False

    bucket_name = os.getenv("GCS_BUCKET", "").strip()
    local_root = os.getenv("LOCAL_STORAGE_ROOT", "").strip()
    feature_repository = repository or FeatureRepository(bucket_name, local_root)
    allowed_origins = {
        origin.strip()
        for origin in os.getenv("ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    }
    dashboard_auth_mode = os.getenv("DASHBOARD_AUTH_MODE", "api-key").strip().casefold()
    dashboard_api_key = os.getenv("DASHBOARD_API_KEY", "")

    @app.after_request
    def add_response_headers(response: Response) -> Response:
        origin = request.headers.get("Origin", "")
        if origin and (origin in allowed_origins or "*" in allowed_origins):
            response.headers["Access-Control-Allow-Origin"] = (
                "*" if "*" in allowed_origins else origin
            )
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Expose-Headers"] = "Content-Disposition"
            response.headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type"
            )
            response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @app.get("/v1/dashboard")
    def dashboard():
        if dashboard_auth_mode not in {"api-key", "cloud-run-iam"}:
            structured_log("dashboard_auth_mode_invalid", severity="ERROR")
            return (
                jsonify(
                    {
                        "error": {
                            "code": "AUTH_NOT_CONFIGURED",
                            "message": "服務驗證設定尚未完成，請聯絡管理者。",
                        }
                    }
                ),
                503,
            )

        if dashboard_auth_mode == "api-key" and not dashboard_api_key:
            structured_log("dashboard_auth_not_configured", severity="ERROR")
            return (
                jsonify(
                    {
                        "error": {
                            "code": "AUTH_NOT_CONFIGURED",
                            "message": "服務驗證設定尚未完成，請聯絡管理者。",
                        }
                    }
                ),
                503,
            )

        if dashboard_auth_mode == "api-key":
            authorization = request.headers.get("Authorization", "")
            scheme, separator, supplied_key = authorization.partition(" ")
            supplied_key = supplied_key.strip()
            if (
                not separator
                or scheme.casefold() != "bearer"
                or not supplied_key
                or len(supplied_key) > 512
                or not secrets.compare_digest(supplied_key, dashboard_api_key)
            ):
                structured_log("dashboard_auth_rejected", severity="WARNING")
                return unauthorized_response()

        try:
            filters = parse_filters()
            document = feature_repository.get_document()
            source_rows = document["records"]
            rows = filter_rows(source_rows, filters)

            if filters["download"] == "csv":
                return build_csv_response(rows)

            payload = build_dashboard_payload(source_rows, rows, document, filters)
            response = jsonify(payload)
            response.headers["Cache-Control"] = "no-store"
            return response
        except InvalidParameter as exc:
            return (
                jsonify(
                    {
                        "error": {
                            "code": "INVALID_PARAMETER",
                            "message": str(exc),
                        }
                    }
                ),
                400,
            )
        except DataUnavailable as exc:
            structured_log("dashboard_data_unavailable", severity="WARNING")
            return (
                jsonify(
                    {
                        "error": {
                            "code": "ANALYSIS_DATA_UNAVAILABLE",
                            "message": str(exc),
                        }
                    }
                ),
                503,
            )
        except Exception as exc:
            structured_log(
                "dashboard_request_failed",
                severity="ERROR",
                error_type=type(exc).__name__,
            )
            return (
                jsonify(
                    {
                        "error": {
                            "code": "INTERNAL_ERROR",
                            "message": "服務暫時無法處理請求，請稍後再試。",
                        }
                    }
                ),
                500,
            )

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
