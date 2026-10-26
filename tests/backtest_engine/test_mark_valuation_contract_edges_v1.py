from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from modular_backtest.funding import FundingEvent, FundingLineageEntry, RegisteredFundingSource
from modular_backtest.valuation import (
    FundingMarkAwareBrokerSimulator,
    MarkAwareBrokerSimulator,
    MarkAwarePortfolio,
    MarkValuationError,
    PriceLineageEntry,
    RegisteredMarkPriceSource,
    VALUATION_POLICY_LAST,
    VALUATION_POLICY_MARK,
    _canonical_json_bytes,
    _finite_positive_float,
    _required_record_count,
    _utc_datetime,
    adapt_registered_mark_price_selection,
    normalize_valuation_policy,
    open_registered_mark_price_source,
    valuation_application_run_id,
    valuation_execution_factory,
    valuation_runtime_evidence,
)
from modular_core.audit import Journal
from modular_core.config import EngineConfig
from modular_core.execution import Portfolio
from modular_core.instruments import InstrumentSpec
from modular_core.models import Candle, Position, PositionSide

START = datetime(2026, 1, 1, tzinfo=UTC)
STEP = timedelta(minutes=5)
END = START + 2 * STEP


def _market_contracts() -> tuple[Any, Any]:
    enums = pytest.importorskip("market_data_ingestion.enums")
    records = pytest.importorskip("market_data_ingestion.records")
    return enums.DataType, records.PriceKlineRecord


def _lineage(
    *,
    artifact_id: str = "mark-owner",
    record_count: int = 2,
    start: datetime = START,
    end: datetime = END,
) -> PriceLineageEntry:
    return PriceLineageEntry(
        artifact_id=artifact_id,
        source_kind="official_rest",
        checksum_sha256="a" * 64,
        record_count=record_count,
        start=start,
        end=end,
    )


def _source() -> RegisteredMarkPriceSource:
    return RegisteredMarkPriceSource(
        dataset_id="binance-btcusdt-mark",
        dataset_version=1,
        dataset_hash="b" * 64,
        symbol="BTCUSDT",
        exchange_id="binance",
        market_type="usdt_m_perpetual",
        timeframe="5m",
        window_start_inclusive=START,
        window_end_exclusive=END,
        owner_row_count=2,
        owner_source_record_count=2,
        lineage=(_lineage(),),
        mark_opens=(100.0, 101.0),
        mark_closes=(100.5, 101.5),
    )


def _bar(
    timestamp: datetime,
    *,
    symbol: str = "BTCUSDT",
    timeframe: str = "5m",
) -> Candle:
    return Candle(
        timestamp=timestamp,
        open=100.0,
        high=102.0,
        low=99.0,
        close=101.0,
        symbol=symbol,
        timeframe=timeframe,
    )


def _position() -> Position:
    return Position(
        id="pos-1",
        entry_order_id="ord-1",
        side=PositionSide.LONG,
        quantity=1.0,
        entry_price=100.0,
        entry_time=START,
        entry_index=0,
        entry_fee=0.0,
    )


def _funding_source() -> RegisteredFundingSource:
    return RegisteredFundingSource(
        dataset_id="binance-btcusdt-funding",
        dataset_version=1,
        dataset_hash="c" * 64,
        symbol="BTCUSDT",
        coverage_start_inclusive=START,
        coverage_end_exclusive=END,
        event_count=1,
        source_record_count=1,
        lineage=(
            FundingLineageEntry(
                artifact_id="funding-owner",
                source_kind="official_rest",
                checksum_sha256="d" * 64,
                record_count=1,
                start=START,
                end=END,
            ),
        ),
        events=(
            FundingEvent(
                ordinal=0,
                timestamp=START,
                symbol="BTCUSDT",
                rate=Decimal("0.001"),
                mark_price=Decimal("100"),
            ),
        ),
    )


def _record(
    timestamp: datetime,
    *,
    price_type: object | None = None,
    symbol: str = "BTCUSDT",
    timeframe: str = "5m",
) -> object:
    DataType, PriceKlineRecord = _market_contracts()
    interval = timedelta(minutes=5 if timeframe == "5m" else 1)
    return PriceKlineRecord(
        price_type=DataType.MARK_PRICE if price_type is None else price_type,
        open_time=timestamp,
        close_time=timestamp + interval,
        open="100",
        high="102",
        low="99",
        close="101",
        symbol=symbol,
        timeframe=timeframe,
        exchange_id="binance",
        market_type="usdt_m_perpetual",
        source_kind="official_rest",
    )


def _selection() -> SimpleNamespace:
    DataType, _ = _market_contracts()
    artifact = SimpleNamespace(
        artifact_id="raw-mark",
        source=SimpleNamespace(value="official_rest"),
        checksum_sha256="e" * 64,
        record_count=2,
        start=START,
        end=END,
    )
    return SimpleNamespace(
        price_type=DataType.MARK_PRICE,
        timeframe="5m",
        manifest=SimpleNamespace(
            instrument=SimpleNamespace(symbol="BTCUSDT"),
            exchange=SimpleNamespace(id="binance"),
            market_type=SimpleNamespace(value="usdt_m_perpetual"),
        ),
        descriptor=SimpleNamespace(
            dataset_id="registered-mark",
            version=7,
            dataset_hash="f" * 64,
        ),
        source_artifacts=(artifact,),
        row_count=2,
        source_record_count=2,
    )


def test_price_lineage_rejects_invalid_identity_counts_and_time() -> None:
    cases = (
        ({"artifact_id": ""}, "artifact_id"),
        ({"source_kind": ""}, "source_kind"),
        ({"checksum_sha256": "A" * 64}, "checksum"),
        ({"record_count": 0}, "record_count"),
        ({"record_count": True}, "record_count"),
        ({"start": START.replace(tzinfo=None)}, "timezone-aware UTC"),
        ({"start": START.astimezone(timezone(timedelta(hours=1)))}, "UTC offset"),
        ({"end": START}, "non-empty"),
    )
    for kwargs, match in cases:
        with pytest.raises(MarkValuationError, match=match):
            replace(_lineage(), **kwargs)


def test_registered_mark_source_rejects_invalid_scalar_contracts() -> None:
    source = _source()
    cases = (
        ({"dataset_id": ""}, "dataset_id"),
        ({"dataset_version": 0}, "dataset_version"),
        ({"dataset_version": True}, "dataset_version"),
        ({"dataset_hash": "B" * 64}, "dataset_hash"),
        ({"symbol": ""}, "symbol"),
        ({"exchange_id": "other"}, "Binance USDT-M"),
        ({"market_type": "spot"}, "Binance USDT-M"),
        ({"timeframe": "unsupported"}, "timeframe"),
        ({"window_start_inclusive": START.replace(tzinfo=None)}, "timezone-aware UTC"),
        ({"window_end_exclusive": START}, "non-empty"),
        ({"schema_version": 2}, "unsupported registered"),
        ({"owner_row_count": 0}, "owner_row_count"),
        ({"owner_source_record_count": True}, "owner_source_record_count"),
        ({"owner_source_record_count": 3}, "normalized/source counts differ"),
    )
    for kwargs, match in cases:
        with pytest.raises(MarkValuationError, match=match):
            replace(source, **kwargs)


def test_registered_mark_source_rejects_lineage_and_price_evidence_drift() -> None:
    source = _source()

    with pytest.raises(MarkValuationError, match="immutable raw lineage"):
        replace(source, lineage=())
    with pytest.raises(MarkValuationError, match="PriceLineageEntry"):
        replace(source, lineage=(object(),))

    earlier = _lineage(artifact_id="a", record_count=1, start=START, end=START + STEP)
    later = _lineage(artifact_id="b", record_count=1, start=START + STEP, end=END)
    with pytest.raises(MarkValuationError, match="canonically ordered"):
        replace(source, lineage=(later, earlier))

    with pytest.raises(MarkValuationError, match="lineage counts"):
        replace(source, owner_row_count=3, owner_source_record_count=3)

    gap_first = _lineage(
        artifact_id="a",
        record_count=1,
        start=START,
        end=START + timedelta(minutes=1),
    )
    gap_second = _lineage(
        artifact_id="b",
        record_count=1,
        start=START + STEP,
        end=END,
    )
    with pytest.raises(MarkValuationError, match="uncovered interval"):
        replace(source, lineage=(gap_first, gap_second))

    late = _lineage(start=START + timedelta(minutes=1), end=END)
    with pytest.raises(MarkValuationError, match="does not cover"):
        replace(source, lineage=(late,))

    short = _lineage(end=END - timedelta(minutes=1))
    with pytest.raises(MarkValuationError, match="does not cover"):
        replace(source, lineage=(short,))

    overlap_first = _lineage(
        artifact_id="a",
        record_count=1,
        start=START,
        end=START + timedelta(minutes=7),
    )
    overlap_second = _lineage(
        artifact_id="b",
        record_count=1,
        start=START + STEP,
        end=END,
    )
    assert replace(source, lineage=(overlap_first, overlap_second)).row_count == 2

    with pytest.raises(MarkValuationError, match="equal non-empty"):
        replace(source, mark_opens=(), mark_closes=())
    with pytest.raises(MarkValuationError, match="equal non-empty"):
        replace(source, mark_opens=(100.0,), mark_closes=(100.0, 101.0))
    with pytest.raises(MarkValuationError, match="finite and positive"):
        replace(source, mark_opens=(0.0, 101.0))
    with pytest.raises(MarkValuationError, match="finite and positive"):
        replace(source, mark_closes=(math.inf, 101.0))

    extended_lineage = _lineage(end=END + STEP)
    with pytest.raises(MarkValuationError, match="exactly cover"):
        replace(source, window_end_exclusive=END + STEP, lineage=(extended_lineage,))


def test_mark_lookup_and_candle_matching_reject_every_boundary_drift() -> None:
    source = _source()

    with pytest.raises(MarkValuationError, match="total_bars"):
        source.assert_matches_candles(_bar(START), _bar(START + STEP), True)
    with pytest.raises(MarkValuationError, match="timeframe"):
        source.assert_matches_candles(
            _bar(START, timeframe="1m"),
            _bar(START + STEP, timeframe="1m"),
            2,
        )
    with pytest.raises(MarkValuationError, match="start"):
        source.assert_matches_candles(_bar(START + STEP), _bar(START + STEP), 2)
    with pytest.raises(MarkValuationError, match="processed candle timeframe"):
        source.mark_open(_bar(START, timeframe="1m"))
    with pytest.raises(MarkValuationError, match="precedes"):
        source.mark_close_at(START - STEP)
    with pytest.raises(MarkValuationError, match="aligned"):
        source.mark_close_at(START + timedelta(minutes=1))
    with pytest.raises(MarkValuationError, match="exceeds"):
        source.mark_close_at(END)

    object.__setattr__(source, "timeframe", "unsupported")
    with pytest.raises(MarkValuationError, match="unsupported"):
        _ = source.interval_seconds


def test_policy_identity_and_helper_validation_fail_closed() -> None:
    source = _source()

    assert normalize_valuation_policy(" MARK ") == VALUATION_POLICY_MARK
    with pytest.raises(MarkValuationError, match="must be a string"):
        normalize_valuation_policy(1)  # type: ignore[arg-type]
    with pytest.raises(MarkValuationError, match="last or mark"):
        normalize_valuation_policy("mid")
    with pytest.raises(MarkValuationError, match="base application run_id"):
        valuation_application_run_id("", VALUATION_POLICY_LAST, None)
    with pytest.raises(MarkValuationError, match="application_id"):
        valuation_runtime_evidence("", VALUATION_POLICY_MARK, source)

    with pytest.raises(MarkValuationError, match="record_count"):
        _required_record_count(None)
    assert _required_record_count(2) == 2
    with pytest.raises(MarkValuationError, match="numeric"):
        _finite_positive_float("bad", "mark")
    with pytest.raises(MarkValuationError, match="finite and positive"):
        _finite_positive_float(0, "mark")
    assert _finite_positive_float("1.25", "mark") == pytest.approx(1.25)
    with pytest.raises(MarkValuationError, match="timezone-aware UTC"):
        _utc_datetime(START.replace(tzinfo=None), "time")
    with pytest.raises(MarkValuationError, match="UTC offset"):
        _utc_datetime(START.astimezone(timezone(timedelta(hours=2))), "time")
    with pytest.raises(MarkValuationError, match="canonical JSON"):
        _canonical_json_bytes({"invalid": object()})


def test_mark_portfolio_and_factory_keep_reference_and_type_contracts() -> None:
    source = _source()
    portfolio = MarkAwarePortfolio(cash=1_000.0, position=_position())

    assert portfolio.unrealized_pnl(105.0) == pytest.approx(5.0)
    portfolio.set_reference_price(110.0)
    assert portfolio.unrealized_pnl(105.0) == pytest.approx(10.0)
    assert portfolio.equity(105.0) == pytest.approx(1_010.0)
    snapshot = portfolio.position_snapshot(105.0)
    assert snapshot is not None
    assert snapshot.unrealized_pnl == pytest.approx(10.0)

    with pytest.raises(MarkValuationError, match="finite and positive"):
        portfolio.set_reference_price(0.0)

    mark_factory = valuation_execution_factory(source)
    mark_broker = mark_factory(
        EngineConfig(initial_cash=10_000),
        Journal(run_id="mark-factory"),
        InstrumentSpec(symbol="BTCUSDT"),
    )
    assert isinstance(mark_broker, MarkAwareBrokerSimulator)

    funding_factory = valuation_execution_factory(source, _funding_source())
    funding_broker = funding_factory(
        EngineConfig(initial_cash=10_000),
        Journal(run_id="funding-mark-factory"),
        InstrumentSpec(symbol="BTCUSDT"),
    )
    assert isinstance(funding_broker, FundingMarkAwareBrokerSimulator)

    with pytest.raises(TypeError, match="RegisteredMarkPriceSource"):
        valuation_execution_factory(object())  # type: ignore[arg-type]

    mark_broker.portfolio = Portfolio(cash=10_000.0)
    with pytest.raises(MarkValuationError, match="lost its portfolio contract"):
        _ = mark_broker._mark_portfolio

    funding_broker.portfolio = Portfolio(cash=10_000.0)
    with pytest.raises(MarkValuationError, match="lost its portfolio contract"):
        _ = funding_broker._mark_portfolio


def test_registered_mark_adapter_accepts_exact_owner_window_and_rejects_drift() -> None:
    DataType, _ = _market_contracts()
    selection = _selection()
    records = (_record(START), _record(START + STEP))

    source = adapt_registered_mark_price_selection(
        selection,
        records,
        expected_symbol="BTCUSDT",
        start=START,
        end=END,
    )
    assert source.dataset_id == "registered-mark"
    assert source.dataset_version == 7
    assert source.row_count == 2

    with pytest.raises(MarkValuationError, match="not MARK_PRICE"):
        adapt_registered_mark_price_selection(
            SimpleNamespace(**{**vars(selection), "price_type": DataType.INDEX_PRICE}),
            records,
            expected_symbol="BTCUSDT",
            start=START,
            end=END,
        )

    wrong_manifest = _selection()
    wrong_manifest.manifest.instrument.symbol = "ETHUSDT"
    with pytest.raises(MarkValuationError, match="symbol differs"):
        adapt_registered_mark_price_selection(
            wrong_manifest,
            records,
            expected_symbol="BTCUSDT",
            start=START,
            end=END,
        )

    bad_count = _selection()
    bad_count.source_artifacts[0].record_count = None
    with pytest.raises(MarkValuationError, match="record_count"):
        adapt_registered_mark_price_selection(
            bad_count,
            records,
            expected_symbol="BTCUSDT",
            start=START,
            end=END,
        )

    with pytest.raises(MarkValuationError, match="non-mark record"):
        adapt_registered_mark_price_selection(
            selection,
            (_record(START, price_type=DataType.INDEX_PRICE), _record(START + STEP)),
            expected_symbol="BTCUSDT",
            start=START,
            end=END,
        )

    with pytest.raises(MarkValuationError, match="row symbol"):
        adapt_registered_mark_price_selection(
            selection,
            (_record(START, symbol="ETHUSDT"), _record(START + STEP)),
            expected_symbol="BTCUSDT",
            start=START,
            end=END,
        )

    with pytest.raises(MarkValuationError, match="row timeframe"):
        adapt_registered_mark_price_selection(
            selection,
            (_record(START, timeframe="1m"), _record(START + STEP)),
            expected_symbol="BTCUSDT",
            start=START,
            end=END,
        )

    with pytest.raises(MarkValuationError, match="exactly aligned"):
        adapt_registered_mark_price_selection(
            selection,
            (_record(START), _record(START + STEP + timedelta(minutes=1))),
            expected_symbol="BTCUSDT",
            start=START,
            end=END,
        )

    with pytest.raises(MarkValuationError, match="exact requested window"):
        adapt_registered_mark_price_selection(
            selection,
            (_record(START),),
            expected_symbol="BTCUSDT",
            start=START,
            end=END,
        )


def test_open_registered_mark_source_wraps_owner_errors_and_uses_owner_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    price_reader = pytest.importorskip("market_data_ingestion.price_dataset_reader")

    selection = _selection()
    records = (_record(START), _record(START + STEP))
    calls: dict[str, object] = {}

    def resolve(*args: object, **kwargs: object) -> object:
        calls["resolve_args"] = args
        calls["resolve_kwargs"] = kwargs
        return selection

    def iterate(*args: object, **kwargs: object):
        calls["iter_args"] = args
        calls["iter_kwargs"] = kwargs
        return iter(records)

    monkeypatch.setattr(price_reader, "resolve_registered_price_dataset", resolve)
    monkeypatch.setattr(price_reader, "iter_registered_prices", iterate)

    source = open_registered_mark_price_source(
        tmp_path,
        "registered-mark",
        7,
        symbol="BTCUSDT",
        timeframe="5m",
        start=START,
        end=END,
    )
    assert source.row_count == 2
    resolve_kwargs = calls["resolve_kwargs"]
    assert isinstance(resolve_kwargs, dict)
    assert resolve_kwargs["consumer"] == "modular_backtest"
    assert resolve_kwargs["deep"] is True

    def fail_resolve(*_args: object, **_kwargs: object) -> object:
        raise OSError("owner unavailable")

    monkeypatch.setattr(price_reader, "resolve_registered_price_dataset", fail_resolve)
    with pytest.raises(MarkValuationError, match="registered mark validation failed"):
        open_registered_mark_price_source(
            tmp_path,
            "registered-mark",
            7,
            symbol="BTCUSDT",
            timeframe="5m",
            start=START,
            end=END,
        )
