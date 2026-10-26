from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping

from modular_core import Feature, MarketView, ModuleDecision, ModuleManifest, StrategyModule

from .config import SetupAdapterConfig, SourceSpec
from .enums import AdapterReason
from .outputs import (
    MODULE_VERSION,
    REJECTION_FEATURE_KEY,
    SETUP_FEATURE_KEY,
    canonical_feature,
    rejection_feature,
)
from .primitives import sha256_hex, source_feature_fingerprint
from .profiles import adapt_source, reason_for_error
from .state import SetupAdapterState
from .stream import StreamTracker


class CanonicalSetupAdapterModule(StrategyModule):
    """Convert source-specific confirmation features into canonical setup schema 1."""

    name = "setup_adapter"

    def __init__(
        self,
        *,
        sources: Iterable[Mapping[str, object]] | None = None,
        max_processed_sources: int = 10_000,
    ) -> None:
        self.config = SetupAdapterConfig.from_parameters(
            sources=sources,
            max_processed_sources=max_processed_sources,
        )
        self.parameters = self.config.as_parameters()
        self.manifest = ModuleManifest(
            name=self.name,
            version=MODULE_VERSION,
            api_version="1.0",
            required_features=tuple(source.feature_key for source in self.config.sources),
            produced_features=(SETUP_FEATURE_KEY, REJECTION_FEATURE_KEY),
            parameters_schema={
                "sources": {"type": "array"},
                "max_processed_sources": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1_000_000,
                },
            },
            skip_until_ready=False,
            description=(
                "Canonicalizes configured setup-confirmation features without adding entry, "
                "risk, protection or execution policy."
            ),
            state_schema_version="1",
        )
        self._state = SetupAdapterState(
            self.config.max_processed_sources,
            self.config.fingerprint,
        )
        self._stream = StreamTracker()

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if self._stream.validate(view):
            self._state.increment("duplicate_events")
            return ModuleDecision(skips=(AdapterReason.DUPLICATE_EVENT.value,))
        try:
            if view.data_gap:
                self._state.increment("data_gaps")
                return ModuleDecision(skips=(AdapterReason.DATA_GAP.value,))
            if view.features is None:
                self._state.increment("feature_view_unavailable")
                return ModuleDecision(skips=(AdapterReason.FEATURE_VIEW_UNAVAILABLE.value,))
            return self._process_sources(view)
        finally:
            self._stream.complete(view)

    def _process_sources(self, view: MarketView) -> ModuleDecision:
        current = view.current
        feature_view = view.features
        if feature_view is None:  # guarded by on_bar
            raise RuntimeError("feature view is required")
        candidates: list[tuple[SourceSpec, Feature]] = []
        for source in self.config.sources:
            features = feature_view.get_all(source.feature_key, symbol=current.symbol)
            candidates.extend(
                (source, feature)
                for feature in features
                if feature.created_at == current.close_time
            )
        candidates.sort(
            key=lambda item: (
                item[0].feature_key,
                item[1].source_module,
                item[1].feature_id or "",
            )
        )
        outputs: list[Feature] = []
        skips: list[str] = []
        counts: dict[str, int] = defaultdict(int)
        batch: dict[str, str] = {}

        for source, feature in candidates:
            try:
                fingerprint = source_feature_fingerprint(feature)
            except ValueError as exc:
                fingerprint = sha256_hex(
                    {
                        "key": feature.key,
                        "source_module": feature.source_module,
                        "created_at": feature.created_at,
                        "feature_id": feature.feature_id,
                        "normalization_error": str(exc),
                    }
                )
                source_id = feature.feature_id or f"anon-{fingerprint[:32]}"
                outputs.append(
                    rejection_feature(
                        module_name=self.name,
                        view=view,
                        source_id=source_id,
                        source_fingerprint=fingerprint,
                        reason=AdapterReason.INVALID_SOURCE_PAYLOAD,
                        details=str(exc),
                    )
                )
                self._remember_rejection(
                    source_id,
                    fingerprint,
                    AdapterReason.INVALID_SOURCE_PAYLOAD,
                    skips,
                    counts,
                )
                continue

            source_id = feature.feature_id or f"anon-{fingerprint[:32]}"
            previous_batch = batch.get(source_id)
            if previous_batch is not None:
                if previous_batch == fingerprint:
                    self._remember_duplicate(skips, counts)
                    continue
                outputs.append(
                    rejection_feature(
                        module_name=self.name,
                        view=view,
                        source_id=source_id,
                        source_fingerprint=fingerprint,
                        reason=AdapterReason.SOURCE_FEATURE_CONFLICT,
                        details="source feature_id was reused with different content in one batch",
                    )
                )
                self._remember_conflict(source_id, fingerprint, skips, counts)
                continue
            batch[source_id] = fingerprint

            previous = self._state.processed.get(source_id)
            if previous is not None:
                if previous.fingerprint == fingerprint:
                    self._remember_duplicate(skips, counts)
                    continue
                outputs.append(
                    rejection_feature(
                        module_name=self.name,
                        view=view,
                        source_id=source_id,
                        source_fingerprint=fingerprint,
                        reason=AdapterReason.SOURCE_FEATURE_CONFLICT,
                        details="source feature_id was reused with different content",
                    )
                )
                self._remember_conflict(source_id, fingerprint, skips, counts)
                continue

            try:
                canonical = adapt_source(source, feature, view)
                output = canonical_feature(
                    module_name=self.name,
                    source=source,
                    source_feature=feature,
                    view=view,
                    canonical=canonical,
                    source_id=source_id,
                    source_fingerprint=fingerprint,
                )
            except ValueError as exc:
                reason = reason_for_error(str(exc))
                output = rejection_feature(
                    module_name=self.name,
                    view=view,
                    source_id=source_id,
                    source_fingerprint=fingerprint,
                    reason=reason,
                    details=str(exc),
                )
                self._remember_rejection(source_id, fingerprint, reason, skips, counts)
            else:
                self._state.remember(source_id, fingerprint, "adapted")
                self._state.increment("adapted")
                counts["adapted"] += 1
            outputs.append(output)

        return ModuleDecision(
            features=tuple(outputs),
            skips=tuple(skips),
            diagnostics={
                "source_features_seen": len(candidates),
                "adapted": counts["adapted"],
                "rejected": counts["rejected"],
                "duplicates": counts["duplicate"],
                "conflicts": counts["conflict"],
            },
        )

    def _remember_duplicate(self, skips: list[str], counts: dict[str, int]) -> None:
        skips.append(AdapterReason.DUPLICATE_SOURCE.value)
        self._state.increment("duplicates")
        counts["duplicate"] += 1

    def _remember_conflict(
        self,
        source_id: str,
        fingerprint: str,
        skips: list[str],
        counts: dict[str, int],
    ) -> None:
        self._state.remember(source_id, fingerprint, "conflict")
        self._state.increment("source_conflicts")
        skips.append(AdapterReason.SOURCE_FEATURE_CONFLICT.value)
        counts["conflict"] += 1

    def _remember_rejection(
        self,
        source_id: str,
        fingerprint: str,
        reason: AdapterReason,
        skips: list[str],
        counts: dict[str, int],
    ) -> None:
        self._state.remember(source_id, fingerprint, "rejected")
        self._state.increment("rejections")
        skips.append(reason.value)
        counts["rejected"] += 1

    def snapshot_state(self) -> Mapping[str, object]:
        return {**dict(self._state.snapshot()), **dict(self._stream.snapshot())}

    def restore_state(self, state: Mapping[str, object]) -> None:
        self._state.restore(state)
        self._stream.restore(state)
