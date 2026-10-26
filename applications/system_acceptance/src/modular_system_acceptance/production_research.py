from __future__ import annotations

import hashlib
import json
from typing import Protocol

from .contracts import (
    AcceptanceErrorCode,
    AcceptanceOwnerError,
    AcceptancePhase,
    EvidenceReference,
    PhaseOutcome,
    PhaseOutcomeStatus,
)
from .native_executor import NativeOwnerPhaseExecutor
from .owner import PhaseExecutionContext
from .production_identity import (
    TrustedAcceptanceIdentity,
    require_identifier,
    require_sha256,
)

_RESEARCH_ACTIVE = frozenset({"CREATED", "VALIDATING", "PLANNED", "RUNNING", "PAUSED"})
_RESEARCH_FAILED = frozenset({"FAILED", "COMPLETED_WITH_FAILURES", "CORRUPTED"})


class ResearchSpecificationPort(Protocol):
    @property
    def fingerprint(self) -> str: ...


class ResearchManifestPort(Protocol):
    @property
    def experiment_id(self) -> str: ...


class PreparedResearchExperimentPort(Protocol):
    @property
    def specification(self) -> ResearchSpecificationPort: ...

    @property
    def manifest(self) -> ResearchManifestPort: ...


class ResearchRepositoryPort(Protocol):
    def experiment_status(self, experiment_id: str) -> object: ...


class ResearchSummaryPort(Protocol):
    @property
    def status(self) -> object: ...


class ResearchRunnerPort(Protocol):
    @property
    def repository(self) -> ResearchRepositoryPort: ...

    def execute(self, experiment_id: str) -> ResearchSummaryPort: ...


class TrustedResearchPhaseAdapter:
    """Bind System Acceptance RESEARCH to one already-prepared durable experiment.

    Deployment owns the ResearchRunner and PreparedExperiment. This adapter never
    creates a new experiment and never accepts browser-supplied repository paths or
    provenance. Recovery resumes the same deterministic experiment_id through the
    Research owner's fenced durable scheduler.
    """

    def __init__(
        self,
        *,
        identity: TrustedAcceptanceIdentity,
        runner: ResearchRunnerPort,
        prepared: PreparedResearchExperimentPort,
        specification_fingerprint: str,
    ) -> None:
        if not isinstance(identity, TrustedAcceptanceIdentity):
            raise TypeError("identity must be TrustedAcceptanceIdentity")
        if not callable(getattr(runner, "execute", None)):
            raise TypeError("runner must provide execute()")
        repository = getattr(runner, "repository", None)
        if repository is None or not callable(getattr(repository, "experiment_status", None)):
            raise TypeError("runner must expose a Research repository")
        require_sha256(specification_fingerprint, "specification_fingerprint")

        try:
            experiment_id = prepared.manifest.experiment_id
            prepared_fingerprint = prepared.specification.fingerprint
        except (AttributeError, TypeError):
            raise TypeError("prepared must expose frozen Research identity") from None
        require_identifier(experiment_id, "experiment_id")
        require_sha256(prepared_fingerprint, "prepared specification fingerprint")
        if prepared_fingerprint != specification_fingerprint:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_BLOCKED,
                "prepared Research specification identity does not match deployment identity",
            )

        self._identity = identity
        self._runner = runner
        self._experiment_id = experiment_id
        self._specification_fingerprint = specification_fingerprint

    @property
    def executor(self) -> NativeOwnerPhaseExecutor:
        return NativeOwnerPhaseExecutor(
            AcceptancePhase.RESEARCH,
            execute=self.execute,
            recover=self.recover,
        )

    def execute(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return self._resume(context)

    def recover(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return self._resume(context)

    def _resume(self, context: PhaseExecutionContext) -> PhaseOutcome:
        self._identity.require_context(context)
        if context.phase is not AcceptancePhase.RESEARCH:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_BLOCKED,
                "Research adapter received the wrong acceptance phase",
            )

        status = _status(self._runner.repository.experiment_status(self._experiment_id))
        if status == "COMPLETED":
            return self._success(status)
        if status == "CANCELED":
            return PhaseOutcome(
                status=PhaseOutcomeStatus.CANCELLED,
                error_code=AcceptanceErrorCode.PHASE_FAILURE.value,
                next_action="OWNER_REVIEW",
            )
        if status == "BLOCKED":
            return PhaseOutcome(
                status=PhaseOutcomeStatus.BLOCKED,
                error_code=AcceptanceErrorCode.PHASE_BLOCKED.value,
                next_action="OWNER_REVIEW",
            )
        if status in _RESEARCH_FAILED:
            return PhaseOutcome(
                status=PhaseOutcomeStatus.FAILED,
                error_code=AcceptanceErrorCode.PHASE_FAILURE.value,
                next_action="OWNER_REVIEW",
            )
        if status not in _RESEARCH_ACTIVE:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "Research owner returned an unsupported durable status",
            )

        summary = self._runner.execute(self._experiment_id)
        final_status = _status(summary.status)
        if final_status == "COMPLETED":
            return self._success(final_status)
        if final_status == "CANCELED":
            return PhaseOutcome(
                status=PhaseOutcomeStatus.CANCELLED,
                error_code=AcceptanceErrorCode.PHASE_FAILURE.value,
                next_action="OWNER_REVIEW",
            )
        if final_status == "BLOCKED":
            return PhaseOutcome(
                status=PhaseOutcomeStatus.BLOCKED,
                error_code=AcceptanceErrorCode.PHASE_BLOCKED.value,
                next_action="OWNER_REVIEW",
            )
        if final_status in _RESEARCH_FAILED:
            return PhaseOutcome(
                status=PhaseOutcomeStatus.FAILED,
                error_code=AcceptanceErrorCode.PHASE_FAILURE.value,
                next_action="OWNER_REVIEW",
            )
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.RECOVERY_REQUIRED,
            "Research owner stopped without a terminal durable result",
        )

    def _success(self, status: str) -> PhaseOutcome:
        material = json.dumps(
            {
                "experiment_id": self._experiment_id,
                "specification_fingerprint": self._specification_fingerprint,
                "status": status,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(material).hexdigest()
        return PhaseOutcome(
            status=PhaseOutcomeStatus.SUCCEEDED,
            evidence=(
                EvidenceReference(
                    kind="research.result",
                    digest=digest,
                    reference=f"research:{digest[:24]}",
                ),
            ),
        )


def _status(value: object) -> str:
    if isinstance(value, str):
        rendered = value
    else:
        raw = getattr(value, "value", None)
        if not isinstance(raw, str):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "Research owner returned an invalid durable status",
            )
        rendered = raw
    if not rendered or len(rendered) > 64:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.RECOVERY_REQUIRED,
            "Research owner returned an invalid durable status",
        )
    return rendered


__all__ = [
    "PreparedResearchExperimentPort",
    "ResearchManifestPort",
    "ResearchRepositoryPort",
    "ResearchRunnerPort",
    "ResearchSpecificationPort",
    "ResearchSummaryPort",
    "TrustedAcceptanceIdentity",
    "TrustedResearchPhaseAdapter",
]
