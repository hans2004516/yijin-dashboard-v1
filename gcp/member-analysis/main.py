"""Cloud Run Job entry point for producing dashboard member features."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_SOURCE_OBJECT = "raw/member_transactions.csv"
DEFAULT_LATEST_OBJECT = "processed/member_features.json"
DEFAULT_HISTORY_PREFIX = "processed/history"
# Backward-compatible aliases used by the existing v1 tests and callers.
LATEST_OBJECT = DEFAULT_LATEST_OBJECT
HISTORY_PREFIX = DEFAULT_HISTORY_PREFIX
HISTORY_OBJECT_PREFIX = f"{HISTORY_PREFIX}/member_features_"
DEFAULT_HISTORY_RETENTION_DAYS = 90
DEFAULT_HISTORY_MIN_COUNT = 3
LATEST_METADATA_PREFIX_BYTES = 16 * 1024
HISTORY_OBJECT_PATTERN = re.compile(
    rf"^{re.escape(HISTORY_OBJECT_PREFIX)}\d{{8}}T\d{{12}}Z\.json$"
)
SCHEMA_VERSION = "member-features-v1"

COLUMN_ALIASES = {
    "member_id": ("member_id", "會員編號", "會員id", "會員代碼"),
    "transaction_id": ("transaction_id", "交易編號", "訂單編號", "發票編號"),
    "transaction_date": (
        "transaction_date",
        "交易日期",
        "消費日期",
        "購買日期",
        "訂單日期",
    ),
    "amount": ("amount", "transaction_amount", "交易金額", "消費金額", "訂單金額"),
    "category": ("category", "product_category", "品類", "商品類別", "產品類別"),
    "is_sale": ("is_sale", "sale_flag", "促銷品", "特價品", "是否促銷"),
    "is_premium": (
        "is_premium",
        "premium_flag",
        "高單價品",
        "高級品",
        "是否高單價",
    ),
}
FEATURE_REQUIRED_FIELDS = (
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
)
FEATURE_OPTIONAL_FIELDS = ("last_purchase_date", "recency_days")
FEATURE_COLUMN_ALIASES = {
    "member_id": ("member_id", "會員編號", "會員id", "會員代碼"),
    "total_tx": ("total_tx", "總消費次數", "總交易次數"),
    "total_amount": ("total_amount", "總消費金額", "累積消費金額"),
    "avg_amount": ("avg_amount", "平均交易金額", "平均消費金額", "客單價"),
    "active_years": ("active_years", "活躍年數"),
    "tx_per_year": ("tx_per_year", "年均交易次數"),
    "avg_gap_days": ("avg_gap_days", "平均間隔天數"),
    "sale_ratio": ("sale_ratio", "促銷占比"),
    "premium_ratio": ("premium_ratio", "高單價占比"),
    "top_category": ("top_category", "主要消費品類", "主要品類"),
    "top_cat_ratio": ("top_cat_ratio", "主要品類占比"),
    "is_loyal": ("is_loyal", "忠誠會員", "是否忠誠"),
    "last_purchase_date": ("last_purchase_date", "最近購買日"),
    "recency_days": ("recency_days", "距最近購買天數"),
}

TRUE_VALUES = {"1", "true", "t", "yes", "y", "是", "有"}
FALSE_VALUES = {"", "0", "false", "f", "no", "n", "否", "無"}
DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y%m%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
)


class AnalysisError(RuntimeError):
    """Expected input, configuration, or storage error."""


@dataclass
class CleanStats:
    input_rows: int = 0
    valid_rows: int = 0
    skipped_rows: int = 0
    duplicate_rows: int = 0
    invalid_date_rows: int = 0
    invalid_flag_values: int = 0


@dataclass(frozen=True)
class StoredObject:
    name: str
    updated: datetime
    generation: int | None = None


@dataclass(frozen=True)
class OutputLayout:
    latest_object: str
    history_prefix: str

    @property
    def history_object_prefix(self) -> str:
        return f"{self.history_prefix}/member_features_"

    @property
    def history_object_pattern(self) -> re.Pattern[str]:
        return re.compile(
            rf"^{re.escape(self.history_object_prefix)}"
            rf"\d{{8}}T\d{{12}}Z\.json$"
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def structured_log(event: str, severity: str = "INFO", **fields: Any) -> None:
    payload = {
        "timestamp": utc_now().isoformat().replace("+00:00", "Z"),
        "severity": severity,
        "event": event,
        **fields,
    }
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False), flush=True)


def normalize_header(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", value.strip().casefold())


def resolve_aliases(
    fieldnames: Iterable[str] | None,
    aliases_by_field: dict[str, tuple[str, ...]],
) -> dict[str, str]:
    if not fieldnames:
        raise AnalysisError("來源 CSV 沒有標題列。")

    names = list(fieldnames)
    if any(name is None or not name.strip().lstrip("\ufeff") for name in names):
        raise AnalysisError("來源 CSV 包含空白欄位名稱。")
    normalized_names = [normalize_header(name.lstrip("\ufeff")) for name in names]
    if len(normalized_names) != len(set(normalized_names)):
        raise AnalysisError("來源 CSV 包含重複欄位名稱。")

    actual_by_normalized = {
        normalize_header(name.lstrip("\ufeff")): name for name in names
    }
    resolved: dict[str, str] = {}
    for canonical, aliases in aliases_by_field.items():
        for alias in aliases:
            actual = actual_by_normalized.get(normalize_header(alias))
            if actual:
                resolved[canonical] = actual
                break
    return resolved


def resolve_columns(fieldnames: Iterable[str] | None) -> dict[str, str]:
    resolved = resolve_aliases(fieldnames, COLUMN_ALIASES)

    missing = [name for name in ("member_id", "amount", "category") if name not in resolved]
    if missing:
        raise AnalysisError(f"來源 CSV 缺少必要欄位：{', '.join(missing)}。")
    return resolved


def resolve_feature_columns(fieldnames: Iterable[str] | None) -> dict[str, str]:
    resolved = resolve_aliases(fieldnames, FEATURE_COLUMN_ALIASES)
    missing = [name for name in FEATURE_REQUIRED_FIELDS if name not in resolved]
    if missing:
        raise AnalysisError(f"會員特徵 CSV 缺少必要欄位：{', '.join(missing)}。")
    return resolved


def detect_source_mode(fieldnames: Iterable[str] | None) -> str:
    transaction_columns = resolve_aliases(fieldnames, COLUMN_ALIASES)
    feature_columns = resolve_aliases(fieldnames, FEATURE_COLUMN_ALIASES)
    if all(name in feature_columns for name in FEATURE_REQUIRED_FIELDS):
        return "member-features"
    if all(name in transaction_columns for name in ("member_id", "amount", "category")):
        return "transactions"
    raise AnalysisError(
        "來源 CSV 既不符合交易明細格式，也不符合會員特徵格式；請檢查必要欄位。"
    )


def parse_amount(value: Any) -> float:
    text = str(value or "").strip()
    text = (
        text.replace(",", "")
        .replace("NT$", "")
        .replace("TWD", "")
        .replace("$", "")
        .strip()
    )
    amount = float(text)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("amount must be a finite non-negative number")
    return amount


def parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None

    iso_text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso_text).date()
    except ValueError:
        pass

    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    raise ValueError("unsupported date format")


def parse_flag(value: Any) -> tuple[int, bool]:
    normalized = str(value or "").strip().casefold()
    if normalized in TRUE_VALUES:
        return 1, True
    if normalized in FALSE_VALUES:
        return 0, True
    return 0, False


def parse_non_negative_number(value: Any, field_name: str) -> float:
    text = str(value or "").strip().replace(",", "")
    try:
        number = float(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field_name} must be a finite non-negative number")
    return number


def parse_positive_integer(value: Any, field_name: str) -> int:
    number = parse_non_negative_number(value, field_name)
    if not number.is_integer() or number < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return int(number)


def parse_ratio(value: Any, field_name: str) -> float:
    number = parse_non_negative_number(value, field_name)
    if number > 1:
        raise ValueError(f"{field_name} must be between 0 and 1")
    return number


def read_transactions(csv_bytes: bytes) -> tuple[list[dict[str, Any]], CleanStats, bool]:
    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AnalysisError("來源 CSV 必須使用 UTF-8 或 UTF-8 BOM 編碼。") from exc

    reader = csv.DictReader(io.StringIO(text, newline=""))
    columns = resolve_columns(reader.fieldnames)
    has_date_column = "transaction_date" in columns
    transactions: list[dict[str, Any]] = []
    stats = CleanStats()
    seen_transaction_ids: set[tuple[str, str]] = set()

    for row in reader:
        stats.input_rows += 1
        member_id = str(row.get(columns["member_id"], "") or "").strip()
        category = str(row.get(columns["category"], "") or "").strip() or "未分類"

        try:
            amount = parse_amount(row.get(columns["amount"]))
        except (TypeError, ValueError):
            stats.skipped_rows += 1
            continue

        if not member_id:
            stats.skipped_rows += 1
            continue

        transaction_id = ""
        if "transaction_id" in columns:
            transaction_id = str(row.get(columns["transaction_id"], "") or "").strip()
            if transaction_id:
                transaction_key = (member_id, transaction_id)
                if transaction_key in seen_transaction_ids:
                    stats.duplicate_rows += 1
                    continue
                seen_transaction_ids.add(transaction_key)

        transaction_date = None
        if has_date_column:
            try:
                transaction_date = parse_date(row.get(columns["transaction_date"]))
            except ValueError:
                stats.invalid_date_rows += 1

        is_sale = 0
        if "is_sale" in columns:
            is_sale, is_valid = parse_flag(row.get(columns["is_sale"]))
            if not is_valid:
                stats.invalid_flag_values += 1

        is_premium = 0
        if "is_premium" in columns:
            is_premium, is_valid = parse_flag(row.get(columns["is_premium"]))
            if not is_valid:
                stats.invalid_flag_values += 1

        transactions.append(
            {
                "member_id": member_id,
                "transaction_id": transaction_id,
                "transaction_date": transaction_date,
                "amount": amount,
                "category": category,
                "is_sale": is_sale,
                "is_premium": is_premium,
            }
        )
        stats.valid_rows += 1

    if not transactions:
        raise AnalysisError("來源 CSV 清理後沒有可分析的有效交易。")
    return transactions, stats, has_date_column


def read_member_features(csv_bytes: bytes) -> tuple[list[dict[str, Any]], CleanStats]:
    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AnalysisError("來源 CSV 必須使用 UTF-8 或 UTF-8 BOM 編碼。") from exc

    reader = csv.DictReader(io.StringIO(text, newline=""))
    columns = resolve_feature_columns(reader.fieldnames)
    optional_fields = [name for name in FEATURE_OPTIONAL_FIELDS if name in columns]
    records: list[dict[str, Any]] = []
    stats = CleanStats()
    seen_members: dict[str, tuple[Any, ...]] = {}

    for row_number, row in enumerate(reader, start=2):
        stats.input_rows += 1
        if None in row:
            raise AnalysisError(f"會員特徵 CSV 第 {row_number} 列的欄位數量超過標題列。")

        member_id = str(row.get(columns["member_id"], "") or "").strip()
        top_category = str(row.get(columns["top_category"], "") or "").strip()
        if not member_id:
            raise AnalysisError(f"會員特徵 CSV 第 {row_number} 列缺少會員編號。")
        if len(member_id) > 500:
            raise AnalysisError(f"會員特徵 CSV 第 {row_number} 列的會員編號長度不合理。")
        if not top_category or len(top_category) > 200 or "\x00" in top_category:
            raise AnalysisError(f"會員特徵 CSV 第 {row_number} 列的主要品類無效。")

        try:
            total_tx = parse_positive_integer(row.get(columns["total_tx"]), "total_tx")
            total_amount = parse_non_negative_number(
                row.get(columns["total_amount"]), "total_amount"
            )
            avg_amount = parse_non_negative_number(
                row.get(columns["avg_amount"]), "avg_amount"
            )
            active_years = parse_positive_integer(
                row.get(columns["active_years"]), "active_years"
            )
            tx_per_year = parse_non_negative_number(
                row.get(columns["tx_per_year"]), "tx_per_year"
            )
            avg_gap_days = parse_non_negative_number(
                row.get(columns["avg_gap_days"]), "avg_gap_days"
            )
            sale_ratio = parse_ratio(row.get(columns["sale_ratio"]), "sale_ratio")
            premium_ratio = parse_ratio(
                row.get(columns["premium_ratio"]), "premium_ratio"
            )
            top_cat_ratio = parse_ratio(
                row.get(columns["top_cat_ratio"]), "top_cat_ratio"
            )
        except ValueError as exc:
            raise AnalysisError(
                f"會員特徵 CSV 第 {row_number} 列包含無效數值。"
            ) from exc

        loyal_text = str(row.get(columns["is_loyal"], "") or "").strip()
        if not loyal_text:
            raise AnalysisError(f"會員特徵 CSV 第 {row_number} 列缺少忠誠會員旗標。")
        is_loyal, is_valid_flag = parse_flag(loyal_text)
        if not is_valid_flag:
            raise AnalysisError(f"會員特徵 CSV 第 {row_number} 列的忠誠會員旗標無效。")
        if is_loyal != int(active_years >= 3):
            raise AnalysisError(
                f"會員特徵 CSV 第 {row_number} 列不符合既有忠誠會員規則。"
            )

        record: dict[str, Any] = {
            "member_id": member_id,
            "total_tx": total_tx,
            "total_amount": rounded(total_amount, 2),
            "avg_amount": rounded(avg_amount, 1),
            "active_years": active_years,
            "tx_per_year": rounded(tx_per_year, 1),
            "avg_gap_days": rounded(avg_gap_days, 1),
            "sale_ratio": rounded(sale_ratio, 3),
            "premium_ratio": rounded(premium_ratio, 3),
            "top_category": top_category,
            "top_cat_ratio": rounded(top_cat_ratio, 3),
            "is_loyal": is_loyal,
        }

        if "last_purchase_date" in optional_fields:
            try:
                last_purchase_date = parse_date(row.get(columns["last_purchase_date"]))
            except ValueError as exc:
                raise AnalysisError(
                    f"會員特徵 CSV 第 {row_number} 列的最近購買日格式錯誤。"
                ) from exc
            record["last_purchase_date"] = (
                last_purchase_date.isoformat() if last_purchase_date else None
            )
        if "recency_days" in optional_fields:
            recency_text = str(row.get(columns["recency_days"], "") or "").strip()
            if not recency_text:
                record["recency_days"] = None
            else:
                try:
                    recency_days = parse_non_negative_number(recency_text, "recency_days")
                except ValueError as exc:
                    raise AnalysisError(
                        f"會員特徵 CSV 第 {row_number} 列的 recency_days 無效。"
                    ) from exc
                if not recency_days.is_integer():
                    raise AnalysisError(
                        f"會員特徵 CSV 第 {row_number} 列的 recency_days 必須是整數。"
                    )
                record["recency_days"] = int(recency_days)

        signature = tuple(
            record.get(name) for name in (*FEATURE_REQUIRED_FIELDS[1:], *optional_fields)
        )
        previous = seen_members.get(member_id)
        if previous is not None:
            if previous != signature:
                raise AnalysisError(
                    f"會員特徵 CSV 第 {row_number} 列與相同會員的既有特徵衝突。"
                )
            stats.duplicate_rows += 1
            continue

        seen_members[member_id] = signature
        records.append(record)
        stats.valid_rows += 1

    if not records:
        raise AnalysisError("來源 CSV 沒有可用的會員特徵資料。")
    records.sort(key=lambda record: record["member_id"])
    return records, stats


def rounded(value: float, digits: int) -> float:
    return round(value + 0.0, digits)


def detect_csv_source_mode(csv_bytes: bytes) -> str:
    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AnalysisError("來源 CSV 必須使用 UTF-8 或 UTF-8 BOM 編碼。") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    return detect_source_mode(reader.fieldnames)


def calculate_member_features(
    transactions: list[dict[str, Any]],
    has_date_column: bool,
    as_of_date: date,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for transaction in transactions:
        grouped[transaction["member_id"]].append(transaction)

    features: list[dict[str, Any]] = []
    for member_id in sorted(grouped):
        rows = grouped[member_id]
        total_tx = len(rows)
        total_amount = sum(row["amount"] for row in rows)
        category_counts = Counter(row["category"] for row in rows)
        category_amounts: dict[str, float] = defaultdict(float)
        for row in rows:
            category_amounts[row["category"]] += row["amount"]

        top_category = sorted(
            category_counts,
            key=lambda label: (
                -category_counts[label],
                -category_amounts[label],
                label,
            ),
        )[0]

        purchase_dates = sorted(
            {row["transaction_date"] for row in rows if row["transaction_date"] is not None}
        )
        active_years = len({value.year for value in purchase_dates}) if purchase_dates else 1
        gaps = [
            (current - previous).days
            for previous, current in zip(purchase_dates, purchase_dates[1:])
        ]

        member_feature: dict[str, Any] = {
            "member_id": member_id,
            "total_tx": total_tx,
            "total_amount": rounded(total_amount, 2),
            "avg_amount": rounded(total_amount / total_tx, 1),
            "active_years": active_years,
            "tx_per_year": rounded(total_tx / active_years, 1),
            "avg_gap_days": rounded(sum(gaps) / len(gaps), 1) if gaps else 0.0,
            "sale_ratio": rounded(sum(row["is_sale"] for row in rows) / total_tx, 3),
            "premium_ratio": rounded(
                sum(row["is_premium"] for row in rows) / total_tx,
                3,
            ),
            "top_category": top_category,
            "top_cat_ratio": rounded(category_counts[top_category] / total_tx, 3),
            # Existing dashboard label logic: active in at least three calendar years.
            "is_loyal": int(active_years >= 3),
        }

        if has_date_column:
            last_purchase_date = purchase_dates[-1] if purchase_dates else None
            member_feature["last_purchase_date"] = (
                last_purchase_date.isoformat() if last_purchase_date else None
            )
            member_feature["recency_days"] = (
                max(0, (as_of_date - last_purchase_date).days)
                if last_purchase_date
                else None
            )

        features.append(member_feature)

    return features


def safe_object_path(root: Path, object_name: str) -> Path:
    candidate = (root / Path(object_name)).resolve()
    resolved_root = root.resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise AnalysisError("物件路徑超出允許的本機測試目錄。")
    return candidate


class LocalObjectStore:
    def __init__(self, root: str):
        self.root = Path(root).resolve()

    def read_bytes(self, object_name: str) -> tuple[bytes, str]:
        path = safe_object_path(self.root, object_name)
        try:
            stat = path.stat()
            return path.read_bytes(), f"local-{stat.st_mtime_ns}-{stat.st_size}"
        except FileNotFoundError as exc:
            raise AnalysisError(f"找不到來源物件：{object_name}。") from exc

    def write_bytes(self, object_name: str, payload: bytes) -> None:
        path = safe_object_path(self.root, object_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_name = temp_file.name
                temp_file.write(payload)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_name, path)
        finally:
            if temp_name:
                temp_path = Path(temp_name)
                if temp_path.exists():
                    temp_path.unlink()

    def read_prefix(self, object_name: str, max_bytes: int) -> bytes:
        path = safe_object_path(self.root, object_name)
        try:
            with path.open("rb") as source_file:
                return source_file.read(max_bytes)
        except FileNotFoundError as exc:
            raise AnalysisError(f"找不到來源物件：{object_name}。") from exc

    def list_objects(self, prefix: str) -> list[StoredObject]:
        prefix_path = safe_object_path(self.root, prefix)
        search_root = prefix_path if prefix_path.is_dir() else prefix_path.parent
        if not search_root.exists():
            return []

        objects: list[StoredObject] = []
        for path in search_root.rglob("*"):
            if not path.is_file():
                continue
            object_name = path.relative_to(self.root).as_posix()
            if not object_name.startswith(prefix):
                continue
            stat = path.stat()
            objects.append(
                StoredObject(
                    name=object_name,
                    updated=datetime.fromtimestamp(stat.st_mtime, timezone.utc),
                )
            )
        return objects

    def delete_object(self, stored_object: StoredObject) -> None:
        path = safe_object_path(self.root, stored_object.name)
        path.unlink()


class GcsObjectStore:
    def __init__(self, bucket_name: str, history_prefix: str = HISTORY_PREFIX):
        from google.cloud import storage

        self.bucket = storage.Client().bucket(bucket_name)
        self.history_prefix = history_prefix

    def read_bytes(self, object_name: str) -> tuple[bytes, str]:
        from google.api_core.exceptions import NotFound

        blob = self.bucket.blob(object_name)
        try:
            blob.reload()
            generation = str(blob.generation or "")
            versioned_blob = self.bucket.blob(object_name, generation=blob.generation)
            return versioned_blob.download_as_bytes(), generation
        except NotFound as exc:
            raise AnalysisError(f"找不到來源物件：{object_name}。") from exc

    def write_bytes(self, object_name: str, payload: bytes) -> None:
        blob = self.bucket.blob(object_name)
        upload_options: dict[str, Any] = {}
        if object_name.startswith(f"{self.history_prefix}/"):
            upload_options["if_generation_match"] = 0
        blob.upload_from_string(
            payload,
            content_type="application/json; charset=utf-8",
            **upload_options,
        )

    def read_prefix(self, object_name: str, max_bytes: int) -> bytes:
        from google.api_core.exceptions import NotFound

        blob = self.bucket.blob(object_name)
        try:
            return blob.download_as_bytes(start=0, end=max_bytes - 1)
        except NotFound as exc:
            raise AnalysisError(f"找不到來源物件：{object_name}。") from exc

    def list_objects(self, prefix: str) -> list[StoredObject]:
        objects: list[StoredObject] = []
        for blob in self.bucket.list_blobs(prefix=prefix):
            updated = blob.updated or blob.time_created
            if updated is None:
                raise AnalysisError(f"無法取得歷史物件更新時間：{blob.name}。")
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            objects.append(
                StoredObject(
                    name=blob.name,
                    updated=updated.astimezone(timezone.utc),
                    generation=int(blob.generation) if blob.generation else None,
                )
            )
        return objects

    def delete_object(self, stored_object: StoredObject) -> None:
        blob = self.bucket.blob(
            stored_object.name,
            generation=stored_object.generation,
        )
        options: dict[str, Any] = {}
        if stored_object.generation is not None:
            options["if_generation_match"] = stored_object.generation
        blob.delete(**options)


def build_store(bucket_name: str, history_prefix: str = HISTORY_PREFIX):
    local_root = os.getenv("LOCAL_STORAGE_ROOT", "").strip()
    if local_root:
        return LocalObjectStore(local_root), "local-storage"
    return GcsObjectStore(bucket_name, history_prefix), "gcs"


def get_source_object() -> str:
    source_object = os.getenv("GCS_SOURCE_OBJECT", DEFAULT_SOURCE_OBJECT).strip()
    if (
        not source_object.startswith("raw/")
        or source_object.endswith("/")
        or "\\" in source_object
        or any(part in {"", ".", ".."} for part in source_object.split("/"))
    ):
        raise AnalysisError("GCS_SOURCE_OBJECT 必須是 raw/ 底下的有效物件路徑。")
    return source_object


def validate_processed_object_name(
    object_name: str,
    variable_name: str,
    *,
    allow_prefix: bool = False,
) -> str:
    normalized = object_name.strip().strip("/") if allow_prefix else object_name.strip()
    segments = normalized.split("/")
    if (
        not normalized.startswith("processed/")
        or "\\" in normalized
        or any(part in {"", ".", ".."} for part in segments)
        or (not allow_prefix and not normalized.endswith(".json"))
    ):
        requirement = (
            "processed/ 底下的有效前綴"
            if allow_prefix
            else "processed/ 底下的 JSON 物件"
        )
        raise AnalysisError(f"{variable_name} 必須是 {requirement}。")
    return normalized


def get_output_layout() -> OutputLayout:
    latest_object = validate_processed_object_name(
        os.getenv("GCS_LATEST_OBJECT", DEFAULT_LATEST_OBJECT),
        "GCS_LATEST_OBJECT",
    )
    history_prefix = validate_processed_object_name(
        os.getenv("GCS_HISTORY_PREFIX", DEFAULT_HISTORY_PREFIX),
        "GCS_HISTORY_PREFIX",
        allow_prefix=True,
    )
    if latest_object.startswith(f"{history_prefix}/"):
        raise AnalysisError("GCS_LATEST_OBJECT 不可放在 GCS_HISTORY_PREFIX 底下。")
    return OutputLayout(
        latest_object=latest_object,
        history_prefix=history_prefix,
    )


def get_positive_int_setting(name: str, default: int, maximum: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    if not re.fullmatch(r"\d+", raw_value):
        raise AnalysisError(f"{name} 必須是正整數。")
    value = int(raw_value)
    if value < 1 or value > maximum:
        raise AnalysisError(f"{name} 必須介於 1 到 {maximum} 之間。")
    return value


def read_latest_source_state(
    store: Any,
    latest_object: str = LATEST_OBJECT,
) -> dict[str, Any] | None:
    try:
        payload = store.read_prefix(latest_object, LATEST_METADATA_PREFIX_BYTES)
    except AnalysisError:
        return None

    try:
        text = payload.decode("utf-8")
        source_marker = re.search(r'"source"\s*:', text)
        if source_marker is None:
            raise ValueError("Missing source metadata")
        source_start = source_marker.end()
        while source_start < len(text) and text[source_start].isspace():
            source_start += 1
        source, _ = json.JSONDecoder().raw_decode(text, source_start)
        version_match = re.search(
            r'"version"\s*:\s*"([^"]*)"',
            text[: source_marker.start()],
        )
        if not isinstance(source, dict):
            raise ValueError("Invalid source metadata")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        structured_log(
            "latest_metadata_unreadable",
            severity="WARNING",
            error_type=type(exc).__name__,
        )
        return None
    return {
        "version": version_match.group(1) if version_match else "",
        "source": source,
    }


def unchanged_source_match(
    latest_document: dict[str, Any] | None,
    source_object: str,
    source_generation: str,
    source_sha256: str,
) -> str | None:
    if latest_document is None:
        return None

    latest_source = latest_document.get("source")
    if not isinstance(latest_source, dict):
        return None
    if str(latest_source.get("object") or "") != source_object:
        return None

    latest_sha256 = str(latest_source.get("sha256") or "")
    if latest_sha256:
        return "sha256" if latest_sha256 == source_sha256 else None

    # Backward compatibility for the latest document created before source
    # hashes were recorded. A matching immutable GCS generation is sufficient.
    latest_generation = str(latest_source.get("generation") or "")
    if source_generation and latest_generation == source_generation:
        return "generation"
    return None


def cleanup_history(
    store: Any,
    now: datetime,
    output_layout: OutputLayout | None = None,
) -> dict[str, Any]:
    layout = output_layout or OutputLayout(LATEST_OBJECT, HISTORY_PREFIX)
    retention_days = get_positive_int_setting(
        "HISTORY_RETENTION_DAYS",
        DEFAULT_HISTORY_RETENTION_DAYS,
        3650,
    )
    minimum_count = get_positive_int_setting(
        "HISTORY_MIN_COUNT",
        DEFAULT_HISTORY_MIN_COUNT,
        100,
    )
    cutoff = now - timedelta(days=retention_days)
    history_objects = sorted(
        (
            stored_object
            for stored_object in store.list_objects(layout.history_object_prefix)
            if layout.history_object_pattern.fullmatch(stored_object.name)
        ),
        key=lambda stored_object: (stored_object.updated, stored_object.name),
        reverse=True,
    )

    deleted_objects: list[str] = []
    for stored_object in history_objects[minimum_count:]:
        if stored_object.updated >= cutoff:
            continue
        store.delete_object(stored_object)
        deleted_objects.append(stored_object.name)

    result = {
        "retention_days": retention_days,
        "minimum_count": minimum_count,
        "history_count_before": len(history_objects),
        "deleted_count": len(deleted_objects),
        "history_count_after": len(history_objects) - len(deleted_objects),
        "deleted_objects": deleted_objects,
    }
    structured_log("history_cleanup_succeeded", **result)
    return result


def run_analysis() -> dict[str, Any]:
    bucket_name = os.getenv("GCS_BUCKET", "").strip()
    if not bucket_name:
        raise AnalysisError("缺少必要環境變數 GCS_BUCKET。")

    source_object = get_source_object()
    output_layout = get_output_layout()
    store, storage_mode = build_store(bucket_name, output_layout.history_prefix)
    structured_log(
        "analysis_started",
        storage_mode=storage_mode,
        source_object=source_object,
    )

    csv_bytes, source_generation = store.read_bytes(source_object)
    source_sha256 = hashlib.sha256(csv_bytes).hexdigest()
    generated_datetime = utc_now()
    latest_source_state = read_latest_source_state(
        store,
        output_layout.latest_object,
    )
    match_basis = unchanged_source_match(
        latest_source_state,
        source_object,
        source_generation,
        source_sha256,
    )
    if match_basis is not None and latest_source_state is not None:
        cleanup_result = cleanup_history(store, generated_datetime, output_layout)
        structured_log(
            "analysis_skipped",
            storage_mode=storage_mode,
            source_object=source_object,
            reason="source_unchanged",
            match_basis=match_basis,
            source_generation=source_generation,
            latest_version=str(latest_source_state.get("version") or ""),
            history_cleanup=cleanup_result,
        )
        return {
            "status": "skipped",
            "version": str(latest_source_state.get("version") or ""),
            "source": latest_source_state.get("source", {}),
        }

    generated_at = generated_datetime.isoformat().replace("+00:00", "Z")
    version = generated_datetime.strftime("%Y%m%dT%H%M%S%fZ")
    input_mode = detect_csv_source_mode(csv_bytes)
    if input_mode == "member-features":
        records, stats = read_member_features(csv_bytes)
    else:
        transactions, stats, has_date_column = read_transactions(csv_bytes)
        records = calculate_member_features(
            transactions,
            has_date_column=has_date_column,
            as_of_date=generated_datetime.date(),
        )

    document = {
        "schema_version": SCHEMA_VERSION,
        "version": version,
        "generated_at": generated_at,
        "source": {
            "object": source_object,
            "generation": source_generation,
            "sha256": source_sha256,
            "input_mode": input_mode,
            "input_rows": stats.input_rows,
            "valid_rows": stats.valid_rows,
            "duplicate_rows": stats.duplicate_rows,
        },
        "records": records,
    }
    payload = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
    ).encode("utf-8")
    history_object = f"{output_layout.history_object_prefix}{version}.json"

    # A failed analysis never reaches these writes. History is written first;
    # overwriting one GCS object is atomic, so a failed latest upload leaves the
    # previous complete generation available.
    store.write_bytes(history_object, payload)
    store.write_bytes(output_layout.latest_object, payload)
    cleanup_result = cleanup_history(store, generated_datetime, output_layout)

    structured_log(
        "analysis_succeeded",
        storage_mode=storage_mode,
        source_object=source_object,
        input_mode=input_mode,
        generated_at=generated_at,
        version=version,
        input_rows=stats.input_rows,
        valid_rows=stats.valid_rows,
        skipped_rows=stats.skipped_rows,
        duplicate_rows=stats.duplicate_rows,
        invalid_date_rows=stats.invalid_date_rows,
        invalid_flag_values=stats.invalid_flag_values,
        member_count=len(records),
        latest_object=output_layout.latest_object,
        history_object=history_object,
        history_cleanup=cleanup_result,
    )
    return document


def main() -> int:
    try:
        run_analysis()
        return 0
    except Exception as exc:
        structured_log(
            "analysis_failed",
            severity="ERROR",
            error_type=type(exc).__name__,
            message=str(exc),
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
