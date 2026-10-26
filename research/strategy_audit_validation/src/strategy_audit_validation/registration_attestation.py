"""Fail-closed validation for accepted BTCUSDT registration evidence."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Final, cast

from .dataset_attestation import load_drive_dataset_attestation
from .errors import EvidenceMalformedError, EvidenceTamperedError

REGISTRATION_EVIDENCE_SCHEMA_VERSION: Final = 1
REGISTRATION_EVIDENCE_STATUS: Final = (
    "ACCEPTED_REGISTRATION_AND_BACKTEST_SMOKE_ENDPOINT_URI_UNPROVEN"
)
REGISTRATION_EVIDENCE_CANONICAL_SHA256: Final = (
    "016e397c3067777a927cdd012e6fc12bfa1056c81504d4c2653d9a6e58a3c573"
)
EXPECTED_DATASET_ID: Final = "binance_usdt-m-perpetual_btcusdt_5m"
EXPECTED_DATASET_HASH: Final = "49ee948c7c22dc3c552bf6a96d2fdce9c5bba97662a694ae98ab85fc521c04f5"
EXPECTED_CORPUS_HASH: Final = "6d32c075f5adf3623679edd49c20ee8ee8758d3ea0e75b7f06613593e065c44e"
EXPECTED_INDEX_BLOB_OID: Final = "4a79dc860d1a582cfc3a2e54847467b9b02cf327"
EXPECTED_TIMEFRAMES: Final = ("5m", "10m", "15m", "1h", "4h")

_PARENT_SHA: Final = "e3cc285ba9fe4e7a50879ae52660f380d7177bb35a20f13c264f993cb6975a91"
_WORKFLOW_HEAD: Final = "656c3439edc1e17a6dfeab46cb68246a3e4cb4f8"
_SUMMARY_SHA: Final = "a5f763fed5912cf0a988f50ac163d1b47f2506d46412aec120cd56a4eaf43893"
_TEN_MINUTE_SHA: Final = "570cb34a910ae36d7cf74d2a68fb93048f2c4f3e86abd262f298e9a8919cdef3"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID = re.compile(r"^[0-9a-f]{40}$")
_EXPECTED_BTC_INDEX_FILES: Final = {
    "5m": ("1yfVgqpzxaev0cyOtxW8obdx57MlSJNR6", 34_020_301),
    "10m": ("1I2SCZiSniyZyLSFkU5KW8HCqHwVfjORo", 19_862_961),
    "15m": ("1fd-Kwzh9XWkG2dxVyzvZp9s8OxTYsFvn", 14_746_795),
    "1h": ("18k4utKCm1yABDRvDz3Oo93niKcI7ru0i", 4_660_189),
    "4h": ("1goH8ED_pysHoH0OMdJAqfUmIZI2zG51J", 1_059_387),
}


@dataclass(frozen=True, slots=True)
class AcceptedWorkflowIdentity:
    workflow: str
    run_id: int
    job_id: int
    head_sha: str
    artifact_id: int
    artifact_name: str
    artifact_zip_sha256: str


@dataclass(frozen=True, slots=True)
class RegisteredDatasetIdentity:
    dataset_id: str
    version: int
    dataset_hash: str
    instrument_id: str
    start: datetime
    end: datetime
    timeframes: tuple[str, ...]
    partition_count: int
    raw_artifact_count: int


@dataclass(frozen=True, slots=True)
class RegisteredBacktestSmoke:
    dataset_id: str
    dataset_version: int
    dataset_hash: str
    source_uri: str
    rows: int
    first_timestamp: datetime
    last_timestamp: datetime
    gaps: int
    duplicates: int
    expected_sha: str


@dataclass(frozen=True, slots=True)
class PersistentRegistrationEvidence:
    evidence_id: str
    canonical_sha256: str
    parent_attestation_path: str
    parent_attestation_sha256: str
    repository_index_path: str
    repository_index_blob_oid: str
    workflow: AcceptedWorkflowIdentity
    corpus_hash: str
    dataset: RegisteredDatasetIdentity
    smoke: RegisteredBacktestSmoke
    current_drive_10m_sha256: str
    limitations: tuple[str, ...]

    @property
    def registration_identity_proven(self) -> bool:
        return True

    @property
    def registered_backtest_consumption_proven(self) -> bool:
        return True

    @property
    def control_evidence_complete(self) -> bool:
        return False


def load_persistent_registration_evidence(
    payload: Mapping[str, object],
) -> PersistentRegistrationEvidence:
    """Load the immutable accepted registration payload."""

    digest = _canonical_digest(payload)
    if digest != REGISTRATION_EVIDENCE_CANONICAL_SHA256:
        raise EvidenceTamperedError("persistent registration evidence digest mismatch")
    _expect(payload, "schema_version", REGISTRATION_EVIDENCE_SCHEMA_VERSION)
    _expect(payload, "status", REGISTRATION_EVIDENCE_STATUS)

    parent = _mapping(payload.get("parent_dataset_attestation"), "parent_dataset_attestation")
    index = _mapping(payload.get("repository_index"), "repository_index")
    workflow_raw = _mapping(payload.get("accepted_workflow"), "accepted_workflow")
    corpus = _mapping(payload.get("corpus"), "corpus")
    dataset_raw = _mapping(payload.get("btc_dataset"), "btc_dataset")
    smoke_raw = _mapping(payload.get("backtest_smoke"), "backtest_smoke")
    current_10m = _mapping(payload.get("current_drive_10m"), "current_drive_10m")
    gates = _mapping(payload.get("gates"), "gates")

    _validate_parent(parent)
    _validate_index_contract(index)
    workflow = _load_workflow(workflow_raw)
    _validate_corpus(corpus)
    dataset = _load_dataset(dataset_raw)
    smoke = _load_smoke(smoke_raw, dataset, workflow)
    _validate_current_10m(current_10m)
    _validate_gates(gates)

    return PersistentRegistrationEvidence(
        evidence_id=_expected_string(
            payload.get("evidence_id"),
            "btcusdt-persistent-registration-acceptance-v1",
            "evidence_id",
        ),
        canonical_sha256=digest,
        parent_attestation_path=_relative_path(parent.get("path"), "parent path"),
        parent_attestation_sha256=_expected_sha(parent.get("canonical_sha256"), _PARENT_SHA),
        repository_index_path=_relative_path(index.get("path"), "repository index path"),
        repository_index_blob_oid=_expected_git_oid(index.get("git_blob_oid")),
        workflow=workflow,
        corpus_hash=EXPECTED_CORPUS_HASH,
        dataset=dataset,
        smoke=smoke,
        current_drive_10m_sha256=_expected_sha(current_10m.get("sha256"), _TEN_MINUTE_SHA),
        limitations=_string_tuple(payload.get("limitations"), "limitations"),
    )


def verify_registration_repository_sources(
    evidence: PersistentRegistrationEvidence, repository_root: Path
) -> None:
    """Verify current repository index and parent-attestation bytes."""

    index_path = repository_root / evidence.repository_index_path
    try:
        index_bytes = index_path.read_bytes()
    except OSError as exc:
        raise EvidenceTamperedError("registration repository index is unavailable") from exc
    if _git_blob_oid(index_bytes) != evidence.repository_index_blob_oid:
        raise EvidenceTamperedError("registration repository index blob mismatch")
    try:
        index_payload = json.loads(index_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceTamperedError("registration repository index is not valid JSON") from exc
    if not isinstance(index_payload, dict):
        raise EvidenceTamperedError("registration repository index must be an object")
    _verify_index_payload(cast(dict[str, object], index_payload))

    parent_path = repository_root / evidence.parent_attestation_path
    try:
        parent_payload = json.loads(parent_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceTamperedError("parent dataset attestation is unavailable") from exc
    if not isinstance(parent_payload, dict):
        raise EvidenceTamperedError("parent dataset attestation must be an object")
    parent = load_drive_dataset_attestation(cast(dict[str, object], parent_payload))
    if parent.canonical_sha256 != evidence.parent_attestation_sha256:
        raise EvidenceTamperedError("parent dataset attestation identity mismatch")


def _load_workflow(payload: Mapping[str, object]) -> AcceptedWorkflowIdentity:
    expected: dict[str, object] = {
        "workflow": "historical-data-persistent-registration-smoke-v2",
        "run_id": 30_589_784_503,
        "job_id": 91_029_327_057,
        "head_sha": _WORKFLOW_HEAD,
        "artifact_id": 8_777_966_748,
        "artifact_name": "historical-data-persistent-registration-v2-30589784503",
        "artifact_zip_size_bytes": 1623,
        "artifact_zip_sha256": "e418e858a3a23b25f55b72dafb1d897105529e147511cfb5bfc5738a7229a00a",
        "registration_json_size_bytes": 5868,
        "registration_json_sha256": (
            "315ee90d4907648e39af50614f1dd3c2c1f41792b8773293264e9fd337f3bbb4"
        ),
        "smoke_json_size_bytes": 656,
        "smoke_json_sha256": ("8cc4655a06a0c66f1d243d7ed1fb35dd4ef813e72af31b246f782d3ef3dd3c54"),
        "conclusion": "success",
    }
    _expect_fields(payload, expected, "accepted workflow")
    return AcceptedWorkflowIdentity(
        workflow=cast(str, expected["workflow"]),
        run_id=cast(int, expected["run_id"]),
        job_id=cast(int, expected["job_id"]),
        head_sha=_WORKFLOW_HEAD,
        artifact_id=cast(int, expected["artifact_id"]),
        artifact_name=cast(str, expected["artifact_name"]),
        artifact_zip_sha256=cast(str, expected["artifact_zip_sha256"]),
    )


def _validate_corpus(payload: Mapping[str, object]) -> None:
    _expect_fields(
        payload,
        {
            "corpus_hash": EXPECTED_CORPUS_HASH,
            "dataset_count": 10,
            "partition_count": 3865,
            "raw_artifact_count": 50,
            "total_rows": 12_968_018,
            "registration_version": 1,
            "registered_at": "2026-07-30T14:33:58.385428+00:00",
            "raw_market_data_uploaded": False,
        },
        "corpus",
    )


def _load_dataset(payload: Mapping[str, object]) -> RegisteredDatasetIdentity:
    _expect_fields(
        payload,
        {
            "dataset_id": EXPECTED_DATASET_ID,
            "version": 1,
            "dataset_hash": EXPECTED_DATASET_HASH,
            "instrument_id": "binance:usdt_m_perpetual:btcusdt",
            "start": "2019-09-08T17:55:00+00:00",
            "end": "2026-07-19T07:00:00+00:00",
            "timeframes": list(EXPECTED_TIMEFRAMES),
            "partition_count": 415,
            "raw_artifact_count": 5,
            "validation_status": "VALID",
            "reused": False,
        },
        "registered BTC dataset",
    )
    return RegisteredDatasetIdentity(
        dataset_id=EXPECTED_DATASET_ID,
        version=1,
        dataset_hash=EXPECTED_DATASET_HASH,
        instrument_id="binance:usdt_m_perpetual:btcusdt",
        start=_utc_timestamp(payload.get("start"), "dataset start"),
        end=_utc_timestamp(payload.get("end"), "dataset end"),
        timeframes=_string_sequence(payload.get("timeframes"), "dataset timeframes"),
        partition_count=415,
        raw_artifact_count=5,
    )


def _load_smoke(
    payload: Mapping[str, object],
    dataset: RegisteredDatasetIdentity,
    workflow: AcceptedWorkflowIdentity,
) -> RegisteredBacktestSmoke:
    _expect_fields(
        payload,
        {
            "status": "PASS",
            "dataset_id": dataset.dataset_id,
            "dataset_version": dataset.version,
            "dataset_hash": dataset.dataset_hash,
            "source_uri": "dataset://binance_usdt-m-perpetual_btcusdt_5m/v1",
            "rows": 576,
            "bars_processed": 576,
            "first_timestamp": "2024-01-01T00:00:00+00:00",
            "last_timestamp": "2024-01-02T23:55:00+00:00",
            "gaps": 0,
            "duplicates": 0,
            "partition_count": 83,
            "run_id": "run-a278b8c34066c7a8",
            "expected_sha": workflow.head_sha,
            "raw_market_data_uploaded": False,
            "local_storage_path_uploaded": False,
            "restart_verified": False,
        },
        "registered Backtest smoke",
    )
    return RegisteredBacktestSmoke(
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.version,
        dataset_hash=dataset.dataset_hash,
        source_uri="dataset://binance_usdt-m-perpetual_btcusdt_5m/v1",
        rows=576,
        first_timestamp=_utc_timestamp(payload.get("first_timestamp"), "smoke first timestamp"),
        last_timestamp=_utc_timestamp(payload.get("last_timestamp"), "smoke last timestamp"),
        gaps=0,
        duplicates=0,
        expected_sha=workflow.head_sha,
    )


def _validate_parent(payload: Mapping[str, object]) -> None:
    _expect_fields(
        payload,
        {
            "attestation_id": "btcusdt-drive-history-20260719-v1",
            "canonical_sha256": _PARENT_SHA,
            "path": (
                "research/strategy_audit_validation/evidence/"
                "btcusdt-drive-dataset-attestation-v1.json"
            ),
        },
        "parent dataset attestation",
    )


def _validate_index_contract(payload: Mapping[str, object]) -> None:
    _expect_fields(
        payload,
        {
            "path": (
                "modules/market_data_ingestion/configs/market_data/"
                "binance_usdtm_baseline_drive_index.json"
            ),
            "git_blob_oid": EXPECTED_INDEX_BLOB_OID,
            "exchange_id": "binance",
            "market_type": "usdt_m_perpetual",
            "folder_id": "1mm_1z1alqfTUF_8stXoaccCQergs6Sk_",
            "summary_file_id": "1nIsjXMI64hPLVu2SpoLbfE9BaRVU_WBs",
            "summary_sha256": _SUMMARY_SHA,
            "market_type_declared": True,
            "api_endpoint_uri_recorded": False,
        },
        "repository index contract",
    )


def _validate_current_10m(payload: Mapping[str, object]) -> None:
    _expect_fields(
        payload,
        {
            "drive_file_id": "1I2SCZiSniyZyLSFkU5KW8HCqHwVfjORo",
            "size_bytes": 19_862_961,
            "sha256": _TEN_MINUTE_SHA,
            "rows_from_download_summary": 360_798,
        },
        "current Drive 10m file",
    )


def _validate_gates(payload: Mapping[str, object]) -> None:
    expected: dict[str, object] = {
        "dataset_registration_identity_proven": True,
        "registered_backtest_consumption_proven": True,
        "repository_market_type_declaration_present": True,
        "api_endpoint_uri_proven": False,
        "persistent_registry_instance_retained": False,
        "component_trade_ledgers_present": False,
        "combined_395_trade_ledger_present": False,
        "strategy_reproduction_complete": False,
        "canonical_strategy_transition_allowed": False,
    }
    if dict(payload) != expected:
        raise EvidenceMalformedError("persistent registration gate state mismatch")


def _verify_index_payload(payload: Mapping[str, object]) -> None:
    _tamper_expect(payload, "exchange_id", "binance")
    _tamper_expect(payload, "market_type", "usdt_m_perpetual")
    _tamper_expect(payload, "folder_id", "1mm_1z1alqfTUF_8stXoaccCQergs6Sk_")
    _tamper_expect(payload, "summary_sha256", _SUMMARY_SHA)
    files = payload.get("files")
    if not isinstance(files, list):
        raise EvidenceTamperedError("repository index files must be an array")
    observed: dict[str, tuple[str, int]] = {}
    for item in files:
        if not isinstance(item, dict) or item.get("symbol") != "BTCUSDT":
            continue
        timeframe = item.get("timeframe")
        drive_id = item.get("drive_file_id")
        byte_size = item.get("byte_size")
        if isinstance(timeframe, str) and isinstance(drive_id, str) and isinstance(byte_size, int):
            observed[timeframe] = (drive_id, byte_size)
    if observed != _EXPECTED_BTC_INDEX_FILES:
        raise EvidenceTamperedError("repository index BTC file matrix mismatch")


def _expect_fields(
    payload: Mapping[str, object], expected: Mapping[str, object], name: str
) -> None:
    for field, value in expected.items():
        if payload.get(field) != value:
            raise EvidenceMalformedError(f"{name} {field} mismatch")


def _expect(payload: Mapping[str, object], field: str, expected: object) -> None:
    if payload.get(field) != expected:
        raise EvidenceMalformedError(f"registration evidence {field} mismatch")


def _tamper_expect(payload: Mapping[str, object], field: str, expected: object) -> None:
    if payload.get(field) != expected:
        raise EvidenceTamperedError(f"repository index {field} mismatch")


def _canonical_digest(payload: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EvidenceMalformedError("registration evidence is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _git_blob_oid(payload: bytes) -> str:
    header = f"blob {len(payload)}\0".encode()
    return hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise EvidenceMalformedError(f"{name} must be a string-keyed mapping")
    return cast(Mapping[str, object], value)


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise EvidenceMalformedError(f"{name} must be a non-empty string array")
    if any(not isinstance(item, str) or not item or item != item.strip() for item in value):
        raise EvidenceMalformedError(f"{name} must contain trimmed non-empty strings")
    return tuple(cast(list[str], value))


def _string_sequence(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise EvidenceMalformedError(f"{name} must be an array")
    if any(not isinstance(item, str) for item in value):
        raise EvidenceMalformedError(f"{name} must contain strings")
    return tuple(cast(Sequence[str], value))


def _expected_string(value: object, expected: str, name: str) -> str:
    if value != expected:
        raise EvidenceMalformedError(f"{name} mismatch")
    return expected


def _expected_sha(value: object, expected: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None or value != expected:
        raise EvidenceMalformedError("SHA-256 identity mismatch")
    return expected


def _expected_git_oid(value: object) -> str:
    if not isinstance(value, str) or _GIT_OID.fullmatch(value) is None:
        raise EvidenceMalformedError("Git blob identity is malformed")
    if value != EXPECTED_INDEX_BLOB_OID:
        raise EvidenceMalformedError("Git blob identity mismatch")
    return value


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


def _relative_path(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise EvidenceMalformedError(f"{name} must be a trimmed relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value.startswith("./"):
        raise EvidenceMalformedError(f"{name} must be a normalized relative path")
    if path.as_posix() != value:
        raise EvidenceMalformedError(f"{name} must use normalized POSIX separators")
    return value


__all__ = [
    "EXPECTED_CORPUS_HASH",
    "EXPECTED_DATASET_HASH",
    "EXPECTED_DATASET_ID",
    "EXPECTED_INDEX_BLOB_OID",
    "EXPECTED_TIMEFRAMES",
    "REGISTRATION_EVIDENCE_CANONICAL_SHA256",
    "REGISTRATION_EVIDENCE_SCHEMA_VERSION",
    "REGISTRATION_EVIDENCE_STATUS",
    "AcceptedWorkflowIdentity",
    "PersistentRegistrationEvidence",
    "RegisteredBacktestSmoke",
    "RegisteredDatasetIdentity",
    "load_persistent_registration_evidence",
    "verify_registration_repository_sources",
]
