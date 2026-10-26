from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

import pytest

from conftest import bearish_triplet, bullish_triplet, candle
from modular_strategy_fvg import (
    EVALUATION_EVENT_KEY,
    PRE_CANDIDATE_KEY,
    PRE_STATUS_KEY,
    FvgConfig,
    FvgModule,
    FvgPreEvaluation,
    FvgPreStatus,
    evaluate_fvg_window,
    evaluate_pre_fvg,
)

UTC = timezone.utc


def _pre_result() -> FvgPreEvaluation:
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
    assert result.candidate is not None
    return result


def _accepted_evaluation():
    a, b, c = bullish_triplet()
    return evaluate_fvg_window(
        a=a,
        b=b,
        c=c,
        config=FvgConfig(timeframes=("15m",)),
    )


@dataclass(frozen=True)
class _IdentityBar:
    symbol: object = "BTCUSDT"
    timeframe: object = "15m"
    timestamp: object = datetime(2024, 1, 1, tzinfo=UTC)
    close_time: object = datetime(2024, 1, 1, 0, 15, tzinfo=UTC)


def test_feature_bar_identity_rejects_noncanonical_identity() -> None:
    with pytest.raises(TypeError, match="canonical strings"):
        FvgModule._feature_bar_identity(_IdentityBar(symbol=1), "test")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="must be datetimes"):
        FvgModule._feature_bar_identity(_IdentityBar(timestamp="bad"), "test")  # type: ignore[arg-type]


def test_pre_evaluation_feature_candidate_and_status() -> None:
    module = FvgModule(
        timeframes=("15m",),
        pre_fvg_enabled=True,
        pre_fvg_evaluation_offset="5m",
    )
    a, b, c = bullish_triplet()
    result = _pre_result()
    candidate = result.candidate
    assert candidate is not None

    feature = module._pre_evaluation_feature(result, a, b, c.close_time)
    assert feature.key == PRE_CANDIDATE_KEY
    assert feature.feature_id == candidate.candidate_id
    assert feature.created_at <= c.close_time
    assert feature.valid_until == candidate.source_c_expected_close_time
    assert feature.value["event_type"] == "pre_fvg"
    assert feature.value["instrument"] == "BTCUSDT"
    assert feature.metadata["provisional"] is True

    not_due = FvgPreEvaluation(status=FvgPreStatus.NOT_DUE)
    status_feature = module._pre_evaluation_feature(not_due, a, b, c.close_time)
    assert status_feature.key == PRE_STATUS_KEY
    assert status_feature.feature_id.startswith("pre-fvg:status:")
    assert status_feature.valid_until == c.close_time
    assert status_feature.value["event_type"] == "pre_fvg"

    with pytest.raises(ValueError, match="share symbol/timeframe"):
        module._pre_evaluation_feature(result, a, replace(b, symbol="ETHUSDT"), c.close_time)


def _resolution_outcome(evaluation, trigger, candidate) -> tuple[str, object, tuple[str, ...]]:
    module = FvgModule(timeframes=("15m",))
    features, resolved = module._pre_conversion_features(
        evaluation=evaluation,
        trigger_bar=trigger,
        visible_until=trigger.close_time,
        candidates={candidate.candidate_id: candidate},
    )
    assert len(features) == 1
    feature = features[0]
    assert feature.key == PRE_STATUS_KEY
    assert feature.value["event_type"] == "pre_fvg_resolution"
    assert feature.metadata["provisional"] is False
    return feature.value["outcome"], feature.value["confirmed_zone_id"], resolved


def test_pre_conversion_features_all_resolution_outcomes() -> None:
    candidate = _pre_result().candidate
    assert candidate is not None
    a, b, c = bullish_triplet()

    accepted = _accepted_evaluation()
    outcome, zone_id, resolved = _resolution_outcome(accepted, c, candidate)
    assert outcome == "confirmed"
    assert zone_id == accepted.zone.zone_id  # type: ignore[union-attr]
    assert resolved == (candidate.candidate_id,)

    next_bar = candle(3, open_=107, high=112, low=106, close=111)
    assert _resolution_outcome(None, next_bar, candidate)[0] == "confirmation_bar_missing"

    ba, bb, bc = bearish_triplet()
    opposite = evaluate_fvg_window(
        a=ba,
        b=bb,
        c=bc,
        config=FvgConfig(timeframes=("15m",)),
    )
    assert _resolution_outcome(opposite, c, candidate)[0] == "disappeared"

    unavailable = evaluate_fvg_window(
        a=a,
        b=b,
        c=c,
        available_at=c.close_time - timedelta(seconds=1),
        config=FvgConfig(timeframes=("15m",)),
    )
    assert _resolution_outcome(unavailable, c, candidate)[0] == "confirmed_unavailable"

    filtered = evaluate_fvg_window(
        a=a,
        b=b,
        c=c,
        config=FvgConfig.from_parameters(
            timeframes=("15m",),
            gap_size_mode="absolute",
            min_gap_size="1000",
        ),
    )
    assert filtered.metrics is not None
    assert _resolution_outcome(filtered, c, candidate)[0] == "confirmed_filtered"
    assert _resolution_outcome(None, c, candidate)[0] == "disappeared"

    module = FvgModule(timeframes=("15m",))
    features, resolved = module._pre_conversion_features(
        evaluation=accepted,
        trigger_bar=replace(c, symbol="ETHUSDT"),
        visible_until=c.close_time,
        candidates={candidate.candidate_id: candidate},
    )
    assert features == ()
    assert resolved == ()


def test_evaluation_feature_is_deterministic_and_bounded() -> None:
    module = FvgModule(
        timeframes=("15m",),
        publish_evaluation_events=True,
        source_name="test-source",
    )
    _, _, c = bullish_triplet()
    evaluation = _accepted_evaluation()
    visible_until = c.close_time - timedelta(seconds=1)

    first = module._evaluation_feature(evaluation, c, visible_until)
    second = module._evaluation_feature(evaluation, c, visible_until)
    assert first == second
    assert first.key == EVALUATION_EVENT_KEY
    assert first.created_at == visible_until
    assert first.valid_until == visible_until
    assert first.feature_id.startswith("fvg:evaluation:")
    assert first.value["source_name"] == "test-source"
    assert first.value["module_version"] == "0.3.0"
    assert first.metadata["evaluation_status"] == evaluation.status.value
