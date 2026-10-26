from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from conftest import bullish_triplet
from modular_strategy_fvg import FvgConfig
from modular_strategy_fvg.analysis import calculate_metrics
from modular_strategy_fvg.detector import detect_zone
from modular_strategy_fvg.models import (
    FvgEvaluation,
    FvgEvaluationStatus,
    FvgFilterDecision,
    FvgFilterTrace,
    FvgMetrics,
    FvgPreCandidate,
    FvgPreEvaluation,
    FvgPreStatus,
    FvgZone,
)

UTC = timezone.utc


def _model_values() -> tuple[
    FvgZone,
    FvgMetrics,
    tuple[FvgFilterTrace, FvgFilterTrace, FvgFilterTrace],
]:
    a, b, c = bullish_triplet()
    zone = detect_zone(a, b, c, FvgConfig(timeframes=("15m",)))
    assert zone is not None
    metrics = calculate_metrics(
        zone=zone,
        a=a,
        b=b,
        c=c,
        history=(a, b, c),
        reference_price=Decimal("100"),
        tick_size=Decimal("1"),
        atr_period=2,
        average_range_period=3,
        volume_ratio=Decimal("1"),
    )
    traces = (
        FvgFilterTrace(
            name="gap_size",
            decision=FvgFilterDecision.PASSED,
            reason="gap_size_within_bounds",
            observed=metrics.gap_size,
            minimum=Decimal("0"),
            metadata={"mode": "absolute", "rank": 1, "optional": None},
        ),
        FvgFilterTrace(
            name="price",
            decision=FvgFilterDecision.SKIPPED,
            reason="price_filter_disabled",
        ),
        FvgFilterTrace(
            name="volume",
            decision=FvgFilterDecision.SKIPPED,
            reason="volume_filter_disabled",
        ),
    )
    return zone, metrics, traces


def _candidate() -> FvgPreCandidate:
    zone, metrics, traces = _model_values()
    evaluated = zone.source_c_open_time + timedelta(minutes=10)
    expected_close = zone.created_at
    return FvgPreCandidate(
        candidate_id="pre-fvg:test",
        symbol=zone.symbol,
        timeframe=zone.timeframe,
        direction=zone.direction,
        lower=zone.lower,
        upper=zone.upper,
        source_a_open_time=zone.source_a_open_time,
        source_b_open_time=zone.source_b_open_time,
        source_c_open_time=zone.source_c_open_time,
        source_c_expected_close_time=expected_close,
        evaluated_at=evaluated,
        processed_at=evaluated,
        remaining_seconds=int((expected_close - evaluated).total_seconds()),
        metrics=metrics,
        filters=traces,
    )


def test_filter_trace_round_trip_and_immutability() -> None:
    _, _, traces = _model_values()
    trace = traces[0]
    restored = FvgFilterTrace.from_value(dict(trace.to_value()))
    assert restored == trace
    with pytest.raises(TypeError):
        trace.metadata["x"] = "y"  # type: ignore[index]
    with pytest.raises(TypeError):
        trace.to_value()["name"] = "x"  # type: ignore[index]


@pytest.mark.parametrize(
    "changes",
    [
        {"name": ""},
        {"reason": ""},
        {"name": 1},
        {"decision": "passed"},
        {"observed": 1},
        {"observed": Decimal("NaN")},
        {"metadata": []},
        {"metadata": {"": "x"}},
        {"metadata": {1: "x"}},
        {"metadata": {"x": object()}},
    ],
)
def test_filter_trace_rejects_noncanonical_values(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "name": "gap_size",
        "decision": FvgFilterDecision.PASSED,
        "reason": "ok",
    }
    values.update(changes)
    with pytest.raises((TypeError, ValueError)):
        FvgFilterTrace(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "mutation",
    ["missing", "extra", "bad_metadata", "bad_name", "bad_decimal", "bad_decision"],
)
def test_filter_trace_from_value_rejects_malformed_payload(mutation: str) -> None:
    payload = dict(_model_values()[2][0].to_value())
    if mutation == "missing":
        payload.pop("name")
    elif mutation == "extra":
        payload["extra"] = "x"
    elif mutation == "bad_metadata":
        payload["metadata"] = []
    elif mutation == "bad_name":
        payload["name"] = 1
    elif mutation == "bad_decimal":
        payload["observed"] = 1
    else:
        payload["decision"] = "unknown"
    with pytest.raises((TypeError, ValueError)):
        FvgFilterTrace.from_value(payload)


def test_metrics_round_trip_and_all_optional_fields() -> None:
    metrics = FvgMetrics(
        gap_size=Decimal("5"),
        gap_fraction=Decimal("0.05"),
        gap_bps=Decimal("500"),
        gap_ticks=Decimal("5"),
        gap_atr=Decimal("0.5"),
        gap_b_range=Decimal("0.4"),
        gap_average_range=Decimal("0.3"),
        candle_b_body=Decimal("2"),
        candle_b_body_fraction=Decimal("0.5"),
        candle_b_direction="bullish",
        upper_wick_fraction=Decimal("0.2"),
        lower_wick_fraction=Decimal("0.3"),
        displacement=Decimal("4"),
        impulse_efficiency=Decimal("0.8"),
        close_strength=Decimal("0.9"),
        volume_ratio=Decimal("1.2"),
        distance_from_reference=Decimal("0.05"),
        utc_session="europe",
    )
    assert FvgMetrics.from_value(dict(metrics.to_value())) == metrics
    with pytest.raises(TypeError):
        metrics.to_value()["gap_size"] = "1"  # type: ignore[index]


@pytest.mark.parametrize(
    "changes",
    [
        {"gap_size": 1},
        {"gap_size": Decimal("NaN")},
        {"gap_size": Decimal("0")},
        {"gap_fraction": Decimal("0")},
        {"gap_bps": Decimal("1")},
        {"gap_ticks": Decimal("0")},
        {"gap_atr": Decimal("-1")},
        {"candle_b_body": Decimal("-1")},
        {"candle_b_body_fraction": Decimal("1.1")},
        {"upper_wick_fraction": Decimal("-0.1")},
        {"close_strength": Decimal("2")},
        {"impulse_efficiency": Decimal("-1")},
        {"volume_ratio": Decimal("-1")},
        {"distance_from_reference": Decimal("-1")},
        {"candle_b_direction": "sideways"},
        {"utc_session": "mars"},
        {"utc_session": 1},
    ],
)
def test_metrics_reject_invalid_contracts(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "gap_size": Decimal("5"),
        "gap_fraction": Decimal("0.05"),
        "gap_bps": Decimal("500"),
    }
    values.update(changes)
    with pytest.raises((TypeError, ValueError)):
        FvgMetrics(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "mutation",
    ["missing", "extra", "numeric_decimal", "bad_decimal", "numeric_direction"],
)
def test_metrics_from_value_rejects_malformed_payload(mutation: str) -> None:
    payload = dict(
        FvgMetrics(
            gap_size=Decimal("5"),
            gap_fraction=Decimal("0.05"),
            gap_bps=Decimal("500"),
        ).to_value()
    )
    if mutation == "missing":
        payload.pop("gap_size")
    elif mutation == "extra":
        payload["extra"] = None
    elif mutation == "numeric_decimal":
        payload["gap_size"] = 5
    elif mutation == "bad_decimal":
        payload["gap_size"] = "not-decimal"
    else:
        payload["utc_session"] = 1
    with pytest.raises((TypeError, ValueError)):
        FvgMetrics.from_value(payload)


def test_confirmed_evaluation_valid_statuses_and_serialization() -> None:
    zone, metrics, traces = _model_values()
    accepted = FvgEvaluation(
        status=FvgEvaluationStatus.ACCEPTED,
        zone=zone,
        metrics=metrics,
        filters=traces,
        evaluated_at=zone.created_at,
    )
    assert accepted.reason == "accepted"
    assert accepted.to_value()["zone"] is not None

    failed = replace(
        traces[0],
        decision=FvgFilterDecision.FAILED,
        reason="gap_size_below_minimum",
    )
    rejected = FvgEvaluation(
        status=FvgEvaluationStatus.REJECTED,
        zone=None,
        metrics=metrics,
        filters=(failed, traces[1], traces[2]),
    )
    assert rejected.reason == "rejected"
    assert rejected.to_value()["zone"] is None

    unavailable = replace(
        traces[2],
        decision=FvgFilterDecision.UNAVAILABLE,
        reason="volume_metric_unavailable",
    )
    result = FvgEvaluation(
        status=FvgEvaluationStatus.UNAVAILABLE_WITH_CURRENT_DATA,
        zone=None,
        metrics=metrics,
        filters=(traces[0], traces[1], unavailable),
    )
    assert result.reason == "unavailable_with_current_data"


@pytest.mark.parametrize(
    "factory",
    [
        lambda z, m, t: FvgEvaluation("accepted", z, m, t),
        lambda z, m, t: FvgEvaluation(FvgEvaluationStatus.ACCEPTED, object(), m, t),
        lambda z, m, t: FvgEvaluation(FvgEvaluationStatus.ACCEPTED, z, object(), t),
        lambda z, m, t: FvgEvaluation(FvgEvaluationStatus.ACCEPTED, z, m, list(t)),
        lambda z, m, t: FvgEvaluation(FvgEvaluationStatus.ACCEPTED, z, m, t, reason=1),
        lambda z, m, t: FvgEvaluation(FvgEvaluationStatus.ACCEPTED, z, m, t, event_type=1),
        lambda z, m, t: FvgEvaluation(FvgEvaluationStatus.ACCEPTED, None, m, t),
        lambda z, m, t: FvgEvaluation(FvgEvaluationStatus.REJECTED, z, m, t),
        lambda z, m, t: FvgEvaluation(
            FvgEvaluationStatus.ACCEPTED,
            z,
            replace(m, gap_size=m.gap_size + 1),
            t,
        ),
        lambda z, m, t: FvgEvaluation(
            FvgEvaluationStatus.ACCEPTED,
            z,
            m,
            t,
            event_type="pre_fvg",
        ),
        lambda z, m, t: FvgEvaluation(
            FvgEvaluationStatus.ACCEPTED,
            z,
            m,
            t,
            evaluated_at="bad",
        ),
        lambda z, m, t: FvgEvaluation(FvgEvaluationStatus.REJECTED, None, None, t),
        lambda z, m, t: FvgEvaluation(FvgEvaluationStatus.REJECTED, None, m, ()),
        lambda z, m, t: FvgEvaluation(FvgEvaluationStatus.ACCEPTED, z, m, t),
        lambda z, m, t: FvgEvaluation(
            FvgEvaluationStatus.ACCEPTED,
            z,
            m,
            t,
            evaluated_at=z.created_at - timedelta(seconds=1),
        ),
        lambda z, m, t: FvgEvaluation(
            FvgEvaluationStatus.ACCEPTED,
            z,
            m,
            (replace(t[0], decision=FvgFilterDecision.FAILED), t[1], t[2]),
            evaluated_at=z.created_at,
        ),
        lambda z, m, t: FvgEvaluation(FvgEvaluationStatus.REJECTED, None, m, t),
        lambda z, m, t: FvgEvaluation(
            FvgEvaluationStatus.UNAVAILABLE_WITH_CURRENT_DATA,
            None,
            m,
            (
                replace(t[0], decision=FvgFilterDecision.FAILED),
                t[1],
                replace(t[2], decision=FvgFilterDecision.UNAVAILABLE),
            ),
        ),
        lambda z, m, t: FvgEvaluation(
            FvgEvaluationStatus.REJECTED,
            None,
            m,
            (t[1], replace(t[0], decision=FvgFilterDecision.FAILED), t[2]),
        ),
    ],
)
def test_confirmed_evaluation_rejects_invalid_combinations(factory: object) -> None:
    zone, metrics, traces = _model_values()
    with pytest.raises((TypeError, ValueError)):
        factory(zone, metrics, traces)  # type: ignore[operator]


def test_pre_candidate_round_trip_and_legacy_state() -> None:
    candidate = _candidate()
    state = dict(candidate.to_state())
    assert FvgPreCandidate.from_state(state) == candidate
    legacy = dict(state)
    legacy.pop("processed_at")
    assert FvgPreCandidate.from_state(legacy).processed_at == candidate.evaluated_at
    with pytest.raises(TypeError):
        candidate.to_value()["candidate_id"] = "x"  # type: ignore[index]
    assert candidate.midpoint == (candidate.lower + candidate.upper) / 2
    assert candidate.size == candidate.upper - candidate.lower


@pytest.mark.parametrize(
    "changes",
    [
        {"candidate_id": 1},
        {"direction": "bullish"},
        {"lower": 1},
        {"remaining_seconds": 1.0},
        {"metrics": object()},
        {"filters": []},
        {"filters": ()},
        {"filters": (replace(_model_values()[2][0], decision=FvgFilterDecision.FAILED),) + _model_values()[2][1:]},
        {"disappearance_probability": "0.1"},
        {"source_a_open_time": "bad"},
        {"candidate_id": ""},
        {"symbol": " BTCUSDT"},
        {"timeframe": "15M"},
        {"source_a_open_time": datetime(2024, 1, 1, 0, 1, tzinfo=UTC)},
        {"source_c_expected_close_time": datetime(2024, 1, 1, 1, 0, tzinfo=UTC)},
        {"lower": Decimal("NaN")},
        {"lower": Decimal("0")},
        {"upper": Decimal("1")},
        {"metrics": replace(_model_values()[1], gap_size=Decimal("6"))},
        {"evaluated_at": datetime(2024, 1, 1, 0, 30, tzinfo=UTC)},
        {"remaining_seconds": 1},
        {"disappearance_probability": Decimal("0.1")},
        {"probability_reason": "model_available"},
    ],
)
def test_pre_candidate_rejects_invalid_contracts(changes: dict[str, object]) -> None:
    candidate = _candidate()
    with pytest.raises((TypeError, ValueError)):
        replace(candidate, **changes)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "event",
        "provisional",
        "metrics",
        "filters",
        "filter_row",
        "text_field",
        "processed_at",
        "remaining",
        "probability",
        "midpoint",
        "gap_size",
    ],
)
def test_pre_candidate_from_state_rejects_malformed_payload(mutation: str) -> None:
    state = dict(_candidate().to_state())
    if mutation == "missing":
        state.pop("candidate_id")
    elif mutation == "event":
        state["event_type"] = "confirmed"
    elif mutation == "provisional":
        state["provisional"] = False
    elif mutation == "metrics":
        state["metrics"] = []
    elif mutation == "filters":
        state["filters"] = "bad"
    elif mutation == "filter_row":
        state["filters"] = (1,)
    elif mutation == "text_field":
        state["symbol"] = 1
    elif mutation == "processed_at":
        state["processed_at"] = 1
    elif mutation == "remaining":
        state["remaining_seconds"] = True
    elif mutation == "probability":
        state["disappearance_probability"] = 1
    elif mutation == "midpoint":
        state["midpoint"] = "999"
    else:
        state["gap_size"] = "999"
    with pytest.raises((TypeError, ValueError)):
        FvgPreCandidate.from_state(state)


def test_pre_evaluation_valid_statuses_and_values() -> None:
    candidate = _candidate()
    candidate_result = FvgPreEvaluation(
        status=FvgPreStatus.CANDIDATE,
        candidate=candidate,
        target_evaluation_time=candidate.evaluated_at,
        processed_at=candidate.processed_at,
        metrics=candidate.metrics,
        filters=candidate.filters,
    )
    assert candidate_result.reason == "candidate"
    assert candidate_result.to_value()["candidate"] is not None

    failed = replace(
        candidate.filters[0],
        decision=FvgFilterDecision.FAILED,
        reason="gap_size_below_minimum",
    )
    rejected = FvgPreEvaluation(
        status=FvgPreStatus.REJECTED,
        metrics=candidate.metrics,
        filters=(failed, candidate.filters[1], candidate.filters[2]),
    )
    assert rejected.reason == "rejected"

    unavailable = replace(
        candidate.filters[2],
        decision=FvgFilterDecision.UNAVAILABLE,
        reason="volume_metric_unavailable",
    )
    missing = FvgPreEvaluation(
        status=FvgPreStatus.UNAVAILABLE_WITH_CURRENT_DATA,
        metrics=candidate.metrics,
        filters=(candidate.filters[0], candidate.filters[1], unavailable),
    )
    assert missing.reason == "unavailable_with_current_data"
    assert FvgPreEvaluation(status=FvgPreStatus.NOT_DUE).to_value()["metrics"] is None


@pytest.mark.parametrize(
    "factory",
    [
        lambda c: FvgPreEvaluation("candidate", candidate=c),
        lambda c: FvgPreEvaluation(FvgPreStatus.CANDIDATE, candidate=object()),
        lambda c: FvgPreEvaluation(FvgPreStatus.CANDIDATE, candidate=c, metrics=object()),
        lambda c: FvgPreEvaluation(FvgPreStatus.CANDIDATE, candidate=c, filters=list(c.filters)),
        lambda c: FvgPreEvaluation(FvgPreStatus.CANDIDATE, candidate=c, reason=1),
        lambda c: FvgPreEvaluation(FvgPreStatus.CANDIDATE),
        lambda c: FvgPreEvaluation(FvgPreStatus.REJECTED, candidate=c),
        lambda c: FvgPreEvaluation(FvgPreStatus.NOT_DUE, target_evaluation_time="bad"),
        lambda c: FvgPreEvaluation(FvgPreStatus.REJECTED, filters=c.filters),
        lambda c: FvgPreEvaluation(FvgPreStatus.REJECTED, metrics=c.metrics),
        lambda c: FvgPreEvaluation(
            FvgPreStatus.CANDIDATE,
            candidate=c,
            metrics=replace(c.metrics, gap_size=Decimal("6")),
            filters=c.filters,
            target_evaluation_time=c.evaluated_at,
            processed_at=c.processed_at,
        ),
        lambda c: FvgPreEvaluation(
            FvgPreStatus.CANDIDATE,
            candidate=c,
            metrics=c.metrics,
            filters=(c.filters[1], c.filters[0], c.filters[2]),
            target_evaluation_time=c.evaluated_at,
            processed_at=c.processed_at,
        ),
        lambda c: FvgPreEvaluation(
            FvgPreStatus.CANDIDATE,
            candidate=c,
            metrics=c.metrics,
            filters=c.filters,
            target_evaluation_time=c.evaluated_at + timedelta(seconds=1),
            processed_at=c.processed_at,
        ),
        lambda c: FvgPreEvaluation(
            FvgPreStatus.CANDIDATE,
            candidate=c,
            metrics=c.metrics,
            filters=c.filters,
            target_evaluation_time=c.evaluated_at,
        ),
        lambda c: FvgPreEvaluation(
            FvgPreStatus.CANDIDATE,
            candidate=c,
            metrics=c.metrics,
            filters=c.filters,
            target_evaluation_time=c.evaluated_at,
            processed_at=c.evaluated_at - timedelta(seconds=1),
        ),
        lambda c: FvgPreEvaluation(
            FvgPreStatus.CANDIDATE,
            candidate=c,
            metrics=c.metrics,
            filters=c.filters,
            target_evaluation_time=c.evaluated_at,
            processed_at=c.source_c_expected_close_time,
        ),
        lambda c: FvgPreEvaluation(
            FvgPreStatus.CANDIDATE,
            candidate=c,
            metrics=c.metrics,
            filters=c.filters,
            target_evaluation_time=c.evaluated_at,
            processed_at=c.processed_at + timedelta(seconds=1),
        ),
        lambda c: FvgPreEvaluation(
            FvgPreStatus.NOT_DUE,
            metrics=c.metrics,
            filters=c.filters,
        ),
        lambda c: FvgPreEvaluation(
            FvgPreStatus.REJECTED,
            metrics=c.metrics,
            filters=c.filters,
        ),
        lambda c: FvgPreEvaluation(
            FvgPreStatus.UNAVAILABLE_WITH_CURRENT_DATA,
            metrics=c.metrics,
            filters=(
                replace(c.filters[0], decision=FvgFilterDecision.FAILED),
                c.filters[1],
                replace(c.filters[2], decision=FvgFilterDecision.UNAVAILABLE),
            ),
        ),
    ],
)
def test_pre_evaluation_rejects_invalid_combinations(factory: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory(_candidate())  # type: ignore[operator]
