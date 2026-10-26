from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

import strategy_audit_validation.registration_attestation as subject
from strategy_audit_validation.errors import EvidenceMalformedError, EvidenceTamperedError

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_EVIDENCE_PATH = (
    _REPOSITORY_ROOT / "research/strategy_audit_validation/evidence/"
    "btcusdt-persistent-registration-acceptance-v1.json"
)


def _payload() -> dict[str, object]:
    value = json.loads(_EVIDENCE_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _section(payload: dict[str, object], name: str) -> dict[str, object]:
    value = payload[name]
    assert isinstance(value, dict)
    return value


def _rehash(monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]) -> None:
    monkeypatch.setattr(
        subject,
        "REGISTRATION_EVIDENCE_CANONICAL_SHA256",
        subject._canonical_digest(payload),
    )


def test_real_registration_evidence_and_repository_sources_are_valid() -> None:
    evidence = subject.load_persistent_registration_evidence(_payload())

    assert evidence.evidence_id == "btcusdt-persistent-registration-acceptance-v1"
    assert evidence.canonical_sha256 == (
        "016e397c3067777a927cdd012e6fc12bfa1056c81504d4c2653d9a6e58a3c573"
    )
    assert evidence.workflow.run_id == 30_589_784_503
    assert evidence.workflow.job_id == 91_029_327_057
    assert evidence.workflow.artifact_id == 8_777_966_748
    assert evidence.corpus_hash == subject.EXPECTED_CORPUS_HASH
    assert evidence.dataset.dataset_id == subject.EXPECTED_DATASET_ID
    assert evidence.dataset.version == 1
    assert evidence.dataset.dataset_hash == subject.EXPECTED_DATASET_HASH
    assert evidence.dataset.timeframes == subject.EXPECTED_TIMEFRAMES
    assert evidence.dataset.partition_count == 415
    assert evidence.dataset.raw_artifact_count == 5
    assert evidence.smoke.rows == 576
    assert evidence.smoke.gaps == 0
    assert evidence.smoke.duplicates == 0
    assert evidence.registration_identity_proven is True
    assert evidence.registered_backtest_consumption_proven is True
    assert evidence.control_evidence_complete is False
    assert len(evidence.limitations) == 5

    subject.verify_registration_repository_sources(evidence, _REPOSITORY_ROOT)


def test_unapproved_payload_mutation_is_tampering() -> None:
    payload = _payload()
    payload["evidence_id"] = "changed"

    with pytest.raises(EvidenceTamperedError, match="digest mismatch"):
        subject.load_persistent_registration_evidence(payload)


def test_non_json_payload_is_rejected() -> None:
    with pytest.raises(EvidenceMalformedError, match="not canonical JSON"):
        subject._canonical_digest({"bad": {"not", "json"}})


@pytest.mark.parametrize(
    ("field", "value"),
    [("schema_version", 2), ("status", "OTHER")],
)
def test_root_contract_is_exact(monkeypatch: pytest.MonkeyPatch, field: str, value: object) -> None:
    payload = _payload()
    payload[field] = value
    _rehash(monkeypatch, payload)

    with pytest.raises(EvidenceMalformedError, match=field):
        subject.load_persistent_registration_evidence(payload)


@pytest.mark.parametrize(
    "section",
    [
        "parent_dataset_attestation",
        "repository_index",
        "accepted_workflow",
        "corpus",
        "btc_dataset",
        "backtest_smoke",
        "current_drive_10m",
        "gates",
    ],
)
def test_required_sections_are_mappings(monkeypatch: pytest.MonkeyPatch, section: str) -> None:
    payload = _payload()
    payload[section] = []
    _rehash(monkeypatch, payload)

    with pytest.raises(EvidenceMalformedError, match="mapping"):
        subject.load_persistent_registration_evidence(payload)


@pytest.mark.parametrize(
    ("section", "field", "value", "match"),
    [
        ("parent_dataset_attestation", "attestation_id", "other", "attestation_id"),
        ("repository_index", "market_type", "spot", "market_type"),
        ("accepted_workflow", "run_id", 1, "run_id"),
        ("corpus", "dataset_count", 9, "dataset_count"),
        ("btc_dataset", "dataset_hash", "a" * 64, "dataset_hash"),
        ("backtest_smoke", "status", "FAIL", "status"),
        ("current_drive_10m", "size_bytes", 1, "size_bytes"),
    ],
)
def test_nested_contracts_are_exact(
    monkeypatch: pytest.MonkeyPatch,
    section: str,
    field: str,
    value: object,
    match: str,
) -> None:
    payload = _payload()
    _section(payload, section)[field] = value
    _rehash(monkeypatch, payload)

    with pytest.raises(EvidenceMalformedError, match=match):
        subject.load_persistent_registration_evidence(payload)


def test_gate_state_cannot_be_overclaimed(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _payload()
    _section(payload, "gates")["api_endpoint_uri_proven"] = True
    _rehash(monkeypatch, payload)

    with pytest.raises(EvidenceMalformedError, match="gate state mismatch"):
        subject.load_persistent_registration_evidence(payload)


def test_repository_verifier_rejects_missing_and_changed_index(tmp_path: Path) -> None:
    evidence = subject.load_persistent_registration_evidence(_payload())

    with pytest.raises(EvidenceTamperedError, match="index is unavailable"):
        subject.verify_registration_repository_sources(evidence, tmp_path)

    index = tmp_path / evidence.repository_index_path
    index.parent.mkdir(parents=True)
    index.write_text("{}", encoding="utf-8")
    with pytest.raises(EvidenceTamperedError, match="blob mismatch"):
        subject.verify_registration_repository_sources(evidence, tmp_path)


def test_repository_verifier_rejects_invalid_index_json(tmp_path: Path) -> None:
    evidence = subject.load_persistent_registration_evidence(_payload())
    index = tmp_path / evidence.repository_index_path
    index.parent.mkdir(parents=True)
    index.write_bytes(b"not-json")
    changed = replace(evidence, repository_index_blob_oid=subject._git_blob_oid(b"not-json"))

    with pytest.raises(EvidenceTamperedError, match="not valid JSON"):
        subject.verify_registration_repository_sources(changed, tmp_path)


def test_repository_verifier_rejects_non_object_index(tmp_path: Path) -> None:
    evidence = subject.load_persistent_registration_evidence(_payload())
    index = tmp_path / evidence.repository_index_path
    index.parent.mkdir(parents=True)
    index.write_bytes(b"[]")
    changed = replace(evidence, repository_index_blob_oid=subject._git_blob_oid(b"[]"))

    with pytest.raises(EvidenceTamperedError, match="must be an object"):
        subject.verify_registration_repository_sources(changed, tmp_path)


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("exchange_id", "other", "exchange_id"),
        ("market_type", "spot", "market_type"),
        ("folder_id", "other", "folder_id"),
        ("summary_sha256", "a" * 64, "summary_sha256"),
        ("files", {}, "files must be an array"),
    ],
)
def test_repository_index_payload_is_fail_closed(field: str, value: object, match: str) -> None:
    payload: dict[str, object] = {
        "exchange_id": "binance",
        "market_type": "usdt_m_perpetual",
        "folder_id": "1mm_1z1alqfTUF_8stXoaccCQergs6Sk_",
        "summary_sha256": "a5f763fed5912cf0a988f50ac163d1b47f2506d46412aec120cd56a4eaf43893",
        "files": [],
    }
    payload[field] = value

    with pytest.raises(EvidenceTamperedError, match=match):
        subject._verify_index_payload(payload)


def test_repository_index_requires_exact_btc_file_matrix() -> None:
    payload: dict[str, object] = {
        "exchange_id": "binance",
        "market_type": "usdt_m_perpetual",
        "folder_id": "1mm_1z1alqfTUF_8stXoaccCQergs6Sk_",
        "summary_sha256": "a5f763fed5912cf0a988f50ac163d1b47f2506d46412aec120cd56a4eaf43893",
        "files": [
            {"symbol": "ETHUSDT"},
            {"symbol": "BTCUSDT", "timeframe": 1},
        ],
    }

    with pytest.raises(EvidenceTamperedError, match="file matrix mismatch"):
        subject._verify_index_payload(payload)


def test_repository_verifier_requires_valid_parent(tmp_path: Path) -> None:
    evidence = subject.load_persistent_registration_evidence(_payload())
    source_index = _REPOSITORY_ROOT / evidence.repository_index_path
    index = tmp_path / evidence.repository_index_path
    index.parent.mkdir(parents=True)
    index.write_bytes(source_index.read_bytes())

    with pytest.raises(EvidenceTamperedError, match="parent dataset attestation is unavailable"):
        subject.verify_registration_repository_sources(evidence, tmp_path)

    parent = tmp_path / evidence.parent_attestation_path
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text("[]", encoding="utf-8")
    with pytest.raises(EvidenceTamperedError, match="must be an object"):
        subject.verify_registration_repository_sources(evidence, tmp_path)


def test_repository_verifier_rejects_parent_identity_mismatch() -> None:
    evidence = subject.load_persistent_registration_evidence(_payload())
    changed = replace(evidence, parent_attestation_sha256="a" * 64)

    with pytest.raises(EvidenceTamperedError, match="identity mismatch"):
        subject.verify_registration_repository_sources(changed, _REPOSITORY_ROOT)


@pytest.mark.parametrize(
    ("call", "match"),
    [
        (lambda: subject._mapping([], "value"), "mapping"),
        (lambda: subject._mapping({1: "x"}, "value"), "mapping"),
        (lambda: subject._string_tuple([], "value"), "non-empty"),
        (lambda: subject._string_tuple([" x "], "value"), "trimmed"),
        (lambda: subject._string_sequence("5m", "value"), "array"),
        (lambda: subject._string_sequence([1], "value"), "contain strings"),
        (lambda: subject._expected_string("x", "y", "value"), "mismatch"),
        (lambda: subject._expected_sha("bad", "a" * 64), "identity mismatch"),
        (lambda: subject._expected_git_oid("bad"), "malformed"),
        (lambda: subject._expected_git_oid("a" * 40), "identity mismatch"),
        (lambda: subject._utc_timestamp(1, "value"), "ISO-8601"),
        (lambda: subject._utc_timestamp("bad", "value"), "ISO-8601"),
        (
            lambda: subject._utc_timestamp("2026-01-01T03:00:00+03:00", "value"),
            "UTC offset",
        ),
        (lambda: subject._relative_path("", "value"), "trimmed relative path"),
        (lambda: subject._relative_path("../bad", "value"), "normalized relative path"),
        (lambda: subject._relative_path("./bad", "value"), "normalized relative path"),
        (lambda: subject._relative_path("a//b", "value"), "POSIX separators"),
    ],
)
def test_validation_helpers_fail_closed(call: Callable[[], object], match: str) -> None:
    with pytest.raises(EvidenceMalformedError, match=match):
        call()


def test_validation_helpers_accept_valid_values() -> None:
    assert subject._mapping({"x": 1}, "value") == {"x": 1}
    assert subject._string_tuple(["x"], "value") == ("x",)
    assert subject._string_sequence(["x"], "value") == ("x",)
    assert subject._expected_string("x", "x", "value") == "x"
    assert subject._expected_sha("a" * 64, "a" * 64) == "a" * 64
    assert subject._expected_git_oid(subject.EXPECTED_INDEX_BLOB_OID) == (
        subject.EXPECTED_INDEX_BLOB_OID
    )
    assert subject._utc_timestamp("2026-01-01T00:00:00Z", "value").tzinfo is not None
    assert subject._relative_path("a/b.json", "value") == "a/b.json"
