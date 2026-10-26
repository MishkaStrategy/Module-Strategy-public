from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from .errors import JournalIntegrityError, ReconciliationError, RecoveryError
from .journal import RuntimeEventJournal
from .ports import RuntimeClock


class RuntimeCheckpointRestoreBoundary(Protocol):
    def restore(self) -> Mapping[str, Any] | None: ...


class RuntimeCheckpointCaptureBoundary(Protocol):
    def capture(self) -> object: ...


class FactualRecoveryBoundary(Protocol):
    def recover(self) -> object | None: ...


class PaperReconciliationBoundary(Protocol):
    def reconcile(
        self,
        *,
        portfolio_id: str,
        instrument: str,
        timestamp: datetime | None = None,
    ) -> object: ...


@dataclass(frozen=True, slots=True, order=True)
class ReconciliationScope:
    portfolio_id: str
    instrument: str

    def __post_init__(self) -> None:
        for name in ("portfolio_id", "instrument"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-blank string")
            if value != value.strip():
                raise ValueError(f"{name} cannot contain surrounding whitespace")
            if len(value) > 256:
                raise ValueError(f"{name} exceeds maximum length")

    @property
    def aggregate_id(self) -> str:
        return f"{self.portfolio_id}:{self.instrument}"


class RuntimeRecoveryCoordinator:
    """Restore Runner-owned state before any venue reconciliation or readiness."""

    def __init__(
        self,
        *,
        checkpoint: RuntimeCheckpointRestoreBoundary,
        factual_recovery: FactualRecoveryBoundary,
        journal: RuntimeEventJournal,
        clock: RuntimeClock,
        aggregate_id: str,
    ) -> None:
        if not callable(getattr(checkpoint, "restore", None)):
            raise TypeError("checkpoint must implement restore()")
        if not callable(getattr(factual_recovery, "recover", None)):
            raise TypeError("factual_recovery must implement recover()")
        if not isinstance(journal, RuntimeEventJournal):
            raise TypeError("journal must be RuntimeEventJournal")
        if not isinstance(aggregate_id, str) or not aggregate_id.strip():
            raise ValueError("aggregate_id must be a non-blank string")
        self.checkpoint = checkpoint
        self.factual_recovery = factual_recovery
        self.journal = journal
        self.clock = clock
        self.aggregate_id = aggregate_id

    def recover(self) -> bool:
        if not self.journal.verify_chain():
            raise JournalIntegrityError("runtime journal is invalid before recovery")
        try:
            checkpoint_state = self.checkpoint.restore()
            factual_result = self.factual_recovery.recover()
        except Exception as exc:
            raise RecoveryError("runtime recovery chain failed") from exc
        if not self.journal.verify_chain():
            raise JournalIntegrityError("runtime journal is invalid after recovery")
        event_id = getattr(factual_result, "event_id", None)
        self.journal.append(
            timestamp=_utc(self.clock.now(), "clock.now"),
            event_type="runtime.recovery_completed",
            aggregate_id=self.aggregate_id,
            payload={
                "checkpoint_restored": checkpoint_state is not None,
                "factual_event_recovered": factual_result is not None,
                "factual_event_id": event_id if isinstance(event_id, str) else None,
            },
        )
        return True


class PaperScopedReconciliationCoordinator:
    """Run canonical Paper reconciliation and capture the recovered startup baseline."""

    def __init__(
        self,
        *,
        paper: PaperReconciliationBoundary,
        scopes: Sequence[ReconciliationScope],
        journal: RuntimeEventJournal,
        clock: RuntimeClock,
        checkpoint: RuntimeCheckpointCaptureBoundary | None = None,
    ) -> None:
        if not callable(getattr(paper, "reconcile", None)):
            raise TypeError("paper must implement reconcile()")
        if isinstance(scopes, (str, bytes)) or not isinstance(scopes, Sequence):
            raise TypeError("scopes must be a sequence")
        normalized = tuple(scopes)
        if not normalized or any(
            not isinstance(scope, ReconciliationScope) for scope in normalized
        ):
            raise ValueError("scopes must contain ReconciliationScope values")
        if len(set(normalized)) != len(normalized):
            raise ValueError("reconciliation scopes cannot contain duplicates")
        if not isinstance(journal, RuntimeEventJournal):
            raise TypeError("journal must be RuntimeEventJournal")
        if checkpoint is not None and not callable(getattr(checkpoint, "capture", None)):
            raise TypeError("checkpoint must implement capture() or be None")
        self.paper = paper
        self.scopes = tuple(sorted(normalized))
        self.journal = journal
        self.clock = clock
        self.checkpoint = checkpoint

    def reconcile(self) -> bool:
        for scope in self.scopes:
            timestamp = _utc(self.clock.now(), "clock.now")
            try:
                report = self.paper.reconcile(
                    portfolio_id=scope.portfolio_id,
                    instrument=scope.instrument,
                    timestamp=timestamp,
                )
            except Exception as exc:
                raise ReconciliationError(
                    f"Paper reconciliation failed for {scope.aggregate_id}"
                ) from exc
            blocked = getattr(report, "blocked", None)
            findings = getattr(report, "findings", None)
            if not isinstance(blocked, bool):
                raise ReconciliationError("Paper reconciliation report has no boolean blocked flag")
            if not isinstance(findings, tuple):
                raise ReconciliationError("Paper reconciliation report findings must be a tuple")
            self.journal.append(
                timestamp=timestamp,
                event_type=(
                    "paper.reconciliation_blocked" if blocked else "paper.reconciliation_completed"
                ),
                aggregate_id=scope.aggregate_id,
                symbol=scope.instrument,
                payload={
                    "portfolio_id": scope.portfolio_id,
                    "instrument": scope.instrument,
                    "blocked": blocked,
                    "finding_count": len(findings),
                },
            )
            if blocked:
                return False
        if self.checkpoint is not None:
            try:
                self.checkpoint.capture()
            except Exception as exc:
                raise ReconciliationError(
                    "startup checkpoint capture failed after Paper reconciliation"
                ) from exc
        return True


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)
