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

_CORE_RUN_ID = "core-run-source-semantics-current"
_FUNDING_MODEL = "registered-owner-funding-runtime-v2"
_MARK_MODEL = "registered-mark-reference-v1"
_EMPTY_KEYS_SHA = hashlib.sha256(b"").hexdigest()


def _canonical_sha256(value: dict[str, Any], *, trailing_newline: bool) -> str:
    text = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if trailing_newline:
        text += "\n"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _funding_source() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "dataset_id": "funding-btcusdt-source-semantics-current",
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
                "artifact_id": "funding-owner-source-semantics-current",
                "source_kind": "official_rest",
                "checksum_sha256": "b" * 64,
                "record_count": 2,
                "start": "2026-01-01T00:00:00+00:00",
                "end": "2026-01-02T00:00:00+00:00",
            }
        ],
    }


def _mark_source() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "model_version": _MARK_MODEL,
        "dataset_id": "mark-btcusdt-source-semantics-current",
        "dataset_version": 1,
        "dataset_hash": "c" * 64,
        "symbol": "BTCUSDT",
        "exchange_id": "binance",
        "market_type": "usdt_m_perpetual",
        "data_type": "mark_price",
        "timeframe": "5m",
        "window_start_inclusive": "2026-01-01T00:00:00+00:00",
        "window_end_exclusive": "2026-01-01T00:05:00+00:00",
        "coverage_semantics": "[start,end)",
        "row_count": 1,
        "owner_row_count": 1,
        "owner_source_record_count": 1,
        "lineage": [
            {
                "artifact_id": "mark-owner-source-semantics-current",
                "source_kind": "official_rest",
                "checksum_sha256": "d" * 64,
                "record_count": 1,
                "start": "2026-01-01T00:00:00+00:00",
                "end": "2026-01-01T00:05:00+00:00",
            }
        ],
    }


def _write_funding_case(
    root: Path,
    *,
    mutate_source: Callable[[dict[str, Any]], None] | None = None,
    funding_model: str = _FUNDING_MODEL,
) -> dict[str, Any]:
    root.mkdir(parents=True)
    source = _funding_source()
    if mutate_source is not None:
        mutate_source(source)
    source_identity = _canonical_sha256(source, trailing_newline=False)
    application_run_id = _expected_application_run_id(
        _CORE_RUN_ID,
        funding_source_identity_sha256=source_identity,
        mark_source_identity_sha256=None,
    )
    runtime = {
        "schema_version": "1",
        "application_run_id": application_run_id,
        "model_version": funding_model,
        "source_identity_sha256": source_identity,
        "source": source,
        "journaled_settlement_count": 0,
        "first_journaled_ordinal": None,
        "last_journaled_ordinal": None,
        "cumulative_journaled_cashflow": "0",
        "event_keys_sha256": _EMPTY_KEYS_SHA,
        "event_stream_encoding": "identity",
    }
    application = {
        "application_run_id": application_run_id,
        "core_run_id": _CORE_RUN_ID,
        "funding_model": funding_model,
        "funding_source_identity_sha256": source_identity,
        "funding_source": source,
        "funding_runtime_evidence": runtime,
    }
    (root / "events.jsonl").write_text('{"event":"done"}\n', encoding="utf-8")
    _write_json(root / "application-run.json", application)
    _write_json(root / "funding-runtime.json", runtime)
    return {
        "schema_version": "1",
        "run_id": _CORE_RUN_ID,
        "application_run_id": application_run_id,
        "files": {},
    }


def _write_mark_case(
    root: Path,
    *,
    mutate_source: Callable[[dict[str, Any]], None] | None = None,
    valuation_model: str = _MARK_MODEL,
) -> dict[str, Any]:
    root.mkdir(parents=True)
    source = _mark_source()
    if mutate_source is not None:
        mutate_source(source)
    source_identity = _canonical_sha256(source, trailing_newline=True)
    application_run_id = _expected_application_run_id(
        _CORE_RUN_ID,
        funding_source_identity_sha256=None,
        mark_source_identity_sha256=source_identity,
    )
    semantics = {
        "execution_price_source": "traded_ohlc",
        "execution_risk_reference": "mark_open",
        "post_bar_reference": "mark_close",
    }
    runtime = {
        "schema_version": "1",
        "application_run_id": application_run_id,
        "valuation_policy": "mark",
        "valuation_model": valuation_model,
        "mark_source_identity_sha256": source_identity,
        "mark_source": source,
        **semantics,
    }
    application = {
        "application_run_id": application_run_id,
        "core_run_id": _CORE_RUN_ID,
        "funding_model": "disabled",
        "funding_source_identity_sha256": None,
        "funding_source": None,
        "funding_runtime_evidence": None,
        "valuation_policy": "mark",
        "valuation_model": valuation_model,
        "mark_source_identity_sha256": source_identity,
        "mark_source": source,
        "valuation_runtime_evidence": runtime,
        **semantics,
    }
    _write_json(root / "application-run.json", application)
    _write_json(root / "valuation-runtime.json", runtime)
    return {
        "schema_version": "1",
        "run_id": _CORE_RUN_ID,
        "application_run_id": application_run_id,
        "files": {},
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda source: source.__setitem__("schema_version", True), "schema_version must be 1"),
        (
            lambda source: source.__setitem__("dataset_version", 0),
            "dataset_version must be positive",
        ),
        (lambda source: source.__setitem__("symbol", "btcusdt"), "uppercase USDT symbol"),
        (
            lambda source: source.__setitem__("coverage_semantics", "(start,end]"),
            "coverage semantics changed",
        ),
        (
            lambda source: source.__setitem__(
                "coverage_start_inclusive", "2026-01-01T02:00:00+02:00"
            ),
            "must use UTC offset",
        ),
        (
            lambda source: source["lineage"][0].__setitem__("record_count", 1),
            "lineage counts differ",
        ),
        (
            lambda source: source["lineage"][0].__setitem__("start", "2026-01-01T00:05:00+00:00"),
            "lineage start differs",
        ),
    ],
)
def test_rejects_consistently_tampered_funding_source_contract(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    root = tmp_path / "funding"
    manifest = _write_funding_case(root, mutate_source=mutation)
    with pytest.raises(SpecificationError, match=message):
        build_futures_producer_assumptions(root, manifest)


def test_rejects_consistently_tampered_funding_model(tmp_path: Path) -> None:
    root = tmp_path / "funding"
    manifest = _write_funding_case(root, funding_model="changed-funding-model")
    with pytest.raises(SpecificationError, match="funding model_version changed"):
        build_futures_producer_assumptions(root, manifest)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda source: source.__setitem__("schema_version", True), "schema_version must be 1"),
        (lambda source: source.__setitem__("model_version", "changed"), "model_version changed"),
        (
            lambda source: source.__setitem__("dataset_version", 0),
            "dataset_version must be positive",
        ),
        (lambda source: source.__setitem__("symbol", "BTCUSD"), "uppercase USDT symbol"),
        (
            lambda source: source.__setitem__("exchange_id", "other"),
            "must be Binance USDT-M perpetual",
        ),
        (
            lambda source: source.__setitem__("data_type", "index_price"),
            "data_type must be mark_price",
        ),
        (lambda source: source.__setitem__("timeframe", "banana"), "timeframe is unsupported"),
        (
            lambda source: source.__setitem__("coverage_semantics", "closed"),
            "coverage semantics changed",
        ),
        (
            lambda source: source.__setitem__("window_end_exclusive", "2026-01-01T00:10:00+00:00"),
            "row_count does not exactly cover selected window",
        ),
        (
            lambda source: source.__setitem__("owner_source_record_count", 2),
            "normalized/source counts differ",
        ),
        (
            lambda source: source.__setitem__("row_count", 2),
            "selected row_count exceeds owner row evidence",
        ),
        (
            lambda source: source["lineage"][0].__setitem__("record_count", 2),
            "lineage counts differ",
        ),
        (
            lambda source: source["lineage"][0].__setitem__("start", "2026-01-01T00:01:00+00:00"),
            "lineage does not cover selected run window",
        ),
    ],
)
def test_rejects_consistently_tampered_mark_source_contract(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    root = tmp_path / "mark"
    manifest = _write_mark_case(root, mutate_source=mutation)
    with pytest.raises(SpecificationError, match=message):
        build_futures_producer_assumptions(root, manifest)


def test_rejects_consistently_tampered_valuation_model(tmp_path: Path) -> None:
    root = tmp_path / "mark"
    manifest = _write_mark_case(root, valuation_model="changed-mark-model")
    with pytest.raises(SpecificationError, match="valuation model_version changed"):
        build_futures_producer_assumptions(root, manifest)


def test_accepts_valid_owner_source_contracts(tmp_path: Path) -> None:
    funding_root = tmp_path / "funding"
    funding_manifest = _write_funding_case(funding_root)
    funding = build_futures_producer_assumptions(funding_root, funding_manifest)
    assert funding["funding"]["model"] == _FUNDING_MODEL
    assert funding["valuation"]["policy"] == "last"

    mark_root = tmp_path / "mark"
    mark_manifest = _write_mark_case(mark_root)
    mark = build_futures_producer_assumptions(mark_root, mark_manifest)
    assert mark["funding"]["model"] == "disabled"
    assert mark["valuation"]["model"] == _MARK_MODEL
