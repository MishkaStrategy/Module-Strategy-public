from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from modular_research_runner.futures_evidence import (
    _expected_application_run_id,
    build_futures_producer_assumptions,
)
from modular_research_runner.models import SpecificationError

_CORE_RUN_ID = "core-run-runtime-semantics-current"
_FUNDING_MODEL = "registered-owner-funding-runtime-v2"
_EMPTY_KEYS_SHA = hashlib.sha256(b"").hexdigest()
_NONEMPTY_KEYS_SHA = hashlib.sha256(b"key-0\nkey-1").hexdigest()


def _canonical_sha256(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _source() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "dataset_id": "funding-btcusdt-runtime-semantics-current",
        "dataset_version": 1,
        "dataset_hash": "a" * 64,
        "symbol": "BTCUSDT",
        "coverage_start_inclusive": "2026-01-01T00:00:00+00:00",
        "coverage_end_exclusive": "2026-01-02T00:00:00+00:00",
        "coverage_semantics": "[start,end)",
        "event_count": 2,
        "source_record_count": 2,
        "lineage": [
            {
                "artifact_id": "funding-owner-runtime-semantics-current",
                "source_kind": "official_rest",
                "checksum_sha256": "b" * 64,
                "record_count": 2,
                "start": "2026-01-01T00:00:00+00:00",
                "end": "2026-01-02T00:00:00+00:00",
            }
        ],
    }


def _runtime(source: dict[str, Any], *, application_run_id: str) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "application_run_id": application_run_id,
        "model_version": _FUNDING_MODEL,
        "source_identity_sha256": _canonical_sha256(source),
        "source": source,
        "journaled_settlement_count": 2,
        "first_journaled_ordinal": 0,
        "last_journaled_ordinal": 1,
        "cumulative_journaled_cashflow": "-0.25",
        "event_keys_sha256": _NONEMPTY_KEYS_SHA,
        "event_stream_encoding": "identity",
    }


def _write_case(
    root: Path,
    *,
    mutate_runtime: Callable[[dict[str, Any]], None] | None = None,
    mutate_source: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    root.mkdir(parents=True)
    source = _source()
    if mutate_source is not None:
        mutate_source(source)
    source_identity = _canonical_sha256(source)
    application_run_id = _expected_application_run_id(
        _CORE_RUN_ID,
        funding_source_identity_sha256=source_identity,
        mark_source_identity_sha256=None,
    )
    runtime = _runtime(source, application_run_id=application_run_id)
    if mutate_runtime is not None:
        mutate_runtime(runtime)
    runtime["source_identity_sha256"] = source_identity
    runtime["source"] = source
    application = {
        "application_run_id": application_run_id,
        "core_run_id": _CORE_RUN_ID,
        "funding_model": _FUNDING_MODEL,
        "funding_source_identity_sha256": source_identity,
        "funding_source": source,
        "funding_runtime_evidence": runtime,
    }
    (root / "events.jsonl").write_text('{"event":"done"}\n', encoding="utf-8")
    (root / "application-run.json").write_text(
        json.dumps(application, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (root / "funding-runtime.json").write_text(
        json.dumps(runtime, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "schema_version": "1",
        "run_id": _CORE_RUN_ID,
        "application_run_id": application_run_id,
        "files": {},
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda runtime: runtime.__setitem__("journaled_settlement_count", True), "non-negative"),
        (lambda runtime: runtime.__setitem__("journaled_settlement_count", -1), "non-negative"),
        (
            lambda runtime: runtime.update(
                journaled_settlement_count=1,
                first_journaled_ordinal=None,
                last_journaled_ordinal=None,
            ),
            "requires first/last ordinals",
        ),
        (
            lambda runtime: runtime.update(
                journaled_settlement_count=0,
                first_journaled_ordinal=0,
                last_journaled_ordinal=0,
            ),
            "requires null first/last ordinals",
        ),
        (
            lambda runtime: runtime.update(
                journaled_settlement_count=1,
                first_journaled_ordinal=0,
                last_journaled_ordinal=1,
            ),
            "ordinal range are inconsistent",
        ),
        (lambda runtime: runtime.__setitem__("first_journaled_ordinal", -1), "non-negative"),
        (lambda runtime: runtime.__setitem__("cumulative_journaled_cashflow", 0), "exact decimal"),
        (
            lambda runtime: runtime.__setitem__("cumulative_journaled_cashflow", "NaN"),
            "finite decimal",
        ),
        (
            lambda runtime: runtime.update(
                journaled_settlement_count=3,
                first_journaled_ordinal=0,
                last_journaled_ordinal=2,
            ),
            "exceeds source event_count",
        ),
        (
            lambda runtime: runtime.update(
                first_journaled_ordinal=2,
                last_journaled_ordinal=3,
            ),
            "ordinal range exceeds source event_count",
        ),
        (
            lambda runtime: runtime.__setitem__("event_keys_sha256", _EMPTY_KEYS_SHA),
            "requires non-empty event-key digest",
        ),
        (
            lambda runtime: runtime.update(
                journaled_settlement_count=0,
                first_journaled_ordinal=None,
                last_journaled_ordinal=None,
            ),
            "requires empty event-key digest",
        ),
        (
            lambda runtime: runtime.update(
                journaled_settlement_count=0,
                first_journaled_ordinal=None,
                last_journaled_ordinal=None,
                event_keys_sha256=_EMPTY_KEYS_SHA,
                cumulative_journaled_cashflow="1",
            ),
            "requires zero cumulative cashflow",
        ),
    ],
)
def test_rejects_consistently_tampered_funding_runtime_values(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    root = tmp_path / "engine"
    manifest = _write_case(root, mutate_runtime=mutation)
    with pytest.raises(SpecificationError, match=message):
        build_futures_producer_assumptions(root, manifest)


def test_rejects_consistently_tampered_funding_source_counts(tmp_path: Path) -> None:
    root = tmp_path / "engine"
    manifest = _write_case(
        root,
        mutate_source=lambda source: source.__setitem__("source_record_count", 1),
    )
    with pytest.raises(SpecificationError, match="record/event counts are inconsistent"):
        build_futures_producer_assumptions(root, manifest)


def test_rejects_consistently_tampered_application_identity(tmp_path: Path) -> None:
    root = tmp_path / "engine"
    manifest = _write_case(root)
    application_path = root / "application-run.json"
    runtime_path = root / "funding-runtime.json"
    application = json.loads(application_path.read_text(encoding="utf-8"))
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    tampered_id = "consistent-but-derived-wrong-application-id"
    application["application_run_id"] = tampered_id
    runtime["application_run_id"] = tampered_id
    application["funding_runtime_evidence"] = runtime
    manifest["application_run_id"] = tampered_id
    application_path.write_text(json.dumps(application, sort_keys=True) + "\n", encoding="utf-8")
    runtime_path.write_text(json.dumps(runtime, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(SpecificationError, match="identity derivation has changed"):
        build_futures_producer_assumptions(root, manifest)


def test_accepts_valid_nonzero_funding_runtime_semantics(tmp_path: Path) -> None:
    root = tmp_path / "engine"
    manifest = _write_case(root)
    assumptions = build_futures_producer_assumptions(root, manifest)
    assert assumptions["funding"]["model"] == _FUNDING_MODEL
    assert assumptions["funding"]["runtime_artifact"] == "funding-runtime.json"
    assert assumptions["valuation"]["policy"] == "last"
