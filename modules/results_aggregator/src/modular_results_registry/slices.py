from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Iterable, Mapping

from .canonical import canonical_sha256
from .errors import ValidationError
from .models import nonblank, utc, validate_id


class SliceDimension(StrEnum):
    INSTRUMENT = "INSTRUMENT"
    PERIOD_YEAR = "PERIOD_YEAR"
    PERIOD_QUARTER = "PERIOD_QUARTER"
    PERIOD_MONTH = "PERIOD_MONTH"
    SIDE = "SIDE"
    REGIME = "REGIME"
    MODULE = "MODULE"
    SETUP = "SETUP"
    EXIT_REASON = "EXIT_REASON"


class SliceAvailability(StrEnum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


def _decimal(
    name: str, value: Decimal | int | float | str | None, *, default: Decimal | None = None
) -> Decimal | None:
    if value is None:
        return default
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{name} must be decimal-compatible") from exc
    if not result.is_finite():
        raise ValidationError(f"{name} must be finite")
    return result


@dataclass(frozen=True, slots=True)
class TradeFact:
    trade_id: str
    run_id: str
    instrument: str
    side: str
    entry_time: datetime
    exit_time: datetime
    net_pnl: Decimal | int | float | str
    fees: Decimal | int | float | str = Decimal("0")
    r_multiple: Decimal | int | float | str | None = None
    module: str | None = None
    setup: str | None = None
    regime: str | None = None
    exit_reason: str | None = None
    source_artifact_hash: str | None = None

    def __post_init__(self) -> None:
        validate_id("trade_id", self.trade_id)
        validate_id("run_id", self.run_id)
        nonblank("instrument", self.instrument)
        if self.side not in {"LONG", "SHORT"}:
            raise ValidationError("side must be LONG or SHORT")
        object.__setattr__(self, "entry_time", utc(self.entry_time, "entry_time"))
        object.__setattr__(self, "exit_time", utc(self.exit_time, "exit_time"))
        if self.exit_time < self.entry_time:
            raise ValidationError("exit_time precedes entry_time")
        object.__setattr__(self, "net_pnl", _decimal("net_pnl", self.net_pnl))
        object.__setattr__(self, "fees", _decimal("fees", self.fees, default=Decimal("0")))
        object.__setattr__(self, "r_multiple", _decimal("r_multiple", self.r_multiple))
        for name in ("module", "setup", "regime", "exit_reason"):
            value = getattr(self, name)
            if value is not None:
                nonblank(name, value)
        if self.source_artifact_hash is not None:
            from .models import validate_sha256

            validate_sha256("source_artifact_hash", self.source_artifact_hash)


@dataclass(frozen=True, slots=True)
class SliceSummary:
    key: str
    trade_count: int
    winning_trades: int
    losing_trades: int
    breakeven_trades: int
    net_pnl: Decimal
    fees: Decimal
    average_r_multiple: Decimal | None
    r_sample_count: int
    source_run_count: int

    def __post_init__(self) -> None:
        nonblank("key", self.key)
        for name in (
            "trade_count",
            "winning_trades",
            "losing_trades",
            "breakeven_trades",
            "r_sample_count",
            "source_run_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValidationError(f"{name} must be a non-negative integer")
        if self.winning_trades + self.losing_trades + self.breakeven_trades != self.trade_count:
            raise ValidationError("trade outcome counts do not add up")
        if self.r_sample_count > self.trade_count:
            raise ValidationError("r_sample_count cannot exceed trade_count")
        if not self.net_pnl.is_finite() or not self.fees.is_finite():
            raise ValidationError("slice monetary values must be finite")
        if self.average_r_multiple is not None and not self.average_r_multiple.is_finite():
            raise ValidationError("average_r_multiple must be finite")


@dataclass(frozen=True, slots=True)
class SliceResult:
    dimension: SliceDimension
    availability: SliceAvailability
    summaries: tuple[SliceSummary, ...]
    total_input_trades: int
    included_trades: int
    missing_label_trades: int
    transform_version: str = "slice-engine.v1"
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("total_input_trades", "included_trades", "missing_label_trades"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValidationError("slice counts must be non-negative integers")
        if self.included_trades + self.missing_label_trades != self.total_input_trades:
            raise ValidationError("slice accounting does not reconcile")
        if tuple(sorted((summary.key for summary in self.summaries))) != tuple(
            summary.key for summary in self.summaries
        ):
            raise ValidationError("slice summaries must be sorted by key")
        if self.availability is SliceAvailability.UNAVAILABLE and self.summaries:
            raise ValidationError("unavailable slice cannot contain summaries")

    @property
    def content_hash(self) -> str:
        return canonical_sha256(self)


@dataclass(slots=True)
class _Bucket:
    trade_count: int = 0
    wins: int = 0
    losses: int = 0
    breakevens: int = 0
    net_pnl: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    r_sum: Decimal = Decimal("0")
    r_count: int = 0
    run_ids: set[str] = field(default_factory=set)

    def add(self, trade: TradeFact) -> None:
        self.trade_count += 1
        assert isinstance(trade.net_pnl, Decimal)
        assert isinstance(trade.fees, Decimal)
        self.net_pnl += trade.net_pnl
        self.fees += trade.fees
        if trade.net_pnl > 0:
            self.wins += 1
        elif trade.net_pnl < 0:
            self.losses += 1
        else:
            self.breakevens += 1
        if trade.r_multiple is not None:
            assert isinstance(trade.r_multiple, Decimal)
            self.r_sum += trade.r_multiple
            self.r_count += 1
        self.run_ids.add(trade.run_id)

    def summary(self, key: str) -> SliceSummary:
        return SliceSummary(
            key=key,
            trade_count=self.trade_count,
            winning_trades=self.wins,
            losing_trades=self.losses,
            breakeven_trades=self.breakevens,
            net_pnl=self.net_pnl,
            fees=self.fees,
            average_r_multiple=(self.r_sum / self.r_count if self.r_count else None),
            r_sample_count=self.r_count,
            source_run_count=len(self.run_ids),
        )


_LABEL_DIMENSIONS = {
    SliceDimension.REGIME,
    SliceDimension.MODULE,
    SliceDimension.SETUP,
    SliceDimension.EXIT_REASON,
}


class SliceAccumulator:
    """Streaming derived summaries over immutable trade facts.

    This component intentionally does not compute official strategy metrics such as
    Profit Factor, drawdown, expectancy, or returns. Those remain owned by the
    registered metric subsystem.
    """

    def __init__(self, dimension: SliceDimension) -> None:
        self.dimension = dimension
        self._buckets: dict[str, _Bucket] = {}
        self._total = 0
        self._missing = 0

    def add(self, trade: TradeFact) -> None:
        self._total += 1
        key = self._key(trade)
        if key is None:
            self._missing += 1
            return
        self._buckets.setdefault(key, _Bucket()).add(trade)

    def extend(self, trades: Iterable[TradeFact]) -> None:
        for trade in trades:
            self.add(trade)

    def result(self) -> SliceResult:
        included = self._total - self._missing
        limitations: tuple[str, ...]
        if self._total == 0:
            availability = SliceAvailability.AVAILABLE
            limitations = ("NO_TRADES",)
        elif included == 0:
            availability = SliceAvailability.UNAVAILABLE
            limitations = (f"{self.dimension.value}_ANALYSIS_UNAVAILABLE",)
        elif self._missing:
            availability = SliceAvailability.PARTIAL
            limitations = (f"{self._missing} trades omitted because verified labels were absent",)
        else:
            availability = SliceAvailability.AVAILABLE
            limitations = ()
        summaries = tuple(self._buckets[key].summary(key) for key in sorted(self._buckets))
        return SliceResult(
            dimension=self.dimension,
            availability=availability,
            summaries=summaries,
            total_input_trades=self._total,
            included_trades=included,
            missing_label_trades=self._missing,
            limitations=limitations,
        )

    def snapshot(self) -> Mapping[str, object]:
        buckets = {
            key: {
                "trade_count": bucket.trade_count,
                "wins": bucket.wins,
                "losses": bucket.losses,
                "breakevens": bucket.breakevens,
                "net_pnl": str(bucket.net_pnl),
                "fees": str(bucket.fees),
                "r_sum": str(bucket.r_sum),
                "r_count": bucket.r_count,
                "run_ids": tuple(sorted(bucket.run_ids)),
            }
            for key, bucket in sorted(self._buckets.items())
        }
        return MappingProxyType(
            {
                "schema_version": "slice-accumulator.v1",
                "dimension": self.dimension.value,
                "total": self._total,
                "missing": self._missing,
                "buckets": MappingProxyType(buckets),
            }
        )

    @classmethod
    def restore(cls, snapshot: Mapping[str, object]) -> SliceAccumulator:
        if snapshot.get("schema_version") != "slice-accumulator.v1":
            raise ValidationError("unsupported slice accumulator snapshot")
        try:
            accumulator = cls(SliceDimension(str(snapshot["dimension"])))
            total = snapshot["total"]
            missing = snapshot["missing"]
            if (
                isinstance(total, bool)
                or not isinstance(total, int)
                or isinstance(missing, bool)
                or not isinstance(missing, int)
            ):
                raise TypeError("slice counters must be integers")
            accumulator._total = total
            accumulator._missing = missing
            raw_buckets = snapshot["buckets"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("malformed slice accumulator snapshot") from exc
        if not isinstance(raw_buckets, Mapping):
            raise ValidationError("slice buckets must be a mapping")
        for key, raw in raw_buckets.items():
            if not isinstance(key, str) or not isinstance(raw, Mapping):
                raise ValidationError("malformed slice bucket")
            try:
                bucket = _Bucket(
                    trade_count=int(raw["trade_count"]),
                    wins=int(raw["wins"]),
                    losses=int(raw["losses"]),
                    breakevens=int(raw["breakevens"]),
                    net_pnl=Decimal(str(raw["net_pnl"])),
                    fees=Decimal(str(raw["fees"])),
                    r_sum=Decimal(str(raw["r_sum"])),
                    r_count=int(raw["r_count"]),
                    run_ids=set(str(item) for item in raw["run_ids"]),
                )
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise ValidationError("malformed slice bucket") from exc
            accumulator._buckets[key] = bucket
        result = accumulator.result()
        if sum(item.trade_count for item in result.summaries) != result.included_trades:
            raise ValidationError("slice snapshot does not reconcile")
        return accumulator

    def _key(self, trade: TradeFact) -> str | None:
        if self.dimension is SliceDimension.INSTRUMENT:
            return trade.instrument
        if self.dimension is SliceDimension.SIDE:
            return trade.side
        if self.dimension is SliceDimension.PERIOD_YEAR:
            return f"{trade.exit_time.year:04d}"
        if self.dimension is SliceDimension.PERIOD_QUARTER:
            return f"{trade.exit_time.year:04d}-Q{(trade.exit_time.month - 1) // 3 + 1}"
        if self.dimension is SliceDimension.PERIOD_MONTH:
            return f"{trade.exit_time.year:04d}-{trade.exit_time.month:02d}"
        if self.dimension is SliceDimension.REGIME:
            return trade.regime
        if self.dimension is SliceDimension.MODULE:
            return trade.module
        if self.dimension is SliceDimension.SETUP:
            return trade.setup
        if self.dimension is SliceDimension.EXIT_REASON:
            return trade.exit_reason
        raise AssertionError(f"unhandled slice dimension: {self.dimension}")


def build_slice(trades: Iterable[TradeFact], dimension: SliceDimension) -> SliceResult:
    accumulator = SliceAccumulator(dimension)
    accumulator.extend(trades)
    return accumulator.result()
