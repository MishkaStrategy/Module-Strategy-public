from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from hypothesis import given, strategies as st
from modular_core import Side

from signal_bot.domain import TradeSignal


@given(
    symbol=st.from_regex(r"[A-Z]{2,10}USDT", fullmatch=True),
    units=st.integers(min_value=1, max_value=10_000_000),
    setup_id=st.text(alphabet=st.characters(categories=("L", "N")), min_size=1, max_size=20),
)
def test_signal_id_is_deterministic_for_canonical_inputs(
    symbol: str, units: int, setup_id: str
) -> None:
    arguments = dict(
        strategy_id="property",
        strategy_version="1.0.0",
        symbol=symbol,
        side=Side.BUY,
        timeframe="5m",
        timestamp=datetime(2026, 7, 22, tzinfo=timezone.utc),
        entry=Decimal(units) / Decimal("100"),
        setup_id=setup_id,
    )
    assert TradeSignal.stable_id(**arguments) == TradeSignal.stable_id(**arguments)
