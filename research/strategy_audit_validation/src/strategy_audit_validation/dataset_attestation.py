"""Fail-closed validation for the recovered BTCUSDT Drive dataset attestation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Final

from .errors import EvidenceMalformedError, EvidenceTamperedError

DRIVE_DATASET_ATTESTATION_SCHEMA_VERSION: Final = 1
DRIVE_DATASET_ATTESTATION_STATUS: Final = "BYTE_AND_CONTENT_ATTESTED_MARKET_ENDPOINT_UNPROVEN"
DRIVE_DATASET_ATTESTATION_CANONICAL_SHA256: Final = (
    "e3cc285ba9fe4e7a50879ae52660f380d7177bb35a20f13c264f993cb6975a91"
)
EXPECTED_TIMEFRAMES: Final = frozenset({"5m", "15m", "1h", "4h"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True, slots=True)
class RepositorySourceIdentity:
    drive_file_id: str
    repository_path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class DatasetFileIdentity:
    timeframe: str
    drive_file_id: str
    file_name: str
    sha256: str
    size_bytes: int
    rows: int
    first_open_time: datetime
    last_open_time: datetime


@dataclass(frozen=True, slots=True)
class DriveDatasetAttestation:
    attestation_id: str
    canonical_sha256: str
    status: str
    symbol: str
    timezone: str
    source_folder_id: str
    source_folder_name: str
    market_claim: str
    market_provenance_status: str
    endpoint_proof_present: bool
    download_config: RepositorySourceIdentity
    download_summary: RepositorySourceIdentity
    files: Mapping[str, DatasetFileIdentity]
    validation_report_sha256: str
    verified_at_utc: datetime
    limitations: tuple[str, ...]
    dataset_registered_in_repository: bool
    component_ledgers_present: bool
    combined_395_trade_ledger_present: bool
    strategy_reproduction_complete: bool

    @property
    def control_evidence_complete(self) -> bool:
        return (
            self.endpoint_proof_present
            and self.dataset_registered_in_repository
            and self.component_ledgers_present
            and self.combined_395_trade_ledger_present
            and self.strategy_reproduction_complete
        )


def load_drive_dataset_attestation(payload: Mapping[str, object]) -> DriveDatasetAttestation:
    """Load only the immutable, independently verified v1 attestation."""

    digest = _canonical_digest(payload)
    if digest != DRIVE_DATASET_ATTESTATION_CANONICAL_SHA256:
        raise EvidenceTamperedError("Drive dataset attestation canonical digest mismatch")

    if payload.get("schema_version") != DRIVE_DATASET_ATTESTATION_SCHEMA_VERSION:
        raise EvidenceMalformedError("unsupported Drive dataset attestation schema_version")
    if payload.get("status") != DRIVE_DATASET_ATTESTATION_STATUS:
        raise EvidenceMalformedError("unexpected Drive dataset attestation status")
    if payload.get("symbol") != "BTCUSDT" or payload.get("timezone") != "UTC":
        raise EvidenceMalformedError("Drive dataset identity must be BTCUSDT in UTC")

    source = _mapping(payload.get("source"), "source")
    controls = _mapping(payload.get("control_evidence"), "control_evidence")
    verification = _mapping(payload.get("verification"), "verification")

    endpoint_proof = _boolean(source.get("endpoint_proof_present"), "endpoint_proof_present")
    registered = _boolean(
        controls.get("dataset_registered_in_repository"),
        "dataset_registered_in_repository",
    )
    component_ledgers = _boolean(
        controls.get("component_ledgers_present"), "component_ledgers_present"
    )
    combined_ledger = _boolean(
        controls.get("combined_395_trade_ledger_present"),
        "combined_395_trade_ledger_present",
    )
    reproduced = _boolean(
        controls.get("strategy_reproduction_complete"),
        "strategy_reproduction_complete",
    )
    if endpoint_proof or registered or component_ledgers or combined_ledger or reproduced:
        raise EvidenceMalformedError("unresolved dataset/control gates must remain false")
    if not _boolean(controls.get("dataset_bytes_attested"), "dataset_bytes_attested"):
        raise EvidenceMalformedError("dataset bytes must be attested")
    if _boolean(controls.get("market_endpoint_proven"), "market_endpoint_proven"):
        raise EvidenceMalformedError("market endpoint cannot be proven by this attestation")

    files_raw = _mapping(payload.get("files"), "files")
    if set(files_raw) != EXPECTED_TIMEFRAMES:
        raise EvidenceMalformedError("attestation must contain exactly 5m, 15m, 1h, and 4h")
    files = {
        timeframe: _load_dataset_file(
            timeframe, _mapping(files_raw.get(timeframe), f"files.{timeframe}")
        )
        for timeframe in sorted(EXPECTED_TIMEFRAMES)
    }

    return DriveDatasetAttestation(
        attestation_id=_string(payload.get("attestation_id"), "attestation_id"),
        canonical_sha256=digest,
        status=DRIVE_DATASET_ATTESTATION_STATUS,
        symbol="BTCUSDT",
        timezone="UTC",
        source_folder_id=_drive_id(source.get("folder_id"), "folder_id"),
        source_folder_name=_string(source.get("folder_name"), "folder_name"),
        market_claim=_string(source.get("market_claim"), "market_claim"),
        market_provenance_status=_string(
            source.get("market_provenance_status"), "market_provenance_status"
        ),
        endpoint_proof_present=False,
        download_config=_load_repository_source(
            _mapping(source.get("download_config"), "download_config"),
            "download_config",
        ),
        download_summary=_load_repository_source(
            _mapping(source.get("download_summary"), "download_summary"),
            "download_summary",
        ),
        files=MappingProxyType(files),
        validation_report_sha256=_sha256(
            verification.get("validation_report_sha256"),
            "validation_report_sha256",
        ),
        verified_at_utc=_utc_timestamp(verification.get("verified_at_utc"), "verified_at_utc"),
        limitations=_string_tuple(payload.get("limitations"), "limitations"),
        dataset_registered_in_repository=False,
        component_ledgers_present=False,
        combined_395_trade_ledger_present=False,
        strategy_reproduction_complete=False,
    )


def verify_attestation_repository_sources(
    attestation: DriveDatasetAttestation, repository_root: Path
) -> None:
    """Verify committed config/summary bytes referenced by the attestation."""

    for source in (attestation.download_config, attestation.download_summary):
        path = repository_root / source.repository_path
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise EvidenceTamperedError(
                f"attestation source is unavailable: {source.repository_path}"
            ) from exc
        if len(payload) != source.size_bytes:
            raise EvidenceTamperedError(
                f"attestation source size mismatch: {source.repository_path}"
            )
        if hashlib.sha256(payload).hexdigest() != source.sha256:
            raise EvidenceTamperedError(
                f"attestation source digest mismatch: {source.repository_path}"
            )


def _canonical_digest(payload: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EvidenceMalformedError("Drive dataset attestation is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _load_repository_source(payload: Mapping[str, object], name: str) -> RepositorySourceIdentity:
    return RepositorySourceIdentity(
        drive_file_id=_drive_id(payload.get("drive_file_id"), f"{name} drive_file_id"),
        repository_path=_relative_path(payload.get("repository_path"), f"{name} repository_path"),
        sha256=_sha256(payload.get("sha256"), f"{name} sha256"),
        size_bytes=_positive_int(payload.get("size_bytes"), f"{name} size_bytes"),
    )


def _load_dataset_file(timeframe: str, payload: Mapping[str, object]) -> DatasetFileIdentity:
    if _non_negative_int(payload.get("duplicates"), f"{timeframe} duplicates") != 0:
        raise EvidenceMalformedError(f"{timeframe} duplicates must be zero")
    if not _boolean(payload.get("strictly_increasing"), f"{timeframe} strictly_increasing"):
        raise EvidenceMalformedError(f"{timeframe} timestamps must be strictly increasing")
    return DatasetFileIdentity(
        timeframe=timeframe,
        drive_file_id=_drive_id(payload.get("drive_file_id"), f"{timeframe} drive_file_id"),
        file_name=_string(payload.get("file_name"), f"{timeframe} file_name"),
        sha256=_sha256(payload.get("sha256"), f"{timeframe} sha256"),
        size_bytes=_positive_int(payload.get("size_bytes"), f"{timeframe} size_bytes"),
        rows=_positive_int(payload.get("rows"), f"{timeframe} rows"),
        first_open_time=_utc_timestamp(
            payload.get("first_open_time"), f"{timeframe} first_open_time"
        ),
        last_open_time=_utc_timestamp(payload.get("last_open_time"), f"{timeframe} last_open_time"),
    )


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise EvidenceMalformedError(f"{name} must be a string-keyed mapping")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise EvidenceMalformedError(f"{name} must be a non-blank trimmed string")
    return value


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise EvidenceMalformedError(f"{name} must be a non-empty array")
    return tuple(_string(item, name) for item in value)


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise EvidenceMalformedError(f"{name} must be boolean")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EvidenceMalformedError(f"{name} must be a positive integer")
    return value


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvidenceMalformedError(f"{name} must be a non-negative integer")
    return value


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise EvidenceMalformedError(f"{name} must be a lowercase SHA-256")
    return value


def _drive_id(value: object, name: str) -> str:
    if not isinstance(value, str) or _DRIVE_ID.fullmatch(value) is None:
        raise EvidenceMalformedError(f"{name} must be a Google Drive identifier")
    return value


def _relative_path(value: object, name: str) -> str:
    rendered = _string(value, name)
    path = PurePosixPath(rendered)
    if path.is_absolute() or ".." in path.parts or rendered.startswith("./"):
        raise EvidenceMalformedError(f"{name} must be a normalized relative path")
    if path.as_posix() != rendered:
        raise EvidenceMalformedError(f"{name} must use normalized POSIX separators")
    return rendered


def _utc_timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise EvidenceMalformedError(f"{name} must be ISO-8601")
    rendered = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as exc:
        raise EvidenceMalformedError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise EvidenceMalformedError(f"{name} must use UTC offset")
    return parsed.astimezone(UTC)


__all__ = [
    "DRIVE_DATASET_ATTESTATION_CANONICAL_SHA256",
    "DRIVE_DATASET_ATTESTATION_SCHEMA_VERSION",
    "DRIVE_DATASET_ATTESTATION_STATUS",
    "EXPECTED_TIMEFRAMES",
    "DatasetFileIdentity",
    "DriveDatasetAttestation",
    "RepositorySourceIdentity",
    "load_drive_dataset_attestation",
    "verify_attestation_repository_sources",
]
