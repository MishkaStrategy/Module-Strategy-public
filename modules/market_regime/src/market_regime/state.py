from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from math import isfinite
from typing import cast

from .config import DECIMAL_CONTEXT, MarketRegimeConfig, canonical_timeframe
from .enums import Regime, VolatilityLevel
from .exceptions import MarketRegimeStateError

STATE_SCHEMA_VERSION = "2"
LEGACY_STATE_SCHEMA_VERSIONS = {"1"}


def _decimal(
    value: object, name: str, *, positive: bool = False, nonnegative: bool = False
) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MarketRegimeStateError(f"invalid decimal in state: {name}") from exc
    if not result.is_finite():
        raise MarketRegimeStateError(f"non-finite decimal in state: {name}")
    if positive and result <= 0:
        raise MarketRegimeStateError(f"state value must be positive: {name}")
    if nonnegative and result < 0:
        raise MarketRegimeStateError(f"state value must be non-negative: {name}")
    return result


def _optional_decimal(value: object, name: str, *, positive: bool = False) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value, name, positive=positive)


def _datetime(value: object, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MarketRegimeStateError(f"state datetime must be ISO string: {name}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MarketRegimeStateError(f"invalid state datetime: {name}") from exc
    if parsed.tzinfo is None:
        raise MarketRegimeStateError(f"state datetime must be timezone-aware: {name}")
    return parsed.astimezone(timezone.utc)


def _int(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise MarketRegimeStateError(f"invalid integer in state: {name}")
    return value


def _decimal_list(
    value: object,
    name: str,
    *,
    maximum_length: int,
    positive: bool = False,
    nonnegative: bool = False,
) -> list[Decimal]:
    if not isinstance(value, (list, tuple)):
        raise MarketRegimeStateError(f"state field must be a list or tuple: {name}")
    if len(value) > maximum_length:
        raise MarketRegimeStateError(f"state list exceeds configured capacity: {name}")
    return [
        _decimal(item, f"{name}[{index}]", positive=positive, nonnegative=nonnegative)
        for index, item in enumerate(value)
    ]


@dataclass(frozen=True, slots=True)
class _FingerprintIdentity:
    timestamp: datetime
    symbol: str
    timeframe: str


def _fingerprint_identity(
    fingerprint: object,
    name: str,
) -> _FingerprintIdentity | None:
    if fingerprint is None:
        return None
    if not isinstance(fingerprint, str) or not fingerprint:
        raise MarketRegimeStateError(f"{name} must be null or a non-empty string")
    parts = fingerprint.rsplit("|", 6)
    if len(parts) != 7:
        raise MarketRegimeStateError(f"{name} has an invalid canonical shape")
    prefix, timeframe, *numeric_parts = parts
    timestamp_and_symbol = prefix.split("|", 1)
    if (
        len(timestamp_and_symbol) != 2
        or not timestamp_and_symbol[0]
        or not timestamp_and_symbol[1]
        or not timeframe.strip()
    ):
        raise MarketRegimeStateError(f"{name} has an invalid event identity")
    timestamp = _datetime(timestamp_and_symbol[0], f"{name} timestamp")
    if timestamp is None:
        raise MarketRegimeStateError(f"{name} timestamp must not be null")
    for index, numeric_part in enumerate(numeric_parts):
        try:
            number = float.fromhex(numeric_part)
        except ValueError as exc:
            raise MarketRegimeStateError(
                f"{name} contains an invalid numeric component at index {index}"
            ) from exc
        if not isfinite(number):
            raise MarketRegimeStateError(f"{name} contains a non-finite numeric component")
    try:
        normalized_timeframe = canonical_timeframe(timeframe)
    except ValueError as exc:
        raise MarketRegimeStateError(f"{name} contains an invalid timeframe") from exc
    return _FingerprintIdentity(
        timestamp=timestamp,
        symbol=timestamp_and_symbol[1],
        timeframe=normalized_timeframe,
    )


def _legacy_base_timeframe(fingerprint: object) -> str | None:
    identity = _fingerprint_identity(fingerprint, "last_base_fingerprint")
    return None if identity is None else identity.timeframe


@dataclass(slots=True)
class RuntimeState:
    bound_symbol: str | None = None
    bound_base_timeframe: str | None = None
    last_base_open_time: datetime | None = None
    last_base_fingerprint: str | None = None
    last_open_time: datetime | None = None
    last_fingerprint: str | None = None
    processed_bars: int = 0
    prev_close: Decimal | None = None
    ema_fast: Decimal | None = None
    ema_slow: Decimal | None = None
    atr: Decimal | None = None
    tr_seed: deque[Decimal] = field(default_factory=deque)
    closes: deque[Decimal] = field(default_factory=deque)
    movements: deque[Decimal] = field(default_factory=deque)
    movement_sum: Decimal = Decimal("0")
    slow_emas: deque[Decimal] = field(default_factory=deque)
    atr_pct_window: deque[Decimal] = field(default_factory=deque)
    atr_pct_sum: Decimal = Decimal("0")
    stable_regime: Regime = Regime.UNKNOWN
    candidate_regime: Regime = Regime.UNKNOWN
    candidate_regime_count: int = 0
    stable_volatility: VolatilityLevel = VolatilityLevel.UNKNOWN
    candidate_volatility: VolatilityLevel = VolatilityLevel.UNKNOWN
    candidate_volatility_count: int = 0
    data_gap_resets: int = 0
    timeframe_gap_resets: int = 0
    duplicate_events: int = 0
    publications: int = 0

    @classmethod
    def empty(
        cls,
        config: MarketRegimeConfig,
        *,
        symbol: str | None = None,
        base_timeframe: str | None = None,
    ) -> "RuntimeState":
        return cls(
            bound_symbol=symbol,
            bound_base_timeframe=base_timeframe,
            tr_seed=deque(maxlen=config.atr_period),
            closes=deque(maxlen=config.efficiency_period + 1),
            movements=deque(maxlen=config.efficiency_period),
            slow_emas=deque(maxlen=config.slope_lookback + 1),
            atr_pct_window=deque(maxlen=config.volatility_baseline_period),
        )

    def to_mapping(self, config: MarketRegimeConfig) -> Mapping[str, object]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "config_signature": config.signature,
            "bound_symbol": self.bound_symbol,
            "bound_base_timeframe": self.bound_base_timeframe,
            "last_base_open_time": self.last_base_open_time.isoformat()
            if self.last_base_open_time
            else None,
            "last_base_fingerprint": self.last_base_fingerprint,
            "last_open_time": self.last_open_time.isoformat() if self.last_open_time else None,
            "last_fingerprint": self.last_fingerprint,
            "processed_bars": self.processed_bars,
            "prev_close": str(self.prev_close) if self.prev_close is not None else None,
            "ema_fast": str(self.ema_fast) if self.ema_fast is not None else None,
            "ema_slow": str(self.ema_slow) if self.ema_slow is not None else None,
            "atr": str(self.atr) if self.atr is not None else None,
            "tr_seed": [str(value) for value in self.tr_seed],
            "closes": [str(value) for value in self.closes],
            "movements": [str(value) for value in self.movements],
            "slow_emas": [str(value) for value in self.slow_emas],
            "atr_pct_window": [str(value) for value in self.atr_pct_window],
            "stable_regime": self.stable_regime.value,
            "candidate_regime": self.candidate_regime.value,
            "candidate_regime_count": self.candidate_regime_count,
            "stable_volatility": self.stable_volatility.value,
            "candidate_volatility": self.candidate_volatility.value,
            "candidate_volatility_count": self.candidate_volatility_count,
            "data_gap_resets": self.data_gap_resets,
            "timeframe_gap_resets": self.timeframe_gap_resets,
            "duplicate_events": self.duplicate_events,
            "publications": self.publications,
        }

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
        config: MarketRegimeConfig,
    ) -> "RuntimeState":
        schema_version = payload.get("schema_version")
        if not isinstance(schema_version, str) or schema_version not in {
            STATE_SCHEMA_VERSION,
            *LEGACY_STATE_SCHEMA_VERSIONS,
        }:
            raise MarketRegimeStateError(
                f"unsupported state schema: {schema_version!r}; expected one of "
                f"{sorted({STATE_SCHEMA_VERSION, *LEGACY_STATE_SCHEMA_VERSIONS})!r}"
            )

        allowed = {
            "schema_version",
            "config_signature",
            "bound_symbol",
            "bound_base_timeframe",
            "last_base_open_time",
            "last_base_fingerprint",
            "last_open_time",
            "last_fingerprint",
            "processed_bars",
            "prev_close",
            "ema_fast",
            "ema_slow",
            "atr",
            "tr_seed",
            "closes",
            "movements",
            "slow_emas",
            "atr_pct_window",
            "stable_regime",
            "candidate_regime",
            "candidate_regime_count",
            "stable_volatility",
            "candidate_volatility",
            "candidate_volatility_count",
            "data_gap_resets",
            "timeframe_gap_resets",
            "duplicate_events",
            "publications",
        }
        required = set(allowed)
        if schema_version in LEGACY_STATE_SCHEMA_VERSIONS:
            required.remove("bound_base_timeframe")
        unknown = sorted(set(payload) - allowed)
        missing = sorted(required - set(payload))
        if unknown or missing:
            details = []
            if unknown:
                details.append(f"unknown keys: {', '.join(unknown)}")
            if missing:
                details.append(f"missing keys: {', '.join(missing)}")
            raise MarketRegimeStateError("invalid state shape; " + "; ".join(details))
        signature = payload["config_signature"]
        if (
            not isinstance(signature, str)
            or not signature
            or signature not in config.compatible_signatures
        ):
            raise MarketRegimeStateError(
                "state configuration signature does not match module configuration"
            )

        symbol = payload["bound_symbol"]
        if symbol is not None and (not isinstance(symbol, str) or not symbol.strip()):
            raise MarketRegimeStateError("bound_symbol must be null or a non-empty string")
        base_timeframe = payload.get("bound_base_timeframe")
        if schema_version in LEGACY_STATE_SCHEMA_VERSIONS:
            base_timeframe = _legacy_base_timeframe(payload["last_base_fingerprint"])
        if base_timeframe is not None and (
            not isinstance(base_timeframe, str) or not base_timeframe.strip()
        ):
            raise MarketRegimeStateError("bound_base_timeframe must be null or a non-empty string")
        if base_timeframe is not None:
            try:
                base_timeframe = canonical_timeframe(base_timeframe)
            except ValueError as exc:
                raise MarketRegimeStateError("bound_base_timeframe is invalid") from exc
        base_fingerprint = payload["last_base_fingerprint"]
        fingerprint = payload["last_fingerprint"]
        base_identity = _fingerprint_identity(base_fingerprint, "last_base_fingerprint")
        target_identity = _fingerprint_identity(fingerprint, "last_fingerprint")
        base_fingerprint_value = cast(str | None, base_fingerprint)
        fingerprint_value = cast(str | None, fingerprint)

        state = cls.empty(config, symbol=symbol, base_timeframe=base_timeframe)
        state.last_base_open_time = _datetime(payload["last_base_open_time"], "last_base_open_time")
        state.last_base_fingerprint = base_fingerprint_value
        state.last_open_time = _datetime(payload["last_open_time"], "last_open_time")
        state.last_fingerprint = fingerprint_value
        state.processed_bars = _int(payload["processed_bars"], "processed_bars")
        state.prev_close = _optional_decimal(payload["prev_close"], "prev_close", positive=True)
        state.ema_fast = _optional_decimal(payload["ema_fast"], "ema_fast", positive=True)
        state.ema_slow = _optional_decimal(payload["ema_slow"], "ema_slow", positive=True)
        state.atr = _optional_decimal(payload["atr"], "atr")
        if state.atr is not None and state.atr < 0:
            raise MarketRegimeStateError("state value must be non-negative: atr")

        state.tr_seed.extend(
            _decimal_list(
                payload["tr_seed"],
                "tr_seed",
                maximum_length=config.atr_period,
                nonnegative=True,
            )
        )
        state.closes.extend(
            _decimal_list(
                payload["closes"],
                "closes",
                maximum_length=config.efficiency_period + 1,
                positive=True,
            )
        )
        state.movements.extend(
            _decimal_list(
                payload["movements"],
                "movements",
                maximum_length=config.efficiency_period,
                nonnegative=True,
            )
        )
        with localcontext(DECIMAL_CONTEXT):
            state.movement_sum = sum(state.movements, Decimal("0"))
        state.slow_emas.extend(
            _decimal_list(
                payload["slow_emas"],
                "slow_emas",
                maximum_length=config.slope_lookback + 1,
                positive=True,
            )
        )
        state.atr_pct_window.extend(
            _decimal_list(
                payload["atr_pct_window"],
                "atr_pct_window",
                maximum_length=config.volatility_baseline_period,
                nonnegative=True,
            )
        )
        with localcontext(DECIMAL_CONTEXT):
            state.atr_pct_sum = sum(state.atr_pct_window, Decimal("0"))

        try:
            state.stable_regime = Regime(str(payload["stable_regime"]))
            state.candidate_regime = Regime(str(payload["candidate_regime"]))
            state.stable_volatility = VolatilityLevel(str(payload["stable_volatility"]))
            state.candidate_volatility = VolatilityLevel(str(payload["candidate_volatility"]))
        except ValueError as exc:
            raise MarketRegimeStateError("state contains an unknown enum value") from exc
        state.candidate_regime_count = _int(
            payload["candidate_regime_count"], "candidate_regime_count"
        )
        state.candidate_volatility_count = _int(
            payload["candidate_volatility_count"], "candidate_volatility_count"
        )
        state.data_gap_resets = _int(payload["data_gap_resets"], "data_gap_resets")
        state.timeframe_gap_resets = _int(payload["timeframe_gap_resets"], "timeframe_gap_resets")
        state.duplicate_events = _int(payload["duplicate_events"], "duplicate_events")
        state.publications = _int(payload["publications"], "publications")

        state._validate_consistency(config)
        state._validate_fingerprint_identity(base_identity, target_identity, config)
        return state

    def _validate_fingerprint_identity(
        self,
        base_identity: _FingerprintIdentity | None,
        target_identity: _FingerprintIdentity | None,
        config: MarketRegimeConfig,
    ) -> None:
        if base_identity is not None:
            if base_identity.timestamp != self.last_base_open_time:
                raise MarketRegimeStateError(
                    "base fingerprint timestamp does not match saved base time"
                )
            if base_identity.symbol != self.bound_symbol:
                raise MarketRegimeStateError("base fingerprint symbol does not match bound_symbol")
            if base_identity.timeframe != self.bound_base_timeframe:
                raise MarketRegimeStateError(
                    "base fingerprint timeframe does not match bound base stream"
                )
        if target_identity is not None:
            if target_identity.timestamp != self.last_open_time:
                raise MarketRegimeStateError(
                    "target fingerprint timestamp does not match saved target time"
                )
            if target_identity.symbol != self.bound_symbol:
                raise MarketRegimeStateError(
                    "target fingerprint symbol does not match bound_symbol"
                )
            if target_identity.timeframe != config.timeframe:
                raise MarketRegimeStateError(
                    "target fingerprint timeframe does not match configuration"
                )

    def _validate_consistency(self, config: MarketRegimeConfig) -> None:
        if (self.last_base_open_time is None) != (self.last_base_fingerprint is None):
            raise MarketRegimeStateError("base event identity is partially saved")
        if self.bound_base_timeframe is None and self.last_base_open_time is not None:
            raise MarketRegimeStateError("saved base event identity requires bound_base_timeframe")
        if self.processed_bars == 0:
            if any(
                value is not None
                for value in (
                    self.last_open_time,
                    self.last_fingerprint,
                    self.prev_close,
                    self.ema_fast,
                    self.ema_slow,
                    self.atr,
                )
            ):
                raise MarketRegimeStateError("empty state cannot contain processed-bar fields")
            if self.bound_symbol is not None:
                # A gap reset may intentionally preserve the instrument binding.
                pass
        else:
            if self.bound_symbol is None:
                raise MarketRegimeStateError("processed state is missing bound_symbol")
            if (
                self.last_base_open_time is not None
                and self.last_open_time is not None
                and self.last_base_open_time < self.last_open_time
            ):
                raise MarketRegimeStateError("base event time precedes the processed target bar")
            if (
                self.last_open_time is None
                or self.last_fingerprint is None
                or self.prev_close is None
            ):
                raise MarketRegimeStateError("processed state is missing last bar identity")
            if self.ema_fast is None or self.ema_slow is None:
                raise MarketRegimeStateError("processed state is missing EMA values")

        expected_closes = min(self.processed_bars, config.efficiency_period + 1)
        expected_movements = min(max(self.processed_bars - 1, 0), config.efficiency_period)
        expected_slow_emas = min(self.processed_bars, config.slope_lookback + 1)
        if len(self.closes) != expected_closes:
            raise MarketRegimeStateError("close history length is inconsistent with processed_bars")
        if len(self.movements) != expected_movements:
            raise MarketRegimeStateError(
                "movement history length is inconsistent with processed_bars"
            )
        if len(self.slow_emas) != expected_slow_emas:
            raise MarketRegimeStateError(
                "slow EMA history length is inconsistent with processed_bars"
            )

        if self.processed_bars < config.atr_period:
            if self.atr is not None or len(self.tr_seed) != self.processed_bars:
                raise MarketRegimeStateError("ATR seed is inconsistent with processed_bars")
        elif self.processed_bars > 0:
            if self.atr is None or self.tr_seed:
                raise MarketRegimeStateError("initialized ATR state is inconsistent")

        expected_atr_values = max(0, self.processed_bars - config.atr_period + 1)
        expected_atr_window = min(expected_atr_values, config.volatility_baseline_period)
        if len(self.atr_pct_window) != expected_atr_window:
            raise MarketRegimeStateError(
                "ATR percentage history is inconsistent with processed_bars"
            )

        if self.candidate_regime is Regime.UNKNOWN:
            if self.candidate_regime_count != 0:
                raise MarketRegimeStateError("unknown candidate regime must have zero count")
        elif not 1 <= self.candidate_regime_count < config.regime_confirmation_bars:
            raise MarketRegimeStateError("candidate regime count is inconsistent")
        if self.candidate_volatility is VolatilityLevel.UNKNOWN:
            if self.candidate_volatility_count != 0:
                raise MarketRegimeStateError("unknown candidate volatility must have zero count")
        elif not 1 <= self.candidate_volatility_count < config.volatility_confirmation_bars:
            raise MarketRegimeStateError("candidate volatility count is inconsistent")
