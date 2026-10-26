from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_futures_evidence_source_semantics_current import (
    _funding_source,
    _mark_source,
)
from test_registry_futures_evidence_current import _documents, _write_json

from modular_research_runner import futures_evidence
from modular_research_runner.models import SpecificationError

_CORE_RUN_ID = "core-run-fail-closed-current"


def _lineage_row(
    *,
    artifact_id: str = "owner-artifact",
    start: str = "2026-01-01T00:00:00+00:00",
    end: str = "2026-01-01T00:05:00+00:00",
) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "source_kind": "official_rest",
        "checksum_sha256": "a" * 64,
        "record_count": 1,
        "start": start,
        "end": end,
    }


def _write_case(
    root: Path,
    *,
    funding: bool,
    mark: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None, dict[str, Any]]:
    root.mkdir(parents=True)
    application, funding_runtime, valuation_runtime, application_run_id = _documents(
        _CORE_RUN_ID,
        funding=funding,
        mark=mark,
    )
    _write_json(root / "application-run.json", application)
    if funding_runtime is not None:
        _write_json(root / "funding-runtime.json", funding_runtime)
        (root / "events.jsonl").write_text('{"event":"done"}\n', encoding="utf-8")
    if valuation_runtime is not None:
        _write_json(root / "valuation-runtime.json", valuation_runtime)
    manifest: dict[str, Any] = {
        "schema_version": "1",
        "run_id": _CORE_RUN_ID,
        "files": {},
    }
    if application_run_id != _CORE_RUN_ID:
        manifest["application_run_id"] = application_run_id
    return application, funding_runtime, valuation_runtime, manifest


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([], "non-empty list"),
        ([{"artifact_id": "partial"}], "item schema is invalid"),
        (
            [
                _lineage_row(
                    start="2026-01-01T00:00:00+00:00",
                    end="2026-01-01T00:00:00+00:00",
                )
            ],
            "interval must be non-empty",
        ),
    ],
)
def test_lineage_rejects_invalid_container_item_and_interval(
    value: object,
    message: str,
) -> None:
    with pytest.raises(SpecificationError, match=message):
        futures_evidence._validate_lineage(value, "source.lineage")


def test_lineage_order_rejects_noncanonical_and_gap_and_accepts_overlap() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    first = ("a", start, start + timedelta(minutes=5), 1)
    second = (
        "b",
        start + timedelta(minutes=5),
        start + timedelta(minutes=10),
        1,
    )
    with pytest.raises(SpecificationError, match="canonically ordered"):
        futures_evidence._validate_lineage_order((second, first), "source.lineage")

    gapped = (
        first,
        (
            "b",
            start + timedelta(minutes=6),
            start + timedelta(minutes=10),
            1,
        ),
    )
    with pytest.raises(SpecificationError, match="uncovered interval"):
        futures_evidence._validate_lineage_order(gapped, "source.lineage")

    overlapping = (
        first,
        (
            "b",
            start + timedelta(minutes=4),
            start + timedelta(minutes=10),
            1,
        ),
    )
    futures_evidence._validate_lineage_order(overlapping, "source.lineage")


def test_source_validator_rejects_schema_and_unsupported_contract() -> None:
    with pytest.raises(SpecificationError, match="funding source schema is invalid"):
        futures_evidence._validate_source(
            {},
            expected_fields=futures_evidence._FUNDING_SOURCE_FIELDS,
            expected_identity="a" * 64,
            label="funding source",
        )

    value = {
        "dataset_hash": "a" * 64,
        "lineage": [_lineage_row()],
    }
    with pytest.raises(SpecificationError, match="unsupported source contract"):
        futures_evidence._validate_source(
            value,
            expected_fields={"dataset_hash", "lineage"},
            expected_identity="b" * 64,
            label="custom source",
        )


def test_funding_source_rejects_empty_interval_and_lineage_end_drift() -> None:
    source = _funding_source()
    source["coverage_end_exclusive"] = source["coverage_start_inclusive"]
    lineage = futures_evidence._validate_lineage(
        source["lineage"],
        "funding source lineage",
    )
    with pytest.raises(SpecificationError, match="coverage interval must be non-empty"):
        futures_evidence._validate_funding_source_values(source, lineage)

    source = _funding_source()
    source["lineage"][0]["end"] = "2026-01-01T12:00:00+00:00"
    lineage = futures_evidence._validate_lineage(
        source["lineage"],
        "funding source lineage",
    )
    with pytest.raises(SpecificationError, match="lineage end differs"):
        futures_evidence._validate_funding_source_values(source, lineage)


def test_mark_source_rejects_market_type_and_empty_window() -> None:
    source = _mark_source()
    source["market_type"] = "spot"
    lineage = futures_evidence._validate_lineage(
        source["lineage"],
        "mark source lineage",
    )
    with pytest.raises(SpecificationError, match="Binance USDT-M perpetual"):
        futures_evidence._validate_mark_source_values(source, lineage)

    source = _mark_source()
    source["window_end_exclusive"] = source["window_start_inclusive"]
    lineage = futures_evidence._validate_lineage(
        source["lineage"],
        "mark source lineage",
    )
    with pytest.raises(SpecificationError, match="mark window must be non-empty"):
        futures_evidence._validate_mark_source_values(source, lineage)


def test_modern_identity_and_disabled_funding_boundaries(tmp_path: Path) -> None:
    root = tmp_path / "run-id"
    application, _, _, manifest = _write_case(root, funding=False, mark=False)
    manifest["run_id"] = "other-core-run"
    with pytest.raises(SpecificationError, match="application/core run identity"):
        futures_evidence.build_futures_producer_assumptions(root, manifest)

    root = tmp_path / "manifest-id"
    application, _, _, manifest = _write_case(root, funding=False, mark=False)
    manifest["application_run_id"] = "unexpected"
    with pytest.raises(SpecificationError, match="unexpectedly binds an application id"):
        futures_evidence.build_futures_producer_assumptions(root, manifest)

    root = tmp_path / "active-source"
    application, _, _, manifest = _write_case(root, funding=False, mark=False)
    application["funding_source"] = {"unexpected": True}
    _write_json(root / "application-run.json", application)
    with pytest.raises(SpecificationError, match="contains an active source"):
        futures_evidence.build_futures_producer_assumptions(root, manifest)

    root = tmp_path / "disabled-runtime"
    _, _, _, manifest = _write_case(root, funding=False, mark=False)
    _write_json(root / "funding-runtime.json", {})
    with pytest.raises(SpecificationError, match="unexpectedly contains funding-runtime"):
        futures_evidence.build_futures_producer_assumptions(root, manifest)


def test_funding_runtime_rejects_missing_schema_embedded_and_provenance(
    tmp_path: Path,
) -> None:
    root = tmp_path / "missing"
    _, _, _, manifest = _write_case(root, funding=True, mark=False)
    (root / "funding-runtime.json").unlink()
    with pytest.raises(SpecificationError, match="missing funding-runtime"):
        futures_evidence.build_futures_producer_assumptions(root, manifest)

    root = tmp_path / "schema"
    application, runtime, _, manifest = _write_case(root, funding=True, mark=False)
    assert runtime is not None
    runtime["unexpected"] = True
    application["funding_runtime_evidence"] = runtime
    _write_json(root / "funding-runtime.json", runtime)
    _write_json(root / "application-run.json", application)
    with pytest.raises(SpecificationError, match="funding runtime evidence schema"):
        futures_evidence.build_futures_producer_assumptions(root, manifest)

    root = tmp_path / "embedded"
    application, _, _, manifest = _write_case(root, funding=True, mark=False)
    application["funding_runtime_evidence"] = "not-a-mapping"
    _write_json(root / "application-run.json", application)
    with pytest.raises(SpecificationError, match="differs from application-run"):
        futures_evidence.build_futures_producer_assumptions(root, manifest)

    root = tmp_path / "provenance"
    application, runtime, _, manifest = _write_case(root, funding=True, mark=False)
    assert runtime is not None
    runtime["schema_version"] = "2"
    application["funding_runtime_evidence"] = runtime
    _write_json(root / "funding-runtime.json", runtime)
    _write_json(root / "application-run.json", application)
    with pytest.raises(SpecificationError, match="identity/provenance is inconsistent"):
        futures_evidence.build_futures_producer_assumptions(root, manifest)


def test_valuation_runtime_rejects_schema_embedded_policy_and_provenance(
    tmp_path: Path,
) -> None:
    root = tmp_path / "schema"
    application, _, runtime, manifest = _write_case(root, funding=False, mark=True)
    assert runtime is not None
    runtime["unexpected"] = True
    application["valuation_runtime_evidence"] = runtime
    _write_json(root / "valuation-runtime.json", runtime)
    _write_json(root / "application-run.json", application)
    with pytest.raises(SpecificationError, match="valuation runtime evidence schema"):
        futures_evidence.build_futures_producer_assumptions(root, manifest)

    root = tmp_path / "embedded"
    application, _, _, manifest = _write_case(root, funding=False, mark=True)
    application["valuation_runtime_evidence"] = "not-a-mapping"
    _write_json(root / "application-run.json", application)
    with pytest.raises(SpecificationError, match="differs from application-run"):
        futures_evidence.build_futures_producer_assumptions(root, manifest)

    root = tmp_path / "policy"
    application, _, _, manifest = _write_case(root, funding=False, mark=True)
    application["valuation_policy"] = "last"
    _write_json(root / "application-run.json", application)
    with pytest.raises(SpecificationError, match="requires explicit mark policy"):
        futures_evidence.build_futures_producer_assumptions(root, manifest)

    root = tmp_path / "provenance"
    application, _, runtime, manifest = _write_case(root, funding=False, mark=True)
    assert runtime is not None
    runtime["schema_version"] = "2"
    application["valuation_runtime_evidence"] = runtime
    _write_json(root / "valuation-runtime.json", runtime)
    _write_json(root / "application-run.json", application)
    with pytest.raises(SpecificationError, match="identity/provenance is inconsistent"):
        futures_evidence.build_futures_producer_assumptions(root, manifest)


def test_source_identity_fields_reject_non_sha256(tmp_path: Path) -> None:
    root = tmp_path / "funding"
    application, _, _, manifest = _write_case(root, funding=True, mark=False)
    application["funding_source_identity_sha256"] = "bad"
    _write_json(root / "application-run.json", application)
    with pytest.raises(SpecificationError, match="lowercase SHA-256"):
        futures_evidence.build_futures_producer_assumptions(root, manifest)

    root = tmp_path / "mark"
    application, _, _, manifest = _write_case(root, funding=False, mark=True)
    application["mark_source_identity_sha256"] = "bad"
    _write_json(root / "application-run.json", application)
    with pytest.raises(SpecificationError, match="lowercase SHA-256"):
        futures_evidence.build_futures_producer_assumptions(root, manifest)


def test_event_stream_representation_contract(tmp_path: Path) -> None:
    with pytest.raises(SpecificationError, match="exactly one"):
        futures_evidence._event_stream_encoding(tmp_path)

    for name, expected in (
        ("events.jsonl", "identity"),
        ("events.jsonl.gz", "gzip"),
        ("events.manifest.json", "gzip-byte-shards-v1"),
    ):
        root = tmp_path / expected
        root.mkdir()
        (root / name).write_bytes(b"event")
        assert futures_evidence._event_stream_encoding(root) == expected

    root = tmp_path / "ambiguous"
    root.mkdir()
    (root / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "events.jsonl.gz").write_bytes(b"gzip")
    with pytest.raises(SpecificationError, match="exactly one"):
        futures_evidence._event_stream_encoding(root)


def test_current_funding_encoding_rejects_unsupported_value(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "funding-runtime.json",
        {"event_stream_encoding": "brotli"},
    )
    with pytest.raises(SpecificationError, match="event_stream_encoding is unsupported"):
        futures_evidence._validate_current_funding_encoding(tmp_path)


def test_current_funding_encoding_absent_runtime_is_noop(tmp_path: Path) -> None:
    assert futures_evidence._validate_current_funding_encoding(tmp_path) is None


def test_funding_provenance_source_object_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "source-mismatch"
    application, runtime, _, manifest = _write_case(root, funding=True, mark=False)
    assert runtime is not None
    changed = deepcopy(runtime)
    changed["source"] = {"unexpected": True}
    application["funding_runtime_evidence"] = changed
    _write_json(root / "funding-runtime.json", changed)
    _write_json(root / "application-run.json", application)
    with pytest.raises(SpecificationError, match="identity/provenance is inconsistent"):
        futures_evidence.build_futures_producer_assumptions(root, manifest)
