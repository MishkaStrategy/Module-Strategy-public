from __future__ import annotations

import math
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from .canonical import canonical_sha256
from .errors import ValidationError


@dataclass(frozen=True, slots=True)
class OperationSample:
    operation: str
    duration_seconds: float
    status: str
    row_count: int = 0
    byte_count: int = 0

    def __post_init__(self) -> None:
        if not self.operation or not self.status:
            raise ValidationError("operation and status are required")
        if (
            isinstance(self.duration_seconds, bool)
            or not isinstance(self.duration_seconds, (int, float))
            or not math.isfinite(self.duration_seconds)
            or self.duration_seconds < 0
        ):
            raise ValidationError("duration_seconds must be finite and non-negative")
        for name in ("row_count", "byte_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValidationError("operation counts must be non-negative integers")


@dataclass(frozen=True, slots=True)
class OperationSummary:
    operation: str
    samples: int
    failures: int
    rows: int
    bytes: int
    p50_seconds: float
    p95_seconds: float
    p99_seconds: float
    maximum_seconds: float


@dataclass(frozen=True, slots=True)
class OperationalMetricsSnapshot:
    summaries: tuple[OperationSummary, ...]
    sample_count: int
    snapshot_hash: str


class OperationalMetrics:
    """Thread-safe local operational telemetry without sensitive payload logging."""

    def __init__(self, *, max_samples_per_operation: int = 100_000) -> None:
        if (
            isinstance(max_samples_per_operation, bool)
            or not isinstance(max_samples_per_operation, int)
            or max_samples_per_operation < 1
        ):
            raise ValueError("max_samples_per_operation must be a positive integer")
        self._max_samples = max_samples_per_operation
        self._samples: dict[str, list[OperationSample]] = defaultdict(list)
        self._lock = threading.Lock()

    def record(self, sample: OperationSample) -> None:
        with self._lock:
            values = self._samples[sample.operation]
            if len(values) >= self._max_samples:
                values.pop(0)
            values.append(sample)

    @contextmanager
    def measure(
        self,
        operation: str,
        *,
        row_count: int = 0,
        byte_count: int = 0,
    ) -> Iterator[None]:
        started = time.perf_counter()
        status = "PASS"
        try:
            yield
        except BaseException:
            status = "FAIL"
            raise
        finally:
            self.record(
                OperationSample(
                    operation=operation,
                    duration_seconds=time.perf_counter() - started,
                    status=status,
                    row_count=row_count,
                    byte_count=byte_count,
                )
            )

    def snapshot(self) -> OperationalMetricsSnapshot:
        with self._lock:
            copied = {key: tuple(values) for key, values in self._samples.items()}
        summaries: list[OperationSummary] = []
        for operation in sorted(copied):
            samples = copied[operation]
            durations = sorted(item.duration_seconds for item in samples)
            summaries.append(
                OperationSummary(
                    operation=operation,
                    samples=len(samples),
                    failures=sum(item.status != "PASS" for item in samples),
                    rows=sum(item.row_count for item in samples),
                    bytes=sum(item.byte_count for item in samples),
                    p50_seconds=_percentile(durations, 0.50),
                    p95_seconds=_percentile(durations, 0.95),
                    p99_seconds=_percentile(durations, 0.99),
                    maximum_seconds=max(durations, default=0.0),
                )
            )
        sample_count = sum(item.samples for item in summaries)
        payload = {"summaries": tuple(summaries), "sample_count": sample_count}
        return OperationalMetricsSnapshot(
            summaries=tuple(summaries),
            sample_count=sample_count,
            snapshot_hash=canonical_sha256(payload),
        )


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    index = max(0, math.ceil(len(values) * fraction) - 1)
    return values[index]
