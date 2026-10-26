"""Streaming historical dataset validation and point-in-time instrument profiling."""

from __future__ import annotations

import csv
import hashlib
import math
import os
import sqlite3
import tempfile
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .canonical import canonical_sha256
from .contracts import (
    DatasetDescriptor,
    DatasetGateStatus,
    DatasetValidationReport,
    InstrumentProfile,
)
from .errors import DatasetRejectedError


@dataclass(frozen=True, slots=True)
class OhlcvColumns:
    timestamp: str = "open_time"
    open: str = "open"
    high: str = "high"
    low: str = "low"
    close: str = "close"
    volume: str = "volume"


@dataclass(frozen=True, slots=True)
class DatasetValidationPolicy:
    interval_seconds: int
    minimum_rows: int = 1_000
    minimum_history_days: Decimal = Decimal("30")
    maximum_gap_count: int = 0
    maximum_duplicate_count: int = 0
    maximum_out_of_order_count: int = 0
    maximum_invalid_ohlc: int = 0
    maximum_invalid_volume: int = 0
    extreme_range_fraction: Decimal = Decimal("0.35")
    allow_zero_volume: bool = True

    def __post_init__(self) -> None:
        for name in (
            "interval_seconds",
            "minimum_rows",
            "maximum_gap_count",
            "maximum_duplicate_count",
            "maximum_out_of_order_count",
            "maximum_invalid_ohlc",
            "maximum_invalid_volume",
        ):
            value = getattr(self, name)
            minimum = 1 if name in {"interval_seconds", "minimum_rows"} else 0
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                raise ValueError(f"{name} is invalid")
        if self.minimum_history_days < 0 or self.extreme_range_fraction <= 0:
            raise ValueError("dataset policy decimal limits are invalid")


@dataclass(frozen=True, slots=True)
class DatasetAnalysis:
    validation: DatasetValidationReport
    profile: InstrumentProfile | None


class DatasetAnalyzer:
    """Validate CSV OHLCV and produce a compact streaming instrument profile."""

    def __init__(
        self,
        policy: DatasetValidationPolicy,
        columns: OhlcvColumns | None = None,
    ) -> None:
        self._policy = policy
        self._columns = columns or OhlcvColumns()

    def analyze(
        self, descriptor: DatasetDescriptor, *, now: datetime | None = None
    ) -> DatasetAnalysis:
        """Run the data gate. A rejected dataset never produces an instrument profile."""

        checked_at = (now or datetime.now(UTC)).astimezone(UTC)
        if descriptor.file_format != "csv":
            return self._rejected(
                descriptor,
                checked_at,
                computed_hash="0" * 64,
                reason_codes=("UNSUPPORTED_DATASET_FORMAT",),
            )
        path = _local_path(descriptor.uri)
        try:
            computed_hash = _sha256_file(path)
        except OSError:
            return self._rejected(
                descriptor,
                checked_at,
                computed_hash="0" * 64,
                reason_codes=("DATASET_UNREADABLE",),
            )
        if computed_hash != descriptor.sha256:
            return self._rejected(
                descriptor,
                checked_at,
                computed_hash=computed_hash,
                reason_codes=("DATASET_HASH_MISMATCH",),
            )

        counters = _Counters()
        profile = _ProfileAccumulator()
        first_timestamp: datetime | None = None
        last_timestamp: datetime | None = None
        previous_timestamp: datetime | None = None
        previous_close: float | None = None
        with _timestamp_index() as index:
            try:
                with path.open("r", encoding="utf-8-sig", newline="") as handle:
                    reader = csv.DictReader(handle)
                    self._validate_header(reader.fieldnames)
                    for row in reader:
                        counters.rows += 1
                        try:
                            timestamp, open_, high, low, close, volume = self._decode_row(row)
                        except (KeyError, TypeError, ValueError, OverflowError):
                            counters.invalid_rows += 1
                            continue
                        if first_timestamp is None:
                            first_timestamp = timestamp
                        last_timestamp = timestamp
                        duplicate = not index.add(timestamp)
                        counters.duplicates += int(duplicate)
                        if previous_timestamp is not None:
                            delta = int((timestamp - previous_timestamp).total_seconds())
                            if delta < 0:
                                counters.out_of_order += 1
                            elif delta == 0:
                                pass
                            elif delta % self._policy.interval_seconds != 0:
                                counters.irregular_intervals += 1
                            elif delta > self._policy.interval_seconds:
                                counters.gaps += delta // self._policy.interval_seconds - 1
                        if not _valid_ohlc(open_, high, low, close):
                            counters.invalid_ohlc += 1
                        if volume < 0 or (volume == 0 and not self._policy.allow_zero_volume):
                            counters.invalid_volume += 1
                        if close > 0 and (high - low) / close >= float(
                            self._policy.extreme_range_fraction
                        ):
                            counters.extreme_outliers += 1
                        if (
                            not duplicate
                            and _valid_ohlc(open_, high, low, close)
                            and volume >= 0
                            and timestamp >= descriptor.period.start
                            and timestamp < descriptor.period.end
                        ):
                            profile.add(
                                timestamp=timestamp,
                                open_=open_,
                                high=high,
                                low=low,
                                close=close,
                                volume=volume,
                                previous_close=previous_close,
                            )
                        previous_timestamp = timestamp
                        previous_close = close
            except (OSError, csv.Error, UnicodeError):
                return self._rejected(
                    descriptor,
                    checked_at,
                    computed_hash=computed_hash,
                    reason_codes=("DATASET_PARSE_FAILURE",),
                )

        reasons = self._reason_codes(
            descriptor=descriptor,
            counters=counters,
            first_timestamp=first_timestamp,
            last_timestamp=last_timestamp,
        )
        status = DatasetGateStatus.REJECTED if reasons else DatasetGateStatus.ACCEPTED
        warnings: list[str] = []
        if counters.extreme_outliers:
            warnings.append("EXTREME_CANDLES_PRESENT")
            if status is DatasetGateStatus.ACCEPTED:
                status = DatasetGateStatus.ACCEPTED_WITH_WARNINGS
        report = DatasetValidationReport(
            report_id=f"dataset-report-{canonical_sha256((descriptor.fingerprint, checked_at))[:24]}",
            dataset_id=descriptor.dataset_id,
            dataset_fingerprint=descriptor.fingerprint,
            status=status,
            checked_at=checked_at,
            rows_scanned=counters.rows,
            duplicates=counters.duplicates,
            gaps=counters.gaps,
            out_of_order=counters.out_of_order,
            invalid_ohlc=counters.invalid_ohlc + counters.invalid_rows,
            invalid_volume=counters.invalid_volume,
            extreme_outliers=counters.extreme_outliers,
            computed_sha256=computed_hash,
            reason_codes=tuple(reasons),
            warnings=tuple(warnings),
            evidence={
                "first_timestamp": first_timestamp,
                "last_timestamp": last_timestamp,
                "irregular_intervals": counters.irregular_intervals,
                "descriptor_row_count": descriptor.row_count,
                "parsed_rows": counters.rows,
                "valid_profile_rows": profile.count,
            },
        )
        if status is DatasetGateStatus.REJECTED:
            return DatasetAnalysis(report, None)
        instrument_profile = profile.build(descriptor, checked_at)
        return DatasetAnalysis(report, instrument_profile)

    def require_accepted(self, descriptor: DatasetDescriptor) -> DatasetAnalysis:
        analysis = self.analyze(descriptor)
        if analysis.validation.status is DatasetGateStatus.REJECTED:
            raise DatasetRejectedError(
                "dataset rejected: " + ", ".join(analysis.validation.reason_codes)
            )
        return analysis

    def _validate_header(self, fields: Sequence[str] | None) -> None:
        required = {
            self._columns.timestamp,
            self._columns.open,
            self._columns.high,
            self._columns.low,
            self._columns.close,
            self._columns.volume,
        }
        if fields is None or not required.issubset(fields):
            raise csv.Error("required OHLCV columns are missing")

    def _decode_row(
        self, row: dict[str, str]
    ) -> tuple[datetime, float, float, float, float, float]:
        return (
            _parse_timestamp(row[self._columns.timestamp]),
            _finite_float(row[self._columns.open]),
            _finite_float(row[self._columns.high]),
            _finite_float(row[self._columns.low]),
            _finite_float(row[self._columns.close]),
            _finite_float(row[self._columns.volume]),
        )

    def _reason_codes(
        self,
        *,
        descriptor: DatasetDescriptor,
        counters: _Counters,
        first_timestamp: datetime | None,
        last_timestamp: datetime | None,
    ) -> list[str]:
        reasons: list[str] = []
        if counters.rows != descriptor.row_count:
            reasons.append("ROW_COUNT_MISMATCH")
        if counters.rows < self._policy.minimum_rows:
            reasons.append("INSUFFICIENT_ROWS")
        if first_timestamp is None or last_timestamp is None:
            reasons.append("EMPTY_OR_UNDECODABLE_DATASET")
        else:
            history_days = Decimal(str((last_timestamp - first_timestamp).total_seconds() / 86_400))
            if history_days < self._policy.minimum_history_days:
                reasons.append("INSUFFICIENT_HISTORY")
            if first_timestamp < descriptor.period.start or last_timestamp >= descriptor.period.end:
                reasons.append("PERIOD_BOUNDARY_MISMATCH")
        if counters.duplicates > self._policy.maximum_duplicate_count:
            reasons.append("DUPLICATE_TIMESTAMPS")
        if counters.gaps > self._policy.maximum_gap_count:
            reasons.append("DATA_GAPS")
        if counters.out_of_order > self._policy.maximum_out_of_order_count:
            reasons.append("OUT_OF_ORDER_TIMESTAMPS")
        if counters.irregular_intervals:
            reasons.append("IRREGULAR_INTERVALS")
        if counters.invalid_ohlc + counters.invalid_rows > self._policy.maximum_invalid_ohlc:
            reasons.append("INVALID_OHLC")
        if counters.invalid_volume > self._policy.maximum_invalid_volume:
            reasons.append("INVALID_VOLUME")
        return sorted(set(reasons))

    def _rejected(
        self,
        descriptor: DatasetDescriptor,
        checked_at: datetime,
        *,
        computed_hash: str,
        reason_codes: tuple[str, ...],
    ) -> DatasetAnalysis:
        return DatasetAnalysis(
            DatasetValidationReport(
                report_id=f"dataset-report-{canonical_sha256((descriptor.fingerprint, checked_at))[:24]}",
                dataset_id=descriptor.dataset_id,
                dataset_fingerprint=descriptor.fingerprint,
                status=DatasetGateStatus.REJECTED,
                checked_at=checked_at,
                rows_scanned=0,
                duplicates=0,
                gaps=0,
                out_of_order=0,
                invalid_ohlc=0,
                invalid_volume=0,
                extreme_outliers=0,
                computed_sha256=computed_hash,
                reason_codes=reason_codes,
            ),
            None,
        )


@dataclass(slots=True)
class _Counters:
    rows: int = 0
    duplicates: int = 0
    gaps: int = 0
    out_of_order: int = 0
    irregular_intervals: int = 0
    invalid_ohlc: int = 0
    invalid_volume: int = 0
    invalid_rows: int = 0
    extreme_outliers: int = 0


@dataclass(slots=True)
class _ProfileAccumulator:
    count: int = 0
    volume: _Moments = None  # type: ignore[assignment]
    range_fraction: _Moments = None  # type: ignore[assignment]
    returns: _Moments = None  # type: ignore[assignment]
    absolute_returns: _Moments = None  # type: ignore[assignment]
    true_range_fraction: _Moments = None  # type: ignore[assignment]
    positive_returns: int = 0
    negative_returns: int = 0
    extreme_returns: int = 0
    lag_products: float = 0.0
    previous_return: float | None = None
    hourly_returns: dict[int, _Moments] = None  # type: ignore[assignment]
    weekday_returns: dict[int, _Moments] = None  # type: ignore[assignment]
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None

    def __post_init__(self) -> None:
        self.volume = _Moments()
        self.range_fraction = _Moments()
        self.returns = _Moments()
        self.absolute_returns = _Moments()
        self.true_range_fraction = _Moments()
        self.hourly_returns = defaultdict(_Moments)
        self.weekday_returns = defaultdict(_Moments)

    def add(
        self,
        *,
        timestamp: datetime,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float,
        previous_close: float | None,
    ) -> None:
        self.count += 1
        self.first_timestamp = self.first_timestamp or timestamp
        self.last_timestamp = timestamp
        self.volume.add(volume)
        self.range_fraction.add((high - low) / close if close > 0 else 0.0)
        reference = previous_close if previous_close is not None and previous_close > 0 else open_
        true_range = max(high - low, abs(high - reference), abs(low - reference))
        self.true_range_fraction.add(true_range / close if close > 0 else 0.0)
        if previous_close is None or previous_close <= 0 or close <= 0:
            return
        result = math.log(close / previous_close)
        self.returns.add(result)
        self.absolute_returns.add(abs(result))
        self.positive_returns += int(result > 0)
        self.negative_returns += int(result < 0)
        self.extreme_returns += int(abs(result) >= 0.10)
        if self.previous_return is not None:
            self.lag_products += self.previous_return * result
        self.previous_return = result
        self.hourly_returns[timestamp.hour].add(result)
        self.weekday_returns[timestamp.weekday()].add(result)

    def build(self, descriptor: DatasetDescriptor, generated_at: datetime) -> InstrumentProfile:
        history_seconds = 0.0
        if self.first_timestamp is not None and self.last_timestamp is not None:
            history_seconds = (self.last_timestamp - self.first_timestamp).total_seconds()
        return InstrumentProfile(
            profile_id=f"instrument-profile-{canonical_sha256((descriptor.fingerprint, self.count))[:24]}",
            dataset_fingerprint=descriptor.fingerprint,
            symbol=descriptor.symbol,
            market_type=descriptor.market_type,
            generated_at=generated_at,
            history_days=Decimal(str(history_seconds / 86_400)),
            observations=self.count,
            liquidity_metrics={
                "volume_mean": self.volume.mean,
                "volume_std": self.volume.stddev,
                "volume_min": self.volume.minimum,
                "volume_max": self.volume.maximum,
            },
            volatility_metrics={
                "return_std": self.returns.stddev,
                "absolute_return_mean": self.absolute_returns.mean,
                "range_fraction_mean": self.range_fraction.mean,
                "atr_fraction_mean": self.true_range_fraction.mean,
            },
            return_distribution={
                "mean": self.returns.mean,
                "std": self.returns.stddev,
                "skew": self.returns.skewness,
                "kurtosis_excess": self.returns.kurtosis_excess,
                "positive_fraction": _fraction(self.positive_returns, self.returns.count),
                "negative_fraction": _fraction(self.negative_returns, self.returns.count),
            },
            trend_metrics={
                "directional_imbalance": _fraction(
                    self.positive_returns - self.negative_returns, self.returns.count
                ),
            },
            mean_reversion_metrics={
                "lag1_autocorrelation": self._lag1_autocorrelation(),
            },
            seasonality_metrics={
                "hourly_mean_returns": {
                    str(key): item.mean for key, item in sorted(self.hourly_returns.items())
                },
                "weekday_mean_returns": {
                    str(key): item.mean for key, item in sorted(self.weekday_returns.items())
                },
            },
            tail_risk_metrics={
                "extreme_return_count": self.extreme_returns,
                "extreme_return_fraction": _fraction(self.extreme_returns, self.returns.count),
                "minimum_return": self.returns.minimum,
                "maximum_return": self.returns.maximum,
            },
            funding_metrics={
                "available": descriptor.funding_sha256 is not None,
                "funding_hash": descriptor.funding_sha256,
            },
            cost_sensitivity={
                "turnover_proxy": self.absolute_returns.mean,
                "small_move_fraction": None,
            },
            stability_metrics={
                "profile_method": "streaming-moments-v1",
                "yearly_stability_requires_research_slices": True,
            },
        )

    def _lag1_autocorrelation(self) -> float | None:
        if self.returns.count < 3 or self.returns.variance <= 0:
            return None
        return self.lag_products / ((self.returns.count - 1) * self.returns.variance)


@dataclass(slots=True)
class _Moments:
    count: int = 0
    mean: float = 0.0
    m2: float = 0.0
    m3: float = 0.0
    m4: float = 0.0
    minimum: float | None = None
    maximum: float | None = None

    def add(self, value: float) -> None:
        previous = self.count
        self.count += 1
        delta = value - self.mean
        delta_n = delta / self.count
        delta_n2 = delta_n * delta_n
        term1 = delta * delta_n * previous
        self.mean += delta_n
        self.m4 += (
            term1 * delta_n2 * (self.count * self.count - 3 * self.count + 3)
            + 6 * delta_n2 * self.m2
            - 4 * delta_n * self.m3
        )
        self.m3 += term1 * delta_n * (self.count - 2) - 3 * delta_n * self.m2
        self.m2 += term1
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)

    @property
    def variance(self) -> float:
        return self.m2 / (self.count - 1) if self.count > 1 else 0.0

    @property
    def stddev(self) -> float:
        return math.sqrt(max(0.0, self.variance))

    @property
    def skewness(self) -> float | None:
        if self.count < 3 or self.m2 == 0:
            return None
        return float(math.sqrt(self.count) * self.m3 / (self.m2**1.5))

    @property
    def kurtosis_excess(self) -> float | None:
        if self.count < 4 or self.m2 == 0:
            return None
        return self.count * self.m4 / (self.m2 * self.m2) - 3.0


class _TimestampIndex:
    def __init__(self) -> None:
        descriptor, name = tempfile.mkstemp(prefix="strategy-factory-ts-", suffix=".sqlite3")
        os.close(descriptor)
        self._path = Path(name)
        self._connection = sqlite3.connect(self._path)
        self._connection.execute("PRAGMA journal_mode=OFF")
        self._connection.execute("PRAGMA synchronous=OFF")
        self._connection.execute("CREATE TABLE timestamps(value TEXT PRIMARY KEY) WITHOUT ROWID")

    def add(self, timestamp: datetime) -> bool:
        try:
            self._connection.execute(
                "INSERT INTO timestamps(value) VALUES (?)", (timestamp.isoformat(),)
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def close(self) -> None:
        self._connection.close()
        self._path.unlink(missing_ok=True)

    def __enter__(self) -> _TimestampIndex:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _timestamp_index() -> _TimestampIndex:
    return _TimestampIndex()


def _local_path(uri: str) -> Path:
    if uri.startswith("file://"):
        return Path(uri[7:])
    return Path(uri)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _finite_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("non-finite numeric value")
    return value


def _parse_timestamp(raw: str) -> datetime:
    stripped = raw.strip()
    try:
        numeric = float(stripped)
    except ValueError:
        parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timestamp must contain timezone") from None
        return parsed.astimezone(UTC)
    if not math.isfinite(numeric):
        raise ValueError("non-finite timestamp")
    seconds = numeric / 1000 if abs(numeric) >= 100_000_000_000 else numeric
    return datetime.fromtimestamp(seconds, UTC)


def _valid_ohlc(open_: float, high: float, low: float, close: float) -> bool:
    return (
        min(open_, high, low, close) > 0
        and high >= max(open_, close)
        and low <= min(open_, close)
        and high >= low
    )


def _fraction(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None
