from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from conftest import bullish_triplet, candle, view_for
from modular_strategy_fvg import (
    FvgConfig,
    FvgDataConflictError,
    FvgFilterDecision,
    FvgModule,
    FvgPreEvaluation,
    FvgPreStatus,
    FvgStateError,
    evaluate_pre_fvg,
)
from modular_strategy_fvg.detector import candle_fingerprint, detect_zone
from modular_strategy_fvg.lifecycle import initial_lifecycle
from modular_strategy_fvg.models import FvgEvaluationStatus
import modular_strategy_fvg.module_runtime as runtime_module

UTC = timezone.utc


@dataclass
class _Bar:
    timestamp: object
    close_time: object
    symbol: object = "BTCUSDT"
    timeframe: object = "15m"
    open: object = 100
    high: object = 110
    low: object = 90
    close: object = 100
    volume: object = 1


class _BrokenBar(_Bar):
    @property
    def high(self) -> object:  # type: ignore[override]
        raise RuntimeError("boom")

    @high.setter
    def high(self, value: object) -> None:
        pass


def _zone():
    a, b, c = bullish_triplet()
    zone = detect_zone(a, b, c, FvgConfig(timeframes=("15m",)))
    assert zone is not None
    return zone


def _candidate_result() -> FvgPreEvaluation:
    a, b, _ = bullish_triplet()
    result = evaluate_pre_fvg(
        a=a,
        b=b,
        lower_bars=(
            candle(6, timeframe="5m", open_=106, high=110, low=105, close=107),
            candle(7, timeframe="5m", open_=107, high=112, low=106, close=111),
        ),
        observed_at=a.timestamp + timedelta(minutes=40),
        config=FvgConfig.from_parameters(
            timeframes=("15m",),
            pre_fvg_enabled=True,
            pre_fvg_evaluation_offset="5m",
        ),
    )
    assert result.status is FvgPreStatus.CANDIDATE
    assert result.candidate is not None
    return result


def _pre_context():
    bars = bullish_triplet()
    view = view_for(bars, base_close_offset_minutes=-5)
    return bars, view, view.current.close_time


def test_canonical_close_and_visible_bar_contracts() -> None:
    bar = _Bar(
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        close_time=datetime(2024, 1, 1, 0, 15, tzinfo=UTC),
    )
    assert FvgModule._canonical_close_time(bar, "x") == bar.close_time
    with pytest.raises(TypeError, match="close_time"):
        FvgModule._canonical_close_time(replace(bar, close_time="bad"), "x")

    assert FvgModule._validate_visible_bar(bar, bar.close_time, "BTCUSDT", "15m") == bar.close_time
    with pytest.raises(TypeError, match="canonical strings"):
        FvgModule._validate_visible_bar(replace(bar, symbol=1), bar.close_time, "BTCUSDT", "15m")
    with pytest.raises(ValueError, match="symbol differs"):
        FvgModule._validate_visible_bar(replace(bar, symbol="ETHUSDT"), bar.close_time, "BTCUSDT", "15m")
    with pytest.raises(ValueError, match="unexpected timeframe"):
        FvgModule._validate_visible_bar(replace(bar, timeframe="4h"), bar.close_time, "BTCUSDT", "15m")
    with pytest.raises(ValueError, match="future/incomplete"):
        FvgModule._validate_visible_bar(bar, bar.close_time - timedelta(seconds=1), "BTCUSDT", "15m")


def test_new_indexes_all_boundaries() -> None:
    series = bullish_triplet()
    assert list(FvgModule._new_indexes_canonical(series, None)) == [0, 1, 2]
    assert list(FvgModule._new_indexes_canonical(series, series[1].close_time)) == [2]
    assert list(FvgModule._new_indexes_canonical(series, series[-1].close_time)) == []
    assert list(FvgModule._new_indexes_canonical(series, series[0].timestamp - timedelta(seconds=1))) == [0, 1, 2]


def test_history_fingerprint_validation_paths() -> None:
    series = bullish_triplet()
    key = ("BTCUSDT", "15m")
    bar = series[1]
    history = ((bar.close_time, candle_fingerprint(bar)),)

    assert FvgModule._validate_history_fingerprints(series, key, (), (), 0) == ()
    with pytest.raises(FvgDataConflictError, match="historical candle missing"):
        FvgModule._validate_history_fingerprints(series, key, history, (), 0)

    known = FvgModule._validate_history_fingerprints(series, key, history, ((bar.close_time, bar),), 2)
    assert known == ((bar.close_time, bar),)

    copied = replace(bar)
    validated = FvgModule._validate_history_fingerprints((series[0], copied, series[2]), key, history, (), 2)
    assert validated[0][0] == bar.close_time

    changed = replace(bar, close=bar.close + 1)
    with pytest.raises(FvgDataConflictError, match="historical candle changed"):
        FvgModule._validate_history_fingerprints((series[0], changed, series[2]), key, history, (), 2)

    with pytest.raises(FvgDataConflictError, match="conflicting duplicate"):
        FvgModule._validate_history_fingerprints((changed,), key, history, (), 1)

    broken = _BrokenBar(bar.timestamp, bar.close_time)
    with pytest.raises(FvgDataConflictError, match="historical candle changed"):
        FvgModule._validate_history_fingerprints((broken, series[2]), key, history, (), 1)
    with pytest.raises(FvgDataConflictError, match="conflicting duplicate"):
        FvgModule._validate_history_fingerprints((broken,), key, history, (), 1)


def test_increment_reason_contracts() -> None:
    diagnostics: dict[str, object] = {"evaluation_reasons": {}}
    FvgModule._increment_reason(diagnostics, "accepted")
    FvgModule._increment_reason(diagnostics, "accepted")
    assert diagnostics["evaluation_reasons"] == {"accepted": 2}
    with pytest.raises(TypeError, match="must be a mapping"):
        FvgModule._increment_reason({}, "x")
    with pytest.raises(TypeError, match="counter must be an integer"):
        FvgModule._increment_reason({"evaluation_reasons": {"x": "1"}}, "x")


def test_evaluate_confirmed_window_simple_and_rich() -> None:
    series = bullish_triplet()
    module = FvgModule(timeframes=("15m",))
    evaluation, zone = module._evaluate_confirmed_window(
        series=series,
        index=2,
        effective_config=module.config,
        visible_until=series[-1].close_time,
    )
    assert evaluation is None and zone is not None

    rich = FvgModule(timeframes=("15m",), publish_evaluation_events=True)
    evaluation, zone = rich._evaluate_confirmed_window(
        series=series,
        index=2,
        effective_config=rich.config,
        visible_until=series[-1].close_time,
    )
    assert evaluation is not None
    assert evaluation.status is FvgEvaluationStatus.ACCEPTED
    assert zone == evaluation.zone


def _diagnostics(module: FvgModule) -> dict[str, object]:
    return dict(module._diagnostics(0, False, symbol="BTCUSDT"))


def test_observe_zone_skips_initializes_and_keeps_active() -> None:
    zone = _zone()
    module = FvgModule(timeframes=("15m",), max_zone_age_bars=0)
    unrelated = replace(zone, zone_id="eth", symbol="ETHUSDT")
    wa, wb, wc = bullish_triplet(timeframe="4h")
    wrong_timeframe = detect_zone(wa, wb, wc, FvgConfig(timeframes=("4h",)))
    assert wrong_timeframe is not None
    zones = {unrelated.zone_id: unrelated, wrong_timeframe.zone_id: wrong_timeframe, zone.zone_id: zone}
    lifecycle = {}
    features: list[object] = []
    deletes: list[object] = []
    diagnostics = _diagnostics(module)
    bar = candle(3, open_=107, high=110, low=106, close=108)
    module._observe_zones_for_bar(
        timeframe="15m",
        bar=bar,
        bar_close=bar.close_time,
        visible_until=bar.close_time,
        zones=zones,
        lifecycle=lifecycle,
        features=features,
        deletes=deletes,
        diagnostics=diagnostics,
    )
    assert zone.zone_id in zones
    assert zone.zone_id in lifecycle
    assert len(features) == 1
    assert deletes == []

    future_zones = {zone.zone_id: zone}
    future_features: list[object] = []
    module._observe_zones_for_bar(
        timeframe="15m",
        bar=bar,
        bar_close=zone.created_at,
        visible_until=bar.close_time,
        zones=future_zones,
        lifecycle={},
        features=future_features,
        deletes=[],
        diagnostics=_diagnostics(module),
    )
    assert future_features == []


def test_observe_zone_invalidation_full_fill_and_partial() -> None:
    zone = _zone()
    full = FvgModule(timeframes=("15m",), publish_lifecycle_events=True)
    zones = {zone.zone_id: zone}
    lifecycle = {zone.zone_id: initial_lifecycle(zone)}
    features: list[object] = []
    deletes: list[object] = []
    diagnostics = _diagnostics(full)
    bar = candle(3, open_=103, high=110, low=99, close=104)
    full._observe_zones_for_bar(
        timeframe="15m",
        bar=bar,
        bar_close=bar.close_time,
        visible_until=bar.close_time,
        zones=zones,
        lifecycle=lifecycle,
        features=features,
        deletes=deletes,
        diagnostics=diagnostics,
    )
    assert zones == {} and lifecycle == {}
    assert len(features) == 2 and len(deletes) == 1
    assert diagnostics["invalidated"] == 1

    partial = FvgModule(timeframes=("15m",), invalidation_level="first_touch", publish_lifecycle_events=False)
    zones = {zone.zone_id: zone}
    lifecycle = {}
    features = []
    deletes = []
    diagnostics = _diagnostics(partial)
    bar = candle(3, open_=107, high=110, low=104, close=108)
    partial._observe_zones_for_bar(
        timeframe="15m",
        bar=bar,
        bar_close=bar.close_time,
        visible_until=bar.close_time,
        zones=zones,
        lifecycle=lifecycle,
        features=features,
        deletes=deletes,
        diagnostics=diagnostics,
    )
    assert zones == {} and len(features) == 1 and len(deletes) == 1


def test_observe_zone_expiry() -> None:
    zone = _zone()
    module = FvgModule(timeframes=("15m",), max_zone_age_bars=1)
    zones = {zone.zone_id: zone}
    lifecycle = {zone.zone_id: initial_lifecycle(zone)}
    features: list[object] = []
    deletes: list[object] = []
    diagnostics = _diagnostics(module)
    bar = candle(3, open_=107, high=110, low=106, close=108)
    module._observe_zones_for_bar(
        timeframe="15m",
        bar=bar,
        bar_close=bar.close_time,
        visible_until=bar.close_time,
        zones=zones,
        lifecycle=lifecycle,
        features=features,
        deletes=deletes,
        diagnostics=diagnostics,
    )
    assert zones == {} and lifecycle == {}
    assert len(features) == 1 and len(deletes) == 1
    assert diagnostics["expired"] == 1


def _run_pre(
    monkeypatch: pytest.MonkeyPatch,
    result: object,
    *,
    config: FvgConfig | None = None,
    timeframe: str = "15m",
    visible_until: datetime | None = None,
    evaluated: dict[tuple[str, str], datetime] | None = None,
):
    bars, view, default_visible = _pre_context()
    module = FvgModule(timeframes=("15m",), pre_fvg_enabled=True, pre_fvg_evaluation_offset="5m")
    effective = config or module.config
    monkeypatch.setattr(runtime_module, "evaluate_pre_fvg", lambda **_: result)
    candidates: dict[str, object] = {}
    evaluated_map = {} if evaluated is None else evaluated
    features: list[object] = []
    diagnostics = _diagnostics(module)
    module._evaluate_pre_for_timeframe(
        view=view,
        target_series=bars,
        timeframe=timeframe,
        visible_until=default_visible if visible_until is None else visible_until,
        effective_config=effective,
        candidates=candidates,
        evaluated=evaluated_map,
        features=features,
        diagnostics=diagnostics,
    )
    return module, candidates, evaluated_map, features, diagnostics


def test_pre_evaluation_early_return_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _candidate_result()
    disabled = FvgConfig(timeframes=("15m",))
    assert _run_pre(monkeypatch, result, config=disabled)[3] == []

    bars, view, visible = _pre_context()
    module = FvgModule(timeframes=("15m",), pre_fvg_enabled=True, pre_fvg_evaluation_offset="5m")
    features: list[object] = []
    module._evaluate_pre_for_timeframe(
        view=view,
        target_series=bars[:1],
        timeframe="15m",
        visible_until=visible,
        effective_config=module.config,
        candidates={},
        evaluated={},
        features=features,
        diagnostics=_diagnostics(module),
    )
    assert features == []

    assert _run_pre(monkeypatch, result, timeframe="5m")[3] == []

    source_c_close = bars[-1].close_time + timedelta(minutes=15)
    assert _run_pre(monkeypatch, result, visible_until=source_c_close)[3] == []

    source_c_open = bars[-1].close_time
    assert _run_pre(monkeypatch, result, evaluated={("BTCUSDT", "15m"): source_c_open})[3] == []

    not_due = FvgPreEvaluation(status=FvgPreStatus.NOT_DUE)
    assert _run_pre(monkeypatch, not_due)[3] == []


def test_pre_evaluation_candidate_rejected_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate_result = _candidate_result()
    _, candidates, evaluated, features, diagnostics = _run_pre(monkeypatch, candidate_result)
    candidate = candidate_result.candidate
    assert candidate is not None
    assert candidate.candidate_id in candidates
    assert evaluated[("BTCUSDT", "15m")] == bullish_triplet()[-1].close_time
    assert len(features) == 1
    assert diagnostics["pre_candidates"] == 1
    assert diagnostics["evaluation_reasons"] == {candidate_result.reason: 1}

    gap, price, volume = candidate.filters
    failed = replace(gap, decision=FvgFilterDecision.FAILED, reason="gap_size_below_minimum")
    rejected = FvgPreEvaluation(
        status=FvgPreStatus.REJECTED,
        metrics=candidate.metrics,
        filters=(failed, price, volume),
    )
    _, candidates, _, features, diagnostics = _run_pre(monkeypatch, rejected)
    assert candidates == {} and len(features) == 1
    assert diagnostics["pre_rejected"] == 1

    unavailable_trace = replace(
        volume,
        decision=FvgFilterDecision.UNAVAILABLE,
        reason="volume_metric_unavailable",
    )
    unavailable = FvgPreEvaluation(
        status=FvgPreStatus.UNAVAILABLE_WITH_CURRENT_DATA,
        metrics=candidate.metrics,
        filters=(gap, price, unavailable_trace),
    )
    _, candidates, _, features, diagnostics = _run_pre(monkeypatch, unavailable)
    assert candidates == {} and len(features) == 1
    assert diagnostics["pre_unavailable_with_current_data"] == 1


def test_pre_candidate_missing_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = SimpleNamespace(status=FvgPreStatus.CANDIDATE, candidate=None, reason="candidate")
    bars, view, visible = _pre_context()
    module = FvgModule(timeframes=("15m",), pre_fvg_enabled=True, pre_fvg_evaluation_offset="5m")
    monkeypatch.setattr(runtime_module, "evaluate_pre_fvg", lambda **_: fake)
    monkeypatch.setattr(module, "_pre_evaluation_feature", lambda *args: object())
    with pytest.raises(FvgStateError, match="missing its candidate"):
        module._evaluate_pre_for_timeframe(
            view=view,
            target_series=bars,
            timeframe="15m",
            visible_until=visible,
            effective_config=module.config,
            candidates={},
            evaluated={},
            features=[],
            diagnostics=_diagnostics(module),
        )
