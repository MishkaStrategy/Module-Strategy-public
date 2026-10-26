from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .config import WaveAnalysisConfig, timeframe_seconds
from .exceptions import WaveAnalysisStateError
from .models import PivotPoint, TimeframeState, WaveLeg

STATE_SCHEMA_VERSION = "1"


def _datetime(value: object, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise WaveAnalysisStateError(f"{name} must be an ISO datetime string or null")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise WaveAnalysisStateError(f"invalid datetime in state: {name}") from exc
    if parsed.tzinfo is None:
        raise WaveAnalysisStateError(f"state datetime must be timezone-aware: {name}")
    return parsed.astimezone(UTC)


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WaveAnalysisStateError(f"{name} must be a non-negative integer")
    return value


def _finite_decimal(value: Decimal, name: str, *, positive: bool = False) -> None:
    if not value.is_finite() or (value <= 0 if positive else value < 0):
        qualifier = "positive" if positive else "non-negative"
        raise WaveAnalysisStateError(f"{name} must be a finite {qualifier} decimal")


def _segment_id(metadata: Mapping[str, object], name: str) -> int:
    raw = metadata.get("segment_id", 0)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise WaveAnalysisStateError(f"{name} segment_id must be a non-negative integer")
    return raw


@dataclass(slots=True)
class RuntimeState:
    timeframes: dict[str, TimeframeState] = field(default_factory=dict)
    bound_symbol: str | None = None
    last_base_open_time: datetime | None = None
    last_base_fingerprint: str | None = None
    processed_base_events: int = 0
    duplicate_base_events: int = 0
    conflicting_duplicates: int = 0
    out_of_order_events: int = 0
    gap_events: int = 0
    publications: int = 0

    @classmethod
    def empty(cls, config: WaveAnalysisConfig, *, symbol: str | None = None) -> RuntimeState:
        return cls(
            timeframes={tf: TimeframeState(timeframe=tf, symbol=symbol) for tf in config.timeframes},
            bound_symbol=symbol,
        )

    def to_mapping(self, config: WaveAnalysisConfig) -> Mapping[str, object]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "config_signature": config.signature,
            "bound_symbol": self.bound_symbol,
            "last_base_open_time": None if self.last_base_open_time is None else self.last_base_open_time.isoformat(),
            "last_base_fingerprint": self.last_base_fingerprint,
            "processed_base_events": self.processed_base_events,
            "duplicate_base_events": self.duplicate_base_events,
            "conflicting_duplicates": self.conflicting_duplicates,
            "out_of_order_events": self.out_of_order_events,
            "gap_events": self.gap_events,
            "publications": self.publications,
            "timeframes": {k: dict(self.timeframes[k].to_mapping()) for k in sorted(self.timeframes)},
        }

    @property
    def digest(self) -> str:
        enc = json.dumps(self._digest_payload(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(enc).hexdigest()

    def _digest_payload(self) -> Mapping[str, object]:
        return {
            "bound_symbol": self.bound_symbol,
            "last_base_open_time": None if self.last_base_open_time is None else self.last_base_open_time.isoformat(),
            "last_base_fingerprint": self.last_base_fingerprint,
            "processed_base_events": self.processed_base_events,
            "gap_events": self.gap_events,
            "publications": self.publications,
            "timeframes": {k: dict(self.timeframes[k].to_mapping()) for k in sorted(self.timeframes)},
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object], config: WaveAnalysisConfig) -> RuntimeState:
        allowed = {
            "schema_version",
            "config_signature",
            "bound_symbol",
            "last_base_open_time",
            "last_base_fingerprint",
            "processed_base_events",
            "duplicate_base_events",
            "conflicting_duplicates",
            "out_of_order_events",
            "gap_events",
            "publications",
            "timeframes",
        }
        unknown = sorted(set(payload) - allowed)
        missing = sorted(allowed - set(payload))
        if unknown or missing:
            details = []
            if unknown:
                details.append("unknown keys: " + ", ".join(unknown))
            if missing:
                details.append("missing keys: " + ", ".join(missing))
            raise WaveAnalysisStateError("invalid state shape; " + "; ".join(details))
        if payload["schema_version"] != STATE_SCHEMA_VERSION:
            raise WaveAnalysisStateError(
                f"unsupported state schema {payload['schema_version']!r}; expected {STATE_SCHEMA_VERSION!r}"
            )
        if payload["config_signature"] != config.signature:
            raise WaveAnalysisStateError("state configuration fingerprint does not match module configuration")
        symbol = payload["bound_symbol"]
        if symbol is not None and (not isinstance(symbol, str) or not symbol.strip()):
            raise WaveAnalysisStateError("bound_symbol must be null or a non-empty string")
        fp = payload["last_base_fingerprint"]
        if fp is not None and (not isinstance(fp, str) or not fp):
            raise WaveAnalysisStateError("last_base_fingerprint must be null or a non-empty string")
        raw = payload["timeframes"]
        if not isinstance(raw, Mapping):
            raise WaveAnalysisStateError("timeframes state must be a mapping")
        if set(raw) != set(config.timeframes):
            raise WaveAnalysisStateError("checkpoint timeframe set does not match configuration")
        state = cls.empty(config, symbol=None if symbol is None else str(symbol))
        state.last_base_open_time = _datetime(payload["last_base_open_time"], "last_base_open_time")
        state.last_base_fingerprint = None if fp is None else str(fp)
        state.processed_base_events = _nonnegative_int(payload["processed_base_events"], "processed_base_events")
        state.duplicate_base_events = _nonnegative_int(payload["duplicate_base_events"], "duplicate_base_events")
        state.conflicting_duplicates = _nonnegative_int(payload["conflicting_duplicates"], "conflicting_duplicates")
        state.out_of_order_events = _nonnegative_int(payload["out_of_order_events"], "out_of_order_events")
        state.gap_events = _nonnegative_int(payload["gap_events"], "gap_events")
        state.publications = _nonnegative_int(payload["publications"], "publications")
        try:
            state.timeframes = {tf: TimeframeState.from_mapping(dict(raw[tf])) for tf in config.timeframes}
            state._validate(config)
        except WaveAnalysisStateError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise WaveAnalysisStateError("invalid nested timeframe state") from exc
        return state

    def _validate(self, config: WaveAnalysisConfig) -> None:
        if (self.last_base_open_time is None) != (self.last_base_fingerprint is None):
            raise WaveAnalysisStateError("base event identity is partially saved")
        for timeframe, item in self.timeframes.items():
            if item.timeframe != timeframe:
                raise WaveAnalysisStateError("timeframe state key does not match embedded timeframe")
            for field_name in (
                "bar_offset",
                "processed_bars",
                "recovery_count",
                "segment_id",
                "revision_counter",
                "publications",
                "duplicate_events",
                "gap_events",
            ):
                value = getattr(item, field_name)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise WaveAnalysisStateError(f"{field_name} must be a non-negative integer")
            if item.symbol != self.bound_symbol:
                raise WaveAnalysisStateError("timeframe state symbol does not match module binding")
            if len(item.bars) > config.maximum_retained_bars:
                raise WaveAnalysisStateError("retained bar state exceeds configured capacity")
            if len(item.confirmed_pivots) > config.maximum_retained_pivots:
                raise WaveAnalysisStateError("retained pivot state exceeds configured capacity")
            if len(item.wave_legs) > config.maximum_retained_waves:
                raise WaveAnalysisStateError("retained wave state exceeds configured capacity")
            if len(item.hypotheses) > config.maximum_hypotheses:
                raise WaveAnalysisStateError("hypothesis state exceeds configured capacity")
            if len(item.tr_seed) > config.atr_period:
                raise WaveAnalysisStateError("ATR seed exceeds configured period")
            if len(item.atr_values) > config.maximum_retained_atr_values:
                raise WaveAnalysisStateError("ATR history exceeds configured capacity")
            if item.processed_bars < len(item.bars):
                raise WaveAnalysisStateError("processed_bars cannot be smaller than retained bars")
            if item.bar_offset != item.processed_bars - len(item.bars):
                raise WaveAnalysisStateError("bar_offset must equal processed_bars minus retained bars")
            if item.segment_id != item.gap_events:
                raise WaveAnalysisStateError("segment_id must equal the number of timeframe gaps")
            if item.gap_blocked and item.lifecycle.value != "gap_blocked":
                raise WaveAnalysisStateError("gap-blocked state must use the gap_blocked lifecycle")
            if item.gap_blocked and item.recovery_count >= config.effective_recovery_bars:
                raise WaveAnalysisStateError("gap-blocked recovery_count must be below the release threshold")
            for key, feature_id in item.published_feature_ids.items():
                if not isinstance(key, str) or "." not in key or not isinstance(feature_id, str) or not feature_id:
                    raise WaveAnalysisStateError("published feature identities must be namespaced non-empty strings")
            if (item.last_open_time is None) != (item.last_fingerprint is None):
                raise WaveAnalysisStateError("target event identity is partially saved")
            if item.last_fingerprint is not None and not item.last_fingerprint:
                raise WaveAnalysisStateError("last_fingerprint must be non-empty")

            expected_delta = timedelta(seconds=timeframe_seconds(timeframe))
            for bar in item.bars:
                if bar.close_time - bar.timestamp != expected_delta:
                    raise WaveAnalysisStateError("retained bar duration does not match timeframe")
            for previous, current in zip(item.bars, item.bars[1:], strict=False):
                if current.timestamp - previous.timestamp != expected_delta:
                    raise WaveAnalysisStateError("retained bars must be strictly contiguous and ordered")
            if item.bars and item.last_open_time != item.bars[-1].timestamp:
                raise WaveAnalysisStateError("last_open_time must match the latest retained bar")

            if item.atr is not None:
                _finite_decimal(item.atr, "atr")
            if item.previous_close is not None:
                _finite_decimal(item.previous_close, "previous_close", positive=True)
            for value in item.tr_seed:
                _finite_decimal(value, "tr_seed item")
            for value in item.atr_values:
                _finite_decimal(value, "atr_values item")

            latest_index = item.processed_bars - 1
            for name, extreme in (("initial_high", item.initial_high), ("initial_low", item.initial_low)):
                if extreme is None:
                    continue
                _finite_decimal(extreme[0], f"{name} price", positive=True)
                if extreme[2] < 0 or extreme[2] > latest_index:
                    raise WaveAnalysisStateError(f"{name} index is outside processed history")

            pivot_ids: set[str] = set()
            previous_segment = -1
            previous_by_segment: dict[int, PivotPoint] = {}
            for pivot in item.confirmed_pivots:
                if pivot.status.value != "confirmed":
                    raise WaveAnalysisStateError("confirmed pivot history contains provisional state")
                if pivot.timeframe != timeframe or pivot.symbol != self.bound_symbol:
                    raise WaveAnalysisStateError("pivot identity does not match timeframe state")
                if pivot.config_fingerprint != config.signature:
                    raise WaveAnalysisStateError("pivot config fingerprint mismatch")
                if pivot.pivot_id in pivot_ids:
                    raise WaveAnalysisStateError("confirmed pivot IDs must be unique")
                pivot_ids.add(pivot.pivot_id)
                segment = _segment_id(pivot.metadata, "pivot")
                if segment > item.segment_id or segment < previous_segment:
                    raise WaveAnalysisStateError("pivot segment history must be ordered and not exceed active segment")
                previous_pivot = previous_by_segment.get(segment)
                if previous_pivot is not None:
                    if pivot.bar_index <= previous_pivot.bar_index or pivot.pivot_time <= previous_pivot.pivot_time:
                        raise WaveAnalysisStateError("pivots inside a segment must be strictly ordered")
                    if pivot.confirmed_at < previous_pivot.confirmed_at:
                        raise WaveAnalysisStateError("pivot confirmation times must be non-decreasing")
                previous_by_segment[segment] = pivot
                previous_segment = segment
                if pivot.bar_index >= item.processed_bars:
                    raise WaveAnalysisStateError("pivot bar_index exceeds processed history")

            wave_ids: set[str] = set()
            end_pivot_ids: set[str] = set()
            previous_segment = -1
            previous_wave_by_segment: dict[int, WaveLeg] = {}
            for leg in item.wave_legs:
                if leg.timeframe != timeframe or leg.symbol != self.bound_symbol:
                    raise WaveAnalysisStateError("wave identity does not match timeframe state")
                if leg.config_fingerprint != config.signature:
                    raise WaveAnalysisStateError("wave config fingerprint mismatch")
                if leg.wave_id in wave_ids or leg.end_pivot_id in end_pivot_ids:
                    raise WaveAnalysisStateError("wave and end-pivot identities must be unique")
                wave_ids.add(leg.wave_id)
                end_pivot_ids.add(leg.end_pivot_id)
                segment = _segment_id(leg.metadata, "wave")
                if segment > item.segment_id or segment < previous_segment:
                    raise WaveAnalysisStateError("wave segment history must be ordered and not exceed active segment")
                previous_wave = previous_wave_by_segment.get(segment)
                if previous_wave is not None:
                    if previous_wave.end_pivot_id != leg.start_pivot_id:
                        raise WaveAnalysisStateError("consecutive waves inside a segment must share a pivot")
                    if leg.confirmed_at < previous_wave.confirmed_at:
                        raise WaveAnalysisStateError("wave confirmation times must be non-decreasing")
                previous_wave_by_segment[segment] = leg
                previous_segment = segment

            if item.provisional is not None:
                provisional = item.provisional
                if provisional.timeframe != timeframe or provisional.symbol != self.bound_symbol:
                    raise WaveAnalysisStateError("provisional pivot identity does not match timeframe state")
                if provisional.config_fingerprint != config.signature:
                    raise WaveAnalysisStateError("provisional pivot config fingerprint mismatch")
                if provisional.bar_index >= item.processed_bars:
                    raise WaveAnalysisStateError("provisional pivot bar_index exceeds processed history")
            if item.structure is not None and (
                item.structure.timeframe != timeframe or item.structure.segment_id != item.segment_id
            ):
                raise WaveAnalysisStateError("structure must belong to the active timeframe segment")
            expected_ranks = list(range(1, len(item.hypotheses) + 1))
            if [hypothesis.alternative_rank for hypothesis in item.hypotheses] != expected_ranks:
                raise WaveAnalysisStateError("hypothesis alternative ranks must be contiguous from one")
            hypothesis_ids: set[str] = set()
            for hypothesis in item.hypotheses:
                if hypothesis.hypothesis_id in hypothesis_ids:
                    raise WaveAnalysisStateError("hypothesis IDs must be unique")
                hypothesis_ids.add(hypothesis.hypothesis_id)
                if hypothesis.degree != timeframe:
                    raise WaveAnalysisStateError("hypothesis degree must match timeframe")
                if _segment_id(hypothesis.metadata, "hypothesis") != item.segment_id:
                    raise WaveAnalysisStateError("hypothesis must belong to the active segment")
