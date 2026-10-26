from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from market_data_ingestion.contracts import InstrumentReference
from market_data_ingestion.enums import MarketType
from market_data_ingestion.records import KlineRecord

_SLOW_FSYNC_RECOVERY_NODEID = (
    "tests/test_application_repair_execution.py::"
    "test_repair_job_recovers_after_crash_after_backend_published_version"
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Give the real fsync crash-recovery proof a finite slow-I/O allowance."""
    for item in items:
        if item.nodeid == _SLOW_FSYNC_RECOVERY_NODEID:
            item.add_marker(pytest.mark.timeout(120, method="thread"))


@pytest.fixture
def instrument() -> InstrumentReference:
    return InstrumentReference(
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        canonical_id="fake:usdt_m_perpetual:btcusdt",
    )


@pytest.fixture
def five_minute_records(instrument: InstrumentReference) -> tuple[KlineRecord, ...]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    output = []
    for index in range(12):
        price = 100 + index
        open_time = start + timedelta(minutes=5 * index)
        output.append(
            KlineRecord(
                open_time=open_time,
                close_time=open_time + timedelta(minutes=5) - timedelta(milliseconds=1),
                open=str(price),
                high=str(price + 2),
                low=str(price - 1),
                close=str(price + 1),
                volume="10",
                quote_volume="1000",
                trade_count=2,
                taker_buy_base_volume="4",
                taker_buy_quote_volume="400",
                symbol=instrument.symbol,
                timeframe="5m",
                exchange_id=instrument.exchange_id,
                market_type=instrument.market_type.value,
                source_kind="official_rest",
            )
        )
    return tuple(output)
