"""Owner-bound Frozen 1H/4H short producers for the canonical FVG-RT composition.

These modules reproduce only the immutable historical signal construction for
the two frozen higher-timeframe components. They publish validated snapshots;
they do not size positions, place orders, or own downstream exit policy.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from modular_core import Candle, Feature, MarketView, ModuleDecision, ModuleManifest, StrategyModule

from .module import (
    _completed_asof,
    _finite,
    _IndicatorCache,
    _number_text,
    _positive_finite,
    _range,
    _rolling_location,
    _sha256,
    _SymbolSeriesValidator,
)

FROZEN_1H_MODULE_NAME = "frozen_1h_short"
FROZEN_1H_MODULE_VERSION = "1.0.0"
FROZEN_1H_FEATURE_KEY = "frozen_1h_short.snapshot"
FROZEN_1H_FEATURE_CONTRACT = "canonical-frozen-1h-short-feature-snapshot-v1"

FROZEN_4H_MODULE_NAME = "frozen_4h_short"
FROZEN_4H_MODULE_VERSION = "1.0.0"
FROZEN_4H_FEATURE_KEY = "frozen_4h_short.snapshot"
FROZEN_4H_FEATURE_CONTRACT = "canonical-frozen-4h-short-feature-snapshot-v1"

FROZEN_1H_CONDITIONS = frozenset(
    {
        "all_inputs_completed",
        "atr_fraction_inclusive",
        "bearish_fvg",
        "close_below_ema50",
        "completed_4h_close_below_ema200",
        "ema50_falling_4",
        "fvg_size_atr_inclusive",
        "impulse_efficiency_min",
        "middle_body_atr_min",
        "middle_bearish",
        "short_location_8h_min",
        "volume_ratio_min",
    }
)
FROZEN_4H_CONDITIONS = frozenset(
    {
        "all_inputs_completed",
        "atr_fraction_inclusive",
        "bearish_fvg",
        "fvg_size_atr_inclusive",
        "impulse_efficiency_min",
        "middle_body_atr_min",
        "middle_bearish",
        "rsi14_max",
        "short_location_32h_min",
        "volume_ratio_min",
    }
)

_VALIDITY_WINDOW = timedelta(microseconds=1)


@dataclass(frozen=True, slots=True)
class _FrozenContract:
    module_name: str
    module_version: str
    source_timeframe: str
    required_timeframes: tuple[str, ...]
    feature_key: str
    feature_contract: str
    completed_timeframes: tuple[str, ...]
    conditions: frozenset[str]
    priority: int
    target_r: float
    ttl_bars_5m: int
    max_hold_bars_5m: int
    no_progress_bars_5m: int
    no_progress_min_mfe_r: float
    maximum_stop_fraction: float


_FROZEN_1H = _FrozenContract(
    module_name=FROZEN_1H_MODULE_NAME,
    module_version=FROZEN_1H_MODULE_VERSION,
    source_timeframe="1h",
    required_timeframes=("1h", "4h"),
    feature_key=FROZEN_1H_FEATURE_KEY,
    feature_contract=FROZEN_1H_FEATURE_CONTRACT,
    completed_timeframes=("5m", "1h", "4h"),
    conditions=FROZEN_1H_CONDITIONS,
    priority=2,
    target_r=6.0,
    ttl_bars_5m=48,
    max_hold_bars_5m=144,
    no_progress_bars_5m=72,
    no_progress_min_mfe_r=0.50,
    maximum_stop_fraction=0.04,
)
_FROZEN_4H = _FrozenContract(
    module_name=FROZEN_4H_MODULE_NAME,
    module_version=FROZEN_4H_MODULE_VERSION,
    source_timeframe="4h",
    required_timeframes=("4h",),
    feature_key=FROZEN_4H_FEATURE_KEY,
    feature_contract=FROZEN_4H_FEATURE_CONTRACT,
    completed_timeframes=("5m", "4h"),
    conditions=FROZEN_4H_CONDITIONS,
    priority=3,
    target_r=6.0,
    ttl_bars_5m=576,
    max_hold_bars_5m=576,
    no_progress_bars_5m=432,
    no_progress_min_mfe_r=0.35,
    maximum_stop_fraction=0.08,
)


class _FrozenShortModule(StrategyModule):
    contract: _FrozenContract

    def __init__(self) -> None:
        contract = self.contract
        self.parameters: Mapping[str, object] = MappingProxyType(
            {
                "contract": contract.feature_contract,
                "module_version": contract.module_version,
                "required_timeframes": contract.required_timeframes,
            }
        )
        self.manifest = ModuleManifest(
            name=contract.module_name,
            version=contract.module_version,
            api_version="1.0",
            required_timeframes=contract.required_timeframes,
            produced_features=(contract.feature_key,),
            warmup_bars={timeframe: 200 for timeframe in contract.required_timeframes},
            parameters_schema={
                "contract": {"type": "string", "default": contract.feature_contract},
                "module_version": {"type": "string", "default": contract.module_version},
                "required_timeframes": {
                    "type": "array",
                    "default": contract.required_timeframes,
                },
            },
            skip_until_ready=False,
            description=(
                f"Owner-bound {contract.module_name} completed-bar signal producer; "
                "publishes validated snapshots only."
            ),
            state_schema_version="1",
        )
        self._last_processed_close: datetime | None = None
        self._bound_symbol: str | None = None
        self._indicator_cache = {
            timeframe: _IndicatorCache() for timeframe in contract.required_timeframes
        }
        self._symbol_series_validator = _SymbolSeriesValidator()

    def on_bar(self, view: MarketView) -> ModuleDecision:
        contract = self.contract
        current = view.current
        prefix = contract.module_name
        if current.timeframe != "5m":
            return ModuleDecision(skips=(f"{prefix}.base_timeframe_mismatch",))
        if view.data_gap:
            self._last_processed_close = None
            self._bound_symbol = None
            self._symbol_series_validator.reset()
            for cache in self._indicator_cache.values():
                cache.reset()
            return ModuleDecision(skips=(f"{prefix}.data_gap_reset",))
        if self._bound_symbol is None:
            self._bound_symbol = current.symbol
        elif self._bound_symbol != current.symbol:
            return ModuleDecision(skips=(f"{prefix}.symbol_mismatch",))

        source = view.current_for(contract.source_timeframe)
        if source is None:
            return ModuleDecision(skips=(f"{prefix}.waiting_for_{contract.source_timeframe}",))
        if source.symbol != current.symbol or source.close_time > current.close_time:
            return ModuleDecision(skips=(f"{prefix}.incomplete_context",))
        if source.close_time != current.close_time:
            return ModuleDecision(
                skips=(f"{prefix}.waiting_for_{contract.source_timeframe}_close",)
            )
        if self._last_processed_close == source.close_time:
            return ModuleDecision(skips=(f"{prefix}.duplicate_{contract.source_timeframe}_close",))

        series = {
            timeframe: view.timeframe_bars.get(timeframe, ())
            for timeframe in contract.required_timeframes
        }
        if any(not series[timeframe] for timeframe in contract.required_timeframes):
            return ModuleDecision(skips=(f"{prefix}.incomplete_context",))
        if not self._symbol_series_validator.matches(
            series,
            contract.required_timeframes,
            current.symbol,
            view_index=view.index,
        ):
            return ModuleDecision(skips=(f"{prefix}.symbol_mismatch",))
        source_series = series[contract.source_timeframe]
        if source_series[-1].close_time != source.close_time:
            return ModuleDecision(skips=(f"{prefix}.incomplete_context",))

        self._last_processed_close = source.close_time
        candidate = self._candidate(series, available_at=current.close_time)
        features: tuple[Feature, ...] = ()
        if candidate is not None:
            entry, stop = candidate
            snapshot = self._snapshot(
                current.symbol,
                current.close_time,
                entry=entry,
                stop=stop,
            )
            if snapshot is not None:
                features = (self._feature(snapshot, current.close_time, current.symbol),)
        return ModuleDecision(
            features=features,
            diagnostics={
                "contract": contract.feature_contract,
                "processed_source_close": source.close_time.isoformat(),
                "snapshots_published": len(features),
            },
        )

    def _candidate(
        self,
        series: Mapping[str, Sequence[Candle]],
        *,
        available_at: datetime,
    ) -> tuple[float, float] | None:
        raise NotImplementedError

    def _snapshot(
        self,
        symbol: str,
        available_at: datetime,
        *,
        entry: float,
        stop: float,
    ) -> Mapping[str, object] | None:
        contract = self.contract
        distance = stop - entry
        if not (_positive_finite(entry) and _positive_finite(stop)) or distance <= 0:
            return None
        if distance / entry > contract.maximum_stop_fraction:
            return None

        identity = {
            "available_at": available_at.astimezone(UTC).isoformat(),
            "entry_price": _number_text(entry),
            "module_id": contract.module_name,
            "module_version": contract.module_version,
            "setup_id": "short",
            "stop_price": _number_text(stop),
            "symbol": symbol,
        }
        snapshot_id = _sha256(identity)
        semantic_input_digest = _sha256(
            {
                "available_at": available_at.astimezone(UTC).isoformat(),
                "entry": round(entry, 12),
                "module_id": contract.module_name,
                "module_version": contract.module_version,
                "setup_id": "short",
                "stop": round(stop, 12),
            }
        )
        return MappingProxyType(
            {
                "snapshot_id": snapshot_id,
                "module_id": contract.module_name,
                "module_version": contract.module_version,
                "setup_id": "short",
                "symbol": symbol,
                "side": "sell",
                "signal_available_at": available_at.astimezone(UTC).isoformat(),
                "first_eligible_5m_open": available_at.astimezone(UTC).isoformat(),
                "completed_timeframes": contract.completed_timeframes,
                "conditions": {name: True for name in sorted(contract.conditions)},
                "entry_price": _number_text(entry),
                "stop_price": _number_text(stop),
                "target_r": _number_text(contract.target_r),
                "ttl_bars_5m": contract.ttl_bars_5m,
                "max_hold_bars_5m": contract.max_hold_bars_5m,
                "no_progress_bars_5m": contract.no_progress_bars_5m,
                "no_progress_min_mfe_r": _number_text(contract.no_progress_min_mfe_r),
                "semantic_input_digest": semantic_input_digest,
                "feature_contract": contract.feature_contract,
            }
        )

    def _feature(
        self,
        snapshot: Mapping[str, object],
        created_at: datetime,
        symbol: str,
    ) -> Feature:
        contract = self.contract
        snapshot_id = str(snapshot["snapshot_id"])
        return Feature(
            key=contract.feature_key,
            source_module=contract.module_name,
            created_at=created_at,
            valid_until=created_at + _VALIDITY_WINDOW,
            value=dict(snapshot),
            symbol=symbol,
            timeframe=contract.source_timeframe,
            schema_version="1",
            feature_id=f"fvg-rt-{snapshot_id}",
            invalidate_on_gap=True,
            metadata={"feature_contract": contract.feature_contract},
        )

    def snapshot_state(self) -> Mapping[str, object]:
        return {
            "payload_version": 1,
            "last_processed_close": (
                self._last_processed_close.astimezone(UTC).isoformat()
                if self._last_processed_close is not None
                else None
            ),
            "bound_symbol": self._bound_symbol,
        }

    def restore_state(self, state: Mapping[str, object]) -> None:
        expected = {"payload_version", "last_processed_close", "bound_symbol"}
        if set(state) != expected:
            raise ValueError("invalid frozen FVG-RT state keys")
        version = state["payload_version"]
        if isinstance(version, bool) or version != 1:
            raise ValueError("unsupported frozen FVG-RT state payload version")

        close_raw = state["last_processed_close"]
        if close_raw is None:
            last_processed_close = None
        elif isinstance(close_raw, str):
            try:
                parsed = datetime.fromisoformat(close_raw)
            except ValueError as exc:
                raise ValueError("invalid frozen FVG-RT last_processed_close") from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("frozen FVG-RT last_processed_close must be timezone-aware")
            last_processed_close = parsed.astimezone(UTC)
        else:
            raise ValueError("invalid frozen FVG-RT last_processed_close type")

        symbol_raw = state["bound_symbol"]
        if symbol_raw is None:
            bound_symbol = None
        elif isinstance(symbol_raw, str) and symbol_raw.strip():
            bound_symbol = symbol_raw
        else:
            raise ValueError("invalid frozen FVG-RT bound_symbol")
        if last_processed_close is not None and bound_symbol is None:
            raise ValueError("processed frozen FVG-RT state requires bound_symbol")

        self._last_processed_close = last_processed_close
        self._bound_symbol = bound_symbol
        self._symbol_series_validator.reset()
        for cache in self._indicator_cache.values():
            cache.reset()


class Frozen1hShortModule(_FrozenShortModule):
    """Reproduce the immutable Frozen 1H short signal contract."""

    name = FROZEN_1H_MODULE_NAME
    contract = _FROZEN_1H

    def _candidate(
        self,
        series: Mapping[str, Sequence[Candle]],
        *,
        available_at: datetime,
    ) -> tuple[float, float] | None:
        bars = series["1h"]
        index = len(bars) - 1
        if index < 4:
            return None
        row, a, b = bars[index], bars[index - 2], bars[index - 1]
        if not row.high < a.low:
            return None
        denominator = _range(a) + _range(b) + _range(row)
        if denominator <= 0:
            return None

        indicators = self._indicator_cache["1h"].values(bars)
        current = indicators[index]
        previous = indicators[index - 1]
        if not _positive_finite(current.atr14):
            return None
        if not (
            _finite(current.ema50)
            and _finite(indicators[index - 4].ema50)
            and _finite(previous.volume_sma20)
            and previous.volume_sma20 > 0
        ):
            return None

        four_bars = series["4h"]
        four = _completed_asof(four_bars, available_at)
        if four is None:
            return None
        four_index = (
            len(four_bars) - 1 if four_bars and four_bars[-1] is four else four_bars.index(four)
        )
        four_indicators = self._indicator_cache["4h"].values(four_bars)
        if four_index >= len(four_indicators) or not _finite(four_indicators[four_index].ema200):
            return None

        atr = current.atr14
        quality = abs(row.close - a.open) / denominator
        fvg_size = (a.low - row.high) / atr
        body_atr = abs(b.close - b.open) / atr
        volume_ratio = b.volume / previous.volume_sma20
        short_location = _rolling_location(bars, 8, short=True)
        atr_fraction = atr / row.close
        if not (
            0.08 <= fvg_size <= 1.25
            and body_atr >= 0.40
            and b.close < b.open
            and volume_ratio >= 0.85
            and quality >= 0.45
            and row.close < current.ema50
            and current.ema50 < indicators[index - 4].ema50
            and four.close < four_indicators[four_index].ema200
            and short_location >= 0.75
            and 0 <= atr_fraction <= 0.025
        ):
            return None

        entry = row.high + 0.75 * (a.low - row.high)
        stop = max(candle.high for candle in bars[index - 4 : index + 1]) + 0.05 * atr
        return entry, stop


class Frozen4hShortModule(_FrozenShortModule):
    """Reproduce the immutable Frozen 4H short signal contract."""

    name = FROZEN_4H_MODULE_NAME
    contract = _FROZEN_4H

    def _candidate(
        self,
        series: Mapping[str, Sequence[Candle]],
        *,
        available_at: datetime,
    ) -> tuple[float, float] | None:
        del available_at
        bars = series["4h"]
        index = len(bars) - 1
        if index < 2:
            return None
        row, a, b = bars[index], bars[index - 2], bars[index - 1]
        if not row.high < a.low:
            return None
        denominator = _range(a) + _range(b) + _range(row)
        if denominator <= 0:
            return None

        indicators = self._indicator_cache["4h"].values(bars)
        current = indicators[index]
        previous = indicators[index - 1]
        if not _positive_finite(current.atr14):
            return None
        if not (
            _finite(current.rsi14) and _finite(previous.volume_sma20) and previous.volume_sma20 > 0
        ):
            return None

        atr = current.atr14
        quality = abs(row.close - a.open) / denominator
        fvg_size = (a.low - row.high) / atr
        body_atr = abs(b.close - b.open) / atr
        volume_ratio = b.volume / previous.volume_sma20
        short_location = _rolling_location(bars, 8, short=True)
        atr_fraction = atr / row.close
        if not (
            0.05 <= fvg_size <= 1.25
            and body_atr >= 0.15
            and b.close < b.open
            and volume_ratio >= 1.15
            and quality >= 0.55
            and current.rsi14 <= 40
            and short_location >= 0.35
            and 0 <= atr_fraction <= 0.025
        ):
            return None

        entry = row.high + 0.50 * (a.low - row.high)
        stop = max(candle.high for candle in bars[index - 1 : index + 1]) + 0.05 * atr
        return entry, stop


__all__ = [
    "FROZEN_1H_CONDITIONS",
    "FROZEN_1H_FEATURE_CONTRACT",
    "FROZEN_1H_FEATURE_KEY",
    "FROZEN_1H_MODULE_NAME",
    "FROZEN_1H_MODULE_VERSION",
    "FROZEN_4H_CONDITIONS",
    "FROZEN_4H_FEATURE_CONTRACT",
    "FROZEN_4H_FEATURE_KEY",
    "FROZEN_4H_MODULE_NAME",
    "FROZEN_4H_MODULE_VERSION",
    "Frozen1hShortModule",
    "Frozen4hShortModule",
]
