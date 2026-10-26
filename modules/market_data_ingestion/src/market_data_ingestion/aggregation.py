from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal

from .records import KlineRecord
from .timeframes import floor_timeframe, timeframe_delta, timeframe_seconds


def aggregate_klines(
    records: Iterable[KlineRecord],
    *,
    target_timeframe: str,
) -> tuple[KlineRecord, ...]:
    """Aggregate complete UTC-aligned groups without filling missing candles."""

    source = tuple(sorted(records, key=lambda item: item.open_time))
    if not source:
        return ()
    base_timeframe = source[0].timeframe
    base_seconds = timeframe_seconds(base_timeframe)
    target_seconds = timeframe_seconds(target_timeframe)
    if target_seconds <= base_seconds or target_seconds % base_seconds:
        raise ValueError("target timeframe must be a larger integer multiple of base timeframe")
    identity = {
        (item.exchange_id, item.market_type, item.symbol, item.timeframe) for item in source
    }
    if len(identity) != 1:
        raise ValueError("aggregation input must contain one instrument and base timeframe")
    expected_count = target_seconds // base_seconds
    groups: dict[datetime, list[KlineRecord]] = {}
    for record in source:
        boundary = floor_timeframe(record.open_time, target_timeframe)
        groups.setdefault(boundary, []).append(record)

    result: list[KlineRecord] = []
    base_step = timeframe_delta(base_timeframe)
    target_step = timeframe_delta(target_timeframe)
    for boundary in sorted(groups):
        group = sorted(groups[boundary], key=lambda item: item.open_time)
        expected_times = [boundary + index * base_step for index in range(expected_count)]
        if len(group) != expected_count or [item.open_time for item in group] != expected_times:
            continue
        result.append(
            KlineRecord(
                open_time=boundary,
                close_time=boundary + target_step - timedelta(milliseconds=1),
                open=group[0].open,
                high=format(max(Decimal(item.high) for item in group), "f"),
                low=format(min(Decimal(item.low) for item in group), "f"),
                close=group[-1].close,
                volume=format(sum((Decimal(item.volume) for item in group), Decimal(0)), "f"),
                quote_volume=format(
                    sum((Decimal(item.quote_volume) for item in group), Decimal(0)), "f"
                ),
                trade_count=sum(item.trade_count for item in group),
                taker_buy_base_volume=format(
                    sum((Decimal(item.taker_buy_base_volume) for item in group), Decimal(0)),
                    "f",
                ),
                taker_buy_quote_volume=format(
                    sum((Decimal(item.taker_buy_quote_volume) for item in group), Decimal(0)),
                    "f",
                ),
                symbol=group[0].symbol,
                timeframe=target_timeframe,
                exchange_id=group[0].exchange_id,
                market_type=group[0].market_type,
                source_kind="derived",
                is_closed=True,
            )
        )
    return tuple(result)


def aggregate_klines_stream(
    records: Iterable[KlineRecord],
    *,
    target_timeframe: str,
) -> Iterable[KlineRecord]:
    """Yield complete UTC-aligned aggregate groups from a sorted base stream."""

    iterator = iter(records)
    try:
        first = next(iterator)
    except StopIteration:
        return
    base_timeframe = first.timeframe
    base_seconds = timeframe_seconds(base_timeframe)
    target_seconds = timeframe_seconds(target_timeframe)
    if target_seconds <= base_seconds or target_seconds % base_seconds:
        raise ValueError("target timeframe must be a larger integer multiple of base timeframe")
    expected_count = target_seconds // base_seconds
    base_step = timeframe_delta(base_timeframe)
    target_step = timeframe_delta(target_timeframe)
    identity = (
        first.exchange_id,
        first.market_type,
        first.symbol,
        first.timeframe,
    )
    current_boundary = floor_timeframe(first.open_time, target_timeframe)
    group: list[KlineRecord] = [first]
    previous_time = first.open_time

    def emit(
        boundary: datetime,
        items: list[KlineRecord],
    ) -> KlineRecord | None:
        expected_times = [boundary + index * base_step for index in range(expected_count)]
        if len(items) != expected_count:
            return None
        if [item.open_time for item in items] != expected_times:
            return None
        return KlineRecord(
            open_time=boundary,
            close_time=boundary + target_step - timedelta(milliseconds=1),
            open=items[0].open,
            high=format(max(Decimal(item.high) for item in items), "f"),
            low=format(min(Decimal(item.low) for item in items), "f"),
            close=items[-1].close,
            volume=format(sum((Decimal(item.volume) for item in items), Decimal(0)), "f"),
            quote_volume=format(
                sum(
                    (Decimal(item.quote_volume) for item in items),
                    Decimal(0),
                ),
                "f",
            ),
            trade_count=sum(item.trade_count for item in items),
            taker_buy_base_volume=format(
                sum(
                    (Decimal(item.taker_buy_base_volume) for item in items),
                    Decimal(0),
                ),
                "f",
            ),
            taker_buy_quote_volume=format(
                sum(
                    (Decimal(item.taker_buy_quote_volume) for item in items),
                    Decimal(0),
                ),
                "f",
            ),
            symbol=items[0].symbol,
            timeframe=target_timeframe,
            exchange_id=items[0].exchange_id,
            market_type=items[0].market_type,
            source_kind="derived",
            is_closed=True,
        )

    for record in iterator:
        record_identity = (
            record.exchange_id,
            record.market_type,
            record.symbol,
            record.timeframe,
        )
        if record_identity != identity:
            raise ValueError("aggregation input must contain one instrument and base timeframe")
        if record.open_time < previous_time:
            raise ValueError("aggregation input must be sorted by open_time")
        boundary = floor_timeframe(record.open_time, target_timeframe)
        if boundary != current_boundary:
            aggregated = emit(current_boundary, group)
            if aggregated is not None:
                yield aggregated
            current_boundary = boundary
            group = []
        group.append(record)
        previous_time = record.open_time

    aggregated = emit(current_boundary, group)
    if aggregated is not None:
        yield aggregated
