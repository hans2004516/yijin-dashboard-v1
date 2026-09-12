"""Cloud Run Job that deidentifies a private GCS CSV before analysis."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from deidentify import (
    DEFAULT_SECRET_ENV,
    MIN_SECRET_BYTES,
    SCHEMA_VERSION,
    DeidentificationError,
    convert_file,
    load_secret,
)


DEFAULT_SOURCE_OBJECT = "incoming/member_features.csv"
DEFAULT_DESTINATION_OBJECT = "raw/member_features.csv"
DEFAULT_REPORT_PREFIX = "audit/deidentification"
DEFAULT_MAX_SOURCE_BYTES = 512 * 1024 * 1024
OBJECT_SEGMENT_PATTERN = re.compile(r"^[^\\\x00]+$")


class CloudDeidentificationError(RuntimeError):
    """Cloud input, configuration, or publication is unsafe."""


class WriteConflictError(CloudDeidentificationError):
    """An object changed while the job was working."""


@dataclass(frozen=True)
class ObjectState:
    generation: str
    metageneration: int
    size: int
    metadata: dict[str, str]


@dataclass(frozen=True)
class JobConfig:
    source_bucket: str
    source_object: str
    destination_bucket: str
    destination_object: str
    report_prefix: str
    key_version: str
    max_source_bytes: int
    allow_initial_replacement: bool

    @classmethod
    def from_environment(cls) -> "JobConfig":
        source_bucket = os.getenv("SOURCE_BUCKET", "").strip()
        destination_bucket = os.getenv("DESTINATION_BUCKET", "").strip()
        source_object = os.getenv("SOURCE_OBJECT", DEFAULT_SOURCE_OBJECT).strip()
        destination_object = os.getenv(
            "DESTINATION_OBJECT", DEFAULT_DESTINATION_OBJECT
        ).strip()
        report_prefix = os.getenv("REPORT_PREFIX", DEFAULT_REPORT_PREFIX).strip().strip("/")
        key_version = os.getenv("DEIDENTIFICATION_KEY_VERSION", "").strip()
        try:
            max_source_bytes = int(
                os.getenv("MAX_SOURCE_BYTES", str(DEFAULT_MAX_SOURCE_BYTES))
            )
        except ValueError as exc:
            raise CloudDeidentificationError("MAX_SOURCE_BYTES 必須是正整數。") from exc
        allow_initial_replacement = parse_boolean_environment(
            "ALLOW_INITIAL_REPLACEMENT", default=False
        )

        if not source_bucket or not destination_bucket:
            raise CloudDeidentificationError(
                "SOURCE_BUCKET 與 DESTINATION_BUCKET 都必須設定。"
            )
        if not key_version:
            raise CloudDeidentificationError(
                "DEIDENTIFICATION_KEY_VERSION 必須對應 Secret Manager 的固定版本。"
            )
        if max_source_bytes <= 0:
            raise CloudDeidentificationError("MAX_SOURCE_BYTES 必須是正整數。")

        validate_object_name(source_object, "incoming/", ".csv", "SOURCE_OBJECT")
        validate_object_name(
            destination_object, "raw/", ".csv", "DESTINATION_OBJECT"
        )
        validate_object_name(
            f"{report_prefix}/placeholder.json",
            "audit/deidentification/",
            ".json",
            "REPORT_PREFIX",
        )
        return cls(
            source_bucket=source_bucket,
            source_object=source_object,
            destination_bucket=destination_bucket,
            destination_object=destination_object,
            report_prefix=report_prefix,
            key_version=key_version,
            max_source_bytes=max_source_bytes,
            allow_initial_replacement=allow_initial_replacement,
        )


class StorageBackend(Protocol):
    def stat(self, bucket: str, object_name: str) -> ObjectState | None: ...

    def download(
        self,
        bucket: str,
        object_name: str,
        destination: Path,
        expected_generation: str,
    ) -> None: ...

    def upload(
        self,
        bucket: str,
        object_name: str,
        source: Path,
        content_type: str,
        metadata: dict[str, str],
        expected_generation: str,
    ) -> ObjectState: ...

    def patch_metadata(
        self,
        bucket: str,
        object_name: str,
        metadata: dict[str, str],
        expected_metageneration: int,
    ) -> ObjectState: ...


class GcsStorage:
    def __init__(self) -> None:
        from google.cloud import storage

        self.client = storage.Client()

    def stat(self, bucket: str, object_name: str) -> ObjectState | None:
        from google.api_core.exceptions import NotFound

        blob = self.client.bucket(bucket).blob(object_name)
        try:
            blob.reload()
        except NotFound:
            return None
        return self._state(blob)

    def download(
        self,
        bucket: str,
        object_name: str,
        destination: Path,
        expected_generation: str,
    ) -> None:
        generation = int(expected_generation)
        blob = self.client.bucket(bucket).blob(object_name, generation=generation)
        blob.download_to_filename(
            str(destination),
            if_generation_match=generation,
        )

    def upload(
        self,
        bucket: str,
        object_name: str,
        source: Path,
        content_type: str,
        metadata: dict[str, str],
        expected_generation: str,
    ) -> ObjectState:
        from google.api_core.exceptions import PreconditionFailed

        blob = self.client.bucket(bucket).blob(object_name)
        blob.metadata = metadata
        try:
            blob.upload_from_filename(
                str(source),
                content_type=content_type,
                if_generation_match=int(expected_generation),
            )
        except PreconditionFailed as exc:
            raise WriteConflictError(
                "目的物件在處理期間被其他執行更新，未覆寫較新的資料。"
            ) from exc
        blob.reload()
        return self._state(blob)

    def patch_metadata(
        self,
        bucket: str,
        object_name: str,
        metadata: dict[str, str],
        expected_metageneration: int,
    ) -> ObjectState:
        from google.api_core.exceptions import PreconditionFailed

        blob = self.client.bucket(bucket).blob(object_name)
        blob.metadata = metadata
        try:
            blob.patch(if_metageneration_match=expected_metageneration)
        except PreconditionFailed as exc:
            raise WriteConflictError(
                "目的物件中繼資料在處理期間被更新，請重新執行。"
            ) from exc
        blob.reload()
        return self._state(blob)

    @staticmethod
    def _state(blob: Any) -> ObjectState:
        return ObjectState(
            generation=str(blob.generation or ""),
            metageneration=int(blob.metageneration or 0),
            size=int(blob.size or 0),
            metadata={str(key): str(value) for key, value in (blob.metadata or {}).items()},
        )


def validate_object_name(
    object_name: str,
    required_prefix: str,
    required_suffix: str,
    variable_name: str,
) -> None:
    if (
        not object_name
        or object_name.startswith("/")
        or not object_name.startswith(required_prefix)
        or not object_name.casefold().endswith(required_suffix)
    ):
        raise CloudDeidentificationError(
            f"{variable_name} 必須位於 {required_prefix} 且使用 {required_suffix}。"
        )
    segments = object_name.split("/")
    if any(segment in {"", ".", ".."} or not OBJECT_SEGMENT_PATTERN.match(segment) for segment in segments):
        raise CloudDeidentificationError(f"{variable_name} 包含不安全的物件路徑。")


def parse_boolean_environment(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise CloudDeidentificationError(
        f"{name} 只能設定為 true/false、yes/no 或 1/0。"
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def structured_log(event: str, severity: str = "INFO", **fields: Any) -> None:
    print(
        json.dumps(
            {
                "severity": severity,
                "event": event,
                "timestamp": utc_now(),
                **fields,
            },
            ensure_ascii=False,
            allow_nan=False,
        ),
        flush=True,
    )


def identity_metadata(config: JobConfig) -> dict[str, str]:
    return {
        "source_bucket": config.source_bucket,
        "source_object": config.source_object,
        "deidentification_schema": SCHEMA_VERSION,
        "deidentification_key_version": config.key_version,
    }


def matching_identity(state: ObjectState | None, config: JobConfig) -> bool:
    if state is None:
        return False
    expected = identity_metadata(config)
    return all(state.metadata.get(key) == value for key, value in expected.items())


def audit_object_name(config: JobConfig, source: ObjectState, source_sha256: str) -> str:
    safe_generation = re.sub(r"[^A-Za-z0-9_-]", "_", source.generation)[:80]
    return (
        f"{config.report_prefix}/member_features_"
        f"{safe_generation}_{source_sha256[:16]}.report.json"
    )


def run_deidentification(
    store: StorageBackend,
    config: JobConfig,
    secret: bytes,
) -> dict[str, Any]:
    if len(secret) < MIN_SECRET_BYTES:
        raise CloudDeidentificationError("去識別化密鑰至少需要 32 bytes。")

    source = store.stat(config.source_bucket, config.source_object)
    if source is None:
        raise CloudDeidentificationError("找不到指定的雲端來源 CSV。")
    if source.size <= 0:
        raise CloudDeidentificationError("雲端來源 CSV 是空檔案。")
    if source.size > config.max_source_bytes:
        raise CloudDeidentificationError("雲端來源 CSV 超過允許的大小上限。")

    destination = store.stat(config.destination_bucket, config.destination_object)
    if (
        matching_identity(destination, config)
        and destination is not None
        and destination.metadata.get("source_generation") == source.generation
    ):
        result = {
            "event": "deidentification_skipped",
            "reason": "source_unchanged",
            "match_basis": "generation",
            "source_generation": source.generation,
            "destination_generation": destination.generation,
        }
        structured_log(**result)
        return result

    with tempfile.TemporaryDirectory(prefix="member-deidentify-") as temporary:
        temporary_path = Path(temporary)
        input_path = temporary_path / "source.csv"
        output_path = temporary_path / "deidentified.csv"
        report_path = temporary_path / "report.json"

        store.download(
            config.source_bucket,
            config.source_object,
            input_path,
            source.generation,
        )
        source_sha256 = sha256_file(input_path)

        if (
            matching_identity(destination, config)
            and destination is not None
            and destination.metadata.get("source_sha256") == source_sha256
        ):
            updated_metadata = {
                **destination.metadata,
                **identity_metadata(config),
                "source_generation": source.generation,
                "source_sha256": source_sha256,
                "source_checked_at": utc_now(),
            }
            store.patch_metadata(
                config.destination_bucket,
                config.destination_object,
                updated_metadata,
                destination.metageneration,
            )
            result = {
                "event": "deidentification_skipped",
                "reason": "source_unchanged",
                "match_basis": "sha256",
                "source_generation": source.generation,
                "destination_generation": destination.generation,
            }
            structured_log(**result)
            return result

        report = convert_file(
            input_path,
            output_path,
            report_path,
            secret,
            overwrite=False,
        )

        current_source = store.stat(config.source_bucket, config.source_object)
        if current_source is None or current_source.generation != source.generation:
            raise WriteConflictError(
                "來源 CSV 在處理期間被更新，本次結果未發布；請重新執行。"
            )

        initial_metadata_only = False
        if (
            destination is not None
            and not matching_identity(destination, config)
            and not config.allow_initial_replacement
        ):
            existing_path = temporary_path / "existing-destination.csv"
            store.download(
                config.destination_bucket,
                config.destination_object,
                existing_path,
                destination.generation,
            )
            if sha256_file(existing_path) != str(report["output_sha256"]):
                raise CloudDeidentificationError(
                    "首次雲端轉換結果與既有安全 CSV 不一致，未覆寫正式來源。"
                    "請確認使用相同原始檔與既有 HMAC 密鑰。"
                )
            initial_metadata_only = True

        audit_name = audit_object_name(config, source, source_sha256)
        audit_payload = {
            **report,
            "cloud_source": {
                "bucket": config.source_bucket,
                "object": config.source_object,
                "generation": source.generation,
            },
            "cloud_destination": {
                "bucket": config.destination_bucket,
                "object": config.destination_object,
            },
            "deidentification_key_version": config.key_version,
        }
        report_path.write_text(
            json.dumps(audit_payload, ensure_ascii=False, indent=2, allow_nan=False)
            + "\n",
            encoding="utf-8",
        )

        if store.stat(config.destination_bucket, audit_name) is None:
            store.upload(
                config.destination_bucket,
                audit_name,
                report_path,
                "application/json; charset=utf-8",
                {
                    **identity_metadata(config),
                    "source_generation": source.generation,
                    "source_sha256": source_sha256,
                },
                "0",
            )

        publish_metadata = {
            **identity_metadata(config),
            "source_generation": source.generation,
            "source_sha256": source_sha256,
            "output_sha256": str(report["output_sha256"]),
            "audit_report_object": audit_name,
            "generated_at": str(report["generated_at"]),
        }
        if initial_metadata_only and destination is not None:
            published = store.patch_metadata(
                config.destination_bucket,
                config.destination_object,
                publish_metadata,
                destination.metageneration,
            )
        else:
            published = store.upload(
                config.destination_bucket,
                config.destination_object,
                output_path,
                "text/csv; charset=utf-8",
                publish_metadata,
                destination.generation if destination is not None else "0",
            )

    stats = report["stats"]
    result = {
        "event": "deidentification_succeeded",
        "input_mode": report["input_mode"],
        "source_generation": source.generation,
        "destination_generation": published.generation,
        "rows": stats["output_rows"],
        "unique_members": stats["unique_members"],
        "sensitive_columns_removed_count": len(report["sensitive_columns_removed"]),
        "audit_report_object": audit_name,
        "publication_mode": "metadata_only" if initial_metadata_only else "object_write",
    }
    structured_log(**result)
    return result


def main() -> int:
    try:
        config = JobConfig.from_environment()
        secret = load_secret(None, DEFAULT_SECRET_ENV)
        run_deidentification(GcsStorage(), config, secret)
        return 0
    except (CloudDeidentificationError, DeidentificationError) as exc:
        structured_log(
            "deidentification_failed",
            severity="ERROR",
            message=str(exc),
        )
        return 1
    except Exception as exc:
        structured_log(
            "deidentification_failed",
            severity="ERROR",
            message="雲端去識別化發生未預期錯誤，未發布新的安全 CSV。",
            error_type=type(exc).__name__,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
