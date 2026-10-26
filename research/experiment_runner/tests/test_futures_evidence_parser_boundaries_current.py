from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from modular_research_runner import futures_evidence
from modular_research_runner.models import SpecificationError


def test_json_mapping_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SpecificationError, match="output is missing application-run.json"):
        futures_evidence._json_mapping(tmp_path / "missing.json", "application-run.json")


def test_json_mapping_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "application-run.json"
    link.symlink_to(target)
    with pytest.raises(SpecificationError, match="output is missing application-run.json"):
        futures_evidence._json_mapping(link, "application-run.json")


def test_json_mapping_rejects_non_utf8(tmp_path: Path) -> None:
    evidence = tmp_path / "application-run.json"
    evidence.write_bytes(b"\xff\xfe")
    with pytest.raises(SpecificationError, match="is not UTF-8"):
        futures_evidence._json_mapping(evidence, "application-run.json")


@pytest.mark.parametrize("payload", ["{", "not-json"])
def test_json_mapping_rejects_invalid_json(tmp_path: Path, payload: str) -> None:
    evidence = tmp_path / "application-run.json"
    evidence.write_text(payload, encoding="utf-8")
    with pytest.raises(SpecificationError, match="is invalid"):
        futures_evidence._json_mapping(evidence, "application-run.json")


def test_json_mapping_rejects_non_object(tmp_path: Path) -> None:
    evidence = tmp_path / "application-run.json"
    evidence.write_text("[]", encoding="utf-8")
    with pytest.raises(SpecificationError, match="must be an object"):
        futures_evidence._json_mapping(evidence, "application-run.json")


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_json_mapping_rejects_nonfinite_constants(tmp_path: Path, constant: str) -> None:
    evidence = tmp_path / "application-run.json"
    evidence.write_text(f'{{"value":{constant}}}', encoding="utf-8")
    with pytest.raises(SpecificationError, match="non-finite JSON constant"):
        futures_evidence._json_mapping(evidence, "application-run.json")


@pytest.mark.parametrize("value", [None, "", "   ", 1])
def test_text_rejects_non_text_or_blank(value: object) -> None:
    with pytest.raises(SpecificationError, match="must be non-empty text"):
        futures_evidence._text(value, "field")


def test_text_returns_valid_text() -> None:
    assert futures_evidence._text("value", "field") == "value"


@pytest.mark.parametrize("value", ["A" * 64, "a" * 63, "g" * 64])
def test_sha256_rejects_noncanonical_digest(value: str) -> None:
    with pytest.raises(SpecificationError, match="lowercase SHA-256"):
        futures_evidence._sha256(value, "digest")


def test_sha256_accepts_canonical_digest() -> None:
    digest = "a" * 64
    assert futures_evidence._sha256(digest, "digest") == digest


@pytest.mark.parametrize("value", [True, False, -1, "1", 1.0])
def test_nonnegative_int_rejects_invalid_values(value: object) -> None:
    with pytest.raises(SpecificationError, match="non-negative integer"):
        futures_evidence._nonnegative_int(value, "count")


def test_nonnegative_and_optional_integer_success_paths() -> None:
    assert futures_evidence._nonnegative_int(0, "count") == 0
    assert futures_evidence._optional_nonnegative_int(None, "count") is None
    assert futures_evidence._optional_nonnegative_int(2, "count") == 2


def test_positive_int_rejects_zero_and_accepts_positive() -> None:
    with pytest.raises(SpecificationError, match="must be positive"):
        futures_evidence._positive_int(0, "count")
    assert futures_evidence._positive_int(3, "count") == 3


@pytest.mark.parametrize("value", [None, 1, ""])
def test_finite_decimal_rejects_non_text(value: object) -> None:
    with pytest.raises(SpecificationError, match="exact decimal text"):
        futures_evidence._finite_decimal_text(value, "amount")


def test_finite_decimal_rejects_invalid_and_nonfinite_text() -> None:
    with pytest.raises(SpecificationError, match="valid decimal text"):
        futures_evidence._finite_decimal_text("not-a-decimal", "amount")
    for value in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(SpecificationError, match="finite decimal text"):
            futures_evidence._finite_decimal_text(value, "amount")
    assert futures_evidence._finite_decimal_text("-1.25", "amount") == Decimal("-1.25")


def test_public_text_accepts_public_identifier_and_rejects_nul() -> None:
    assert futures_evidence._public_text("funding-owner-artifact", "artifact") is None
    with pytest.raises(SpecificationError, match="private storage metadata"):
        futures_evidence._public_text("artifact\x00secret", "artifact")


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("not-a-time", "ISO-8601 timestamp"),
        ("2026-01-01T00:00:00", "timezone-aware UTC"),
        ("2026-01-01T02:00:00+02:00", "must use UTC offset"),
    ],
)
def test_utc_timestamp_rejects_invalid_time_contract(value: str, message: str) -> None:
    with pytest.raises(SpecificationError, match=message):
        futures_evidence._utc_timestamp(value, "timestamp")


def test_utc_timestamp_accepts_zulu_and_zero_offset() -> None:
    first = futures_evidence._utc_timestamp("2026-01-01T00:00:00Z", "timestamp")
    second = futures_evidence._utc_timestamp("2026-01-01T00:00:00+00:00", "timestamp")
    assert first == second
