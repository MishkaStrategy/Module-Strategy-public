from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime
from decimal import MAX_EMAX, MIN_EMIN, Context, Decimal, DecimalException
from threading import RLock
from types import MappingProxyType

from modular_core import (
    Candle,
    Feature,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    Side,
    StrategyModule,
)

from .config import (
    InvalidationMode,
    ProtectionTargetConfig,
    StructuralTargetSource,
    TargetMode,
    to_decimal,
)
from .constants import (
    ENTRY_PLAN_FEATURE_KEY,
    FEATURE_SCHEMA_VERSION,
    PROTECTION_PLAN_FEATURE_KEY,
    PROTECTION_REJECTION_FEATURE_KEY,
)
from .contracts import (
    EntryPlanContract,
    canonical_fingerprint,
    decimal_value,
    feature_fingerprint,
    feature_process_key,
    get_path,
    parse_entry_plan_feature,
    safe_identifier,
    select_structural_feature,
)
from .decimal_utils import decimal_text
from .exceptions import (
    ProtectionTargetConfigurationError,
    ProtectionTargetContractError,
    ProtectionTargetDataError,
)
from .models import ProtectionPlan, ProtectionRejection, ProtectionTargetReason
from .state import Counters, parse_snapshot, snapshot_payload

LOGGER = logging.getLogger(__name__)
_BPS = Decimal("10000")
_DERIVED_REWARD_PRECISION = 128


def _canonical_derived_reward_r(value: Decimal) -> Decimal:
    """Bound a derived ratio to the module's public Decimal precision."""

    return Context(
        prec=_DERIVED_REWARD_PRECISION,
        Emin=MIN_EMIN,
        Emax=MAX_EMAX,
    ).plus(value)


def _freeze_parameter_schema(
    schema: Mapping[str, Mapping[str, object]],
) -> Mapping[str, Mapping[str, object]]:
    return MappingProxyType(
        {name: MappingProxyType(dict(definition)) for name, definition in schema.items()}
    )


class ProtectionTargetModule(StrategyModule):
    """Build deterministic stop/invalidation/target plans from completed EntryPlan features.

    The module owns protection geometry only. It never sizes a position, places an order,
    rounds to exchange tick size, mutates a live position, or calls an exchange adapter.
    """

    name = "protection_target"
    version = "1.1.1"
    api_version = "1.0"
    state_schema_version = "2"

    ENTRY_FEATURE_KEY = ENTRY_PLAN_FEATURE_KEY
    PLAN_FEATURE_KEY = PROTECTION_PLAN_FEATURE_KEY
    REJECTION_FEATURE_KEY = PROTECTION_REJECTION_FEATURE_KEY
    PRODUCED_FEATURES = (PLAN_FEATURE_KEY, REJECTION_FEATURE_KEY)

    _PARAMETERS_SCHEMA: Mapping[str, Mapping[str, object]] = _freeze_parameter_schema(
        {
            "entry_feature_key": {"type": "string"},
            "invalidation_mode": {
                "type": "string",
                "enum": tuple(mode.value for mode in InvalidationMode),
            },
            "invalidation_field": {"type": "string"},
            "invalidation_feature_key": {"type": "string"},
            "invalidation_feature_field": {"type": "string"},
            "fixed_stop_percent": {"type": "number", "minimum": 0, "maximum": 1},
            "stop_buffer_bps": {"type": "number", "minimum": 0, "maximum": 10000},
            "target_mode": {"type": "string", "enum": tuple(mode.value for mode in TargetMode)},
            "structural_target_source": {
                "type": "string",
                "enum": tuple(source.value for source in StructuralTargetSource),
            },
            "target_field": {"type": "string"},
            "target_feature_key": {"type": "string"},
            "target_feature_field": {"type": "string"},
            "max_holding_seconds_field": {"type": "string"},
            "no_progress_rule_field": {"type": "string"},
            "reward_r": {"type": "number", "minimum": 0},
            "min_reward_r": {"type": "number", "minimum": 0},
            "max_reward_r": {"type": "number", "minimum": 0},
            "min_stop_distance_bps": {"type": "number", "minimum": 0},
            "max_stop_distance_bps": {"type": "number", "minimum": 0},
            "require_same_bar_entry": {"type": "boolean"},
            "require_structural_setup_match": {"type": "boolean"},
            "max_processed_plans": {"type": "integer", "minimum": 1, "maximum": 1_000_000},
            "max_diagnostic_results": {"type": "integer", "minimum": 0, "maximum": 1000},
            "emit_rejection_feature": {"type": "boolean"},
        }
    )

    def __init__(
        self,
        *,
        entry_feature_key: str = ENTRY_FEATURE_KEY,
        invalidation_mode: str = InvalidationMode.ENTRY_PLAN.value,
        invalidation_field: str = "invalidation_price",
        invalidation_feature_key: str | None = None,
        invalidation_feature_field: str = "price",
        fixed_stop_percent: float | str | Decimal = Decimal("0.005"),
        stop_buffer_bps: float | str | Decimal = Decimal("0"),
        target_mode: str = TargetMode.FIXED_R.value,
        structural_target_source: str = StructuralTargetSource.ENTRY_PLAN.value,
        target_field: str = "target_price",
        target_feature_key: str | None = None,
        target_feature_field: str = "price",
        max_holding_seconds_field: str | None = None,
        no_progress_rule_field: str | None = None,
        reward_r: float | str | Decimal = Decimal("3"),
        min_reward_r: float | str | Decimal = Decimal("0.5"),
        max_reward_r: float | str | Decimal = Decimal("20"),
        min_stop_distance_bps: float | str | Decimal = Decimal("1"),
        max_stop_distance_bps: float | str | Decimal = Decimal("5000"),
        require_same_bar_entry: bool = True,
        require_structural_setup_match: bool = False,
        max_processed_plans: int = 4096,
        max_diagnostic_results: int = 100,
        emit_rejection_feature: bool = True,
    ) -> None:
        try:
            self.config = ProtectionTargetConfig(
                entry_feature_key=entry_feature_key,
                invalidation_mode=InvalidationMode(invalidation_mode),
                invalidation_field=invalidation_field,
                invalidation_feature_key=invalidation_feature_key,
                invalidation_feature_field=invalidation_feature_field,
                fixed_stop_percent=to_decimal(fixed_stop_percent, "fixed_stop_percent"),
                stop_buffer_bps=to_decimal(stop_buffer_bps, "stop_buffer_bps"),
                target_mode=TargetMode(target_mode),
                structural_target_source=StructuralTargetSource(structural_target_source),
                target_field=target_field,
                target_feature_key=target_feature_key,
                target_feature_field=target_feature_field,
                max_holding_seconds_field=max_holding_seconds_field,
                no_progress_rule_field=no_progress_rule_field,
                reward_r=to_decimal(reward_r, "reward_r"),
                min_reward_r=to_decimal(min_reward_r, "min_reward_r"),
                max_reward_r=to_decimal(max_reward_r, "max_reward_r"),
                min_stop_distance_bps=to_decimal(min_stop_distance_bps, "min_stop_distance_bps"),
                max_stop_distance_bps=to_decimal(max_stop_distance_bps, "max_stop_distance_bps"),
                require_same_bar_entry=require_same_bar_entry,
                require_structural_setup_match=require_structural_setup_match,
                max_processed_plans=max_processed_plans,
                max_diagnostic_results=max_diagnostic_results,
                emit_rejection_feature=emit_rejection_feature,
            )
        except (ValueError, TypeError, ProtectionTargetConfigurationError) as exc:
            raise ProtectionTargetConfigurationError(
                f"invalid protection_target configuration: {exc}"
            ) from exc

        self.parameters = MappingProxyType(dict(self.config.as_parameters()))
        self.manifest = ModuleManifest(
            name=self.name,
            version=self.version,
            api_version=self.api_version,
            required_features=self.config.required_feature_keys,
            produced_features=self.PRODUCED_FEATURES,
            parameters_schema=self._PARAMETERS_SCHEMA,
            skip_until_ready=False,
            description=(
                "Deterministic protection-plan builder. Consumes EntryPlan features, calculates "
                "invalidation, stop-loss and take-profit geometry, and emits no orders or sizing decisions."
            ),
            state_schema_version=self.state_schema_version,
        )
        self._config_fingerprint = self.config.fingerprint()
        self._legacy_config_fingerprint = self.config.legacy_fingerprint_v1()
        self._math_context = Context(prec=512, Emin=MIN_EMIN, Emax=MAX_EMAX)
        self._bound_symbol: str | None = None
        self._bound_timeframe: str | None = None
        self._last_bar_time: datetime | None = None
        self._last_bar_signature: tuple[str, ...] | None = None
        self._last_event_fingerprint: str | None = None
        self._processed: OrderedDict[str, str] = OrderedDict()
        self._counters = Counters()
        self._restored = False
        self._lock = RLock()

    @property
    def metrics(self) -> Mapping[str, int]:
        """Return an immutable point-in-time observability snapshot."""

        with self._lock:
            return MappingProxyType(
                {
                    **dict(self._counters.as_mapping()),
                    "processed_plans": len(self._processed),
                }
            )

    def on_start(self) -> None:
        LOGGER.debug(
            "protection_target_started",
            extra={
                "module_name": self.name,
                "restored": self._restored,
                "processed_plans": len(self._processed),
            },
        )

    def on_finish(self) -> None:
        LOGGER.debug(
            "protection_target_finished",
            extra={"module_name": self.name, **dict(self._counters.as_mapping())},
        )

    def on_bar(self, view: MarketView) -> ModuleDecision:
        with self._lock:
            if not view.base_bars:
                return ModuleDecision(
                    skips=(ProtectionTargetReason.EMPTY_MARKET_VIEW.value,),
                    diagnostics={"event": "empty_market_view"},
                )

            bar = view.current
            if view.features is not None and view.features.timestamp != bar.close_time:
                raise ProtectionTargetDataError(
                    "feature snapshot timestamp must equal the current completed bar close"
                )
            duplicate = self._validate_stream(bar)
            if duplicate:
                if self._last_event_fingerprint is None:
                    raise ProtectionTargetDataError(
                        "duplicate event cannot be verified after legacy checkpoint migration; "
                        "advance to the next completed bar"
                    )
                event_fingerprint = self._event_fingerprint(view)
                if (
                    self._last_event_fingerprint is not None
                    and event_fingerprint != self._last_event_fingerprint
                ):
                    raise ProtectionTargetDataError(
                        f"contradictory duplicate event at {bar.timestamp.isoformat()}"
                    )
                self._counters = self._counters.increment(duplicate_bars=1)
                return ModuleDecision(
                    skips=(ProtectionTargetReason.DUPLICATE_EVENT_IGNORED.value,),
                    diagnostics={
                        "event": "duplicate_event_ignored",
                        "timestamp": bar.timestamp.isoformat(),
                        **dict(self._counters.as_mapping()),
                    },
                )

            staged_counters = self._counters.increment(bars_seen=1)

            if view.data_gap:
                staged_counters = staged_counters.increment(gap_bars=1)
                self._commit_bar(
                    bar,
                    self._event_fingerprint_from_parts(view, []),
                    self._processed,
                    staged_counters,
                )
                return ModuleDecision(
                    skips=(ProtectionTargetReason.DATA_GAP_SKIPPED.value,),
                    diagnostics={
                        "event": "data_gap_skipped",
                        "timestamp": bar.close_time.isoformat(),
                        **dict(self._counters.as_mapping()),
                    },
                )

            if view.features is None:
                staged_counters = staged_counters.increment(missing_entry_bars=1)
                self._commit_bar(
                    bar,
                    self._event_fingerprint_from_parts(view, []),
                    self._processed,
                    staged_counters,
                )
                return ModuleDecision(
                    skips=(ProtectionTargetReason.NO_FEATURE_VIEW.value,),
                    diagnostics={
                        "event": "no_feature_view",
                        **dict(self._counters.as_mapping()),
                    },
                )

            entry_features = view.features.get_all(
                self.config.entry_feature_key,
                symbol=bar.symbol,
                timeframe=bar.timeframe,
            )
            if self.config.require_same_bar_entry:
                current_features = tuple(
                    feature for feature in entry_features if feature.created_at == bar.close_time
                )
                if not current_features:
                    staged_counters = staged_counters.increment(missing_entry_bars=1)
                    self._commit_bar(
                        bar,
                        self._event_fingerprint(view),
                        self._processed,
                        staged_counters,
                    )
                    reason = (
                        ProtectionTargetReason.STALE_ENTRY_PLAN
                        if entry_features
                        else ProtectionTargetReason.NO_ENTRY_PLAN
                    )
                    return ModuleDecision(
                        skips=(reason.value,),
                        diagnostics={
                            "event": reason.value.rsplit(".", 1)[-1],
                            "visible_entry_features": len(entry_features),
                            **dict(self._counters.as_mapping()),
                        },
                    )
                entry_features = current_features

            if not entry_features:
                staged_counters = staged_counters.increment(missing_entry_bars=1)
                self._commit_bar(
                    bar,
                    self._event_fingerprint(view),
                    self._processed,
                    staged_counters,
                )
                return ModuleDecision(
                    skips=(ProtectionTargetReason.NO_ENTRY_PLAN.value,),
                    diagnostics={
                        "event": "no_entry_plan",
                        **dict(self._counters.as_mapping()),
                    },
                )

            output_features: list[Feature] = []
            skips: list[str] = []
            results: list[Mapping[str, object]] = []
            accepted = 0
            rejected = 0
            duplicate_plans = 0
            pending_processed: list[tuple[str, str]] = []
            batch_fingerprints: dict[str, str] = {}
            event_parts: list[tuple[str, str]] = []
            feature_view = view.features
            if feature_view is None:
                raise ProtectionTargetDataError(
                    "feature view disappeared during protection batch processing"
                )
            for key in self.config.required_feature_keys:
                if key == self.config.entry_feature_key:
                    continue
                event_parts.extend(
                    (key, self._feature_fingerprint_or_data_error(feature, f"feature {key!r}"))
                    for feature in feature_view.get_all(key, symbol=bar.symbol)
                )

            for entry_feature in entry_features:
                try:
                    process_key = feature_process_key(entry_feature)
                    incoming_fingerprint = self._feature_fingerprint_or_data_error(
                        entry_feature, "entry feature"
                    )
                except ProtectionTargetContractError as exc:
                    raise ProtectionTargetDataError(
                        f"entry feature identity cannot be derived deterministically: {exc}"
                    ) from exc
                event_parts.append((self.config.entry_feature_key, incoming_fingerprint))
                existing = self._processed.get(process_key)
                if existing is None:
                    existing = batch_fingerprints.get(process_key)
                if existing is not None:
                    if existing != incoming_fingerprint:
                        raise ProtectionTargetDataError(
                            f"entry plan {process_key!r} was reused with conflicting content"
                        )
                    duplicate_plans += 1
                    skips.append(ProtectionTargetReason.ENTRY_PLAN_ALREADY_PROCESSED.value)
                    self._append_result(
                        results,
                        {
                            "process_key": process_key,
                            "status": "duplicate",
                            "reason": ProtectionTargetReason.ENTRY_PLAN_ALREADY_PROCESSED.value,
                        },
                    )
                    continue

                try:
                    entry = parse_entry_plan_feature(
                        entry_feature,
                        expected_symbol=bar.symbol,
                        expected_timeframe=bar.timeframe,
                        source_feature_fingerprint=incoming_fingerprint,
                    )
                    plan = self._calculate_plan(entry, view, bar.close_time)
                except ProtectionTargetContractError as exc:
                    entry_plan_id, setup_id = self._best_effort_ids(entry_feature)
                    rejection = ProtectionRejection(
                        process_key=process_key,
                        entry_plan_id=entry_plan_id,
                        setup_id=setup_id,
                        reason=exc.reason,
                        detail=str(exc),
                        created_at=bar.close_time,
                        source_entry_feature_id=entry_feature.feature_id,
                    )
                    rejected += 1
                    skips.append(rejection.reason.value)
                    self._append_result(
                        results,
                        {
                            "process_key": process_key,
                            "status": "rejected",
                            "reason": rejection.reason.value,
                            "detail": rejection.detail,
                        },
                    )
                    if self.config.emit_rejection_feature:
                        output_features.append(self._rejection_feature(rejection, bar))
                    batch_fingerprints[process_key] = incoming_fingerprint
                    pending_processed.append((process_key, incoming_fingerprint))
                    LOGGER.info(
                        "protection_target_rejected",
                        extra={
                            "module_name": self.name,
                            "process_key": process_key,
                            "reason_code": rejection.reason.value,
                        },
                    )
                    continue

                accepted += 1
                self._append_result(
                    results,
                    {
                        "process_key": process_key,
                        "status": "accepted",
                        "protection_plan_id": plan.protection_plan_id,
                        "reward_r": decimal_text(plan.reward_r),
                    },
                )
                output_features.append(self._plan_feature(plan, bar))
                batch_fingerprints[process_key] = incoming_fingerprint
                pending_processed.append((process_key, incoming_fingerprint))
                LOGGER.info(
                    "protection_target_plan_created",
                    extra={
                        "module_name": self.name,
                        "entry_plan_id": plan.entry_plan_id,
                        "protection_plan_id": plan.protection_plan_id,
                        "side": plan.side.value,
                    },
                )

            staged_counters = staged_counters.increment(
                accepted_plans=accepted,
                rejected_plans=rejected,
                duplicate_plans=duplicate_plans,
            )
            for process_key, fingerprint in pending_processed:
                self._processed[process_key] = fingerprint
            self._prune_processed(self._processed)
            event_fingerprint = self._event_fingerprint_from_parts(view, event_parts)
            self._commit_bar(
                bar,
                event_fingerprint,
                self._processed,
                staged_counters,
            )
            return ModuleDecision(
                features=tuple(output_features),
                skips=tuple(skips),
                diagnostics={
                    "event": "protection_batch_processed",
                    "entry_features": len(entry_features),
                    "accepted": accepted,
                    "rejected": rejected,
                    "duplicates": duplicate_plans,
                    "results": tuple(results),
                    "results_truncated": max(0, len(entry_features) - len(results)),
                    "config_fingerprint": self._config_fingerprint,
                    "processed_plans": len(self._processed),
                    **dict(self._counters.as_mapping()),
                },
            )

    def snapshot_state(self) -> Mapping[str, object]:
        with self._lock:
            return snapshot_payload(
                module_version=self.version,
                config_fingerprint=self._config_fingerprint,
                bound_symbol=self._bound_symbol,
                bound_timeframe=self._bound_timeframe,
                last_bar_time=self._last_bar_time,
                last_bar_signature=self._last_bar_signature,
                last_event_fingerprint=self._last_event_fingerprint,
                processed=self._processed,
                counters=self._counters,
            )

    def restore_state(self, state: Mapping[str, object]) -> None:
        with self._lock:
            restored = parse_snapshot(
                state,
                module_version=self.version,
                config_fingerprint=self._config_fingerprint,
                legacy_config_fingerprint=self._legacy_config_fingerprint,
                compatible_module_versions=("1.1.0",),
                max_processed_plans=self.config.max_processed_plans,
            )
            self._bound_symbol = restored.bound_symbol
            self._bound_timeframe = restored.bound_timeframe
            self._last_bar_time = restored.last_bar_time
            self._last_bar_signature = restored.last_bar_signature
            self._last_event_fingerprint = restored.last_event_fingerprint
            self._processed = OrderedDict(restored.processed)
            self._counters = restored.counters
            self._restored = True

    def _calculate_plan(
        self,
        entry: EntryPlanContract,
        view: MarketView,
        created_at: datetime,
    ) -> ProtectionPlan:
        if entry.valid_until is not None and entry.valid_until < created_at:
            raise ProtectionTargetContractError(
                "entry plan is expired at decision time",
                ProtectionTargetReason.INVALID_ENTRY_PLAN,
            )
        try:
            invalidation, invalidation_feature_id = self._resolve_invalidation(entry, view)
            context = self._math_context
            context.clear_flags()
            if entry.side is Side.BUY and invalidation >= entry.entry_price:
                raise ProtectionTargetContractError(
                    "invalidation_price must be below entry_price for BUY",
                    ProtectionTargetReason.INVALID_INVALIDATION,
                )
            if entry.side is Side.SELL and invalidation <= entry.entry_price:
                raise ProtectionTargetContractError(
                    "invalidation_price must be above entry_price for SELL",
                    ProtectionTargetReason.INVALID_INVALIDATION,
                )

            buffer_fraction = context.divide(self.config.stop_buffer_bps, _BPS)
            if entry.side is Side.BUY:
                stop_loss = context.multiply(
                    invalidation, context.subtract(Decimal(1), buffer_fraction)
                )
                risk_distance = context.subtract(entry.entry_price, stop_loss)
            else:
                stop_loss = context.multiply(invalidation, context.add(Decimal(1), buffer_fraction))
                risk_distance = context.subtract(stop_loss, entry.entry_price)
            if stop_loss <= 0 or risk_distance <= 0:
                raise ProtectionTargetContractError(
                    "stop_loss geometry is non-positive",
                    ProtectionTargetReason.INVALID_GEOMETRY,
                )

            stop_distance_bps = context.multiply(
                context.divide(risk_distance, entry.entry_price), _BPS
            )
            if stop_distance_bps < self.config.min_stop_distance_bps:
                raise ProtectionTargetContractError(
                    f"stop distance {stop_distance_bps} bps is below minimum "
                    f"{self.config.min_stop_distance_bps}",
                    ProtectionTargetReason.STOP_DISTANCE_BELOW_MINIMUM,
                )
            if stop_distance_bps > self.config.max_stop_distance_bps:
                raise ProtectionTargetContractError(
                    f"stop distance {stop_distance_bps} bps is above maximum "
                    f"{self.config.max_stop_distance_bps}",
                    ProtectionTargetReason.STOP_DISTANCE_ABOVE_MAXIMUM,
                )

            target_feature_id: str | None = None
            if self.config.target_mode is TargetMode.FIXED_R:
                reward_r = self.config.reward_r
                reward_distance = context.multiply(risk_distance, reward_r)
                take_profit = (
                    context.add(entry.entry_price, reward_distance)
                    if entry.side is Side.BUY
                    else context.subtract(entry.entry_price, reward_distance)
                )
            else:
                take_profit, target_feature_id = self._resolve_structural_target(entry, view)
                reward_distance = (
                    context.subtract(take_profit, entry.entry_price)
                    if entry.side is Side.BUY
                    else context.subtract(entry.entry_price, take_profit)
                )
                if reward_distance <= 0:
                    raise ProtectionTargetContractError(
                        "structural target is not on the profitable side of entry",
                        ProtectionTargetReason.INVALID_STRUCTURAL_TARGET,
                    )
                reward_r = context.divide(reward_distance, risk_distance)

            if take_profit <= 0:
                raise ProtectionTargetContractError(
                    "take_profit must be positive",
                    ProtectionTargetReason.INVALID_GEOMETRY,
                )
            if reward_r < self.config.min_reward_r:
                raise ProtectionTargetContractError(
                    f"reward_r {reward_r} is below minimum {self.config.min_reward_r}",
                    ProtectionTargetReason.REWARD_R_BELOW_MINIMUM,
                )
            if reward_r > self.config.max_reward_r:
                raise ProtectionTargetContractError(
                    f"reward_r {reward_r} is above maximum {self.config.max_reward_r}",
                    ProtectionTargetReason.REWARD_R_ABOVE_MAXIMUM,
                )
            if self.config.target_mode is TargetMode.STRUCTURAL:
                reward_r = _canonical_derived_reward_r(reward_r)

            self._validate_geometry(
                side=entry.side,
                entry_price=entry.entry_price,
                invalidation=invalidation,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
        except ProtectionTargetContractError:
            raise
        except DecimalException as exc:
            raise ProtectionTargetContractError(
                "financial arithmetic failed",
                ProtectionTargetReason.INVALID_NUMERIC_OPERATION,
            ) from exc

        max_holding_seconds, no_progress_rule = self._resolve_lifecycle_intent(entry)

        plan_payload = {
            "entry_plan_id": entry.entry_plan_id,
            "setup_id": entry.setup_id,
            "side": entry.side.value,
            "entry_price": decimal_text(entry.entry_price),
            "invalidation_price": decimal_text(invalidation),
            "stop_loss": decimal_text(stop_loss),
            "take_profit": decimal_text(take_profit),
            "reward_r": decimal_text(reward_r),
            "invalidation_mode": self.config.invalidation_mode.value,
            "target_mode": self.config.target_mode.value,
            "source_entry_feature_id": entry.source_feature_id,
            "source_invalidation_feature_id": invalidation_feature_id,
            "source_target_feature_id": target_feature_id,
            "max_holding_seconds": max_holding_seconds,
            "no_progress_rule": no_progress_rule,
            "valid_until": entry.valid_until,
            "config_fingerprint": self._config_fingerprint,
        }
        protection_plan_id = f"protection-{canonical_fingerprint(plan_payload)[:24]}"
        return ProtectionPlan(
            protection_plan_id=protection_plan_id,
            entry_plan_id=entry.entry_plan_id,
            setup_id=entry.setup_id,
            symbol=entry.symbol,
            timeframe=entry.timeframe,
            side=entry.side,
            entry_price=entry.entry_price,
            invalidation_price=invalidation,
            stop_loss=stop_loss,
            take_profit=take_profit,
            risk_distance=risk_distance,
            reward_distance=reward_distance,
            stop_distance_bps=stop_distance_bps,
            reward_r=reward_r,
            invalidation_mode=self.config.invalidation_mode.value,
            target_mode=self.config.target_mode.value,
            created_at=created_at,
            valid_until=entry.valid_until,
            source_entry_feature_id=entry.source_feature_id,
            source_invalidation_feature_id=invalidation_feature_id,
            source_target_feature_id=target_feature_id,
            max_holding_seconds=max_holding_seconds,
            no_progress_rule=no_progress_rule,
        )

    def _resolve_lifecycle_intent(
        self,
        entry: EntryPlanContract,
    ) -> tuple[int | None, Mapping[str, object] | None]:
        max_holding_seconds: int | None = None
        if self.config.max_holding_seconds_field is not None:
            raw_max_holding = get_path(
                entry.payload,
                self.config.max_holding_seconds_field,
                "entry plan max holding seconds",
            )
            if (
                isinstance(raw_max_holding, bool)
                or not isinstance(raw_max_holding, int)
                or raw_max_holding <= 0
            ):
                raise ProtectionTargetContractError(
                    "max_holding_seconds must be a positive integer",
                    ProtectionTargetReason.INVALID_ENTRY_PLAN,
                )
            max_holding_seconds = raw_max_holding

        no_progress_rule: Mapping[str, object] | None = None
        if self.config.no_progress_rule_field is not None:
            raw_no_progress = get_path(
                entry.payload,
                self.config.no_progress_rule_field,
                "entry plan no-progress rule",
            )
            if not isinstance(raw_no_progress, Mapping):
                raise ProtectionTargetContractError(
                    "no_progress_rule must be a mapping",
                    ProtectionTargetReason.INVALID_ENTRY_PLAN,
                )
            if any(not isinstance(key, str) or not key.strip() for key in raw_no_progress):
                raise ProtectionTargetContractError(
                    "no_progress_rule keys must be non-blank strings",
                    ProtectionTargetReason.INVALID_ENTRY_PLAN,
                )
            no_progress_rule = MappingProxyType(dict(raw_no_progress))

        return max_holding_seconds, no_progress_rule

    def _resolve_invalidation(
        self,
        entry: EntryPlanContract,
        view: MarketView,
    ) -> tuple[Decimal, str | None]:
        if self.config.invalidation_mode is InvalidationMode.ENTRY_PLAN:
            value = get_path(
                entry.payload,
                self.config.invalidation_field,
                "entry plan invalidation",
                missing_reason=ProtectionTargetReason.MISSING_INVALIDATION,
            )
            return decimal_value(
                value,
                "invalidation_price",
                reason=ProtectionTargetReason.INVALID_INVALIDATION,
            ), None
        if self.config.invalidation_mode is InvalidationMode.FIXED_PERCENT:
            context = self._math_context
            context.clear_flags()
            if entry.side is Side.BUY:
                return (
                    context.multiply(
                        entry.entry_price,
                        context.subtract(Decimal(1), self.config.fixed_stop_percent),
                    ),
                    None,
                )
            return (
                context.multiply(
                    entry.entry_price,
                    context.add(Decimal(1), self.config.fixed_stop_percent),
                ),
                None,
            )

        invalidation_feature_key = self.config.invalidation_feature_key
        if invalidation_feature_key is None:
            raise ProtectionTargetConfigurationError(
                "feature invalidation requires invalidation_feature_key"
            )
        feature = self._structural_feature(
            view,
            invalidation_feature_key,
            entry.setup_id,
        )
        if feature is None:
            raise ProtectionTargetContractError(
                f"missing structural invalidation feature {invalidation_feature_key!r}",
                ProtectionTargetReason.MISSING_INVALIDATION,
            )
        if not isinstance(feature.value, Mapping):
            raise ProtectionTargetContractError(
                "structural invalidation feature value must be a mapping",
                ProtectionTargetReason.INVALID_INVALIDATION,
            )
        value = get_path(
            feature.value,
            self.config.invalidation_feature_field,
            "structural invalidation feature",
            missing_reason=ProtectionTargetReason.MISSING_INVALIDATION,
        )
        return decimal_value(
            value,
            "invalidation_price",
            reason=ProtectionTargetReason.INVALID_INVALIDATION,
        ), feature.feature_id

    def _resolve_structural_target(
        self,
        entry: EntryPlanContract,
        view: MarketView,
    ) -> tuple[Decimal, str | None]:
        if self.config.structural_target_source is StructuralTargetSource.ENTRY_PLAN:
            value = get_path(
                entry.payload,
                self.config.target_field,
                "entry plan structural target",
                missing_reason=ProtectionTargetReason.MISSING_STRUCTURAL_TARGET,
            )
            return decimal_value(
                value,
                "target_price",
                reason=ProtectionTargetReason.INVALID_STRUCTURAL_TARGET,
            ), None

        target_feature_key = self.config.target_feature_key
        if target_feature_key is None:
            raise ProtectionTargetConfigurationError(
                "structural feature target requires target_feature_key"
            )
        feature = self._structural_feature(view, target_feature_key, entry.setup_id)
        if feature is None:
            raise ProtectionTargetContractError(
                f"missing structural target feature {target_feature_key!r}",
                ProtectionTargetReason.MISSING_STRUCTURAL_TARGET,
            )
        if not isinstance(feature.value, Mapping):
            raise ProtectionTargetContractError(
                "structural target feature value must be a mapping",
                ProtectionTargetReason.INVALID_STRUCTURAL_TARGET,
            )
        value = get_path(
            feature.value,
            self.config.target_feature_field,
            "structural target feature",
            missing_reason=ProtectionTargetReason.MISSING_STRUCTURAL_TARGET,
        )
        return decimal_value(
            value,
            "target_price",
            reason=ProtectionTargetReason.INVALID_STRUCTURAL_TARGET,
        ), feature.feature_id

    def _structural_feature(
        self,
        view: MarketView,
        key: str,
        setup_id: str,
    ) -> Feature | None:
        feature_view = view.features
        if feature_view is None:
            raise ProtectionTargetDataError(
                "structural feature resolution requires a point-in-time FeatureView"
            )
        features = feature_view.get_all(key, symbol=view.current.symbol)
        return select_structural_feature(
            features,
            setup_id=setup_id,
            require_setup_match=self.config.require_structural_setup_match,
        )

    @staticmethod
    def _validate_geometry(
        *,
        side: Side,
        entry_price: Decimal,
        invalidation: Decimal,
        stop_loss: Decimal,
        take_profit: Decimal,
    ) -> None:
        if side is Side.BUY:
            valid = stop_loss <= invalidation < entry_price < take_profit
        else:
            valid = take_profit < entry_price < invalidation <= stop_loss
        if not valid:
            raise ProtectionTargetContractError(
                "invalid trade geometry for side: expected BUY stop<=invalidation<entry<target "
                "or SELL target<entry<invalidation<=stop",
                ProtectionTargetReason.INVALID_GEOMETRY,
            )

    def _plan_feature(self, plan: ProtectionPlan, bar: Candle) -> Feature:
        return Feature(
            key=self.PLAN_FEATURE_KEY,
            source_module=self.name,
            created_at=plan.created_at,
            value=plan.as_mapping(),
            symbol=plan.symbol,
            timeframe=plan.timeframe,
            valid_until=plan.valid_until,
            schema_version=FEATURE_SCHEMA_VERSION,
            feature_id=f"{plan.protection_plan_id}:plan",
            invalidate_on_gap=True,
            metadata={
                "event": ProtectionTargetReason.PLAN_CREATED.value,
                "setup_id": plan.setup_id,
                "entry_plan_id": plan.entry_plan_id,
                "bar_close_time": bar.close_time.isoformat(),
            },
        )

    def _rejection_feature(self, rejection: ProtectionRejection, bar: Candle) -> Feature:
        digest = canonical_fingerprint(
            {
                "process_key": rejection.process_key,
                "reason": rejection.reason.value,
                "detail": rejection.detail,
            }
        )[:24]
        return Feature(
            key=self.REJECTION_FEATURE_KEY,
            source_module=self.name,
            created_at=rejection.created_at,
            value=rejection.as_mapping(),
            symbol=bar.symbol,
            timeframe=bar.timeframe,
            valid_until=bar.close_time,
            schema_version=FEATURE_SCHEMA_VERSION,
            feature_id=f"protection-rejection-{digest}",
            invalidate_on_gap=True,
            metadata={"event": "rejected", "reason": rejection.reason.value},
        )

    @staticmethod
    def _feature_fingerprint_or_data_error(feature: Feature, context: str) -> str:
        try:
            return feature_fingerprint(feature)
        except ProtectionTargetContractError as exc:
            raise ProtectionTargetDataError(
                f"{context} cannot be fingerprinted deterministically: {exc}"
            ) from exc

    def _event_fingerprint(self, view: MarketView) -> str:
        parts: list[tuple[str, str]] = []
        if not view.data_gap and view.features is not None:
            for key in self.config.required_feature_keys:
                parts.extend(
                    (key, self._feature_fingerprint_or_data_error(feature, f"feature {key!r}"))
                    for feature in view.features.get_all(key, symbol=view.current.symbol)
                )
        return self._event_fingerprint_from_parts(view, parts)

    def _event_fingerprint_from_parts(
        self,
        view: MarketView,
        parts: list[tuple[str, str]],
    ) -> str:
        snapshot = view.features.timestamp.isoformat() if view.features is not None else "none"
        values = [
            "protection_target.event.v1",
            *self._bar_signature(view.current),
            "gap:1" if view.data_gap else "gap:0",
            f"snapshot:{snapshot}",
        ]
        for key, fingerprint in sorted(parts):
            values.extend((key, fingerprint))

        digest = hashlib.sha256()
        for value in values:
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        return digest.hexdigest()

    def _validate_stream(self, bar: Candle) -> bool:
        signature = self._bar_signature(bar)
        if self._bound_symbol is not None:
            if bar.symbol != self._bound_symbol or bar.timeframe != self._bound_timeframe:
                raise ProtectionTargetDataError(
                    "one ProtectionTargetModule instance cannot switch symbol or timeframe"
                )
        if self._last_bar_time is None:
            return False
        if bar.timestamp == self._last_bar_time:
            if signature != self._last_bar_signature:
                raise ProtectionTargetDataError(
                    f"contradictory duplicate bar at {bar.timestamp.isoformat()}"
                )
            return True
        if bar.timestamp < self._last_bar_time:
            raise ProtectionTargetDataError(
                f"out-of-order bar {bar.timestamp.isoformat()} after "
                f"{self._last_bar_time.isoformat()}"
            )
        return False

    @staticmethod
    def _bar_signature(bar: Candle) -> tuple[str, ...]:
        return (
            bar.timestamp.isoformat(),
            str(bar.open),
            str(bar.high),
            str(bar.low),
            str(bar.close),
            str(bar.volume),
            str(bar.symbol),
            str(bar.timeframe),
        )

    def _commit_bar(
        self,
        bar: Candle,
        event_fingerprint: str,
        processed: OrderedDict[str, str],
        counters: Counters,
    ) -> None:
        if self._bound_symbol is None:
            self._bound_symbol = bar.symbol
            self._bound_timeframe = bar.timeframe
        self._processed = processed
        self._counters = counters
        self._last_bar_time = bar.timestamp
        self._last_bar_signature = self._bar_signature(bar)
        self._last_event_fingerprint = event_fingerprint

    def _prune_processed(self, processed: OrderedDict[str, str]) -> None:
        while len(processed) > self.config.max_processed_plans:
            processed.popitem(last=False)

    def _append_result(
        self,
        results: list[Mapping[str, object]],
        result: Mapping[str, object],
    ) -> None:
        if len(results) < self.config.max_diagnostic_results:
            results.append(result)

    @staticmethod
    def _best_effort_ids(feature: Feature) -> tuple[str | None, str | None]:
        if not isinstance(feature.value, Mapping):
            return None, None
        return (
            safe_identifier(feature.value.get("entry_plan_id")),
            safe_identifier(feature.value.get("setup_id")),
        )
