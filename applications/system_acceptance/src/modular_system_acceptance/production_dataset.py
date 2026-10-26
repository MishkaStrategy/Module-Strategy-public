from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
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
from .production_identity import TrustedAcceptanceIdentity, require_identifier, require_sha256


class RegisteredDatasetDescriptorPort(Protocol):
    @property
    def dataset_id(self) -> str: ...

    @property
    def version(self) -> int: ...

    @property
    def dataset_hash(self) -> str: ...


class RegisteredDatasetSelectionPort(Protocol):
    @property
    def descriptor(self) -> RegisteredDatasetDescriptorPort: ...

    @property
    def timeframe(self) -> str: ...

    @property
    def row_count(self) -> int: ...


class TrustedDatasetPhaseAdapter:
    """Bind DATASET to a trusted registered-dataset resolver owned by deployment.

    The resolver is a server-side zero-argument closure around the authoritative
    Market Data registry/storage configuration. No filesystem path, version or
    timeframe is accepted from the browser contract. Re-running the resolver during
    recovery is read-only integrity verification and cannot duplicate a side effect.
    """

    def __init__(
        self,
        *,
        identity: TrustedAcceptanceIdentity,
        resolver: Callable[[], RegisteredDatasetSelectionPort],
        version: int,
        timeframe: str,
    ) -> None:
        if not isinstance(identity, TrustedAcceptanceIdentity):
            raise TypeError("identity must be TrustedAcceptanceIdentity")
        if not callable(resolver):
            raise TypeError("resolver must be callable")
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "trusted dataset version must be a positive integer",
            )
        normalized_timeframe = require_identifier(timeframe.strip().lower(), "dataset timeframe")
        self._identity = identity
        self._resolver = resolver
        self._version = version
        self._timeframe = normalized_timeframe

    @property
    def executor(self) -> NativeOwnerPhaseExecutor:
        return NativeOwnerPhaseExecutor(
            AcceptancePhase.DATASET,
            execute=self.execute,
            recover=self.recover,
        )

    def execute(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return self._verify(context)

    def recover(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return self._verify(context)

    def _verify(self, context: PhaseExecutionContext) -> PhaseOutcome:
        self._identity.require_context(context)
        if context.phase is not AcceptancePhase.DATASET:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_BLOCKED,
                "dataset adapter received the wrong acceptance phase",
            )
        selection = self._resolver()
        try:
            descriptor = selection.descriptor
            dataset_id = descriptor.dataset_id
            version = descriptor.version
            dataset_hash = descriptor.dataset_hash
            timeframe = selection.timeframe
            row_count = selection.row_count
        except (AttributeError, TypeError):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "registered dataset resolver returned an invalid selection",
            ) from None

        require_identifier(dataset_id, "registered dataset_id")
        require_sha256(dataset_hash, "registered dataset digest")
        normalized_timeframe = require_identifier(
            timeframe.strip().lower() if isinstance(timeframe, str) else timeframe,
            "registered dataset timeframe",
        )
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "registered dataset version is invalid",
            )
        if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count <= 0:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "registered dataset row count is invalid",
            )
        if dataset_id != self._identity.dataset_id or dataset_hash != self._identity.dataset_digest:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_BLOCKED,
                "registered dataset identity does not match the durable acceptance candidate",
            )
        if version != self._version or normalized_timeframe != self._timeframe:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_BLOCKED,
                "registered dataset selection differs from trusted deployment scope",
            )

        material = json.dumps(
            {
                "dataset_id": dataset_id,
                "dataset_hash": dataset_hash,
                "version": version,
                "timeframe": normalized_timeframe,
                "row_count": row_count,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(material).hexdigest()
        return PhaseOutcome(
            status=PhaseOutcomeStatus.SUCCEEDED,
            evidence=(
                EvidenceReference(
                    kind="dataset.registered",
                    digest=digest,
                    reference=f"dataset:{digest[:24]}",
                ),
            ),
        )


__all__ = [
    "RegisteredDatasetDescriptorPort",
    "RegisteredDatasetSelectionPort",
    "TrustedDatasetPhaseAdapter",
]
