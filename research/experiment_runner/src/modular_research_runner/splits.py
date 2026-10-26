from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from .canonical import canonical_sha256
from .models import (
    ChronologicalSplitPolicy,
    SpecificationError,
    SplitMode,
    TimeRange,
    WalkForwardMode,
    WalkForwardPolicy,
)


@dataclass(frozen=True, slots=True)
class PlannedSplit:
    split_id: str
    train: TimeRange
    validation: TimeRange
    test: TimeRange
    final_untouched_test: TimeRange | None = None

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    window_id: str
    train: TimeRange
    validation: TimeRange
    oos: TimeRange

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(self)


def _require_gap(left: TimeRange, right: TimeRange, gap_seconds: int) -> None:
    required = left.end + timedelta(seconds=gap_seconds)
    if right.start < required:
        raise SpecificationError("chronological splits overlap or violate the configured gap")


def plan_chronological_split(
    policy: ChronologicalSplitPolicy,
    overall: TimeRange,
) -> PlannedSplit:
    gap = timedelta(seconds=policy.gap_seconds)
    if policy.mode is SplitMode.FIXED:
        if policy.train is None or policy.validation is None or policy.test is None:
            raise SpecificationError("fixed split requires train, validation and test ranges")
        train = policy.train
        validation = policy.validation
        test = policy.test
        final = policy.final_untouched_test
    else:
        if policy.train_fraction is None or policy.validation_fraction is None:
            raise SpecificationError("percentage split requires train and validation fractions")
        duration_us = int((overall.end - overall.start).total_seconds() * 1_000_000)
        gap_us = policy.gap_seconds * 1_000_000
        final_count = int(duration_us * policy.final_fraction)
        train_count = int(duration_us * policy.train_fraction)
        validation_count = int(duration_us * policy.validation_fraction)
        usable = duration_us - gap_us * (3 if policy.final_fraction else 2)
        if usable <= 0:
            raise SpecificationError("split gaps consume the entire date range")
        scale = usable / duration_us
        train_count = int(train_count * scale)
        validation_count = int(validation_count * scale)
        final_count = int(final_count * scale)
        cursor = overall.start
        train = TimeRange(cursor, cursor + timedelta(microseconds=train_count))
        cursor = train.end + gap
        validation = TimeRange(cursor, cursor + timedelta(microseconds=validation_count))
        cursor = validation.end + gap
        final = None
        if policy.final_fraction:
            test_end = overall.end - gap - timedelta(microseconds=final_count)
            test = TimeRange(cursor, test_end)
            final = TimeRange(test.end + gap, overall.end)
        else:
            test = TimeRange(cursor, overall.end)
    ranges = [train, validation, test]
    if final is not None:
        ranges.append(final)
    if ranges[0].start < overall.start or ranges[-1].end > overall.end:
        raise SpecificationError("split ranges must be contained in the research date range")
    for left, right in zip(ranges, ranges[1:], strict=False):
        _require_gap(left, right, policy.gap_seconds)
    digest = canonical_sha256(
        {"train": train, "validation": validation, "test": test, "final": final}
    )
    return PlannedSplit(
        split_id=f"split-{digest[:20]}",
        train=train,
        validation=validation,
        test=test,
        final_untouched_test=final,
    )


def plan_walk_forward(
    policy: WalkForwardPolicy,
    overall: TimeRange,
) -> tuple[WalkForwardWindow, ...]:
    train_delta = timedelta(seconds=policy.train_seconds)
    validation_delta = timedelta(seconds=policy.validation_seconds)
    oos_delta = timedelta(seconds=policy.oos_seconds)
    step_delta = timedelta(seconds=policy.step_seconds)
    purge = timedelta(seconds=policy.purge_seconds)
    embargo = timedelta(seconds=policy.embargo_seconds)
    anchor = overall.start
    cursor = overall.start
    windows: list[WalkForwardWindow] = []
    while True:
        train_start = anchor if policy.mode is WalkForwardMode.ANCHORED else cursor
        train_end = cursor + train_delta
        if policy.mode is WalkForwardMode.ANCHORED:
            train_end = cursor + train_delta
        validation_start = train_end + purge
        validation_end = validation_start + validation_delta
        oos_start = validation_end + embargo
        oos_end = oos_start + oos_delta
        if oos_end > overall.end:
            break
        if (train_end - train_start).total_seconds() < policy.minimum_history_seconds:
            cursor += step_delta
            continue
        raw = {
            "train": TimeRange(train_start, train_end),
            "validation": TimeRange(validation_start, validation_end),
            "oos": TimeRange(oos_start, oos_end),
        }
        digest = canonical_sha256(raw)
        windows.append(
            WalkForwardWindow(
                window_id=f"wf-{len(windows):04d}-{digest[:16]}",
                train=raw["train"],
                validation=raw["validation"],
                oos=raw["oos"],
            )
        )
        cursor += step_delta
    if not windows:
        raise SpecificationError("walk-forward policy produced no complete OOS windows")
    return tuple(windows)
