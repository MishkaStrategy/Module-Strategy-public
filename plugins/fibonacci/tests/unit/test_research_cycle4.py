from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from datetime import timedelta

import pytest

from modular_strategy_fibonacci.config import FibonacciConfig
from modular_strategy_fibonacci.research import (
    history_only_walk_forward,
    scan_bearish_reactions,
    scan_payload,
)
from tests.corrections.test_short_setup_cycle4 import (
    _qualified_setup_snapshot,
    _snapshot,
)


def _permissive_config() -> FibonacciConfig:
    return FibonacciConfig(
        short_reaction_minimum_samples=1,
        short_reaction_minimum_success_rate=Decimal("0"),
        short_reaction_minimum_streak=1,
        short_regime_window_size=4,
        short_regime_minimum_window_samples=1,
        short_regime_minimum_success_rate=Decimal("0"),
        short_regime_minimum_level_samples=1,
        short_regime_minimum_level_success_rate=Decimal("0"),
        short_regime_change_threshold=Decimal("0.15"),
    )


def test_scanner_sorts_symbol_timeframe_and_cutoff() -> None:
    btc = _qualified_setup_snapshot()
    eth = replace(btc, symbol="ETHUSDT")
    rows = scan_bearish_reactions((eth, btc), config=_permissive_config())
    assert [row.symbol for row in rows] == ["BTCUSDT", "ETHUSDT"]


def test_scanner_reports_level_specific_evidence() -> None:
    row = scan_bearish_reactions(
        (_qualified_setup_snapshot(),),
        config=_permissive_config(),
    )[0]
    assert {item[0] for item in row.recent_level_rates} == {"0.5", "0.618"}
    assert row.historical_sample_size == 12
    assert row.active_setup_id is not None


def test_scanner_does_not_create_orders_or_trades() -> None:
    payload = scan_payload(
        scan_bearish_reactions(
            (_qualified_setup_snapshot(),),
            config=_permissive_config(),
        )
    )[0]
    assert payload["orders"] == ()
    assert payload["trades"] == ()
    assert payload["schema_version"] == "fibonacci.bearish_reaction_scan/v2"


def test_history_only_walk_forward_uses_each_frozen_cutoff() -> None:
    first = _snapshot([True] * 4, active=False)
    second = replace(
        _qualified_setup_snapshot(),
        cutoff_time=first.cutoff_time + timedelta(days=1),
    )
    summary = history_only_walk_forward(
        (second, first),
        config=_permissive_config(),
    )[0]
    assert summary.evaluated_cutoffs == 2
    assert summary.selected_cutoffs == 1
    assert len(summary.selected_setup_ids) == 1


def test_walk_forward_groups_symbols_independently() -> None:
    btc = _qualified_setup_snapshot()
    eth = replace(btc, symbol="ETHUSDT")
    summaries = history_only_walk_forward((eth, btc), config=_permissive_config())
    assert [(row.symbol, row.evaluated_cutoffs) for row in summaries] == [
        ("BTCUSDT", 1),
        ("ETHUSDT", 1),
    ]


def test_walk_forward_rejects_duplicate_cutoffs() -> None:
    snapshot = _qualified_setup_snapshot()
    with pytest.raises(ValueError, match="unique"):
        history_only_walk_forward(
            (snapshot, snapshot),
            config=_permissive_config(),
        )


def test_default_config_can_produce_negative_scan_result() -> None:
    row = scan_bearish_reactions((_snapshot([], active=False),))[0]
    assert row.active_setup_id is None
    assert row.regime == "insufficient_data"


def test_empty_scanner_and_walk_forward_are_deterministic() -> None:
    assert scan_bearish_reactions(()) == ()
    assert history_only_walk_forward(()) == ()
    assert scan_payload(()) == ()
