from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from modular_backtest import runner
from modular_backtest.config import BacktestAppConfig, DataSourceConfig, OutputConfig
from modular_backtest.funding import FundingEvent, FundingLineageEntry, RegisteredFundingSource
from modular_backtest.funding_application import (
    FundingApplicationError,
    application_run_id,
    collect_funding_runtime_evidence,
    funding_application_metadata,
    funding_execution_factory,
)
from modular_backtest.futures_execution import (
    FUNDING_RUNTIME_MODEL_VERSION,
    FundingAwareBrokerSimulator,
)
from modular_backtest.scalability import PerformanceConfig
from modular_core.audit import Journal
from modular_core.config import EngineConfig
from modular_core.instruments import InstrumentSpec


START = datetime(2026, 2, 1, tzinfo=UTC)
END = START + timedelta(hours=1)


def _source(*, dataset_hash: str = "a" * 64) -> RegisteredFundingSource:
    event = FundingEvent(
        ordinal=0,
        timestamp=START,
        symbol="BTCUSDT",
        rate=Decimal("0.001"),
        mark_price=Decimal("100"),
    )
    return RegisteredFundingSource(
        dataset_id="funding-app-test",
        dataset_version=1,
        dataset_hash=dataset_hash,
        symbol="BTCUSDT",
        coverage_start_inclusive=START,
        coverage_end_exclusive=END,
        event_count=1,
        source_record_count=1,
        lineage=(
            FundingLineageEntry(
                artifact_id="owner",
                source_kind="official_rest",
                checksum_sha256="b" * 64,
                record_count=1,
                start=START,
                end=END,
            ),
        ),
        events=(event,),
    )


def _write_events(path: Path, rows: list[object]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "events.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _funding_row(
    source: RegisteredFundingSource,
    *,
    ordinal: int = 0,
    cashflow: str = "-1.25",
    event_source: str = "backtest_funding",
    model: str = FUNDING_RUNTIME_MODEL_VERSION,
    source_identity: str | None = None,
) -> dict[str, object]:
    event = source.events[0]
    identity = source.identity_sha256()
    return {
        "event_type": "portfolio.funding",
        "source": event_source,
        "payload": {
            "funding": {
                "model_version": model,
                "source_identity_sha256": source_identity or identity,
                "ordinal": ordinal,
                "event_key": event.key(identity),
                "cashflow": cashflow,
            }
        },
    }


def test_application_identity_preserves_baseline_and_binds_funding_owner() -> None:
    source = _source()
    other = _source(dataset_hash="c" * 64)

    assert application_run_id("core-run", None) == "core-run"
    first = application_run_id("core-run", source)
    assert first == application_run_id("core-run", source)
    assert first.startswith("core-run-funding-")
    assert first != application_run_id("core-run", other)

    with pytest.raises(FundingApplicationError, match="non-empty"):
        application_run_id("", source)


def test_funding_metadata_is_backward_compatible_when_disabled() -> None:
    disabled = funding_application_metadata(None)
    assert disabled == {
        "funding_model": "disabled",
        "funding_source_identity_sha256": None,
        "funding_source": None,
    }

    enabled = funding_application_metadata(_source())
    assert enabled["funding_model"] == FUNDING_RUNTIME_MODEL_VERSION
    assert enabled["funding_source_identity_sha256"] is not None
    assert "uri" not in str(enabled["funding_source"]).lower()


def test_funding_execution_factory_creates_owner_bound_broker() -> None:
    source = _source()
    factory = funding_execution_factory(source)
    broker = factory(
        EngineConfig(initial_cash=1_000),
        Journal(run_id="funding-app-factory"),
        InstrumentSpec(symbol="BTCUSDT"),
    )

    assert isinstance(broker, FundingAwareBrokerSimulator)
    assert broker.funding_source is source

    with pytest.raises(TypeError, match="RegisteredFundingSource"):
        funding_execution_factory(SimpleNamespace())  # type: ignore[arg-type]


def test_runner_engine_factory_injects_funding_execution_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = BacktestAppConfig(
        data=DataSourceConfig(path=tmp_path / "bars.csv", format="csv", symbol="BTCUSDT"),
        modules=tmp_path / "modules.yaml",
        requested_timeframes=(),
        output=OutputConfig(tmp_path / "results"),
    )
    captured: dict[str, object] = {}

    class FakeRegistry:
        def load_config(self, path: Path) -> tuple[str, ...]:
            del path
            return ("module",)

    class FakeEngine:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(runner, "ModuleRegistry", FakeRegistry)
    monkeypatch.setattr(runner, "ScalableBacktestEngine", FakeEngine)
    engine = runner._engine_factory(
        config,
        PerformanceConfig(),
        scalable=True,
        funding_source=_source(),
    )()

    assert isinstance(engine, FakeEngine)
    execution_factory = captured["execution_factory"]
    assert callable(execution_factory)
    broker = execution_factory(
        EngineConfig(initial_cash=1_000),
        Journal(run_id="funding-runner-factory"),
        InstrumentSpec(symbol="BTCUSDT"),
    )
    assert isinstance(broker, FundingAwareBrokerSimulator)


def test_collect_funding_evidence_ignores_nonfunding_rows_and_hashes_events(
    tmp_path: Path,
) -> None:
    source = _source()
    _write_events(
        tmp_path,
        [
            {"event_type": "run.started", "source": "kernel", "payload": {}},
            _funding_row(source),
            {"event_type": "run.finished", "source": "kernel", "payload": {}},
        ],
    )

    evidence = dict(collect_funding_runtime_evidence(tmp_path, source, application_id="app-run"))

    assert evidence["application_run_id"] == "app-run"
    assert evidence["journaled_settlement_count"] == 1
    assert evidence["first_journaled_ordinal"] == 0
    assert evidence["last_journaled_ordinal"] == 0
    assert evidence["cumulative_journaled_cashflow"] == "-1.25"
    assert len(str(evidence["event_keys_sha256"])) == 64


def test_collect_funding_evidence_allows_no_runtime_settlement(tmp_path: Path) -> None:
    source = _source()
    _write_events(tmp_path, [{"event_type": "run.started", "source": "kernel"}])

    evidence = dict(collect_funding_runtime_evidence(tmp_path, source, application_id="app-run"))

    assert evidence["journaled_settlement_count"] == 0
    assert evidence["first_journaled_ordinal"] is None
    assert evidence["last_journaled_ordinal"] is None
    assert evidence["cumulative_journaled_cashflow"] == "0"


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ({"event_type": "portfolio.funding", "source": "wrong", "payload": {}}, "source"),
        ({"event_type": "portfolio.funding", "source": "backtest_funding"}, "payload"),
        (
            {
                "event_type": "portfolio.funding",
                "source": "backtest_funding",
                "payload": {},
            },
            "funding payload",
        ),
    ],
)
def test_collect_funding_evidence_rejects_invalid_envelopes(
    tmp_path: Path,
    row: dict[str, object],
    message: str,
) -> None:
    _write_events(tmp_path, [row])
    with pytest.raises(FundingApplicationError, match=message):
        collect_funding_runtime_evidence(tmp_path, _source(), application_id="app")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"model_version": "wrong"}, "model version"),
        ({"source_identity_sha256": "f" * 64}, "source identity"),
        ({"ordinal": -1}, "ordinal"),
        ({"event_key": "short"}, "event key"),
        ({"cashflow": 1.25}, "cashflow must be exact text"),
        ({"cashflow": "NaN"}, "cashflow must be finite"),
    ],
)
def test_collect_funding_evidence_rejects_invalid_settlement_fields(
    tmp_path: Path,
    mutation: dict[str, object],
    message: str,
) -> None:
    source = _source()
    row = _funding_row(source)
    funding = row["payload"]["funding"]
    funding.update(mutation)
    _write_events(tmp_path, [row])

    with pytest.raises(FundingApplicationError, match=message):
        collect_funding_runtime_evidence(tmp_path, source, application_id="app")


def test_collect_funding_evidence_rejects_duplicate_out_of_order_and_gap(
    tmp_path: Path,
) -> None:
    source = _source()
    duplicate = [_funding_row(source), _funding_row(source)]
    _write_events(tmp_path, duplicate)
    with pytest.raises(FundingApplicationError, match="duplicated"):
        collect_funding_runtime_evidence(tmp_path, source, application_id="app")

    second = _funding_row(source, ordinal=1)
    first = _funding_row(source, ordinal=0)
    _write_events(tmp_path, [second, first])
    with pytest.raises(FundingApplicationError, match="chronological"):
        collect_funding_runtime_evidence(tmp_path, source, application_id="app")

    third = _funding_row(source, ordinal=2)
    _write_events(tmp_path, [first, third])
    with pytest.raises(FundingApplicationError, match="gap"):
        collect_funding_runtime_evidence(tmp_path, source, application_id="app")


def test_collect_funding_evidence_rejects_missing_or_invalid_stream(tmp_path: Path) -> None:
    source = _source()
    with pytest.raises(FundingApplicationError, match="regular events.jsonl"):
        collect_funding_runtime_evidence(tmp_path, source, application_id="app")

    _write_events(tmp_path, [])
    (tmp_path / "events.jsonl").write_text("{invalid\n", encoding="utf-8")
    with pytest.raises(FundingApplicationError, match="invalid JSON"):
        collect_funding_runtime_evidence(tmp_path, source, application_id="app")

    with pytest.raises(FundingApplicationError, match="application_id"):
        collect_funding_runtime_evidence(tmp_path, source, application_id="")
