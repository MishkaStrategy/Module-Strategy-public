from __future__ import annotations

from .module_shared import *  # noqa: F401,F403


class _FvgInitMixin:
    """Strict, bounded three-candle Fair Value Gap producer for Module API v1 draft."""

    name = "fvg"

    def __init__(
        self,
        *,
        timeframes: Sequence[str] = ("15m", "4h"),
        direction_filter: str = "both",
        min_gap_absolute: str | int | float | Decimal = "0",
        min_gap_fraction: str | int | float | Decimal = "0",
        invalidation_level: str = "full_fill",
        invalidation_basis: str = "wick",
        require_consecutive_bars: bool = True,
        order_violation_policy: str = "raise",
        publish_lifecycle_events: bool = True,
        max_active_zones_per_timeframe: int = 4096,
        max_state_bytes: int = 8 * 1024 * 1024,
        gap_size_mode: str = "legacy",
        min_gap_size: str | int | float | Decimal = "0",
        max_gap_size: str | int | float | Decimal | None = None,
        tick_size: str | int | float | Decimal | None = None,
        atr_period: int = 14,
        average_range_period: int = 20,
        price_filter_mode: str = "disabled",
        price_filter_min: str | int | float | Decimal | None = None,
        price_filter_max: str | int | float | Decimal | None = None,
        price_filter_anchor: str | int | float | Decimal | None = None,
        price_filter_lower_fraction: str | int | float | Decimal = "0",
        price_filter_upper_fraction: str | int | float | Decimal = "0",
        price_filter_match_policy: str = "midpoint",
        price_filter_target: str = "both",
        volume_filter_mode: str = "disabled",
        volume_lookback: int = 20,
        volume_min_ratio: str | int | float | Decimal = "0",
        volume_max_ratio: str | int | float | Decimal | None = None,
        volume_missing_policy: str = "reject",
        volume_filter_target: str = "both",
        pre_fvg_enabled: bool = False,
        pre_fvg_evaluation_offset: str = "3m",
        publish_evaluation_events: bool = False,
        max_zone_age_bars: int = 0,
        source_name: str = "market_view",
        timeframe_overrides: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None:
        self.config = FvgConfig.from_parameters(
            timeframes=timeframes,
            direction_filter=direction_filter,
            min_gap_absolute=min_gap_absolute,
            min_gap_fraction=min_gap_fraction,
            invalidation_level=invalidation_level,
            invalidation_basis=invalidation_basis,
            require_consecutive_bars=require_consecutive_bars,
            order_violation_policy=order_violation_policy,
            publish_lifecycle_events=publish_lifecycle_events,
            max_active_zones_per_timeframe=max_active_zones_per_timeframe,
            max_state_bytes=max_state_bytes,
            gap_size_mode=gap_size_mode,
            min_gap_size=min_gap_size,
            max_gap_size=max_gap_size,
            tick_size=tick_size,
            atr_period=atr_period,
            average_range_period=average_range_period,
            price_filter_mode=price_filter_mode,
            price_filter_min=price_filter_min,
            price_filter_max=price_filter_max,
            price_filter_anchor=price_filter_anchor,
            price_filter_lower_fraction=price_filter_lower_fraction,
            price_filter_upper_fraction=price_filter_upper_fraction,
            price_filter_match_policy=price_filter_match_policy,
            price_filter_target=price_filter_target,
            volume_filter_mode=volume_filter_mode,
            volume_lookback=volume_lookback,
            volume_min_ratio=volume_min_ratio,
            volume_max_ratio=volume_max_ratio,
            volume_missing_policy=volume_missing_policy,
            volume_filter_target=volume_filter_target,
            pre_fvg_enabled=pre_fvg_enabled,
            pre_fvg_evaluation_offset=pre_fvg_evaluation_offset,
            publish_evaluation_events=publish_evaluation_events,
            max_zone_age_bars=max_zone_age_bars,
            source_name=source_name,
            timeframe_overrides={} if timeframe_overrides is None else timeframe_overrides,
        )
        self.parameters = self.config.as_parameters()
        self.manifest = ModuleManifest(
            name=self.name,
            version=MODULE_VERSION,
            api_version="1.0",
            required_timeframes=self.config.timeframes,
            produced_features=(
                ACTIVE_ZONE_KEY,
                CREATED_EVENT_KEY,
                INVALIDATED_EVENT_KEY,
                EVALUATION_EVENT_KEY,
                PRE_CANDIDATE_KEY,
                PRE_STATUS_KEY,
                ZONE_STATE_KEY,
            ),
            warmup_bars={timeframe: 3 for timeframe in self.config.timeframes},
            parameters_schema={
                "timeframes": {
                    "type": "array",
                    "default": ("15m", "4h"),
                },
                "direction_filter": {
                    "type": "string",
                    "enum": tuple(item.value for item in FvgDirectionFilter),
                    "default": FvgDirectionFilter.BOTH.value,
                },
                "min_gap_absolute": {"type": "string", "default": "0"},
                "min_gap_fraction": {"type": "string", "default": "0"},
                "invalidation_level": {
                    "type": "string",
                    "enum": tuple(item.value for item in FvgInvalidationLevel),
                    "default": FvgInvalidationLevel.FULL_FILL.value,
                },
                "invalidation_basis": {
                    "type": "string",
                    "enum": tuple(item.value for item in FvgInvalidationBasis),
                    "default": FvgInvalidationBasis.WICK.value,
                },
                "require_consecutive_bars": {"type": "boolean", "default": True},
                "order_violation_policy": {
                    "type": "string",
                    "enum": tuple(item.value for item in FvgOrderViolationPolicy),
                    "default": FvgOrderViolationPolicy.RAISE.value,
                },
                "publish_lifecycle_events": {"type": "boolean", "default": True},
                "max_active_zones_per_timeframe": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100_000,
                    "default": 4096,
                },
                "max_state_bytes": {
                    "type": "integer",
                    "minimum": 1024,
                    "maximum": 64 * 1024 * 1024,
                    "default": 8 * 1024 * 1024,
                },
                "gap_size_mode": {
                    "type": "string",
                    "enum": tuple(item.value for item in FvgGapSizeMode),
                    "default": FvgGapSizeMode.LEGACY.value,
                },
                "min_gap_size": {"type": "string", "default": "0"},
                "max_gap_size": {"default": None},
                "tick_size": {"default": None},
                "atr_period": {"type": "integer", "minimum": 1, "maximum": 100_000, "default": 14},
                "average_range_period": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100_000,
                    "default": 20,
                },
                "price_filter_mode": {
                    "type": "string",
                    "enum": tuple(item.value for item in FvgPriceFilterMode),
                    "default": FvgPriceFilterMode.DISABLED.value,
                },
                "price_filter_min": {"default": None},
                "price_filter_max": {"default": None},
                "price_filter_anchor": {"default": None},
                "price_filter_lower_fraction": {"type": "string", "default": "0"},
                "price_filter_upper_fraction": {"type": "string", "default": "0"},
                "price_filter_match_policy": {
                    "type": "string",
                    "enum": tuple(item.value for item in FvgPriceMatchPolicy),
                    "default": FvgPriceMatchPolicy.MIDPOINT.value,
                },
                "price_filter_target": {
                    "type": "string",
                    "enum": tuple(item.value for item in FvgFilterTarget),
                    "default": FvgFilterTarget.BOTH.value,
                },
                "volume_filter_mode": {
                    "type": "string",
                    "enum": tuple(item.value for item in FvgVolumeFilterMode),
                    "default": FvgVolumeFilterMode.DISABLED.value,
                },
                "volume_lookback": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100_000,
                    "default": 20,
                },
                "volume_min_ratio": {"type": "string", "default": "0"},
                "volume_max_ratio": {"default": None},
                "volume_missing_policy": {
                    "type": "string",
                    "enum": tuple(item.value for item in FvgMissingDataPolicy),
                    "default": FvgMissingDataPolicy.REJECT.value,
                },
                "volume_filter_target": {
                    "type": "string",
                    "enum": tuple(item.value for item in FvgFilterTarget),
                    "default": FvgFilterTarget.BOTH.value,
                },
                "pre_fvg_enabled": {"type": "boolean", "default": False},
                "pre_fvg_evaluation_offset": {
                    "type": "string",
                    "default": "3m",
                },
                "publish_evaluation_events": {"type": "boolean", "default": False},
                "max_zone_age_bars": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 10_000_000,
                    "default": 0,
                },
                "source_name": {"type": "string", "default": "market_view"},
                "timeframe_overrides": {"type": "object", "default": {}},
            },
            skip_until_ready=False,
            description=(
                "Detects strict three-candle bullish/bearish Fair Value Gaps on completed bars, "
                "publishes bounded active zones and removes them deterministically after mitigation."
            ),
            state_schema_version=STATE_SCHEMA_VERSION,
        )
        self._zones: dict[str, FvgZone] = {}
        self._zone_lifecycle: dict[str, FvgZoneLifecycle] = {}
        self._pre_candidates: dict[str, FvgPreCandidate] = {}
        self._pre_evaluated: dict[tuple[str, str], datetime] = {}
        self._last_processed: dict[tuple[str, str], datetime] = {}
        self._last_fingerprints: dict[tuple[str, str], str | None] = {}
        self._history_fingerprints: dict[tuple[str, str], tuple[tuple[datetime, str], ...]] = {}
        self._history_references: dict[tuple[str, str], tuple[_HistoryReference, ...]] = {}
        self._last_signatures: dict[tuple[str, str], _BarSignature | None] = {}
        self._barriers: dict[tuple[str, str], datetime] = {}
        self._bound_symbol: str | None = None
        self._config_fingerprint = self._fingerprint_parameters(self.parameters)
        self._v3_config_fingerprint = self._fingerprint_parameters(self.config.v3_parameters())
        v3_compatible_config = FvgConfig.from_parameters(**dict(self.config.v3_parameters()))
        self._v3_compatible_configuration = self.parameters == v3_compatible_config.as_parameters()
        self._v2_config_fingerprint = self._fingerprint_parameters(self.config.v2_parameters())
        self._legacy_config_fingerprint = self._fingerprint_parameters(self.config.legacy_v1_parameters())
        self._republish_pending = False
        self._state_size_bytes = self._state_size(self._state_payload())
