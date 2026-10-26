"""Strategy Factory lifecycle, checkpoint, resume, and safe cancellation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .artifacts import ContentAddressedArtifactStore
from .budget import BudgetLedger
from .canonical import canonical_sha256
from .checkpoint import AtomicCheckpointStore
from .contract_codec import (
    decode_strategy_factory_manifest,
    decode_strategy_factory_request,
)
from .contracts import (
    DecisionAction,
    FactoryDecision,
    FactoryStage,
    FactoryStatus,
    StrategyFactoryCheckpoint,
    StrategyFactoryManifest,
    StrategyFactoryRequest,
)
from .dataset import DatasetAnalysis, DatasetAnalyzer
from .decision_log import DecisionLog
from .errors import CheckpointError, CompatibilityError

if TYPE_CHECKING:
    from .coordinator import StageBudgetRequest

_STAGE_SEQUENCE = tuple(FactoryStage)
_TERMINAL = {
    FactoryStatus.CANCELED,
    FactoryStatus.COMPLETED,
    FactoryStatus.FAILED,
    FactoryStatus.DATASET_REJECTED,
    FactoryStatus.BUDGET_EXHAUSTED,
    FactoryStatus.CORRUPTED,
}


class StrategyFactoryRun:
    """Recoverable high-level orchestrator; computation stays in owned components."""

    def __init__(
        self,
        *,
        request: StrategyFactoryRequest,
        commit_sha: str,
        state_root: str | Path,
        module_versions: dict[str, str],
        now: datetime | None = None,
    ) -> None:
        self.request = request
        self.commit_sha = commit_sha
        self.run_id = f"sf-run-{canonical_sha256((request.request_id, request.fingerprint))[:24]}"
        root = Path(state_root) / self.run_id
        self._checkpoint_store = AtomicCheckpointStore(root / "checkpoints")
        self._artifact_store = ContentAddressedArtifactStore(root / "artifacts")
        self._decision_log = DecisionLog(root / "decisions.jsonl")
        self._budget = BudgetLedger(request.budget)
        self._budget_reservations: dict[str, dict[str, int | None]] = {}
        created_at = (now or datetime.now(UTC)).astimezone(UTC)
        manifest_seed = {
            "run_id": self.run_id,
            "request_fingerprint": request.fingerprint,
            "dataset_fingerprint": request.dataset.fingerprint,
            "commit_sha": commit_sha,
            "module_versions": module_versions,
            "random_seed": request.random_seed,
        }
        self.manifest = StrategyFactoryManifest(
            manifest_id=f"sf-manifest-{canonical_sha256(manifest_seed)[:24]}",
            run_id=self.run_id,
            request_fingerprint=request.fingerprint,
            dataset_fingerprint=request.dataset.fingerprint,
            commit_sha=commit_sha,
            core_version=_component_version("modular_core", "0.4.2"),
            research_runner_version=_component_version("modular_research_runner", "unavailable"),
            results_registry_version=_component_version("modular_results_registry", "unavailable"),
            strategy_factory_version="0.1.0",
            module_versions=module_versions,
            random_seed=request.random_seed,
            created_at=created_at,
        )
        self._checkpoint = StrategyFactoryCheckpoint(
            run_id=self.run_id,
            request_fingerprint=request.fingerprint,
            status=FactoryStatus.CREATED,
            stage=FactoryStage.DATA_GATE,
            stage_sequence=0,
            dataset_fingerprint=request.dataset.fingerprint,
            commit_sha=commit_sha,
            manifest_id=self.manifest.manifest_id,
            completed_artifacts={},
            submitted_batches={},
            processed_event_ids=frozenset(),
            budget_state=_budget_mapping(self._budget, self._budget_reservations),
            decision_chain_head=None,
            updated_at=created_at,
        )

    @classmethod
    def restore_existing(
        cls,
        *,
        state_root: str | Path,
        run_id: str,
    ) -> StrategyFactoryRun:
        """Reconstruct a run from immutable request/manifest artifacts and checkpoint."""

        root = Path(state_root) / run_id
        checkpoint = AtomicCheckpointStore(root / "checkpoints").load(run_id)
        artifacts = ContentAddressedArtifactStore(root / "artifacts")
        request_id = checkpoint.completed_artifacts.get("request")
        manifest_id = checkpoint.completed_artifacts.get("manifest")
        if request_id is None or manifest_id is None:
            raise CompatibilityError(
                "run predates recoverable request/manifest artifacts; resume with its original config"
            )
        request = decode_strategy_factory_request(artifacts.get(request_id))
        manifest = decode_strategy_factory_manifest(artifacts.get(manifest_id))
        if manifest.run_id != run_id or manifest.manifest_id != checkpoint.manifest_id:
            raise CompatibilityError("stored manifest does not match checkpoint identity")
        if manifest.request_fingerprint != request.fingerprint:
            raise CompatibilityError("stored request does not match manifest fingerprint")
        restored = cls(
            request=request,
            commit_sha=manifest.commit_sha,
            state_root=state_root,
            module_versions={
                str(key): str(value) for key, value in manifest.module_versions.items()
            },
            now=manifest.created_at,
        )
        if restored.run_id != run_id:
            raise CompatibilityError("stored request derives a different run ID")
        restored.manifest = manifest
        restored.initialize()
        return restored

    @property
    def checkpoint(self) -> StrategyFactoryCheckpoint:
        return self._checkpoint

    def load_artifact(self, artifact_id: str) -> Any:
        """Load a checksummed primitive artifact for a later lifecycle stage."""

        return self._artifact_store.get(artifact_id)

    def store_artifact(self, *, artifact_type: str, value: Any) -> str:
        """Persist an auxiliary immutable artifact without advancing the stage."""

        return self._artifact_store.put(artifact_type=artifact_type, value=value)

    def initialize(self) -> StrategyFactoryCheckpoint:
        """Persist the manifest and initial checkpoint, rejecting conflicting reuse."""

        if self._checkpoint_store.exists(self.run_id):
            existing = self._checkpoint_store.load(self.run_id)
            if existing.request_fingerprint != self.request.fingerprint:
                raise CompatibilityError("run ID exists for a different request")
            if existing.dataset_fingerprint != self.request.dataset.fingerprint:
                raise CompatibilityError("run dataset fingerprint changed")
            if existing.commit_sha != self.commit_sha:
                raise CompatibilityError("run commit SHA changed")
            if existing.manifest_id != self.manifest.manifest_id:
                raise CompatibilityError("run manifest identity changed")
            self._checkpoint = existing
            self._budget.restore(dict(existing.budget_state))
            self._budget_reservations = _budget_reservations_from_state(existing.budget_state)
            return existing
        request_artifact = self._artifact_store.put(
            artifact_type="factory_request", value=self.request
        )
        manifest_artifact = self._artifact_store.put(
            artifact_type="factory_manifest", value=self.manifest
        )
        self._checkpoint = replace(
            self._checkpoint,
            completed_artifacts={
                "request": request_artifact,
                "manifest": manifest_artifact,
            },
        )
        return self._persist()

    def run_data_gate(self, analyzer: DatasetAnalyzer) -> DatasetAnalysis:
        """Validate data and stop at DATASET_REJECTED on critical defects."""

        self._require_active_stage(FactoryStage.DATA_GATE)
        self._checkpoint = replace(self._checkpoint, status=FactoryStatus.RUNNING)
        analysis = analyzer.analyze(self.request.dataset)
        validation_id = self._artifact_store.put(
            artifact_type="dataset_validation", value=analysis.validation
        )
        artifacts = dict(self._checkpoint.completed_artifacts)
        artifacts[FactoryStage.DATA_GATE.value] = validation_id
        if analysis.profile is None:
            self._record_decision(
                stage=FactoryStage.DATA_GATE,
                action=DecisionAction.REJECT,
                subject_id=self.request.dataset.dataset_id,
                reason_codes=analysis.validation.reason_codes,
                evidence={"validation_artifact": validation_id},
            )
            self._checkpoint = replace(
                self._checkpoint,
                status=FactoryStatus.DATASET_REJECTED,
                completed_artifacts=artifacts,
            )
            self._persist()
            return analysis
        profile_id = self._artifact_store.put(
            artifact_type="instrument_profile", value=analysis.profile
        )
        artifacts[FactoryStage.INSTRUMENT_PROFILE.value] = profile_id
        self._record_decision(
            stage=FactoryStage.DATA_GATE,
            action=DecisionAction.ACCEPT,
            subject_id=self.request.dataset.dataset_id,
            reason_codes=("DATASET_GATE_PASSED",),
            evidence={
                "validation_artifact": validation_id,
                "profile_artifact": profile_id,
                "warnings": analysis.validation.warnings,
            },
        )
        self._checkpoint = replace(
            self._checkpoint,
            status=FactoryStatus.RUNNING,
            stage=FactoryStage.REGIME_DISCOVERY,
            stage_sequence=2,
            completed_artifacts=artifacts,
        )
        self._persist()
        return analysis

    def reserve_budget(
        self,
        *,
        stage: FactoryStage,
        request: StageBudgetRequest,
    ) -> StrategyFactoryCheckpoint:
        """Reserve declared capacity before a stage performs external work."""

        self._require_active_stage(stage)
        requested = {
            "experiments": request.experiments,
            "architectures": request.architectures,
            "finalists": request.finalists,
        }
        existing = self._budget_reservations.get(
            stage.value,
            {"experiments": 0, "architectures": 0, "finalists": None},
        )
        merged = dict(existing)
        apply_experiments = False
        apply_architectures = False
        apply_finalists = False
        for name, neutral in (
            ("experiments", 0),
            ("architectures", 0),
            ("finalists", None),
        ):
            value = requested[name]
            if value == neutral:
                continue
            prior = existing[name]
            if prior != neutral and prior != value:
                raise CompatibilityError(
                    f"stage budget reservation changed after persistence: {stage.value}:{name}"
                )
            if prior == neutral:
                merged[name] = value
                apply_experiments = apply_experiments or name == "experiments"
                apply_architectures = apply_architectures or name == "architectures"
                apply_finalists = apply_finalists or name == "finalists"
        if not any((apply_experiments, apply_architectures, apply_finalists)):
            return self._checkpoint
        if apply_experiments:
            self._budget.reserve_experiments(stage, request.experiments)
        if apply_architectures:
            self._budget.record_architectures(request.architectures)
        if apply_finalists and request.finalists is not None:
            self._budget.record_finalists(request.finalists)
        self._budget_reservations[stage.value] = merged
        return self._persist()

    def ensure_budget_time(self) -> None:
        """Fail before external work when the configured duration is exhausted."""

        self._budget.ensure_time_available()

    def complete_stage(
        self,
        *,
        stage: FactoryStage,
        artifact_type: str,
        artifact: Any,
        reason_codes: tuple[str, ...],
        subject_id: str,
    ) -> str:
        """Persist one stage output and advance exactly one stage."""

        self._require_active_stage(stage)
        artifact_id = self._artifact_store.put(artifact_type=artifact_type, value=artifact)
        artifacts = dict(self._checkpoint.completed_artifacts)
        artifacts[stage.value] = artifact_id
        next_stage = self._next_stage(stage)
        self._record_decision(
            stage=stage,
            action=DecisionAction.ACCEPT,
            subject_id=subject_id,
            reason_codes=reason_codes,
            evidence={"artifact_id": artifact_id},
        )
        status = FactoryStatus.COMPLETED if next_stage is None else FactoryStatus.RUNNING
        self._checkpoint = replace(
            self._checkpoint,
            status=status,
            stage=stage if next_stage is None else next_stage,
            stage_sequence=self._checkpoint.stage_sequence + 1,
            completed_artifacts=artifacts,
        )
        self._persist()
        return artifact_id

    def skip_stage(
        self,
        *,
        stage: FactoryStage,
        reason_codes: tuple[str, ...],
        subject_id: str,
    ) -> StrategyFactoryCheckpoint:
        """Advance an inapplicable stage while retaining an explicit decision."""

        self._require_active_stage(stage)
        next_stage = self._next_stage(stage)
        self._record_decision(
            stage=stage,
            action=DecisionAction.SKIP,
            subject_id=subject_id,
            reason_codes=reason_codes,
            evidence={"mode": self.request.mode.value},
        )
        self._checkpoint = replace(
            self._checkpoint,
            status=FactoryStatus.COMPLETED if next_stage is None else FactoryStatus.RUNNING,
            stage=stage if next_stage is None else next_stage,
            stage_sequence=self._checkpoint.stage_sequence + 1,
        )
        return self._persist()

    def block(
        self,
        *,
        reason_codes: tuple[str, ...],
        evidence: dict[str, Any],
    ) -> StrategyFactoryCheckpoint:
        """Persist a retryable dependency block at the current stage."""

        if self._checkpoint.status in _TERMINAL:
            return self._checkpoint
        self._record_decision(
            stage=self._checkpoint.stage,
            action=DecisionAction.BLOCK,
            subject_id=self.run_id,
            reason_codes=reason_codes,
            evidence=evidence,
        )
        self._checkpoint = replace(self._checkpoint, status=FactoryStatus.BLOCKED)
        return self._persist()

    def fail(
        self,
        *,
        reason_codes: tuple[str, ...],
        evidence: dict[str, Any],
    ) -> StrategyFactoryCheckpoint:
        """Fail closed while retaining all evidence gathered before the defect."""

        if self._checkpoint.status in _TERMINAL:
            return self._checkpoint
        self._record_decision(
            stage=self._checkpoint.stage,
            action=DecisionAction.FAIL,
            subject_id=self.run_id,
            reason_codes=reason_codes,
            evidence=evidence,
        )
        self._checkpoint = replace(self._checkpoint, status=FactoryStatus.FAILED)
        return self._persist()

    def exhaust_budget(
        self,
        *,
        reason_codes: tuple[str, ...],
        evidence: dict[str, Any],
    ) -> StrategyFactoryCheckpoint:
        """Stop cleanly at BUDGET_EXHAUSTED with the best evidence preserved."""

        if self._checkpoint.status in _TERMINAL:
            return self._checkpoint
        self._record_decision(
            stage=self._checkpoint.stage,
            action=DecisionAction.EXHAUST,
            subject_id=self.run_id,
            reason_codes=reason_codes,
            evidence=evidence,
        )
        self._checkpoint = replace(
            self._checkpoint,
            status=FactoryStatus.BUDGET_EXHAUSTED,
        )
        return self._persist()

    def register_batch(self, *, batch_id: str, batch_fingerprint: str) -> bool:
        """Record an external batch once; conflicting content fails closed."""

        batches = dict(self._checkpoint.submitted_batches)
        existing = batches.get(batch_id)
        if existing is not None:
            if existing != batch_fingerprint:
                raise CompatibilityError("batch ID already exists with different content")
            return False
        batches[batch_id] = batch_fingerprint
        self._checkpoint = replace(self._checkpoint, submitted_batches=batches)
        self._persist()
        return True

    def process_event(self, event_id: str) -> bool:
        """Return False for a safe duplicate completion event."""

        if event_id in self._checkpoint.processed_event_ids:
            return False
        events = set(self._checkpoint.processed_event_ids)
        events.add(event_id)
        self._checkpoint = replace(self._checkpoint, processed_event_ids=frozenset(events))
        self._persist()
        return True

    def cancel(self, *, reason: str = "USER_CANCELED") -> StrategyFactoryCheckpoint:
        if self._checkpoint.status in _TERMINAL:
            return self._checkpoint
        self._record_decision(
            stage=self._checkpoint.stage,
            action=DecisionAction.CANCEL,
            subject_id=self.run_id,
            reason_codes=(reason,),
            evidence={},
        )
        self._checkpoint = replace(self._checkpoint, status=FactoryStatus.CANCELED)
        return self._persist()

    def pause(self, *, reason: str) -> StrategyFactoryCheckpoint:
        if self._checkpoint.status in _TERMINAL:
            raise CompatibilityError("terminal run cannot be paused")
        self._record_decision(
            stage=self._checkpoint.stage,
            action=DecisionAction.PAUSE,
            subject_id=self.run_id,
            reason_codes=(reason,),
            evidence={},
        )
        self._checkpoint = replace(self._checkpoint, status=FactoryStatus.PAUSED)
        return self._persist()

    def resume(self) -> StrategyFactoryCheckpoint:
        if self._checkpoint.status not in {FactoryStatus.PAUSED, FactoryStatus.BLOCKED}:
            raise CompatibilityError("only a paused or blocked run can be resumed")
        self._record_decision(
            stage=self._checkpoint.stage,
            action=DecisionAction.RESUME,
            subject_id=self.run_id,
            reason_codes=("RESUME_REQUESTED",),
            evidence={},
        )
        self._checkpoint = replace(self._checkpoint, status=FactoryStatus.RUNNING)
        return self._persist()

    def restore(self) -> StrategyFactoryCheckpoint:
        restored = self._checkpoint_store.load(self.run_id)
        if restored.request_fingerprint != self.request.fingerprint:
            raise CheckpointError("checkpoint request fingerprint changed")
        if restored.dataset_fingerprint != self.request.dataset.fingerprint:
            raise CheckpointError("dataset hash changed during run")
        if restored.commit_sha != self.commit_sha:
            raise CheckpointError("commit SHA changed during run")
        if restored.decision_chain_head != self._decision_log.chain_head:
            raise CheckpointError("decision log and checkpoint chain heads differ")
        self._budget.restore(dict(restored.budget_state))
        self._budget_reservations = _budget_reservations_from_state(restored.budget_state)
        self._checkpoint = restored
        return restored

    def _record_decision(
        self,
        *,
        stage: FactoryStage,
        action: DecisionAction,
        subject_id: str,
        reason_codes: tuple[str, ...],
        evidence: dict[str, Any],
    ) -> None:
        sequence = len(self._decision_log.read_all()) + 1
        decision = FactoryDecision(
            decision_id=f"decision-{self.run_id}-{sequence:06d}",
            run_id=self.run_id,
            stage=stage,
            action=action,
            subject_id=subject_id,
            reason_codes=reason_codes,
            evidence=evidence,
            alternatives=(),
            budget_snapshot=_budget_mapping(self._budget, self._budget_reservations),
            decided_at=datetime.now(UTC),
        )
        entry = self._decision_log.append(decision)
        self._checkpoint = replace(self._checkpoint, decision_chain_head=entry.entry_hash)

    def _persist(self) -> StrategyFactoryCheckpoint:
        self._checkpoint = replace(
            self._checkpoint,
            budget_state=_budget_mapping(self._budget, self._budget_reservations),
            decision_chain_head=self._decision_log.chain_head,
            updated_at=datetime.now(UTC),
        )
        self._checkpoint_store.save(self._checkpoint)
        return self._checkpoint

    def _require_active_stage(self, stage: FactoryStage) -> None:
        if self._checkpoint.status in _TERMINAL | {FactoryStatus.PAUSED, FactoryStatus.BLOCKED}:
            raise CompatibilityError(f"run is not active: {self._checkpoint.status.value}")
        if self._checkpoint.stage is not stage:
            raise CompatibilityError(
                f"expected stage {self._checkpoint.stage.value}, received {stage.value}"
            )

    @staticmethod
    def _next_stage(stage: FactoryStage) -> FactoryStage | None:
        index = _STAGE_SEQUENCE.index(stage)
        return _STAGE_SEQUENCE[index + 1] if index + 1 < len(_STAGE_SEQUENCE) else None


def _budget_mapping(
    ledger: BudgetLedger,
    reservations: dict[str, dict[str, int | None]],
) -> dict[str, Any]:
    snapshot = ledger.snapshot()
    return {
        "experiments_reserved": snapshot.experiments_reserved,
        "architectures_generated": snapshot.architectures_generated,
        "finalists_selected": snapshot.finalists_selected,
        "stage_experiments": snapshot.stage_experiments,
        "elapsed_seconds": snapshot.elapsed_seconds,
        "stage_reservations": {key: dict(value) for key, value in sorted(reservations.items())},
    }


def _budget_reservations_from_state(
    state: Any,
) -> dict[str, dict[str, int | None]]:
    if not isinstance(state, dict | Mapping):
        raise CheckpointError("budget_state must be a mapping")
    raw = state.get("stage_reservations", {})
    if not isinstance(raw, dict | Mapping):
        raise CheckpointError("stage_reservations must be a mapping")
    valid_stages = {stage.value for stage in FactoryStage}
    result: dict[str, dict[str, int | None]] = {}
    for stage, reservation in raw.items():
        if not isinstance(stage, str) or stage not in valid_stages:
            raise CheckpointError("stage_reservations contains an invalid stage")
        if not isinstance(reservation, dict | Mapping):
            raise CheckpointError("stage reservation must be a mapping")
        if set(reservation) != {"experiments", "architectures", "finalists"}:
            raise CheckpointError("stage reservation fields are invalid")
        normalized: dict[str, int | None] = {}
        for name in ("experiments", "architectures", "finalists"):
            value = reservation[name]
            if name == "finalists" and value is None:
                normalized[name] = None
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CheckpointError("stage reservation contains an invalid counter")
            normalized[name] = value
        result[stage] = normalized
    return result


def _component_version(module_name: str, fallback: str) -> str:
    try:
        module = __import__(module_name)
    except ImportError:
        return fallback
    return str(getattr(module, "__version__", fallback))
