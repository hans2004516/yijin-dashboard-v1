"""Create an analysis-ready CSV without exposing source member identifiers."""

from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable


SCHEMA_VERSION = "yijin-deidentified-csv-v2"
DEFAULT_SECRET_ENV = "YIJIN_DEIDENTIFICATION_SECRET"
MIN_SECRET_BYTES = 32
TRANSACTION_CANONICAL_ORDER = (
    "member_id",
    "transaction_id",
    "transaction_date",
    "amount",
    "category",
    "is_sale",
    "is_premium",
)
TRANSACTION_REQUIRED_COLUMNS = {"member_id", "amount", "category"}
TRANSACTION_COLUMN_ALIASES = {
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
FEATURE_CANONICAL_ORDER = (
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
)
FEATURE_REQUIRED_COLUMNS = {
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
PII_HEADER_HINTS = (
    "name",
    "fullname",
    "customername",
    "membername",
    "姓名",
    "會員姓名",
    "顧客姓名",
    "phone",
    "mobile",
    "tel",
    "電話",
    "手機",
    "聯絡電話",
    "email",
    "mail",
    "電子郵件",
    "電子信箱",
    "address",
    "地址",
    "住址",
    "birthday",
    "birthdate",
    "生日",
    "出生日期",
    "nationalid",
    "identitynumber",
    "身分證",
    "身份證",
    "統一編號",
    "postalcode",
    "zipcode",
    "郵遞區號",
)
EMAIL_PATTERN = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?886[- ]?|0)?9\d{8}(?!\d)|(?<!\d)0\d{8,9}(?!\d)")
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")


class DeidentificationError(RuntimeError):
    """Input is unsafe, invalid, or cannot be converted completely."""


@dataclass
class ConversionStats:
    input_rows: int = 0
    output_rows: int = 0
    unique_members: int = 0
    duplicate_transaction_ids: int = 0
    blank_transaction_ids: int = 0
    duplicate_member_rows: int = 0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_header(value: str) -> str:
    return re.sub(r"[\s_\-()（）]+", "", value.strip().lstrip("\ufeff").casefold())


def resolve_aliases(
    names: list[str],
    aliases_by_field: dict[str, tuple[str, ...]],
) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for canonical, aliases in aliases_by_field.items():
        alias_names = {normalize_header(alias) for alias in aliases}
        matches = [name for name in names if normalize_header(name) in alias_names]
        if len(matches) > 1:
            raise DeidentificationError(f"{canonical} 對應到多個來源欄位，請只保留一個。")
        if matches:
            resolved[canonical] = matches[0]
    return resolved


def resolve_columns(
    fieldnames: Iterable[str] | None,
) -> tuple[dict[str, str], list[str], str]:
    if not fieldnames:
        raise DeidentificationError("來源 CSV 沒有標題列。")

    names = list(fieldnames)
    if not names or any(name is None or not name.strip().lstrip("\ufeff") for name in names):
        raise DeidentificationError("來源 CSV 包含空白欄位名稱。")
    normalized_names = [normalize_header(name) for name in names]
    if len(normalized_names) != len(set(normalized_names)):
        raise DeidentificationError("來源 CSV 包含重複欄位名稱。")

    feature_columns = resolve_aliases(names, FEATURE_COLUMN_ALIASES)
    transaction_columns = resolve_aliases(names, TRANSACTION_COLUMN_ALIASES)
    if FEATURE_REQUIRED_COLUMNS.issubset(feature_columns):
        resolved = feature_columns
        input_mode = "member-features"
    elif TRANSACTION_REQUIRED_COLUMNS.issubset(transaction_columns):
        resolved = transaction_columns
        input_mode = "transactions"
    else:
        raise DeidentificationError(
            "來源 CSV 既不符合交易明細格式，也不符合會員特徵格式；請檢查必要欄位。"
        )

    used = set(resolved.values())
    dropped = [name.strip().lstrip("\ufeff") for name in names if name not in used]
    return resolved, dropped, input_mode


def is_sensitive_header(name: str) -> bool:
    normalized = normalize_header(name)
    return any(normalize_header(hint) in normalized for hint in PII_HEADER_HINTS)


def normalize_identifier(value: object, row_number: int, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise DeidentificationError(f"第 {row_number} 列缺少{label}。")
    if len(text) > 500:
        raise DeidentificationError(f"第 {row_number} 列的{label}長度不合理。")
    return text


def pseudonymize(secret: bytes, namespace: str, value: str, prefix: str) -> str:
    digest = hmac.new(
        secret,
        f"{namespace}\0{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32].upper()
    return f"{prefix}{digest}"


def normalize_amount(value: object, row_number: int) -> str:
    text = (
        str(value or "")
        .strip()
        .replace(",", "")
        .replace("NT$", "")
        .replace("TWD", "")
        .replace("$", "")
        .strip()
    )
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise DeidentificationError(f"第 {row_number} 列的交易金額格式錯誤。") from exc
    if not amount.is_finite() or amount < 0:
        raise DeidentificationError(f"第 {row_number} 列的交易金額必須是非負有限數值。")
    normalized = format(amount, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def normalize_non_negative_number(
    value: object,
    row_number: int,
    label: str,
    *,
    integer: bool = False,
    positive: bool = False,
    maximum: Decimal | None = None,
) -> str:
    text = str(value or "").strip().replace(",", "")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise DeidentificationError(f"第 {row_number} 列的{label}格式錯誤。") from exc
    if not number.is_finite() or number < 0 or (positive and number == 0):
        qualifier = "正數" if positive else "非負有限數值"
        raise DeidentificationError(f"第 {row_number} 列的{label}必須是{qualifier}。")
    if integer and number != number.to_integral_value():
        raise DeidentificationError(f"第 {row_number} 列的{label}必須是整數。")
    if maximum is not None and number > maximum:
        raise DeidentificationError(f"第 {row_number} 列的{label}不可大於 {maximum}。")
    normalized = format(number, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def normalize_date(value: object, row_number: int, label: str = "交易日期") -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(text, date_format).date().isoformat()
        except ValueError:
            continue
    raise DeidentificationError(f"第 {row_number} 列的{label}格式錯誤。")


def normalize_flag(value: object, row_number: int, label: str) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized in TRUE_VALUES:
        return "1"
    if normalized in FALSE_VALUES:
        return "0"
    raise DeidentificationError(f"第 {row_number} 列的{label}只能使用 0/1、是/否或 true/false。")


def normalize_required_flag(value: object, row_number: int, label: str) -> str:
    if not str(value or "").strip():
        raise DeidentificationError(f"第 {row_number} 列缺少{label}。")
    return normalize_flag(value, row_number, label)


def normalize_category(value: object, row_number: int) -> str:
    category = str(value or "").strip() or "未分類"
    if len(category) > 200 or "\x00" in category:
        raise DeidentificationError(f"第 {row_number} 列的品類內容不合理。")
    if category.startswith(CSV_FORMULA_PREFIXES):
        raise DeidentificationError(f"第 {row_number} 列的品類不可使用試算表公式開頭字元。")
    if EMAIL_PATTERN.search(category) or PHONE_PATTERN.search(category):
        raise DeidentificationError(f"第 {row_number} 列的品類疑似包含個人資料。")
    return category


def normalize_feature_row(
    row: dict[str | None, str | list[str] | None],
    columns: dict[str, str],
    row_number: int,
    safe_member_id: str,
) -> dict[str, str]:
    output_row = {
        "member_id": safe_member_id,
        "total_tx": normalize_non_negative_number(
            row.get(columns["total_tx"]), row_number, "總交易次數", integer=True, positive=True
        ),
        "total_amount": normalize_non_negative_number(
            row.get(columns["total_amount"]), row_number, "累積消費金額"
        ),
        "avg_amount": normalize_non_negative_number(
            row.get(columns["avg_amount"]), row_number, "平均交易金額"
        ),
        "active_years": normalize_non_negative_number(
            row.get(columns["active_years"]), row_number, "活躍年數", integer=True, positive=True
        ),
        "tx_per_year": normalize_non_negative_number(
            row.get(columns["tx_per_year"]), row_number, "年均交易次數"
        ),
        "avg_gap_days": normalize_non_negative_number(
            row.get(columns["avg_gap_days"]), row_number, "平均間隔天數"
        ),
        "sale_ratio": normalize_non_negative_number(
            row.get(columns["sale_ratio"]), row_number, "促銷占比", maximum=Decimal("1")
        ),
        "premium_ratio": normalize_non_negative_number(
            row.get(columns["premium_ratio"]),
            row_number,
            "高單價占比",
            maximum=Decimal("1"),
        ),
        "top_category": normalize_category(row.get(columns["top_category"]), row_number),
        "top_cat_ratio": normalize_non_negative_number(
            row.get(columns["top_cat_ratio"]),
            row_number,
            "主要品類占比",
            maximum=Decimal("1"),
        ),
        "is_loyal": normalize_required_flag(
            row.get(columns["is_loyal"]), row_number, "忠誠會員旗標"
        ),
    }
    expected_loyalty = "1" if Decimal(output_row["active_years"]) >= 3 else "0"
    if output_row["is_loyal"] != expected_loyalty:
        raise DeidentificationError(f"第 {row_number} 列不符合既有忠誠會員規則。")

    if "last_purchase_date" in columns:
        output_row["last_purchase_date"] = normalize_date(
            row.get(columns["last_purchase_date"]), row_number, "最近購買日"
        )
    if "recency_days" in columns:
        recency_text = str(row.get(columns["recency_days"], "") or "").strip()
        output_row["recency_days"] = (
            normalize_non_negative_number(
                recency_text, row_number, "距最近購買天數", integer=True
            )
            if recency_text
            else ""
        )
    return output_row


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_destination(input_path: Path, output_path: Path, report_path: Path, overwrite: bool) -> None:
    if input_path == output_path or input_path == report_path or output_path == report_path:
        raise DeidentificationError("來源、輸出與報告檔案必須使用不同路徑。")
    if not input_path.is_file():
        raise DeidentificationError("找不到來源 CSV。")
    for target in (output_path, report_path):
        if target.exists() and not overwrite:
            raise DeidentificationError(f"輸出檔已存在：{target.name}；如要覆寫請加上 --overwrite。")
        target.parent.mkdir(parents=True, exist_ok=True)


def convert_file(
    input_path: Path,
    output_path: Path,
    report_path: Path,
    secret: bytes,
    overwrite: bool = False,
) -> dict[str, object]:
    input_path = input_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    report_path = report_path.expanduser().resolve()
    if len(secret) < MIN_SECRET_BYTES:
        raise DeidentificationError("去識別化密鑰至少需要 32 bytes，且不得與 Dashboard API Key 共用。")
    ensure_destination(input_path, output_path, report_path, overwrite)

    output_fields: list[str] = []
    dropped_columns: list[str] = []
    sensitive_columns: list[str] = []
    stats = ConversionStats()
    member_ids: set[str] = set()
    seen_transactions: set[tuple[str, str]] = set()
    seen_feature_rows: dict[str, tuple[str, ...]] = {}
    input_mode = ""
    output_temp_path: Path | None = None
    report_temp_path: Path | None = None

    try:
        with input_path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            columns, dropped_columns, input_mode = resolve_columns(reader.fieldnames)
            sensitive_columns = [name for name in dropped_columns if is_sensitive_header(name)]
            canonical_order = (
                FEATURE_CANONICAL_ORDER
                if input_mode == "member-features"
                else TRANSACTION_CANONICAL_ORDER
            )
            output_fields = [name for name in canonical_order if name in columns]

            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8-sig",
                newline="",
                dir=output_path.parent,
                prefix=f".{output_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as output_temp:
                output_temp_path = Path(output_temp.name)
                writer = csv.DictWriter(output_temp, fieldnames=output_fields, lineterminator="\n")
                writer.writeheader()

                for row_index, row in enumerate(reader, start=2):
                    stats.input_rows += 1
                    if None in row:
                        raise DeidentificationError(f"第 {row_index} 列的欄位數量超過標題列。")

                    original_member_id = normalize_identifier(
                        row.get(columns["member_id"]),
                        row_index,
                        "會員編號",
                    )
                    safe_member_id = pseudonymize(
                        secret,
                        "member-id",
                        original_member_id,
                        "ANON-M-",
                    )
                    member_ids.add(safe_member_id)
                    if input_mode == "member-features":
                        output_row = normalize_feature_row(
                            row, columns, row_index, safe_member_id
                        )
                        signature = tuple(output_row.get(name, "") for name in output_fields)
                        previous = seen_feature_rows.get(safe_member_id)
                        if previous is not None:
                            if previous != signature:
                                raise DeidentificationError(
                                    f"第 {row_index} 列與相同會員的既有特徵衝突。"
                                )
                            stats.duplicate_member_rows += 1
                            continue
                        seen_feature_rows[safe_member_id] = signature
                    else:
                        output_row = {
                            "member_id": safe_member_id,
                            "amount": normalize_amount(
                                row.get(columns["amount"]), row_index
                            ),
                            "category": normalize_category(
                                row.get(columns["category"]), row_index
                            ),
                        }

                        if "transaction_id" in columns:
                            original_transaction_id = str(
                                row.get(columns["transaction_id"], "") or ""
                            ).strip()
                            if len(original_transaction_id) > 500:
                                raise DeidentificationError(
                                    f"第 {row_index} 列的交易編號長度不合理。"
                                )
                            if original_transaction_id:
                                safe_transaction_id = pseudonymize(
                                    secret,
                                    "transaction-id",
                                    f"{original_member_id}\0{original_transaction_id}",
                                    "ANON-T-",
                                )
                                transaction_key = (safe_member_id, safe_transaction_id)
                                if transaction_key in seen_transactions:
                                    stats.duplicate_transaction_ids += 1
                                seen_transactions.add(transaction_key)
                                output_row["transaction_id"] = safe_transaction_id
                            else:
                                stats.blank_transaction_ids += 1
                                output_row["transaction_id"] = ""

                        if "transaction_date" in columns:
                            output_row["transaction_date"] = normalize_date(
                                row.get(columns["transaction_date"]), row_index
                            )
                        if "is_sale" in columns:
                            output_row["is_sale"] = normalize_flag(
                                row.get(columns["is_sale"]), row_index, "促銷旗標"
                            )
                        if "is_premium" in columns:
                            output_row["is_premium"] = normalize_flag(
                                row.get(columns["is_premium"]), row_index, "高單價旗標"
                            )

                    writer.writerow(output_row)
                    stats.output_rows += 1

        if stats.output_rows == 0:
            raise DeidentificationError("來源 CSV 沒有可轉換的資料。")

        stats.unique_members = len(member_ids)
        output_sha256 = sha256_file(output_temp_path)
        report: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "input_mode": input_mode,
            "generated_at": utc_now(),
            "input_filename": input_path.name,
            "output_filename": output_path.name,
            "input_sha256": sha256_file(input_path),
            "output_sha256": output_sha256,
            "output_encoding": "UTF-8 BOM",
            "identifier_method": "HMAC-SHA256 truncated to 128 bits",
            "output_columns": output_fields,
            "dropped_columns": dropped_columns,
            "sensitive_columns_removed": sensitive_columns,
            "stats": asdict(stats),
        }

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=report_path.parent,
            prefix=f".{report_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as report_temp:
            report_temp_path = Path(report_temp.name)
            json.dump(report, report_temp, ensure_ascii=False, indent=2, allow_nan=False)
            report_temp.write("\n")

        os.replace(output_temp_path, output_path)
        output_temp_path = None
        os.replace(report_temp_path, report_path)
        report_temp_path = None
        return report
    except UnicodeDecodeError as exc:
        raise DeidentificationError("來源 CSV 必須使用 UTF-8 或 UTF-8 BOM 編碼。") from exc
    except csv.Error as exc:
        raise DeidentificationError("來源 CSV 格式無法解析。") from exc
    finally:
        for temporary in (output_temp_path, report_temp_path):
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def load_secret(secret_file: Path | None, secret_env: str) -> bytes:
    if secret_file is not None:
        try:
            secret_text = secret_file.expanduser().read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as exc:
            raise DeidentificationError("無法讀取去識別化密鑰檔。") from exc
    else:
        secret_text = os.getenv(secret_env, "").rstrip("\r\n")
        if not secret_text:
            if not sys.stdin.isatty():
                raise DeidentificationError(
                    f"請設定 {secret_env}、使用 --secret-file，或在互動終端執行。"
                )
            first = getpass.getpass("請輸入去識別化密鑰（畫面不會顯示）：")
            second = getpass.getpass("請再次輸入相同密鑰：")
            if not hmac.compare_digest(first, second):
                raise DeidentificationError("兩次輸入的去識別化密鑰不同。")
            secret_text = first

    secret = secret_text.encode("utf-8")
    if len(secret) < MIN_SECRET_BYTES:
        raise DeidentificationError("去識別化密鑰至少需要 32 bytes。")
    return secret


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="在本機將會員交易或會員特徵 CSV 轉為可安全上傳的分析欄位。"
    )
    parser.add_argument("--input", required=True, type=Path, help="來源會員交易或會員特徵 CSV")
    parser.add_argument("--output", required=True, type=Path, help="去識別化輸出 CSV")
    parser.add_argument(
        "--report",
        type=Path,
        help="稽核報告 JSON；預設為輸出檔名加上 .report.json",
    )
    parser.add_argument(
        "--secret-file",
        type=Path,
        help="從本機受保護檔案讀取密鑰；不得放入專案或 Git",
    )
    parser.add_argument(
        "--secret-env",
        default=DEFAULT_SECRET_ENV,
        help=f"密鑰環境變數名稱（預設 {DEFAULT_SECRET_ENV}）",
    )
    parser.add_argument("--overwrite", action="store_true", help="覆寫既有輸出與報告")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    output_path: Path = args.output
    report_path: Path = args.report or output_path.with_suffix(
        output_path.suffix + ".report.json"
    )
    try:
        report = convert_file(
            args.input,
            output_path,
            report_path,
            load_secret(args.secret_file, args.secret_env),
            overwrite=args.overwrite,
        )
        print(
            json.dumps(
                {
                    "event": "deidentification_succeeded",
                    "input_mode": report["input_mode"],
                    "output": str(output_path),
                    "report": str(report_path),
                    "rows": report["stats"]["output_rows"],
                    "unique_members": report["stats"]["unique_members"],
                    "sensitive_columns_removed": report["sensitive_columns_removed"],
                },
                ensure_ascii=False,
                allow_nan=False,
            )
        )
        return 0
    except DeidentificationError as exc:
        print(
            json.dumps(
                {
                    "event": "deidentification_failed",
                    "message": str(exc),
                },
                ensure_ascii=False,
                allow_nan=False,
            ),
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "deidentification_failed",
                    "message": "轉檔發生未預期錯誤，未產生完整輸出。",
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
                allow_nan=False,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
