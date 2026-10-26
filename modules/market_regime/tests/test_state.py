from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from market_regime import MarketRegimeModule, MarketRegimeStateError

from conftest import candle, feature_trace, feed, view_for


def test_roundtrip_preserves_next_decision(small_kwargs, start) -> None:
    source = MarketRegimeModule(**small_kwargs)
    feed(source, [candle(start, i, 100 + i) for i in range(12)])
    state = source.snapshot_state()
    restored = MarketRegimeModule(**small_kwargs)
    restored.restore_state(state)
    next_bar = candle(start, 12, 112)
    assert source.on_bar(view_for(next_bar)) == restored.on_bar(view_for(next_bar))


def test_restart_at_multiple_cut_points(small_kwargs, start) -> None:
    bars = [candle(start, i, 100 + i * 0.25) for i in range(40)]
    expected = feature_trace(feed(MarketRegimeModule(**small_kwargs), bars))
    for cut in (1, 5, 11, 19, 31):
        before = MarketRegimeModule(**small_kwargs)
        prefix = feed(before, bars[:cut])
        after = MarketRegimeModule(**small_kwargs)
        after.restore_state(before.snapshot_state())
        suffix = feed(after, bars[cut:])
        assert feature_trace(prefix + suffix) == expected


def test_restore_is_atomic_on_failure(module: MarketRegimeModule, start) -> None:
    feed(module, [candle(start, i, 100 + i) for i in range(8)])
    before = dict(module.snapshot_state())
    corrupted = dict(before)
    corrupted["ema_fast"] = "NaN"
    with pytest.raises(MarketRegimeStateError):
        module.restore_state(corrupted)
    assert module.snapshot_state() == before


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda state: state.update(schema_version="999"), "unsupported state schema"),
        (lambda state: state.update(schema_version=[]), "unsupported state schema"),
        (lambda state: state.update(config_signature={}), "configuration signature"),
        (lambda state: state.update(extra="x"), "unknown keys"),
        (lambda state: state.pop("atr"), "missing keys"),
        (lambda state: state.update(stable_regime="nonsense"), "unknown enum"),
        (lambda state: state.update(processed_bars=-1), "invalid integer"),
        (lambda state: state.update(closes=["1"] * 100), "configured capacity"),
    ],
)
def test_corrupted_state_rejected(module: MarketRegimeModule, start, mutation, match: str) -> None:
    feed(module, [candle(start, i, 100 + i) for i in range(12)])
    payload = dict(module.snapshot_state())
    mutation(payload)
    with pytest.raises(MarketRegimeStateError, match=match):
        module.restore_state(payload)


def test_config_mismatch_rejected(small_kwargs, start) -> None:
    source = MarketRegimeModule(**small_kwargs)
    feed(source, [candle(start, i, 100 + i) for i in range(12)])
    other = MarketRegimeModule(**{**small_kwargs, "trend_spread_threshold": "0.2"})
    with pytest.raises(MarketRegimeStateError, match="configuration signature"):
        other.restore_state(source.snapshot_state())


def test_diagnostics_toggle_checkpoint_compatible(small_kwargs, start) -> None:
    verbose = MarketRegimeModule(**small_kwargs, publish_diagnostics=True)
    feed(verbose, [candle(start, i, 100 + i) for i in range(12)])
    quiet = MarketRegimeModule(**small_kwargs, publish_diagnostics=False)
    quiet.restore_state(verbose.snapshot_state())
    assert quiet.snapshot_state() == verbose.snapshot_state()


def test_real_v1_checkpoint_migrates(small_kwargs, start) -> None:
    path = Path(__file__).with_name("fixture_v1_real_checkpoint.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    restored = MarketRegimeModule(**small_kwargs, publish_diagnostics=False)
    restored.restore_state(payload)
    migrated = restored.snapshot_state()
    assert migrated["schema_version"] == "2"
    assert migrated["bound_base_timeframe"] == "1h"
    decision = restored.on_bar(view_for(candle(start, 12, 112)))
    assert decision.features


def test_legacy_signature_is_not_accepted_for_different_semantics(small_kwargs) -> None:
    payload = json.loads(Path(__file__).with_name("fixture_v1_real_checkpoint.json").read_text())
    target = MarketRegimeModule(**{**small_kwargs, "slow_ema_period": 5})
    with pytest.raises(MarketRegimeStateError, match="configuration signature"):
        target.restore_state(payload)


def test_fingerprint_symbol_tamper_rejected(module: MarketRegimeModule, start) -> None:
    feed(module, [candle(start, i, 100 + i) for i in range(12)])
    payload = dict(module.snapshot_state())
    payload["last_fingerprint"] = str(payload["last_fingerprint"]).replace("BTCUSDT", "ETHUSDT")
    with pytest.raises(MarketRegimeStateError, match="fingerprint symbol"):
        module.restore_state(payload)


def test_fingerprint_timeframe_tamper_rejected(module: MarketRegimeModule, start) -> None:
    feed(module, [candle(start, i, 100 + i) for i in range(12)])
    payload = dict(module.snapshot_state())
    payload["last_fingerprint"] = str(payload["last_fingerprint"]).replace("|1h|", "|4h|")
    with pytest.raises(MarketRegimeStateError, match="fingerprint timeframe"):
        module.restore_state(payload)


def test_fingerprint_timestamp_tamper_rejected(module: MarketRegimeModule, start) -> None:
    feed(module, [candle(start, i, 100 + i) for i in range(12)])
    payload = dict(module.snapshot_state())
    payload["last_open_time"] = start.isoformat()
    with pytest.raises(MarketRegimeStateError, match="fingerprint timestamp"):
        module.restore_state(payload)


def test_symbol_with_pipe_roundtrips(small_kwargs, start) -> None:
    module = MarketRegimeModule(**small_kwargs)
    bars = [candle(start, i, 100 + i, symbol="SYN|TH") for i in range(12)]
    feed(module, bars)
    restored = MarketRegimeModule(**small_kwargs)
    restored.restore_state(module.snapshot_state())
    assert restored.snapshot_state() == module.snapshot_state()


def test_checkpoint_bounded_after_long_stream(module: MarketRegimeModule, start) -> None:
    for i in range(10_000):
        module.on_bar(view_for(candle(start, i, 100 + i * 0.01)))
    payload = module.snapshot_state()
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    assert len(payload["closes"]) <= module.config.efficiency_period + 1
    assert len(payload["movements"]) <= module.config.efficiency_period
    assert len(payload["slow_emas"]) <= module.config.slope_lookback + 1
    assert len(payload["atr_pct_window"]) <= module.config.volatility_baseline_period
    assert len(encoded) < 16_384


def test_on_start_resets_reused_instance(small_kwargs, start) -> None:
    module = MarketRegimeModule(**small_kwargs)
    feed(module, [candle(start, i, 100 + i) for i in range(12)])
    module.on_start()
    assert module.snapshot_state()["processed_bars"] == 0
    assert module.snapshot_state()["bound_symbol"] is None


def test_on_finish_does_not_destroy_checkpoint(module: MarketRegimeModule, start) -> None:
    feed(module, [candle(start, i, 100 + i) for i in range(12)])
    before = copy.deepcopy(module.snapshot_state())
    module.on_finish()
    assert module.snapshot_state() == before
