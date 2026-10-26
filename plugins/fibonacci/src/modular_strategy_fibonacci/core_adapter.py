"""Modular Strategy Core adapter for the standalone Fibonacci engine.

This module intentionally imports ``modular_core`` only when the adapter is
used. The standalone research/labeling package remains usable without Core.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence, cast

from modular_core import (
    Feature,
    FeatureDelete,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    StrategyModule,
)

from .config import FibonacciConfig
from .contracts import Candle, FibonacciWaveSnapshot
from .corrections.short_setup import (
    active_bearish_continuation_setup,
    build_bearish_reaction_profile,
)
from .levels.calculator import calculate_levels
from .workflow.state_machine import SequentialFibonacciEngine

from .core_contracts import ACTIVE_PROJECTION_KEY, COMPLETED_WAVE_KEY, SHORT_SETUP_KEY

MODULE_VERSION = "0.3.0"
STATE_SCHEMA_VERSION = "1"


class _CoreCandleLike(Protocol):
    timestamp: datetime
    close_time: datetime
    open: object
    high: object
    low: object
    close: object
    volume: object
    symbol: str
    timeframe: str


class FibonacciCoreStateError(ValueError):
    pass


class FibonacciModule(StrategyModule):
    """Point-in-time Fibonacci feature producer; never creates orders."""

    name = "fibonacci"

    def __init__(
        self,
        *,
        timeframes: Sequence[str] = ("15m", "1h", "4h", "1d"),
        dataset_hash: str = "core-stream-unhashed",
        min_atr_multiple: str | int | float | Decimal = "1.5",
        publish_short_setup: bool = True,
    ) -> None:
        normalized = tuple(dict.fromkeys(str(item) for item in timeframes))
        if not normalized or any(not item for item in normalized):
            raise ValueError("timeframes must be non-empty")
        self.config = FibonacciConfig(min_atr_multiple=Decimal(str(min_atr_multiple)))
        self.timeframes = normalized
        self.dataset_hash = str(dataset_hash)
        self.publish_short_setup = bool(publish_short_setup)
        self.parameters = MappingProxyType(
            {
                "timeframes": list(self.timeframes),
                "dataset_hash": self.dataset_hash,
                "min_atr_multiple": str(self.config.min_atr_multiple),
                "publish_short_setup": self.publish_short_setup,
            }
        )
        self.manifest = ModuleManifest(
            name=self.name,
            version=MODULE_VERSION,
            api_version="1.0",
            required_timeframes=self.timeframes,
            produced_features=(
                ACTIVE_PROJECTION_KEY,
                COMPLETED_WAVE_KEY,
                SHORT_SETUP_KEY,
            ),
            warmup_bars={timeframe: 20 for timeframe in self.timeframes},
            parameters_schema={
                "timeframes": {"type": "array", "default": ["15m", "1h", "4h", "1d"]},
                "dataset_hash": {"type": "string", "default": "core-stream-unhashed"},
                "min_atr_multiple": {"type": "string", "default": "1.5"},
                "publish_short_setup": {"type": "boolean", "default": True},
            },
            skip_until_ready=False,
            description=(
                "Tracks sequential real-time Fibonacci waves to their full final extreme, "
                "publishes immutable projections and optional 4h/1d bearish reaction setups."
            ),
            state_schema_version=STATE_SCHEMA_VERSION,
        )
        self._engines: dict[tuple[str, str], SequentialFibonacciEngine] = {}
        self._active_feature_ids: dict[tuple[str, str], str] = {}
        self._short_feature_ids: dict[tuple[str, str], str] = {}
        self._published_completed_ids: set[str] = set()
        self._cursor_barriers: dict[tuple[str, str], datetime] = {}
        self._republish_pending = False
        self._config_fingerprint = self._fingerprint_parameters(self.parameters)

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if not view.base_bars:
            return ModuleDecision(skips=("fibonacci.empty_base_bars",))
        current = view.current
        symbol = current.symbol
        features: list[Feature] = []
        deletes: list[FeatureDelete] = []
        skips: list[str] = []
        diagnostics: dict[str, object] = {
            "symbol": symbol,
            "base_index": view.index,
            "data_gap": view.data_gap,
            "processed_bars": 0,
            "endpoint_updates": 0,
            "active_timeframes": 0,
            "completed_published": 0,
            "short_setups_published": 0,
        }

        if view.data_gap:
            for key in tuple(self._engines):
                if key[0] != symbol:
                    continue
                self._engines.pop(key)
                active_id = self._active_feature_ids.pop(key, None)
                if active_id is not None:
                    deletes.append(self._delete(ACTIVE_PROJECTION_KEY, active_id, *key, "fibonacci_data_gap"))
                short_id = self._short_feature_ids.pop(key, None)
                if short_id is not None:
                    deletes.append(self._delete(SHORT_SETUP_KEY, short_id, *key, "fibonacci_data_gap"))
            skips.append("fibonacci.data_gap_reset")
            self._republish_pending = False

        for timeframe in self.timeframes:
            series = self._series_for(view, timeframe)
            if not series:
                skips.append(f"fibonacci.no_data:{timeframe}")
                continue
            latest = series[-1]
            if latest.symbol != symbol:
                raise ValueError("Fibonacci timeframe series symbol differs from current symbol")
            if latest.close_time > current.close_time:
                raise ValueError(f"future/incomplete {timeframe} candle exposed to Fibonacci module")
            key = (symbol, timeframe)
            if view.data_gap:
                self._cursor_barriers[key] = latest.timestamp
                skips.append(f"fibonacci.gap_barrier:{timeframe}")
                continue
            engine = self._engines.get(key)
            if engine is None:
                engine = SequentialFibonacciEngine(
                    symbol=symbol,
                    timeframe=timeframe,
                    dataset_hash=self.dataset_hash,
                    config=self.config,
                )
                self._engines[key] = engine
            last_open = engine.last_processed_open_time
            barrier = self._cursor_barriers.get(key)
            new_bars = self._new_bars(series, last_open=last_open, barrier=barrier)
            if last_open is not None and latest.timestamp < last_open:
                raise ValueError(f"Fibonacci {timeframe} series regressed in time")
            before_events = len(engine.snapshot(latest.close_time).events)
            for bar in new_bars:
                if bar.close_time > current.close_time:
                    raise ValueError(f"future/incomplete {timeframe} candle exposed to Fibonacci module")
                engine.process(self._to_internal_candle(bar))
                self._increment_counter(diagnostics, "processed_bars")
            if new_bars and barrier is not None:
                self._cursor_barriers.pop(key, None)
            snapshot = engine.snapshot(latest.close_time)
            self._increment_counter(
                diagnostics,
                "endpoint_updates",
                sum(event.event_type.value == "endpoint_updated" for event in snapshot.events[before_events:]),
            )

            active = snapshot.active_wave
            previous_active_id = self._active_feature_ids.get(key)
            if active is None:
                if previous_active_id is not None:
                    deletes.append(
                        self._delete(
                            ACTIVE_PROJECTION_KEY,
                            previous_active_id,
                            symbol,
                            timeframe,
                            "fibonacci_wave_inactive",
                        )
                    )
                    self._active_feature_ids.pop(key, None)
            else:
                self._increment_counter(diagnostics, "active_timeframes")
                feature_id = self._active_feature_id(active)
                if previous_active_id != feature_id or self._republish_pending:
                    if previous_active_id is not None and previous_active_id != feature_id:
                        deletes.append(
                            self._delete(
                                ACTIVE_PROJECTION_KEY,
                                previous_active_id,
                                symbol,
                                timeframe,
                                "fibonacci_projection_revised",
                            )
                        )
                    features.append(self._active_feature(active, symbol, timeframe, latest.close_time))
                    self._active_feature_ids[key] = feature_id

            for wave in snapshot.completed_waves:
                if wave.wave_id in self._published_completed_ids:
                    continue
                features.append(self._completed_feature(wave, symbol, timeframe))
                self._published_completed_ids.add(wave.wave_id)
                self._increment_counter(diagnostics, "completed_published")

            if self.publish_short_setup and timeframe in {"4h", "1d"}:
                profile = build_bearish_reaction_profile(snapshot)
                setup = active_bearish_continuation_setup(snapshot, profile)
                previous_short_id = self._short_feature_ids.get(key)
                if setup is None:
                    if previous_short_id is not None:
                        deletes.append(
                            self._delete(
                                SHORT_SETUP_KEY,
                                previous_short_id,
                                symbol,
                                timeframe,
                                "fibonacci_short_setup_inactive",
                            )
                        )
                        self._short_feature_ids.pop(key, None)
                elif previous_short_id != setup.setup_id or self._republish_pending:
                    if previous_short_id is not None and previous_short_id != setup.setup_id:
                        deletes.append(
                            self._delete(
                                SHORT_SETUP_KEY,
                                previous_short_id,
                                symbol,
                                timeframe,
                                "fibonacci_short_setup_revised",
                            )
                        )
                    features.append(
                        Feature(
                            key=SHORT_SETUP_KEY,
                            source_module=self.name,
                            created_at=min(setup.armed_at, latest.close_time),
                            value={
                                "setup_id": setup.setup_id,
                                "wave_id": setup.wave_id,
                                "correction_level": str(setup.correction_level),
                                "correction_ratio": str(setup.correction_ratio),
                                "correction_price": str(setup.correction_price),
                                "impulse_start_price": str(setup.impulse_start_price),
                                "impulse_end_price": str(setup.impulse_end_price),
                                "historical_success_rate": setup.historical_success_rate,
                                "historical_sample_size": setup.historical_sample_size,
                                "current_success_streak": setup.current_success_streak,
                                "evidence": setup.evidence,
                            },
                            symbol=symbol,
                            timeframe=timeframe,
                            schema_version="1",
                            feature_id=setup.setup_id,
                            invalidate_on_gap=True,
                            metadata={"execution": "none", "kind": "analytical_short_setup"},
                        )
                    )
                    self._short_feature_ids[key] = setup.setup_id
                    self._increment_counter(diagnostics, "short_setups_published")

        self._republish_pending = False
        return ModuleDecision(
            features=tuple(features),
            feature_deletes=tuple(deletes),
            skips=tuple(skips),
            diagnostics=MappingProxyType(diagnostics),
        )

    def snapshot_state(self) -> Mapping[str, object]:
        return {
            "state_schema_version": STATE_SCHEMA_VERSION,
            "module_version": MODULE_VERSION,
            "config_fingerprint": self._config_fingerprint,
            "engines": [
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "state": engine.snapshot_state(),
                }
                for (symbol, timeframe), engine in sorted(self._engines.items())
            ],
            "active_feature_ids": [
                {"symbol": symbol, "timeframe": timeframe, "feature_id": feature_id}
                for (symbol, timeframe), feature_id in sorted(self._active_feature_ids.items())
            ],
            "short_feature_ids": [
                {"symbol": symbol, "timeframe": timeframe, "feature_id": feature_id}
                for (symbol, timeframe), feature_id in sorted(self._short_feature_ids.items())
            ],
            "published_completed_ids": sorted(self._published_completed_ids),
            "cursor_barriers": [
                {"symbol": symbol, "timeframe": timeframe, "open_time": value.isoformat()}
                for (symbol, timeframe), value in sorted(self._cursor_barriers.items())
            ],
        }

    def restore_state(self, state: Mapping[str, object]) -> None:
        expected = {
            "state_schema_version",
            "module_version",
            "config_fingerprint",
            "engines",
            "active_feature_ids",
            "short_feature_ids",
            "published_completed_ids",
            "cursor_barriers",
        }
        if set(state) != expected:
            raise FibonacciCoreStateError("invalid Fibonacci Core state keys")
        if str(state["state_schema_version"]) != STATE_SCHEMA_VERSION:
            raise FibonacciCoreStateError("unsupported Fibonacci Core state schema")
        if str(state["module_version"]) != MODULE_VERSION:
            raise FibonacciCoreStateError("Fibonacci Core module version mismatch")
        if str(state["config_fingerprint"]) != self._config_fingerprint:
            raise FibonacciCoreStateError("Fibonacci Core configuration mismatch")
        raw_engines = self._require_sequence(state["engines"])
        raw_active = self._require_sequence(state["active_feature_ids"])
        raw_short = self._require_sequence(state["short_feature_ids"])
        raw_completed = self._require_sequence(state["published_completed_ids"])
        raw_barriers = self._require_sequence(state["cursor_barriers"])

        engines: dict[tuple[str, str], SequentialFibonacciEngine] = {}
        for row in raw_engines:
            if not isinstance(row, Mapping) or set(row) != {"symbol", "timeframe", "state"}:
                raise FibonacciCoreStateError("invalid engine state row")
            key = (str(row["symbol"]), str(row["timeframe"]))
            if key in engines or key[1] not in self.timeframes:
                raise FibonacciCoreStateError("duplicate or unconfigured engine state")
            nested = row["state"]
            if not isinstance(nested, Mapping):
                raise FibonacciCoreStateError("nested Fibonacci engine state must be a mapping")
            engine = SequentialFibonacciEngine.from_state(nested, config=self.config)
            if (engine.symbol, engine.timeframe) != key:
                raise FibonacciCoreStateError("engine state identity mismatch")
            engines[key] = engine

        active_ids = self._restore_feature_ids(raw_active, "active")
        short_ids = self._restore_feature_ids(raw_short, "short")
        completed_ids = {str(item) for item in raw_completed}
        if len(completed_ids) != len(raw_completed) or any(not item for item in completed_ids):
            raise FibonacciCoreStateError("completed feature IDs are invalid")
        barriers: dict[tuple[str, str], datetime] = {}
        for row in raw_barriers:
            if not isinstance(row, Mapping) or set(row) != {"symbol", "timeframe", "open_time"}:
                raise FibonacciCoreStateError("invalid cursor barrier row")
            key = (str(row["symbol"]), str(row["timeframe"]))
            try:
                value = datetime.fromisoformat(str(row["open_time"]))
            except ValueError as exc:
                raise FibonacciCoreStateError("invalid cursor barrier timestamp") from exc
            if value.tzinfo is None or value.utcoffset() is None:
                raise FibonacciCoreStateError("cursor barrier timestamp must be timezone-aware")
            if key in barriers or key[1] not in self.timeframes:
                raise FibonacciCoreStateError("duplicate or unconfigured cursor barrier")
            barriers[key] = value.astimezone(timezone.utc)

        self._engines = engines
        self._active_feature_ids = active_ids
        self._short_feature_ids = short_ids
        self._published_completed_ids = completed_ids
        self._cursor_barriers = barriers
        self._republish_pending = True

    @staticmethod
    def _require_sequence(value: object) -> Sequence[object]:
        if not isinstance(value, (list, tuple)):
            raise FibonacciCoreStateError("Fibonacci Core state lists are malformed")
        return value

    @staticmethod
    def _increment_counter(
        diagnostics: dict[str, object],
        key: str,
        amount: int = 1,
    ) -> None:
        current = diagnostics.get(key, 0)
        if isinstance(current, bool) or not isinstance(current, int):
            raise TypeError(f"diagnostic counter {key!r} is not an integer")
        diagnostics[key] = current + amount

    def _restore_feature_ids(
        self,
        rows: Sequence[object],
        kind: str,
    ) -> dict[tuple[str, str], str]:
        output: dict[tuple[str, str], str] = {}
        for row in rows:
            if not isinstance(row, Mapping) or set(row) != {"symbol", "timeframe", "feature_id"}:
                raise FibonacciCoreStateError(f"invalid {kind} feature ID row")
            key = (str(row["symbol"]), str(row["timeframe"]))
            feature_id = str(row["feature_id"])
            if key in output or key[1] not in self.timeframes or not feature_id:
                raise FibonacciCoreStateError(f"duplicate or invalid {kind} feature ID")
            output[key] = feature_id
        return output

    def _series_for(self, view: MarketView, timeframe: str) -> Sequence[_CoreCandleLike]:
        if timeframe == view.current.timeframe:
            return cast(Sequence[_CoreCandleLike], view.base_bars)
        return cast(Sequence[_CoreCandleLike], view.timeframe_bars.get(timeframe, ()))

    @staticmethod
    def _new_bars(
        series: Sequence[_CoreCandleLike],
        *,
        last_open: datetime | None,
        barrier: datetime | None,
    ) -> tuple[_CoreCandleLike, ...]:
        thresholds = tuple(value for value in (last_open, barrier) if value is not None)
        if not thresholds:
            return tuple(series)
        threshold = max(thresholds)
        start = len(series) - 1
        while start >= 0 and series[start].timestamp > threshold:
            start -= 1
        return tuple(series[start + 1 :])

    @staticmethod
    def _to_internal_candle(candle: _CoreCandleLike) -> Candle:
        return Candle(
            open_time=candle.timestamp,
            close_time=candle.close_time,
            open=Decimal(str(candle.open)),
            high=Decimal(str(candle.high)),
            low=Decimal(str(candle.low)),
            close=Decimal(str(candle.close)),
            volume=Decimal(str(candle.volume)),
        )

    def _active_feature(
        self,
        wave: FibonacciWaveSnapshot,
        symbol: str,
        timeframe: str,
        visible_at: datetime,
    ) -> Feature:
        levels = calculate_levels(wave.start_price, wave.end_price, wave.direction, self.config.levels)
        feature_id = self._active_feature_id(wave)
        return Feature(
            key=ACTIVE_PROJECTION_KEY,
            source_module=self.name,
            created_at=visible_at,
            value={
                "wave_id": wave.wave_id,
                "sequence_number": wave.sequence_number,
                "direction": wave.direction,
                "phase": wave.phase.value,
                "start_time": wave.start_time.isoformat(),
                "start_price": str(wave.start_price),
                "end_time": wave.end_time.isoformat(),
                "end_price": str(wave.end_price),
                "endpoint_revision": wave.endpoint_revision,
                "endpoint_confirmed_at": (
                    None if wave.endpoint_confirmed_at is None else wave.endpoint_confirmed_at.isoformat()
                ),
                "levels": tuple((str(level.ratio), str(level.price)) for level in levels),
                "correction": (
                    None
                    if wave.correction is None
                    else {
                        "actual_ratio": str(wave.correction.actual_ratio),
                        "reference_level": str(wave.correction.reference_level),
                        "price": str(wave.correction.price),
                        "touched_at": wave.correction.touched_at.isoformat(),
                    }
                ),
                "dataset_hash": self.dataset_hash,
                "config_hash": self.config.stable_hash(),
            },
            symbol=symbol,
            timeframe=timeframe,
            schema_version="2",
            feature_id=feature_id,
            invalidate_on_gap=True,
            metadata={"endpoint_rule": "full_favorable_extreme", "execution": "none"},
        )

    def _completed_feature(
        self,
        wave: FibonacciWaveSnapshot,
        symbol: str,
        timeframe: str,
    ) -> Feature:
        assert wave.completion_time is not None
        return Feature(
            key=COMPLETED_WAVE_KEY,
            source_module=self.name,
            created_at=wave.completion_time,
            valid_until=wave.completion_time,
            value={
                "wave_id": wave.wave_id,
                "direction": wave.direction,
                "start_price": str(wave.start_price),
                "end_price": str(wave.end_price),
                "completion_target": str(wave.completion_target),
                "completion_price": str(wave.completion_price),
                "correction_level": (None if wave.correction is None else str(wave.correction.reference_level)),
                "correction_ratio": (None if wave.correction is None else str(wave.correction.actual_ratio)),
            },
            symbol=symbol,
            timeframe=timeframe,
            schema_version="1",
            feature_id=f"{wave.wave_id}:completed",
            invalidate_on_gap=False,
            metadata={"lifecycle": "completed", "execution": "none"},
        )

    @staticmethod
    def _active_feature_id(wave: FibonacciWaveSnapshot) -> str:
        confirmation = wave.endpoint_confirmation_index if wave.endpoint_confirmation_index is not None else "u"
        return f"{wave.wave_id}:r{wave.endpoint_revision}:c{confirmation}:p{wave.phase.value}"

    def _delete(
        self,
        key: str,
        feature_id: str,
        symbol: str,
        timeframe: str,
        reason: str,
    ) -> FeatureDelete:
        return FeatureDelete(
            key=key,
            source_module=self.name,
            feature_id=feature_id,
            symbol=symbol,
            timeframe=timeframe,
            reason=reason,
        )

    @staticmethod
    def _fingerprint_parameters(parameters: Mapping[str, object]) -> str:
        payload = json.dumps(dict(parameters), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
