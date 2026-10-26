from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC
from decimal import Decimal
from types import MappingProxyType

from modular_core import Candle, Feature, MarketView, ModuleDecision, StrategyModule

from .config import EntryModelConfig
from .enums import (
    EntryKind,
    EntryModelDataError,
    EntryRejectionReason,
    SetupPayloadError,
)
from .manifest import (
    ENTRY_PLAN_FEATURE_KEY,
    ENTRY_REJECTION_FEATURE_KEY,
    MANIFEST,
    SETUP_FEATURE_KEY,
)
from .planner import build_entry_plan
from .primitives import canonical_json, sha256_hex
from .setup import ConfirmedSetup, source_feature_fingerprint
from .state import EntryModelState


class EntryModelModule(StrategyModule):
    """Deterministic setup-to-entry-plan transformer.

    This module never sizes positions, calculates protection/targets, applies
    exchange tick/step rules, or submits orders. It only turns a canonical
    confirmed setup into an immutable EntryPlan feature.
    """

    name = MANIFEST.name
    manifest = MANIFEST

    def __init__(
        self,
        *,
        default_entry_kind: str | EntryKind = EntryKind.LIMIT_RETRACEMENT,
        retracement_fraction: int | float | str | Decimal = Decimal("0.382"),
        ttl_bars: int = 36,
        honor_setup_hint: bool = True,
        post_only_limits: bool = True,
        stop_limit_offset_bps: int | float | str | Decimal = Decimal("0"),
        max_setup_age_seconds: int | float | str | Decimal = Decimal("0"),
        max_processed_setups: int = 10_000,
        allowed_entry_kinds: tuple[str | EntryKind, ...] = tuple(EntryKind),
    ) -> None:
        self.config = EntryModelConfig.from_parameters(
            default_entry_kind=default_entry_kind,
            retracement_fraction=retracement_fraction,
            ttl_bars=ttl_bars,
            honor_setup_hint=honor_setup_hint,
            post_only_limits=post_only_limits,
            stop_limit_offset_bps=stop_limit_offset_bps,
            max_setup_age_seconds=max_setup_age_seconds,
            max_processed_setups=max_processed_setups,
            allowed_entry_kinds=allowed_entry_kinds,
        )
        self.parameters = self.config.as_parameters()
        self._state = EntryModelState(
            self.config.max_processed_setups,
            self.config.fingerprint,
        )

    def on_bar(self, view: MarketView) -> ModuleDecision:
        duplicate = self._validate_stream(view.current)
        if duplicate:
            self._state.increment("duplicate_events")
            return self._skip(EntryRejectionReason.DUPLICATE_EVENT)

        try:
            if view.data_gap:
                self._state.increment("data_gaps")
                return self._skip(EntryRejectionReason.DATA_GAP)
            if view.features is None:
                self._state.increment("feature_view_unavailable")
                return self._skip(EntryRejectionReason.FEATURE_VIEW_UNAVAILABLE)
            setup_features = view.features.get_all(
                SETUP_FEATURE_KEY,
                symbol=view.current.symbol,
            )
            if not setup_features:
                return self._decision(diagnostics={"setup_features_seen": 0})
            return self._process_features(view, setup_features)
        finally:
            self._complete_bar(view.current)

    def _process_features(
        self,
        view: MarketView,
        setup_features: tuple[Feature, ...],
    ) -> ModuleDecision:
        outputs: list[Feature] = []
        skips: list[str] = []
        rejection_counts: dict[str, int] = defaultdict(int)
        planned = rejected = duplicate = conflicts = 0

        valid_candidates: list[tuple[Feature, str, str, ConfirmedSetup, str]] = []
        malformed_candidates: list[tuple[Feature, str, str, str]] = []
        batch_sources: dict[str, str] = {}
        for feature in setup_features:
            try:
                source_fingerprint = source_feature_fingerprint(feature)
                fingerprint_error: str | None = None
            except (TypeError, ValueError) as exc:
                fingerprint_error = str(exc)
                source_fingerprint = sha256_hex(
                    {
                        "feature_id": feature.feature_id,
                        "key": feature.key,
                        "created_at": feature.created_at.isoformat(),
                        "symbol": feature.symbol,
                        "timeframe": feature.timeframe,
                        "schema_version": feature.schema_version,
                        "normalization_error": fingerprint_error,
                    }
                )
            source_id = feature.feature_id or f"anon-{source_fingerprint[:32]}"
            batch_previous = batch_sources.get(source_id)
            if batch_previous is not None:
                if batch_previous == source_fingerprint:
                    duplicate += 1
                    skips.append(EntryRejectionReason.DUPLICATE_SETUP.value)
                    self._state.increment("duplicates")
                    continue
                conflicts += 1
                reason = EntryRejectionReason.SOURCE_FEATURE_CONFLICT
                skips.append(reason.value)
                rejection_counts[reason.value] += 1
                outputs.append(
                    self._rejection_feature(
                        view=view,
                        setup_id=None,
                        reason=reason,
                        details="source feature_id was reused with different content in one batch",
                        source_id=source_id,
                        source_fingerprint=source_fingerprint,
                    )
                )
                self._state.remember_feature(source_id, source_fingerprint, "conflict")
                self._state.increment("source_conflicts")
                continue
            batch_sources[source_id] = source_fingerprint
            if fingerprint_error is not None:
                malformed_candidates.append(
                    (feature, source_id, source_fingerprint, fingerprint_error)
                )
                continue
            previous_source = self._state.seen_features.get(source_id)
            if previous_source is not None:
                if previous_source.fingerprint == source_fingerprint:
                    duplicate += 1
                    skips.append(EntryRejectionReason.DUPLICATE_SETUP.value)
                    self._state.increment("duplicates")
                    continue
                conflicts += 1
                reason = EntryRejectionReason.SOURCE_FEATURE_CONFLICT
                skips.append(reason.value)
                rejection_counts[reason.value] += 1
                outputs.append(
                    self._rejection_feature(
                        view=view,
                        setup_id=None,
                        reason=reason,
                        details="source feature_id was reused with different content",
                        source_id=source_id,
                        source_fingerprint=source_fingerprint,
                    )
                )
                self._state.remember_feature(
                    source_id,
                    source_fingerprint,
                    "conflict",
                )
                self._state.increment("source_conflicts")
                continue
            try:
                setup = ConfirmedSetup.from_feature(
                    feature,
                    fallback_symbol=view.current.symbol,
                )
                valid_candidates.append(
                    (feature, source_id, source_fingerprint, setup, setup.fingerprint())
                )
            except (SetupPayloadError, ValueError, TypeError) as exc:
                malformed_candidates.append((feature, source_id, source_fingerprint, str(exc)))

        # Handle malformed inputs once per deterministic source identity.
        for feature, source_id, source_fingerprint, error in malformed_candidates:
            reason = self._payload_reason(feature, error)
            rejected += 1
            skips.append(reason.value)
            rejection_counts[reason.value] += 1
            outputs.append(
                self._rejection_feature(
                    view=view,
                    setup_id=None,
                    reason=reason,
                    details=error,
                    source_id=source_id,
                    source_fingerprint=source_fingerprint,
                )
            )
            self._state.remember_feature(
                source_id,
                source_fingerprint,
                "rejected",
            )
            self._state.increment("rejections")

        # Group first, then decide, so conflicting revisions never depend on input ordering.
        grouped: dict[str, list[tuple[Feature, str, str, ConfirmedSetup, str]]] = defaultdict(list)
        for row in valid_candidates:
            grouped[row[3].setup_id].append(row)

        for setup_id in sorted(grouped):
            rows = sorted(
                grouped[setup_id],
                key=lambda row: (
                    row[0].created_at,
                    row[1],
                    row[4],
                ),
            )
            unique_fingerprints = {row[4] for row in rows}
            if len(unique_fingerprints) > 1:
                conflicts += 1
                reason = EntryRejectionReason.SETUP_REVISION_CONFLICT
                skips.append(reason.value)
                rejection_counts[reason.value] += 1
                for _feature, source_id, source_fingerprint, _setup, _setup_fp in rows:
                    self._state.remember_feature(
                        source_id,
                        source_fingerprint,
                        "conflict",
                    )
                conflict_fingerprint = sha256_hex(tuple(sorted(unique_fingerprints)))
                self._state.remember(setup_id, conflict_fingerprint, "conflict")
                outputs.append(
                    self._rejection_feature(
                        view=view,
                        setup_id=setup_id,
                        reason=reason,
                        details="same setup_id appears with conflicting canonical payloads",
                        source_id="batch:" + setup_id,
                        source_fingerprint=conflict_fingerprint,
                    )
                )
                self._state.increment("setup_conflicts")
                continue

            feature, source_id, source_fingerprint, setup, setup_fingerprint = rows[0]
            if len(rows) > 1:
                duplicate += len(rows) - 1
                self._state.increment("duplicates", len(rows) - 1)
                for _f, duplicate_source_id, duplicate_source_fp, _s, _fp in rows[1:]:
                    self._state.remember_feature(
                        duplicate_source_id,
                        duplicate_source_fp,
                        "duplicate",
                    )

            previous = self._state.processed.get(setup_id)
            if previous is not None:
                if previous.fingerprint == setup_fingerprint:
                    duplicate += 1
                    skips.append(EntryRejectionReason.DUPLICATE_SETUP.value)
                    self._state.remember_feature(
                        source_id,
                        source_fingerprint,
                        "duplicate",
                    )
                    self._state.increment("duplicates")
                    continue
                reason = EntryRejectionReason.SETUP_REVISION_CONFLICT
                conflicts += 1
                skips.append(reason.value)
                rejection_counts[reason.value] += 1
                outputs.append(
                    self._rejection_feature(
                        view=view,
                        setup_id=setup_id,
                        reason=reason,
                        details="setup_id was previously processed with different content",
                        source_id=source_id,
                        source_fingerprint=source_fingerprint,
                    )
                )
                self._state.remember_feature(
                    source_id,
                    source_fingerprint,
                    "conflict",
                )
                self._state.increment("setup_conflicts")
                continue

            rejection = self._validate_setup(feature, setup, view)
            if rejection is not None:
                reason, details = rejection
                rejected += 1
                skips.append(reason.value)
                rejection_counts[reason.value] += 1
                output = self._rejection_feature(
                    view=view,
                    setup_id=setup_id,
                    reason=reason,
                    details=details,
                    source_id=source_id,
                    source_fingerprint=source_fingerprint,
                )
                outputs.append(output)
                self._state.remember(
                    setup_id,
                    setup_fingerprint,
                    "rejected",
                    output.feature_id,
                )
                self._state.remember_feature(
                    source_id,
                    source_fingerprint,
                    "rejected",
                )
                self._state.increment("rejections")
                continue

            try:
                plan = build_entry_plan(
                    config=self.config,
                    manifest_version=self.manifest.version,
                    setup=setup,
                    setup_fingerprint=setup_fingerprint,
                    view=view,
                )
            except ValueError as exc:
                reason = self._reason_from_build_error(str(exc))
                rejected += 1
                skips.append(reason.value)
                rejection_counts[reason.value] += 1
                output = self._rejection_feature(
                    view=view,
                    setup_id=setup_id,
                    reason=reason,
                    details=str(exc),
                    source_id=source_id,
                    source_fingerprint=source_fingerprint,
                )
                outputs.append(output)
                self._state.remember(
                    setup_id,
                    setup_fingerprint,
                    "rejected",
                    output.feature_id,
                )
                self._state.remember_feature(
                    source_id,
                    source_fingerprint,
                    "rejected",
                )
                self._state.increment("rejections")
                continue

            planned += 1
            output = Feature(
                key=ENTRY_PLAN_FEATURE_KEY,
                source_module=self.name,
                created_at=view.current.close_time,
                value=plan.to_feature_value(),
                symbol=setup.symbol,
                timeframe=view.current.timeframe,
                valid_until=plan.expires_at,
                schema_version=plan.schema_version,
                feature_id=plan.plan_id,
                invalidate_on_gap=True,
                metadata={
                    "setup_id": setup.setup_id,
                    "entry_kind": plan.entry_kind.value,
                    "config_fingerprint": self.config.fingerprint,
                },
            )
            outputs.append(output)
            self._state.remember(
                setup_id,
                setup_fingerprint,
                "planned",
                plan.plan_id,
            )
            self._state.remember_feature(
                source_id,
                source_fingerprint,
                "planned",
            )
            self._state.increment("plans")

        return self._decision(
            features=tuple(outputs),
            skips=tuple(skips),
            diagnostics={
                "setup_features_seen": len(setup_features),
                "plans_created": planned,
                "setups_rejected": rejected,
                "duplicates_ignored": duplicate,
                "conflicts_rejected": conflicts,
                "processed_cache_size": len(self._state.processed),
                "seen_feature_cache_size": len(self._state.seen_features),
                "rejection_counts": dict(sorted(rejection_counts.items())),
                "config_fingerprint": self.config.fingerprint,
            },
        )

    def snapshot_state(self) -> Mapping[str, object]:
        return self._state.snapshot()

    def restore_state(self, state: Mapping[str, object]) -> None:
        self._state.restore(state)

    def _validate_setup(
        self,
        feature: Feature,
        setup: ConfirmedSetup,
        view: MarketView,
    ) -> tuple[EntryRejectionReason, str] | None:
        if setup.status != "confirmed":
            return EntryRejectionReason.SETUP_NOT_CONFIRMED, "setup status is not confirmed"
        if setup.symbol != view.current.symbol:
            return (
                EntryRejectionReason.SYMBOL_MISMATCH,
                f"setup symbol {setup.symbol} does not match {view.current.symbol}",
            )
        if setup.confirmed_at > feature.created_at:
            return (
                EntryRejectionReason.FEATURE_CAUSALITY_VIOLATION,
                "setup confirmed_at is later than feature publication time",
            )
        now = view.current.close_time
        if feature.created_at > now or setup.confirmed_at > now:
            return EntryRejectionReason.FUTURE_SETUP, "setup or feature is in the future"
        age = Decimal(str((now - setup.confirmed_at).total_seconds()))
        if age > self.config.max_setup_age_seconds:
            return (
                EntryRejectionReason.STALE_SETUP,
                f"setup age {age}s exceeds {self.config.max_setup_age_seconds}s",
            )
        selected = (
            setup.entry_kind_hint
            if self.config.honor_setup_hint and setup.entry_kind_hint is not None
            else self.config.default_entry_kind
        )
        if selected not in self.config.allowed_entry_kind_set:
            return (
                EntryRejectionReason.ENTRY_KIND_NOT_ALLOWED,
                f"entry kind {selected.value} is disabled",
            )
        return None

    def _validate_stream(self, bar: Candle) -> bool:
        if self._state.bound_symbol is None:
            self._state.bound_symbol = bar.symbol
            self._state.bound_timeframe = bar.timeframe
        elif bar.symbol != self._state.bound_symbol or bar.timeframe != self._state.bound_timeframe:
            raise EntryModelDataError(
                "one EntryModelModule instance cannot switch symbol or timeframe"
            )
        signature = self._bar_signature(bar)
        last = self._state.last_bar_time
        if last is None:
            return False
        if bar.timestamp == last:
            if signature == self._state.last_bar_signature:
                return True
            raise EntryModelDataError(f"conflicting duplicate bar at {bar.timestamp.isoformat()}")
        if bar.timestamp < last:
            raise EntryModelDataError(
                f"out-of-order bar {bar.timestamp.isoformat()} after {last.isoformat()}"
            )
        return False

    def _complete_bar(self, bar: Candle) -> None:
        self._state.last_bar_time = bar.timestamp
        self._state.last_bar_signature = self._bar_signature(bar)
        self._state.increment("bars_processed")

    @staticmethod
    def _bar_signature(bar: Candle) -> tuple[str, ...]:
        return (
            bar.timestamp.astimezone(UTC).isoformat(),
            str(bar.open),
            str(bar.high),
            str(bar.low),
            str(bar.close),
            str(bar.volume),
            bar.symbol,
            bar.timeframe,
        )

    @staticmethod
    def _payload_reason(feature: Feature, error: str) -> EntryRejectionReason:
        if feature.schema_version != "1" or error.startswith("unsupported setup schema"):
            return EntryRejectionReason.UNSUPPORTED_SETUP_SCHEMA
        return EntryRejectionReason.INVALID_SETUP_PAYLOAD

    @staticmethod
    def _reason_from_build_error(message: str) -> EntryRejectionReason:
        if message.startswith("invalid setup metadata"):
            return EntryRejectionReason.INVALID_SETUP_PAYLOAD
        if message.startswith("required price missing"):
            return EntryRejectionReason.REQUIRED_PRICE_MISSING
        if message.startswith("invalid price geometry"):
            return EntryRejectionReason.INVALID_PRICE_GEOMETRY
        if message == "entry plan expired at creation":
            return EntryRejectionReason.EXPIRED_AT_CREATION
        if message.startswith("entry kind not allowed"):
            return EntryRejectionReason.ENTRY_KIND_NOT_ALLOWED
        if message.startswith("current timeframe") or message.startswith("entry expiry overflow"):
            return EntryRejectionReason.INVALID_TIMEFRAME
        return EntryRejectionReason.INTERNAL_ERROR

    def _rejection_feature(
        self,
        *,
        view: MarketView,
        setup_id: str | None,
        reason: EntryRejectionReason,
        details: str,
        source_id: str,
        source_fingerprint: str,
    ) -> Feature:
        payload: dict[str, object] = {
            "schema_version": "2",
            "module_version": self.manifest.version,
            "setup_id": setup_id,
            "reason": reason.value,
            "details": details,
            "created_at": view.current.close_time.isoformat(),
            "symbol": view.current.symbol,
            "source_id": source_id,
            "source_fingerprint": source_fingerprint,
            "config_fingerprint": self.config.fingerprint,
        }
        digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:32]
        return Feature(
            key=ENTRY_REJECTION_FEATURE_KEY,
            source_module=self.name,
            created_at=view.current.close_time,
            value=payload,
            symbol=view.current.symbol,
            timeframe=view.current.timeframe,
            valid_until=view.current.close_time,
            schema_version="2",
            feature_id=f"entry-reject-{digest}",
            invalidate_on_gap=False,
            metadata={"reason": reason.value},
        )

    @staticmethod
    def _skip(reason: EntryRejectionReason) -> ModuleDecision:
        return ModuleDecision(
            skips=(reason.value,),
            diagnostics=MappingProxyType(
                {
                    "plans_created": 0,
                    "reason": reason.value,
                }
            ),
        )

    @staticmethod
    def _decision(
        *,
        features: tuple[Feature, ...] = (),
        skips: tuple[str, ...] = (),
        diagnostics: Mapping[str, object] | None = None,
    ) -> ModuleDecision:
        frozen_diagnostics = MappingProxyType(
            {
                key: (MappingProxyType(dict(value)) if isinstance(value, Mapping) else value)
                for key, value in (diagnostics or {}).items()
            }
        )
        return ModuleDecision(
            features=features,
            skips=skips,
            diagnostics=frozen_diagnostics,
        )
