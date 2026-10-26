from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import strategy_audit_validation.dataset_attestation as subject
from strategy_audit_validation.errors import EvidenceMalformedError, EvidenceTamperedError

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_ATTESTATION_PATH = (
    _REPOSITORY_ROOT / "research/strategy_audit_validation/evidence/"
    "btcusdt-drive-dataset-attestation-v1.json"
)


def _payload() -> dict[str, object]:
    value = json.loads(_ATTESTATION_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _rehash(monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]) -> None:
    monkeypatch.setattr(
        subject,
        "DRIVE_DATASET_ATTESTATION_CANONICAL_SHA256",
        subject._canonical_digest(payload),
    )


def test_real_attestation_loads_and_keeps_control_gate_open() -> None:
    attestation = subject.load_drive_dataset_attestation(_payload())

    assert attestation.attestation_id == "btcusdt-drive-history-20260719-v1"
    assert attestation.canonical_sha256 == (
        "e3cc285ba9fe4e7a50879ae52660f380d7177bb35a20f13c264f993cb6975a91"
    )
    assert attestation.symbol == "BTCUSDT"
    assert attestation.timezone == "UTC"
    assert attestation.source_folder_id == "1mm_1z1alqfTUF_8stXoaccCQergs6Sk_"
    assert attestation.endpoint_proof_present is False
    assert attestation.files["5m"].rows == 721_597
    assert attestation.files["15m"].rows == 240_532
    assert attestation.files["1h"].rows == 60_133
    assert attestation.files["4h"].rows == 15_032
    assert attestation.files["5m"].sha256 == (
        "2b8a20ef09e1c292abbbee4a6963a1cedb7904cfc4340c9de72d0d2cd0b87ed1"
    )
    assert attestation.verified_at_utc.isoformat() == "2026-08-02T19:20:00+00:00"
    assert len(attestation.limitations) == 5
    assert attestation.control_evidence_complete is False

    complete = replace(
        attestation,
        endpoint_proof_present=True,
        dataset_registered_in_repository=True,
        component_ledgers_present=True,
        combined_395_trade_ledger_present=True,
        strategy_reproduction_complete=True,
    )
    assert complete.control_evidence_complete is True


def test_committed_source_files_match_attestation() -> None:
    attestation = subject.load_drive_dataset_attestation(_payload())
    subject.verify_attestation_repository_sources(attestation, _REPOSITORY_ROOT)


def test_any_unapproved_attestation_mutation_is_tampering() -> None:
    payload = _payload()
    payload["symbol"] = "ETHUSDT"

    with pytest.raises(EvidenceTamperedError, match="canonical digest mismatch"):
        subject.load_drive_dataset_attestation(payload)


def test_non_json_payload_is_rejected() -> None:
    with pytest.raises(EvidenceMalformedError, match="not canonical JSON"):
        subject._canonical_digest({"bad": {"not", "json"}})


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("schema_version", 2, "schema_version"),
        ("status", "OTHER", "status"),
        ("symbol", "ETHUSDT", "BTCUSDT in UTC"),
        ("timezone", "Europe/Berlin", "BTCUSDT in UTC"),
    ],
)
def test_root_identity_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object, match: str
) -> None:
    payload = _payload()
    payload[field] = value
    _rehash(monkeypatch, payload)

    with pytest.raises(EvidenceMalformedError, match=match):
        subject.load_drive_dataset_attestation(payload)


@pytest.mark.parametrize("field", ["source", "control_evidence", "verification", "files"])
def test_required_mappings_are_validated(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    payload = _payload()
    payload[field] = []
    _rehash(monkeypatch, payload)

    with pytest.raises(EvidenceMalformedError, match="mapping"):
        subject.load_drive_dataset_attestation(payload)


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("source", "endpoint_proof_present"),
        ("control_evidence", "dataset_registered_in_repository"),
        ("control_evidence", "component_ledgers_present"),
        ("control_evidence", "combined_395_trade_ledger_present"),
        ("control_evidence", "strategy_reproduction_complete"),
    ],
)
def test_unresolved_gates_cannot_be_claimed(
    monkeypatch: pytest.MonkeyPatch, section: str, field: str
) -> None:
    payload = _payload()
    target = payload[section]
    assert isinstance(target, dict)
    target[field] = True
    _rehash(monkeypatch, payload)

    with pytest.raises(EvidenceMalformedError, match="must remain false"):
        subject.load_drive_dataset_attestation(payload)


def test_dataset_attestation_flags_are_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _payload()
    controls = payload["control_evidence"]
    assert isinstance(controls, dict)
    controls["dataset_bytes_attested"] = False
    _rehash(monkeypatch, payload)
    with pytest.raises(EvidenceMalformedError, match="bytes must be attested"):
        subject.load_drive_dataset_attestation(payload)

    payload = _payload()
    controls = payload["control_evidence"]
    assert isinstance(controls, dict)
    controls["market_endpoint_proven"] = True
    _rehash(monkeypatch, payload)
    with pytest.raises(EvidenceMalformedError, match="cannot be proven"):
        subject.load_drive_dataset_attestation(payload)


def test_timeframe_inventory_is_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _payload()
    files = payload["files"]
    assert isinstance(files, dict)
    files.pop("4h")
    _rehash(monkeypatch, payload)

    with pytest.raises(EvidenceMalformedError, match="exactly 5m"):
        subject.load_drive_dataset_attestation(payload)


def test_dataset_file_invariants_are_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _payload()
    files = payload["files"]
    assert isinstance(files, dict)
    base = files["5m"]
    assert isinstance(base, dict)
    base["duplicates"] = 1
    _rehash(monkeypatch, payload)
    with pytest.raises(EvidenceMalformedError, match="duplicates must be zero"):
        subject.load_drive_dataset_attestation(payload)

    payload = _payload()
    files = payload["files"]
    assert isinstance(files, dict)
    base = files["5m"]
    assert isinstance(base, dict)
    base["strictly_increasing"] = False
    _rehash(monkeypatch, payload)
    with pytest.raises(EvidenceMalformedError, match="strictly increasing"):
        subject.load_drive_dataset_attestation(payload)


def test_repository_source_verifier_rejects_missing_size_and_digest(tmp_path: Path) -> None:
    attestation = subject.load_drive_dataset_attestation(_payload())

    with pytest.raises(EvidenceTamperedError, match="unavailable"):
        subject.verify_attestation_repository_sources(attestation, tmp_path)

    config = tmp_path / attestation.download_config.repository_path
    summary = tmp_path / attestation.download_summary.repository_path
    config.parent.mkdir(parents=True)
    config.write_bytes(b"x")
    summary.write_bytes(b"x")
    with pytest.raises(EvidenceTamperedError, match="size mismatch"):
        subject.verify_attestation_repository_sources(attestation, tmp_path)

    config.write_bytes(
        (_REPOSITORY_ROOT / attestation.download_config.repository_path).read_bytes()
    )
    summary.write_bytes(
        (_REPOSITORY_ROOT / attestation.download_summary.repository_path).read_bytes()
    )
    changed = bytearray(config.read_bytes())
    changed[0] ^= 1
    config.write_bytes(changed)
    with pytest.raises(EvidenceTamperedError, match="digest mismatch"):
        subject.verify_attestation_repository_sources(attestation, tmp_path)


@pytest.mark.parametrize(
    ("call", "match"),
    [
        (lambda: subject._mapping([], "value"), "mapping"),
        (lambda: subject._mapping({1: "x"}, "value"), "mapping"),
        (lambda: subject._string("", "value"), "non-blank"),
        (lambda: subject._string(" x ", "value"), "non-blank"),
        (lambda: subject._string_tuple([], "value"), "non-empty"),
        (lambda: subject._string_tuple([1], "value"), "non-blank"),
        (lambda: subject._boolean(1, "value"), "boolean"),
        (lambda: subject._positive_int(0, "value"), "positive integer"),
        (lambda: subject._positive_int(True, "value"), "positive integer"),
        (lambda: subject._non_negative_int(-1, "value"), "non-negative integer"),
        (lambda: subject._non_negative_int(False, "value"), "non-negative integer"),
        (lambda: subject._sha256("bad", "value"), "lowercase SHA-256"),
        (lambda: subject._drive_id("bad id", "value"), "Drive identifier"),
        (lambda: subject._relative_path("../bad", "value"), "normalized relative"),
        (lambda: subject._relative_path("./bad", "value"), "normalized relative"),
        (lambda: subject._utc_timestamp(1, "value"), "ISO-8601"),
        (lambda: subject._utc_timestamp("bad", "value"), "ISO-8601"),
        (
            lambda: subject._utc_timestamp("2026-01-01T03:00:00+03:00", "value"),
            "UTC offset",
        ),
    ],
)
def test_validation_helpers_fail_closed(call: object, match: str) -> None:
    assert callable(call)
    with pytest.raises(EvidenceMalformedError, match=match):
        call()


def test_validation_helpers_accept_valid_values() -> None:
    assert subject._mapping({"x": 1}, "value") == {"x": 1}
    assert subject._string("x", "value") == "x"
    assert subject._string_tuple(["x"], "value") == ("x",)
    assert subject._boolean(True, "value") is True
    assert subject._positive_int(1, "value") == 1
    assert subject._non_negative_int(0, "value") == 0
    assert subject._sha256("a" * 64, "value") == "a" * 64
    assert subject._drive_id("abc_123-Z", "value") == "abc_123-Z"
    assert subject._relative_path("a/b.json", "value") == "a/b.json"
    assert subject._utc_timestamp("2026-01-01T00:00:00Z", "value").tzinfo is not None
