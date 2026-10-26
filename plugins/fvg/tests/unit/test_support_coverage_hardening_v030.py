from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal

import pytest

from conftest import FakeCandle, bearish_triplet, bullish_triplet, candle
from modular_strategy_fvg import (
    FvgConfig,
    FvgDataOrderError,
    FvgFilterDecision,
    FvgFilterTrace,
    FvgModule,
    FvgReasonCode,
    FvgStateError,
    evaluate_fvg_window,
    evaluate_pre_fvg,
)
from modular_strategy_fvg.detector import detect_zone
from modular_strategy_fvg.evaluator import _status_for, _target_skipped
from modular_strategy_fvg.models import FvgEvaluationStatus
from modular_strategy_fvg.timeframes import (
    as_utc,
    canonical_timeframe,
    is_period_aligned,
    parse_canonical_utc_datetime,
    period_close,
    period_open,
    timeframe_seconds,
)

UTC = timezone.utc


def _zones():
    a, b, c = bullish_triplet()
    bull = detect_zone(a, b, c, FvgConfig(timeframes=("15m",)))
    ba, bb, bc = bearish_triplet()
    bear = detect_zone(ba, bb, bc, FvgConfig(timeframes=("15m",)))
    assert bull is not None and bear is not None
    return bull, bear


def _candidate():
    a, b, _ = bullish_triplet()
    result = evaluate_pre_fvg(
        a=a,
        b=b,
        lower_bars=(
            candle(6, timeframe="5m", open_=106, high=110, low=105, close=107),
            candle(7, timeframe="5m", open_=107, high=112, low=106, close=111),
        ),
        observed_at=a.timestamp + timedelta(minutes=40),
        config=FvgConfig.from_parameters(timeframes=("15m",), pre_fvg_enabled=True, pre_fvg_evaluation_offset="5m"),
    )
    assert result.candidate is not None
    return result.candidate


def test_order_violation_raise_and_reset_scope() -> None:
    bull, bear = _zones()
    key = ("BTCUSDT", "15m")
    latest = bull.created_at - timedelta(minutes=15)
    previous = bull.created_at
    with pytest.raises(FvgDataOrderError, match="regressed"):
        FvgModule(timeframes=("15m",))._handle_order_violation(
            key=key,
            latest_close=latest,
            previous_close=previous,
            zones={bull.zone_id: bull},
            last_processed={},
            fingerprints={},
            history_fingerprints={},
            history_references={},
            signatures={},
            barriers={},
            skips=[],
        )

    module = FvgModule(timeframes=("15m",), order_violation_policy="reset_and_skip")
    other = replace(bear, zone_id="eth", symbol="ETHUSDT")
    zones = {bull.zone_id: bull, other.zone_id: other}
    last = {key: previous}
    fingerprints = {key: "x"}
    histories = {key: ((previous, "x"),)}
    refs = {key: ((previous, bullish_triplet()[-1]),)}
    signatures = {key: ("x",)}
    barriers = {key: previous}
    skips: list[str] = []
    removed = module._handle_order_violation(
        key=key,
        latest_close=latest,
        previous_close=previous,
        zones=zones,
        last_processed=last,
        fingerprints=fingerprints,
        history_fingerprints=histories,
        history_references=refs,
        signatures=signatures,
        barriers=barriers,
        skips=skips,
    )
    assert removed == (bull,)
    assert list(zones.values()) == [other]
    assert last[key] == latest and barriers[key] == latest
    assert key not in fingerprints and key not in histories and key not in refs and key not in signatures
    assert skips == ["fvg.order_violation_reset:15m"]


def test_clear_pre_and_symbol_scopes() -> None:
    candidate = _candidate()
    other = replace(candidate, candidate_id="other", symbol="ETHUSDT")
    candidates = {candidate.candidate_id: candidate, other.candidate_id: other}
    evaluated = {
        (candidate.symbol, candidate.timeframe): candidate.source_c_open_time,
        (other.symbol, other.timeframe): other.source_c_open_time,
    }
    removed = FvgModule._clear_pre_timeframe_scope("BTCUSDT", "15m", candidates, evaluated)
    assert removed == (candidate,)
    assert list(candidates.values()) == [other]
    assert ("BTCUSDT", "15m") not in evaluated

    bull, bear = _zones()
    eth = replace(bear, zone_id="eth-zone", symbol="ETHUSDT")
    zones = {bull.zone_id: bull, eth.zone_id: eth}
    btc = ("BTCUSDT", "15m")
    eth_key = ("ETHUSDT", "15m")
    last = {btc: bull.created_at, eth_key: eth.created_at}
    fps = {btc: "a", eth_key: "b"}
    histories = {btc: (), eth_key: ()}
    refs = {btc: (), eth_key: ()}
    signatures = {btc: None, eth_key: None}
    barriers = {btc: bull.created_at, eth_key: eth.created_at}
    removed_zones = FvgModule._clear_symbol("BTCUSDT", zones, last, fps, histories, refs, signatures, barriers)
    assert removed_zones == (bull,)
    assert list(zones.values()) == [eth]
    for mapping in (last, fps, histories, refs, signatures, barriers):
        assert btc not in mapping and eth_key in mapping


def test_support_serialization_signatures_and_helpers() -> None:
    bull, _ = _zones()
    module = FvgModule(timeframes=("15m",))
    module._zones[bull.zone_id] = bull
    assert module._active_counts() == {"15m": 1}
    deletion = module._delete(bull, FvgReasonCode.EXPIRED_DELETE)
    assert deletion.feature_id == bull.zone_id and deletion.reason == "fvg_expired"

    stamp = bull.created_at
    assert module._parse_aware_datetime(stamp.isoformat(), "x") == stamp
    with pytest.raises(FvgStateError):
        module._parse_aware_datetime("bad", "x")

    assert module._fingerprint_parameters({"a": {"b": (1, 2)}, "c": [3]}) == module._fingerprint_parameters(
        {"c": [3], "a": {"b": [1, 2]}}
    )
    assert module._state_size({"x": "é"}) > 0
    with pytest.raises(FvgStateError, match="JSON-serializable"):
        module._state_size({"x": object()})

    assert module._raw_value_signature(None) is None
    assert module._raw_value_signature(1.5)[0] == "float"
    assert module._raw_value_signature(2) == ("int", 2)
    assert module._raw_value_signature(Decimal("2.0")) == ("decimal", "2")

    bar = bullish_triplet()[0]
    signature = module._raw_signature(bar, bar.close_time)
    assert signature[0:2] == ("BTCUSDT", "15m")
    fake = FakeCandle(bar.timestamp, bar.close_time, bar.high, bar.low, bar.close)
    fake_signature = module._raw_signature(fake, fake.close_time)
    assert fake_signature[0:2] == ("BTCUSDT", "15m")
    bad_symbol = replace(fake, symbol=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="canonical strings"):
        module._raw_signature(bad_symbol, fake.close_time)
    bad_clock = replace(fake, timestamp="bad")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="timestamp"):
        module._raw_signature(bad_clock, fake.close_time)

    assert "conflicting duplicate" in str(module._conflicting_duplicate_error(("BTCUSDT", "15m"), stamp))
    assert module._no_data_reason("15m") == "fvg.no_data:15m"
    assert module._order_reset_reason("15m") == "fvg.order_violation_reset:15m"


def test_evaluator_status_and_target_branches() -> None:
    passed = FvgFilterTrace("gap_size", FvgFilterDecision.PASSED, "ok")
    failed = FvgFilterTrace("gap_size", FvgFilterDecision.FAILED, "failed")
    unavailable = FvgFilterTrace("gap_size", FvgFilterDecision.UNAVAILABLE, "missing")
    assert _status_for((failed, unavailable))[0] is FvgEvaluationStatus.REJECTED
    assert _status_for((passed, unavailable))[0] is FvgEvaluationStatus.UNAVAILABLE_WITH_CURRENT_DATA
    assert _status_for((passed,))[0] is FvgEvaluationStatus.ACCEPTED

    skipped = _target_skipped("price", FvgConfig.from_parameters().price_filter_target)
    assert skipped.decision is FvgFilterDecision.SKIPPED

    a, b, _ = bullish_triplet()
    no_gap_c = candle(2, open_=99, high=103, low=99, close=101)
    result = evaluate_fvg_window(a=a, b=b, c=no_gap_c, config=FvgConfig(timeframes=("15m",)))
    assert result.reason == "fvg_geometry_not_present"

    targeted = evaluate_fvg_window(
        a=a,
        b=b,
        c=bullish_triplet()[-1],
        config=FvgConfig.from_parameters(
            timeframes=("15m",), price_filter_target="pre_fvg", volume_filter_target="pre_fvg"
        ),
    )
    assert targeted.status is FvgEvaluationStatus.ACCEPTED
    assert targeted.filters[1].reason == "price_filter_target_not_applicable"
    assert targeted.filters[2].reason == "volume_filter_target_not_applicable"


class _NoneOffset(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None


class _BrokenOffset(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        raise ValueError("bad offset")

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None


def test_timeframe_edge_contracts() -> None:
    assert canonical_timeframe(" 15M ") == "15m"
    with pytest.raises(TypeError):
        canonical_timeframe(15)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        timeframe_seconds("999999999999999999999999999999999999d")

    aware = datetime(2024, 1, 1, tzinfo=timezone(timedelta(hours=2)))
    assert as_utc(aware).hour == 22
    with pytest.raises(ValueError, match="timezone-aware"):
        as_utc(datetime(2024, 1, 1, tzinfo=_NoneOffset()))
    with pytest.raises(ValueError, match="cannot be represented"):
        as_utc(datetime(2024, 1, 1, tzinfo=_BrokenOffset()))

    canonical = datetime(2024, 1, 1, tzinfo=UTC).isoformat()
    assert parse_canonical_utc_datetime(canonical) == datetime(2024, 1, 1, tzinfo=UTC)
    with pytest.raises(TypeError):
        parse_canonical_utc_datetime(1)
    with pytest.raises(ValueError, match="valid ISO"):
        parse_canonical_utc_datetime("not-a-date")
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_canonical_utc_datetime("2024-01-01T00:00:00")
    with pytest.raises(ValueError, match="canonical ISO"):
        parse_canonical_utc_datetime("2024-01-01T00:00:00Z")

    monday = datetime(2024, 1, 3, tzinfo=UTC)
    assert period_open(monday, "1w").weekday() == 0
    assert period_close(monday, "1d") > period_open(monday, "1d")
    assert is_period_aligned(datetime(2024, 1, 1, tzinfo=UTC), "1d")
    assert not is_period_aligned(datetime(2024, 1, 1, 0, 1, tzinfo=UTC), "1d")


def test_checkpoint_thaw_fail_closed_contracts() -> None:
    from modular_strategy_fvg.module_shared import _thaw_checkpoint_payload

    assert _thaw_checkpoint_payload({"x": (1, {"y": 2})}) == {"x": [1, {"y": 2}]}
    with pytest.raises(FvgStateError, match="nesting"):
        _thaw_checkpoint_payload("x", depth=65)
    with pytest.raises(FvgStateError, match="canonical strings"):
        _thaw_checkpoint_payload({1: "x"})

    cyclic_mapping: dict[str, object] = {}
    cyclic_mapping["self"] = cyclic_mapping
    with pytest.raises(FvgStateError, match="cyclic mapping"):
        _thaw_checkpoint_payload(cyclic_mapping)

    cyclic_sequence: list[object] = []
    cyclic_sequence.append(cyclic_sequence)
    with pytest.raises(FvgStateError, match="cyclic sequence"):
        _thaw_checkpoint_payload(cyclic_sequence)
