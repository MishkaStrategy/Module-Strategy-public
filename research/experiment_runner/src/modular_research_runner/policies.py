from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .models import SpecificationError
from .state import FailureCategory


def _integer(value: Any, name: str, default: int, *, minimum: int = 0) -> int:
    if value is None:
        value = default
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise SpecificationError(f"{name} must be an integer >= {minimum}")
    return value


def _number(value: Any, name: str, default: float, *, minimum: float = 0.0) -> float:
    if value is None:
        value = default
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise SpecificationError(f"{name} must be numeric")
    try:
        rendered = float(value)
    except OverflowError as exc:
        raise SpecificationError(f"{name} must be finite") from exc
    if not math.isfinite(rendered):
        raise SpecificationError(f"{name} must be finite")
    if rendered < minimum:
        raise SpecificationError(f"{name} must be >= {minimum}")
    return rendered


def _direct_number(value: Any, name: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise SpecificationError(f"{name} must be numeric")
    try:
        rendered = float(value)
    except OverflowError as exc:
        raise SpecificationError(f"{name} must be finite") from exc
    if not math.isfinite(rendered):
        raise SpecificationError(f"{name} must be finite")
    return rendered


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 1
    initial_backoff_seconds: float = 0.0
    backoff_multiplier: float = 2.0
    maximum_backoff_seconds: float = 300.0
    retryable_categories: frozenset[FailureCategory] = frozenset(
        {FailureCategory.TRANSIENT, FailureCategory.RESOURCE}
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_attempts, int)
            or isinstance(self.max_attempts, bool)
            or self.max_attempts < 1
        ):
            raise SpecificationError("max_attempts must be positive")

        initial = _direct_number(
            self.initial_backoff_seconds,
            "initial_backoff_seconds",
        )
        multiplier = _direct_number(
            self.backoff_multiplier,
            "backoff_multiplier",
        )
        maximum = _direct_number(
            self.maximum_backoff_seconds,
            "maximum_backoff_seconds",
        )
        object.__setattr__(self, "initial_backoff_seconds", initial)
        object.__setattr__(self, "backoff_multiplier", multiplier)
        object.__setattr__(self, "maximum_backoff_seconds", maximum)

        if initial < 0 or maximum < 0:
            raise SpecificationError("backoff values must be non-negative")
        if multiplier < 1:
            raise SpecificationError("backoff_multiplier must be >= 1")
        if maximum < initial:
            raise SpecificationError("maximum backoff must not be below initial backoff")

        categories = self.retryable_categories
        if not isinstance(categories, set | frozenset) or any(
            not isinstance(item, FailureCategory) for item in categories
        ):
            raise SpecificationError("retryable_categories must contain FailureCategory values")
        object.__setattr__(self, "retryable_categories", frozenset(categories))

    def can_retry(self, category: FailureCategory, attempt_count: int) -> bool:
        if not isinstance(category, FailureCategory):
            raise SpecificationError("category must be a FailureCategory")
        if (
            not isinstance(attempt_count, int)
            or isinstance(attempt_count, bool)
            or attempt_count < 0
        ):
            raise SpecificationError("attempt_count must be a non-negative integer")
        return attempt_count < self.max_attempts and category in self.retryable_categories

    def delay_seconds(self, attempt_count: int) -> float:
        if (
            not isinstance(attempt_count, int)
            or isinstance(attempt_count, bool)
            or attempt_count < 1
        ):
            raise SpecificationError("attempt_count must be positive")
        try:
            value = self.initial_backoff_seconds * self.backoff_multiplier ** (attempt_count - 1)
        except OverflowError:
            return self.maximum_backoff_seconds
        return min(value, self.maximum_backoff_seconds)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> RetryPolicy:
        allowed = {
            "max_attempts",
            "initial_backoff_seconds",
            "backoff_multiplier",
            "maximum_backoff_seconds",
            "retryable_categories",
        }
        unknown = set(value) - allowed
        if unknown:
            raise SpecificationError(f"unknown retry_policy fields: {sorted(unknown)}")
        raw_categories = value.get("retryable_categories", ["TRANSIENT", "RESOURCE"])
        if not isinstance(raw_categories, list | tuple | set | frozenset):
            raise SpecificationError("retryable_categories must be a sequence")
        try:
            categories = frozenset(FailureCategory(str(item).upper()) for item in raw_categories)
        except ValueError as exc:
            raise SpecificationError("retry_policy contains an unknown failure category") from exc
        return cls(
            max_attempts=_integer(value.get("max_attempts"), "max_attempts", 1, minimum=1),
            initial_backoff_seconds=_number(
                value.get("initial_backoff_seconds"),
                "initial_backoff_seconds",
                0.0,
            ),
            backoff_multiplier=_number(
                value.get("backoff_multiplier"),
                "backoff_multiplier",
                2.0,
                minimum=1.0,
            ),
            maximum_backoff_seconds=_number(
                value.get("maximum_backoff_seconds"),
                "maximum_backoff_seconds",
                300.0,
            ),
            retryable_categories=categories,
        )


@dataclass(frozen=True, slots=True)
class ResumePolicy:
    lease_seconds: int = 900
    heartbeat_seconds: int = 60
    recover_expired_leases: bool = True
    verify_success_artifacts: bool = True

    def __post_init__(self) -> None:
        if (
            not isinstance(self.lease_seconds, int)
            or isinstance(self.lease_seconds, bool)
            or self.lease_seconds < 1
        ):
            raise SpecificationError("lease_seconds must be positive")
        if (
            not isinstance(self.heartbeat_seconds, int)
            or isinstance(self.heartbeat_seconds, bool)
            or self.heartbeat_seconds < 1
            or self.heartbeat_seconds >= self.lease_seconds
        ):
            raise SpecificationError("heartbeat_seconds must be positive and below lease_seconds")
        if not isinstance(self.recover_expired_leases, bool):
            raise SpecificationError("recover_expired_leases must be boolean")
        if not isinstance(self.verify_success_artifacts, bool):
            raise SpecificationError("verify_success_artifacts must be boolean")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ResumePolicy:
        allowed = {
            "lease_seconds",
            "heartbeat_seconds",
            "recover_expired_leases",
            "verify_success_artifacts",
        }
        unknown = set(value) - allowed
        if unknown:
            raise SpecificationError(f"unknown resume_policy fields: {sorted(unknown)}")
        for boolean_name in ("recover_expired_leases", "verify_success_artifacts"):
            if boolean_name in value and not isinstance(value[boolean_name], bool):
                raise SpecificationError(f"{boolean_name} must be boolean")
        return cls(
            lease_seconds=_integer(value.get("lease_seconds"), "lease_seconds", 900, minimum=1),
            heartbeat_seconds=_integer(
                value.get("heartbeat_seconds"), "heartbeat_seconds", 60, minimum=1
            ),
            recover_expired_leases=value.get("recover_expired_leases", True),
            verify_success_artifacts=value.get("verify_success_artifacts", True),
        )
