from __future__ import annotations

import pytest
from modular_core import capture_module_states, restore_module_states

from market_regime import MarketRegimeModule, MarketRegimeStateError

from conftest import candle, feed

FROZEN_SEQUENCE_FIELDS = (
    "tr_seed",
    "closes",
    "movements",
    "slow_emas",
    "atr_pct_window",
)


@pytest.mark.parametrize("bars_count", (2, 12))
def test_core_frozen_checkpoint_payload_restores(
    small_kwargs: dict[str, object], start, bars_count: int
) -> None:
    source = MarketRegimeModule(**small_kwargs)
    feed(source, [candle(start, i, 100 + i * 0.25) for i in range(bars_count)])

    states = capture_module_states([source])

    assert len(states) == 1
    for field in FROZEN_SEQUENCE_FIELDS:
        assert isinstance(states[0].payload[field], tuple)
    if bars_count == 2:
        assert states[0].payload["tr_seed"]
    else:
        assert states[0].payload["atr_pct_window"]

    restored = MarketRegimeModule(**small_kwargs)
    restore_module_states([restored], states)

    assert restored.snapshot_state() == source.snapshot_state()


@pytest.mark.parametrize("field", FROZEN_SEQUENCE_FIELDS)
@pytest.mark.parametrize("invalid", ({}, "values", {"1"}, 1, None))
def test_restore_sequence_fields_still_reject_non_list_tuple_containers(
    small_kwargs: dict[str, object], start, field: str, invalid: object
) -> None:
    source = MarketRegimeModule(**small_kwargs)
    feed(source, [candle(start, i, 100 + i * 0.25) for i in range(12)])
    payload = dict(source.snapshot_state())
    payload[field] = invalid

    with pytest.raises(
        MarketRegimeStateError,
        match=rf"state field must be a list or tuple: {field}",
    ):
        MarketRegimeModule(**small_kwargs).restore_state(payload)


def test_core_frozen_tuple_still_enforces_capacity(small_kwargs: dict[str, object], start) -> None:
    source = MarketRegimeModule(**small_kwargs)
    feed(source, [candle(start, i, 100 + i * 0.25) for i in range(12)])
    payload = dict(source.snapshot_state())
    payload["closes"] = tuple("1" for _ in range(100))

    with pytest.raises(MarketRegimeStateError, match="configured capacity"):
        MarketRegimeModule(**small_kwargs).restore_state(payload)
