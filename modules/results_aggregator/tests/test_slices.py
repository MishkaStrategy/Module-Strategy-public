from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from modular_results_registry.errors import ValidationError
from modular_results_registry.slices import (
    SliceAccumulator,
    SliceAvailability,
    SliceDimension,
    TradeFact,
    build_slice,
)

UTC = timezone.utc


def _trade(
    trade_id: str,
    *,
    run_id: str = "run-001",
    instrument: str = "BTCUSDT",
    side: str = "LONG",
    exit_time: datetime = datetime(2026, 1, 15, tzinfo=UTC),
    net_pnl: str = "10",
    fees: str = "1",
    r_multiple: str | None = "2",
    module: str | None = "fvg",
    setup: str | None = "retest",
    regime: str | None = "trend",
    exit_reason: str | None = "target",
) -> TradeFact:
    return TradeFact(
        trade_id=trade_id,
        run_id=run_id,
        instrument=instrument,
        side=side,
        entry_time=datetime(2026, 1, 14, tzinfo=UTC),
        exit_time=exit_time,
        net_pnl=net_pnl,
        fees=fees,
        r_multiple=r_multiple,
        module=module,
        setup=setup,
        regime=regime,
        exit_reason=exit_reason,
    )


def test_streaming_slice_by_instrument_and_month() -> None:
    trades = (
        _trade("trade-001", net_pnl="10", r_multiple="2"),
        _trade("trade-002", net_pnl="-4", r_multiple="-1", side="SHORT"),
        _trade(
            "trade-003",
            instrument="ETHUSDT",
            exit_time=datetime(2026, 2, 1, tzinfo=UTC),
            net_pnl="0",
            r_multiple=None,
        ),
    )
    instrument = build_slice(trades, SliceDimension.INSTRUMENT)
    assert instrument.availability is SliceAvailability.AVAILABLE
    by_key = {item.key: item for item in instrument.summaries}
    assert by_key["BTCUSDT"].trade_count == 2
    assert by_key["BTCUSDT"].winning_trades == 1
    assert by_key["BTCUSDT"].losing_trades == 1
    assert by_key["BTCUSDT"].net_pnl == Decimal("6")
    assert by_key["BTCUSDT"].average_r_multiple == Decimal("0.5")
    month = build_slice(trades, SliceDimension.PERIOD_MONTH)
    assert tuple(item.key for item in month.summaries) == ("2026-01", "2026-02")


def test_missing_verified_labels_are_not_coerced_to_unknown() -> None:
    result = build_slice(
        (
            _trade("trade-001", regime="trend"),
            _trade("trade-002", regime=None),
        ),
        SliceDimension.REGIME,
    )
    assert result.availability is SliceAvailability.PARTIAL
    assert result.included_trades == 1
    assert result.missing_label_trades == 1
    assert tuple(item.key for item in result.summaries) == ("trend",)
    unavailable = build_slice((_trade("trade-003", regime=None),), SliceDimension.REGIME)
    assert unavailable.availability is SliceAvailability.UNAVAILABLE
    assert unavailable.summaries == ()


def test_incremental_snapshot_matches_full_rebuild() -> None:
    trades = tuple(_trade(f"trade-{index:03d}", net_pnl=str(index - 2)) for index in range(5))
    full = build_slice(trades, SliceDimension.SIDE)
    accumulator = SliceAccumulator(SliceDimension.SIDE)
    accumulator.extend(trades[:2])
    restored = SliceAccumulator.restore(accumulator.snapshot())
    restored.extend(trades[2:])
    assert restored.result() == full
    assert restored.result().content_hash == full.content_hash


def test_trade_validation_and_snapshot_corruption_are_fail_closed() -> None:
    with pytest.raises(ValidationError):
        _trade("trade-bad", side="BOTH")
    with pytest.raises(ValidationError):
        TradeFact(
            trade_id="trade-bad",
            run_id="run-001",
            instrument="BTCUSDT",
            side="LONG",
            entry_time=datetime(2026, 1, 2, tzinfo=UTC),
            exit_time=datetime(2026, 1, 1, tzinfo=UTC),
            net_pnl="1",
        )
    with pytest.raises(ValidationError):
        SliceAccumulator.restore({"schema_version": "wrong"})
    with pytest.raises(ValidationError):
        SliceAccumulator.restore(
            {
                "schema_version": "slice-accumulator.v1",
                "dimension": "SIDE",
                "total": 2,
                "missing": 0,
                "buckets": {},
            }
        )


def test_empty_slice_is_available_but_explicitly_limited() -> None:
    result = build_slice((), SliceDimension.SIDE)
    assert result.availability is SliceAvailability.AVAILABLE
    assert result.limitations == ("NO_TRADES",)
    assert result.total_input_trades == 0
