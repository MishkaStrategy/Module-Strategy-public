from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from conftest import bullish_triplet, candle
from modular_strategy_fvg import FvgConfig, FvgModule, FvgStateError
from modular_strategy_fvg.detector import detect_zone
from modular_strategy_fvg.lifecycle import initial_lifecycle, observe_lifecycle
from modular_strategy_fvg.models import (
    FvgFilterDecision,
    FvgFilterTrace,
    FvgLifecycleStatus,
)
from modular_strategy_fvg.pre_fvg import evaluate_pre_fvg


def _config(**overrides: object) -> FvgConfig:
    parameters: dict[str, object] = {
        "timeframes": ("15m",),
        "pre_fvg_enabled": True,
        "pre_fvg_evaluation_offset": "5m",
    }
    parameters.update(overrides)
    return FvgConfig.from_parameters(**parameters)


def _lower_rows() -> tuple[object, object]:
    return (
        candle(6, timeframe="5m", open_=106, high=110, low=105, close=107),
        candle(7, timeframe="5m", open_=107, high=112, low=106, close=111),
    )


def _candidate(config: FvgConfig):
    a, b, _ = bullish_triplet()
    result = evaluate_pre_fvg(
        a=a,
        b=b,
        lower_bars=_lower_rows(),
        observed_at=a.timestamp + timedelta(minutes=40),
        config=config,
    )
    assert result.candidate is not None
    return result.candidate


def _zone():
    a, b, c = bullish_triplet()
    zone = detect_zone(a, b, c, FvgConfig(timeframes=("15m",)))
    assert zone is not None
    return zone


def _corrupt(candidate, **changes: object):
    for name, value in changes.items():
        object.__setattr__(candidate, name, value)
    return candidate


def test_state_payload_and_fast_size_match_canonical_json() -> None:
    module = FvgModule(
        timeframes=("15m",),
        pre_fvg_enabled=True,
        pre_fvg_evaluation_offset="5m",
    )
    zone = _zone()
    active = initial_lifecycle(zone)
    partial = observe_lifecycle(
        zone,
        active,
        candle(3, open_=107, high=110, low=102, close=108),
    )
    assert partial.status is FvgLifecycleStatus.PARTIALLY_FILLED
    candidate = _candidate(module.config)
    key = (zone.symbol, zone.timeframe)
    fingerprint = "f" * 64
    history_fingerprint = "h" * 64

    module._zones[zone.zone_id] = zone
    module._zone_lifecycle[zone.zone_id] = partial
    module._pre_candidates[candidate.candidate_id] = candidate
    module._pre_evaluated[key] = candidate.source_c_open_time
    module._last_processed[key] = zone.created_at
    module._last_fingerprints[key] = fingerprint
    module._history_fingerprints[key] = ((zone.created_at, history_fingerprint),)
    module._barriers[key] = zone.created_at
    module._bound_symbol = zone.symbol

    payload = module._state_payload()
    assert payload["bound_symbol"] == zone.symbol
    assert len(payload["zones"]) == 1
    assert len(payload["zone_lifecycle"]) == 1
    assert len(payload["pre_candidates"]) == 1
    assert len(payload["pre_evaluated"]) == 1
    assert len(payload["last_processed"]) == 1

    fast_size = module._state_size_fast(
        zones=module._zones,
        zone_lifecycle=module._zone_lifecycle,
        pre_candidates=module._pre_candidates,
        pre_evaluated=module._pre_evaluated,
        last_processed=module._last_processed,
        fingerprints=module._last_fingerprints,
        history_fingerprints=module._history_fingerprints,
        barriers=module._barriers,
        bound_symbol=module._bound_symbol,
    )
    assert fast_size == module._state_size(payload)

    assert module._zone_state_row_size_fast(zone) > 0
    assert module._lifecycle_state_row_size_fast(active) > 0
    assert module._lifecycle_state_row_size_fast(partial) > 0
    assert module._history_state_row_size_fast(zone.created_at, history_fingerprint) > 0
    assert (
        module._cursor_state_row_size_fast(
            symbol=zone.symbol,
            timeframe=zone.timeframe,
            close_time=zone.created_at,
            fingerprint=None,
            history=(),
            barrier=None,
        )
        > 0
    )
    assert (
        module._cursor_state_row_size_fast(
            symbol=zone.symbol,
            timeframe=zone.timeframe,
            close_time=zone.created_at,
            fingerprint=fingerprint,
            history=((zone.created_at, history_fingerprint),),
            barrier=zone.created_at,
        )
        > 0
    )


def test_validate_pre_candidate_default_and_target_skips() -> None:
    default_config = _config()
    default_candidate = _candidate(default_config)
    FvgModule._validate_pre_candidate_filter_traces(default_candidate, default_config)

    target_config = _config(
        price_filter_mode="absolute",
        price_filter_min="1",
        price_filter_target="confirmed",
        volume_filter_mode="b_to_sma",
        volume_lookback=1,
        volume_filter_target="confirmed",
    )
    target_candidate = _candidate(target_config)
    FvgModule._validate_pre_candidate_filter_traces(target_candidate, target_config)

    allowed_config = _config(
        volume_filter_mode="b_to_sma",
        volume_lookback=20,
        volume_missing_policy="allow",
    )
    allowed_candidate = _candidate(allowed_config)
    assert allowed_candidate.filters[2].decision is FvgFilterDecision.SKIPPED
    FvgModule._validate_pre_candidate_filter_traces(allowed_candidate, allowed_config)


def test_validate_pre_candidate_rejects_corrupt_core_traces() -> None:
    config = _config()
    candidate = _candidate(config)
    gap, price, volume = candidate.filters

    reordered = _candidate(config)
    _corrupt(reordered, filters=(price, gap, volume))
    with pytest.raises(FvgStateError, match="set or order"):
        FvgModule._validate_pre_candidate_filter_traces(reordered, config)

    failed = _candidate(config)
    failed_gap = replace(
        gap,
        decision=FvgFilterDecision.FAILED,
        reason="gap_size_below_minimum",
    )
    _corrupt(failed, filters=(failed_gap, price, volume))
    with pytest.raises(FvgStateError, match="non-candidate"):
        FvgModule._validate_pre_candidate_filter_traces(failed, config)

    inconsistent_gap = _candidate(config)
    wrong_gap = replace(gap, reason="wrong")
    _corrupt(inconsistent_gap, filters=(wrong_gap, price, volume))
    with pytest.raises(FvgStateError, match="gap filter"):
        FvgModule._validate_pre_candidate_filter_traces(inconsistent_gap, config)

    invalid_reference_type = _candidate(config)
    bad_price = replace(price, metadata={"reference_price": 1})
    _corrupt(invalid_reference_type, filters=(gap, bad_price, volume))
    with pytest.raises(FvgStateError, match="canonical string or null"):
        FvgModule._validate_pre_candidate_filter_traces(invalid_reference_type, config)

    invalid_reference_value = _candidate(config)
    bad_price = replace(price, metadata={"reference_price": "0"})
    _corrupt(invalid_reference_value, filters=(gap, bad_price, volume))
    with pytest.raises(FvgStateError, match="finite and positive"):
        FvgModule._validate_pre_candidate_filter_traces(invalid_reference_value, config)

    mismatched_distance = _candidate(config)
    bad_price = replace(price, metadata={"reference_price": "100"})
    _corrupt(mismatched_distance, filters=(gap, bad_price, volume))
    with pytest.raises(FvgStateError, match="reference distance"):
        FvgModule._validate_pre_candidate_filter_traces(mismatched_distance, config)

    inconsistent_price = _candidate(config)
    bad_price = replace(price, reason="wrong")
    _corrupt(inconsistent_price, filters=(gap, bad_price, volume))
    with pytest.raises(FvgStateError, match="price filter"):
        FvgModule._validate_pre_candidate_filter_traces(inconsistent_price, config)

    inconsistent_volume = _candidate(config)
    bad_volume = replace(volume, reason="wrong")
    _corrupt(inconsistent_volume, filters=(gap, price, bad_volume))
    with pytest.raises(FvgStateError, match="volume filter"):
        FvgModule._validate_pre_candidate_filter_traces(inconsistent_volume, config)

    ratio_when_disabled = _candidate(config)
    _corrupt(
        ratio_when_disabled,
        metrics=replace(ratio_when_disabled.metrics, volume_ratio=Decimal("1")),
    )
    with pytest.raises(FvgStateError, match="unavailable when filter is disabled"):
        FvgModule._validate_pre_candidate_filter_traces(ratio_when_disabled, config)


def _passed_volume_candidate(config: FvgConfig):
    candidate = _candidate(_config())
    ratio = Decimal("2")
    volume_trace = FvgFilterTrace(
        name="volume",
        decision=FvgFilterDecision.PASSED,
        reason="volume_within_bounds",
        observed=ratio,
        minimum=config.volume_min_ratio,
        maximum=config.volume_max_ratio,
        metadata={
            "mode": config.volume_filter_mode.value,
            "lookback": config.volume_lookback,
            "numerator": "2",
            "reference": "1",
        },
    )
    _corrupt(
        candidate,
        metrics=replace(candidate.metrics, volume_ratio=ratio),
        filters=(candidate.filters[0], candidate.filters[1], volume_trace),
    )
    return candidate


def test_validate_pre_candidate_passed_volume_and_failures() -> None:
    config = _config(
        volume_filter_mode="b_to_sma",
        volume_lookback=1,
        volume_min_ratio="0",
        volume_max_ratio="3",
    )
    valid = _passed_volume_candidate(config)
    FvgModule._validate_pre_candidate_filter_traces(valid, config)

    checks = [
        (replace(valid.filters[2], reason="wrong"), "reason"),
        (replace(valid.filters[2], observed=Decimal("1")), "ratio does not match"),
        (replace(valid.filters[2], minimum=Decimal("1")), "bounds"),
        (replace(valid.filters[2], metadata={"mode": "b_to_sma"}), "metadata"),
        (
            replace(
                valid.filters[2],
                metadata={
                    "mode": "wrong",
                    "lookback": 1,
                    "numerator": "2",
                    "reference": "1",
                },
            ),
            "metadata",
        ),
        (
            replace(
                valid.filters[2],
                metadata={
                    "mode": "b_to_sma",
                    "lookback": 1,
                    "numerator": "bad",
                    "reference": "1",
                },
            ),
            "numerator is invalid",
        ),
        (
            replace(
                valid.filters[2],
                metadata={
                    "mode": "b_to_sma",
                    "lookback": 1,
                    "numerator": "-1",
                    "reference": "1",
                },
            ),
            "finite and non-negative",
        ),
        (
            replace(
                valid.filters[2],
                metadata={
                    "mode": "b_to_sma",
                    "lookback": 1,
                    "numerator": "2",
                    "reference": "bad",
                },
            ),
            "reference is invalid",
        ),
        (
            replace(
                valid.filters[2],
                metadata={
                    "mode": "b_to_sma",
                    "lookback": 1,
                    "numerator": "2",
                    "reference": "2",
                },
            ),
            "ratio is inconsistent",
        ),
    ]
    for trace, message in checks:
        candidate = _passed_volume_candidate(config)
        _corrupt(candidate, filters=(candidate.filters[0], candidate.filters[1], trace))
        with pytest.raises(FvgStateError, match=message):
            FvgModule._validate_pre_candidate_filter_traces(candidate, config)


def test_validate_pre_candidate_percentile_volume() -> None:
    config = _config(
        volume_filter_mode="percentile",
        volume_lookback=1,
        volume_min_ratio="0",
        volume_max_ratio="1",
    )
    candidate = _passed_volume_candidate(config)
    ratio = Decimal("0.5")
    trace = replace(
        candidate.filters[2],
        observed=ratio,
        maximum=Decimal("1"),
        metadata={
            "mode": "percentile",
            "lookback": 1,
            "numerator": "2",
            "reference": "percentile_rank",
        },
    )
    _corrupt(
        candidate,
        metrics=replace(candidate.metrics, volume_ratio=ratio),
        filters=(candidate.filters[0], candidate.filters[1], trace),
    )
    FvgModule._validate_pre_candidate_filter_traces(candidate, config)

    wrong_reference = replace(trace, metadata={**dict(trace.metadata), "reference": "1"})
    _corrupt(candidate, filters=(candidate.filters[0], candidate.filters[1], wrong_reference))
    with pytest.raises(FvgStateError, match="percentile volume trace"):
        FvgModule._validate_pre_candidate_filter_traces(candidate, config)

    out_of_range = replace(trace, observed=Decimal("2"))
    _corrupt(
        candidate,
        metrics=replace(candidate.metrics, volume_ratio=Decimal("2")),
        filters=(candidate.filters[0], candidate.filters[1], out_of_range),
    )
    with pytest.raises(FvgStateError, match=r"in \[0, 1\]"):
        FvgModule._validate_pre_candidate_filter_traces(candidate, config)
