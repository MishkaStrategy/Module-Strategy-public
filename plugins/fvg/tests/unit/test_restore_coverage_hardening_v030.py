from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

import pytest

from conftest import bullish_triplet, candle, view_for
from modular_strategy_fvg import (
    FvgLifecycleStatus,
    FvgModule,
    FvgStateError,
    evaluate_pre_fvg,
    initial_lifecycle,
    observe_lifecycle,
    terminal_lifecycle,
)


def _active_module(*, symbol: str = "BTCUSDT", **kwargs: object) -> FvgModule:
    module = FvgModule(timeframes=("15m",), **kwargs)
    module.on_bar(view_for(bullish_triplet(symbol=symbol), symbol=symbol))
    return module


def _candidate_module() -> FvgModule:
    module = FvgModule(
        timeframes=("15m",),
        pre_fvg_enabled=True,
        pre_fvg_evaluation_offset="5m",
    )
    a, b, _ = bullish_triplet()
    result = evaluate_pre_fvg(
        a=a,
        b=b,
        lower_bars=(
            candle(6, timeframe="5m", open_=106, high=110, low=105, close=107),
            candle(7, timeframe="5m", open_=107, high=112, low=106, close=111),
        ),
        observed_at=a.timestamp + timedelta(minutes=40),
        config=module.config,
    )
    candidate = result.candidate
    assert candidate is not None
    key = (candidate.symbol, candidate.timeframe)
    module._pre_candidates[candidate.candidate_id] = candidate
    module._pre_evaluated[key] = candidate.source_c_open_time
    module._last_processed[key] = candidate.source_c_open_time
    module._last_fingerprints[key] = None
    module._history_fingerprints[key] = ()
    module._bound_symbol = candidate.symbol
    return module


def _restore(state: object, **kwargs: object) -> None:
    FvgModule(timeframes=("15m",), **kwargs).restore_state(state)  # type: ignore[arg-type]


def test_snapshot_and_restore_size_guards() -> None:
    module = FvgModule(timeframes=("15m",), max_state_bytes=1024)
    module._state_payload = lambda: {"padding": "x" * 2000}  # type: ignore[method-assign]
    with pytest.raises(FvgStateError, match="exceeds"):
        module.snapshot_state()

    with pytest.raises(FvgStateError, match="must be a mapping"):
        _restore([])

    target = FvgModule(timeframes=("15m",))
    target._state_payload_from = lambda **_: {"padding": "x" * 9_000_000}  # type: ignore[method-assign]
    with pytest.raises(FvgStateError, match="migrated FVG state"):
        target.restore_state(FvgModule(timeframes=("15m",)).snapshot_state())


def test_restore_rejects_top_level_and_cursor_types() -> None:
    state = deepcopy(_active_module().snapshot_state())
    for key in ("module_version", "config_fingerprint"):
        mutated = deepcopy(state)
        mutated[key] = 1
        with pytest.raises(FvgStateError, match="canonical strings"):
            _restore(mutated)

    mutated = deepcopy(state)
    mutated["last_processed"][0]["symbol"] = 1
    with pytest.raises(FvgStateError, match="canonical strings"):
        _restore(mutated)

    mutated = deepcopy(state)
    mutated["last_processed"][0] = 1
    with pytest.raises(FvgStateError, match="row must be a mapping"):
        _restore(mutated)


def test_restore_rejects_history_fingerprint_corruption() -> None:
    state = deepcopy(_active_module().snapshot_state())
    row = state["last_processed"][0]

    mutations = []
    bad = deepcopy(state)
    bad["last_processed"][0]["history_fingerprints"] = "bad"
    mutations.append((bad, "must be a sequence"))

    bad = deepcopy(state)
    bad["last_processed"][0]["history_fingerprints"] = [1]
    mutations.append((bad, "row is malformed"))

    bad = deepcopy(state)
    bad["last_processed"][0]["history_fingerprints"] = [{"close_time": row["close_time"]}]
    mutations.append((bad, "row is malformed"))

    bad = deepcopy(state)
    bad["last_processed"][0]["history_fingerprints"][-1]["bar_fingerprint"] = "bad"
    mutations.append((bad, "bar_fingerprint is invalid"))

    bad = deepcopy(state)
    history = bad["last_processed"][0]["history_fingerprints"]
    history.insert(0, deepcopy(history[-1]))
    mutations.append((bad, "exceeds the retained window"))

    bad = deepcopy(state)
    bad["last_processed"][0]["history_fingerprints"][-1]["close_time"] = (
        bullish_triplet()[-1].close_time + timedelta(minutes=15)
    ).isoformat()
    mutations.append((bad, "cannot exceed"))

    bad = deepcopy(state)
    bad["last_processed"][0]["bar_fingerprint"] = None
    mutations.append((bad, "require a current"))

    bad = deepcopy(state)
    bad["last_processed"][0]["history_fingerprints"] = []
    mutations.append((bad, "must match history"))

    for payload, message in mutations:
        with pytest.raises(FvgStateError, match=message):
            _restore(payload)


def test_restore_rejects_zone_capacity_and_multi_symbol() -> None:
    state = deepcopy(_active_module().snapshot_state())
    from conftest import bearish_triplet

    bearish = FvgModule(timeframes=("15m",))
    bearish.on_bar(view_for(bearish_triplet()))
    second = deepcopy(bearish.snapshot_state())
    state["zones"].append(second["zones"][0])
    state["zone_lifecycle"].append(second["zone_lifecycle"][0])
    with pytest.raises(FvgStateError, match="capacity"):
        target = FvgModule(timeframes=("15m",), max_active_zones_per_timeframe=1)
        state["config_fingerprint"] = target._config_fingerprint
        target.restore_state(state)

    btc = deepcopy(_active_module(symbol="BTCUSDT").snapshot_state())
    eth = deepcopy(_active_module(symbol="ETHUSDT").snapshot_state())
    btc["last_processed"].extend(eth["last_processed"])
    btc["zones"].extend(eth["zones"])
    btc["zone_lifecycle"].extend(eth["zone_lifecycle"])
    with pytest.raises(FvgStateError, match="too many cursor rows"):
        _restore(btc)

    malformed = deepcopy(_active_module().snapshot_state())
    malformed["zones"][0] = 1
    with pytest.raises(FvgStateError, match="zone state row"):
        _restore(malformed)


def test_restore_rejects_lifecycle_corruption() -> None:
    state = deepcopy(_active_module().snapshot_state())

    malformed = deepcopy(state)
    malformed["zone_lifecycle"] = "bad"
    with pytest.raises(FvgStateError, match="must be lists"):
        _restore(malformed)

    malformed = deepcopy(state)
    malformed["zone_lifecycle"] = []
    with pytest.raises(FvgStateError, match="exactly cover"):
        _restore(malformed)

    malformed = deepcopy(state)
    malformed["zone_lifecycle"][0] = 1
    with pytest.raises(FvgStateError, match="row must be a mapping"):
        _restore(malformed)

    malformed = deepcopy(state)
    malformed["zone_lifecycle"][0]["status"] = "unknown"
    with pytest.raises(FvgStateError, match="invalid zone lifecycle"):
        _restore(malformed)

    zone = _active_module().active_zones[0]
    terminal = terminal_lifecycle(
        initial_lifecycle(zone),
        FvgLifecycleStatus.EXPIRED,
        zone.created_at,
    )
    malformed = deepcopy(state)
    malformed["zone_lifecycle"][0] = dict(terminal.to_state())
    with pytest.raises(FvgStateError, match="terminal lifecycle"):
        _restore(malformed)

    partial = observe_lifecycle(
        zone,
        initial_lifecycle(zone),
        candle(3, open_=107, high=110, low=102, close=108),
    )
    malformed = deepcopy(state)
    malformed["zone_lifecycle"][0] = dict(partial.to_state())
    with pytest.raises(FvgStateError, match="causally covered"):
        _restore(malformed)

    malformed = deepcopy(state)
    malformed["zone_lifecycle"][0]["detected_at"] = (zone.created_at - timedelta(minutes=15)).isoformat()
    malformed["zone_lifecycle"][0]["last_transition_at"] = malformed["zone_lifecycle"][0]["detected_at"]
    malformed["zone_lifecycle"][0]["last_observed_at"] = malformed["zone_lifecycle"][0]["detected_at"]
    with pytest.raises(FvgStateError, match="detection time"):
        _restore(malformed)


def test_restore_valid_candidate_and_rejects_candidate_corruption() -> None:
    source = _candidate_module()
    state = deepcopy(source.snapshot_state())
    restored = FvgModule(
        timeframes=("15m",),
        pre_fvg_enabled=True,
        pre_fvg_evaluation_offset="5m",
    )
    restored.restore_state(state)
    assert restored.pre_candidates == source.pre_candidates

    malformed = deepcopy(state)
    malformed["pre_candidates"] = [1]
    with pytest.raises(FvgStateError, match="row must be a mapping"):
        restored.restore_state(malformed)

    malformed = deepcopy(state)
    malformed["pre_candidates"][0]["candidate_id"] = 1
    with pytest.raises(FvgStateError, match="invalid pre-FVG candidate"):
        restored.restore_state(malformed)

    malformed = deepcopy(state)
    malformed["pre_candidates"].append(deepcopy(malformed["pre_candidates"][0]))
    with pytest.raises(FvgStateError, match="too many unresolved"):
        restored.restore_state(malformed)

    malformed = deepcopy(state)
    malformed["pre_candidates"][0]["candidate_id"] = "pre-fvg:not-canonical"
    with pytest.raises(FvgStateError, match="not canonical"):
        restored.restore_state(malformed)

    malformed = deepcopy(state)
    malformed["pre_candidates"][0]["processed_at"] = malformed["pre_candidates"][0]["source_c_expected_close_time"]
    with pytest.raises(FvgStateError, match="invalid pre-FVG candidate"):
        restored.restore_state(malformed)

    malformed = deepcopy(state)
    malformed["pre_candidates"][0]["metrics"]["distance_from_reference"] = None
    with pytest.raises(FvgStateError, match="distance_from_reference"):
        restored.restore_state(malformed)


def test_restore_rejects_pre_evaluated_corruption() -> None:
    source = _candidate_module()
    state = deepcopy(source.snapshot_state())
    target = FvgModule(
        timeframes=("15m",),
        pre_fvg_enabled=True,
        pre_fvg_evaluation_offset="5m",
    )

    mutations = []
    bad = deepcopy(state)
    bad["pre_evaluated"] = [1]
    mutations.append((bad, "row must be a mapping"))
    bad = deepcopy(state)
    bad["pre_evaluated"][0]["extra"] = 1
    mutations.append((bad, "malformed"))
    bad = deepcopy(state)
    bad["pre_evaluated"][0]["symbol"] = 1
    mutations.append((bad, "canonical strings"))
    bad = deepcopy(state)
    bad["pre_evaluated"].append(deepcopy(bad["pre_evaluated"][0]))
    mutations.append((bad, "too many timeframe"))
    bad = deepcopy(state)
    bad["pre_evaluated"][0]["source_c_open_time"] = (
        source.pre_candidates[0].source_c_open_time + timedelta(minutes=1)
    ).isoformat()
    mutations.append((bad, "not timeframe-aligned"))
    bad = deepcopy(state)
    bad["last_processed"][0]["close_time"] = (
        source.pre_candidates[0].source_c_open_time + timedelta(minutes=15)
    ).isoformat()
    bad["last_processed"][0]["bar_fingerprint"] = None
    bad["last_processed"][0]["history_fingerprints"] = []
    mutations.append((bad, "inconsistent with cursor"))
    bad = deepcopy(state)
    bad["pre_evaluated"] = []
    mutations.append((bad, "not covered"))

    for payload, message in mutations:
        with pytest.raises(FvgStateError, match=message):
            target.restore_state(payload)
