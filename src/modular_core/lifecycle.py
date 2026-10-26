"""Typed contracts for Core-owned lifecycle fences and effect commits.

These immutable values describe requests and receipts; constructing a request,
or receiving a validation result, is not proof that Core enforced anything.
The eventual Core commit owner must derive risk from canonical payload and
atomically recheck the writer epoch, binding, authorities and generation while
committing the effect/transition.
"""

from __future__ import annotations

import re
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class CoreLifecycleAction(StrEnum):
    """Descriptive transition requested of Core; no transition authority implied."""

    SUSPEND = "SUSPEND"
    RESUME = "RESUME"


@dataclass(frozen=True, slots=True)
class CoreWriterIdentity:
    owner_id: str
    epoch: int

    def __post_init__(self) -> None:
        _text(self.owner_id, "owner_id")
        _nonnegative(self.epoch, "epoch")


@dataclass(frozen=True, slots=True)
class CoreAuthorityReference:
    authority: str
    reference_id: str
    digest: str

    def __post_init__(self) -> None:
        _text(self.authority, "authority")
        _text(self.reference_id, "reference_id")
        _digest(self.digest, "authority digest")


@dataclass(frozen=True, slots=True)
class CoreLifecycleExpectedGeneration:
    """Caller claim only; this value proves no Core observation or enforcement."""

    instance_id: str
    incarnation_id: str
    package_id: str
    package_digest: str
    expected_generation: int

    def __post_init__(self) -> None:
        for field in ("instance_id", "incarnation_id", "package_id"):
            _text(getattr(self, field), field)
        _digest(self.package_digest, "package_digest")
        _nonnegative(self.expected_generation, "expected_generation")


@dataclass(frozen=True, slots=True)
class CoreLifecycleEffect:
    effect_id: str
    lifecycle: CoreLifecycleExpectedGeneration
    writer: CoreWriterIdentity
    operation: str
    canonical_payload_digest: str
    canonical_payload: bytes
    target_ids: tuple[str, ...]
    authority_refs: tuple[CoreAuthorityReference, ...]

    def __post_init__(self) -> None:
        _text(self.effect_id, "effect_id")
        if not isinstance(self.lifecycle, CoreLifecycleExpectedGeneration):
            raise TypeError("lifecycle must be CoreLifecycleExpectedGeneration")
        if not isinstance(self.writer, CoreWriterIdentity):
            raise TypeError("writer must be CoreWriterIdentity")
        _text(self.operation, "operation")
        _digest(self.canonical_payload_digest, "canonical_payload_digest")
        if not isinstance(self.canonical_payload, bytes):
            raise TypeError("canonical_payload must be bytes")
        if hashlib.sha256(self.canonical_payload).hexdigest() != self.canonical_payload_digest:
            raise ValueError("canonical_payload_digest does not match canonical_payload")
        _text_tuple(self.target_ids, "target_ids")
        _authority_tuple(self.authority_refs)

    @property
    def request_digest(self) -> str:
        return _fingerprint(
            {
                "kind": "effect",
                "effect_id": self.effect_id,
                "instance": _claim_dict(self.lifecycle),
                "writer": [self.writer.owner_id, self.writer.epoch],
                "operation": self.operation,
                "payload_digest": self.canonical_payload_digest,
                "targets": self.target_ids,
                "authorities": _authorities_dict(self.authority_refs),
            }
        )


@dataclass(frozen=True, slots=True)
class CoreFenceTransition:
    transition_id: str
    lifecycle: CoreLifecycleExpectedGeneration
    writer: CoreWriterIdentity
    resulting_generation: int
    action: CoreLifecycleAction
    canonical_payload_digest: str
    canonical_payload: bytes
    authority_refs: tuple[CoreAuthorityReference, ...]

    def __post_init__(self) -> None:
        _text(self.transition_id, "transition_id")
        if not isinstance(self.lifecycle, CoreLifecycleExpectedGeneration):
            raise TypeError("lifecycle must be CoreLifecycleExpectedGeneration")
        if not isinstance(self.writer, CoreWriterIdentity):
            raise TypeError("writer must be CoreWriterIdentity")
        _nonnegative(self.resulting_generation, "resulting_generation")
        if self.resulting_generation != self.lifecycle.expected_generation + 1:
            raise ValueError("resulting_generation must advance exactly once")
        if not isinstance(self.action, CoreLifecycleAction):
            raise TypeError("action must be CoreLifecycleAction")
        _digest(self.canonical_payload_digest, "canonical_payload_digest")
        if not isinstance(self.canonical_payload, bytes):
            raise TypeError("canonical_payload must be bytes")
        if hashlib.sha256(self.canonical_payload).hexdigest() != self.canonical_payload_digest:
            raise ValueError("canonical_payload_digest does not match canonical_payload")
        _authority_tuple(self.authority_refs)

    @property
    def request_digest(self) -> str:
        return _fingerprint(
            {
                "kind": "transition",
                "transition_id": self.transition_id,
                "instance": _claim_dict(self.lifecycle),
                "writer": [self.writer.owner_id, self.writer.epoch],
                "resulting_generation": self.resulting_generation,
                "action": self.action.value,
                "payload_digest": self.canonical_payload_digest,
                "authorities": _authorities_dict(self.authority_refs),
            }
        )


@dataclass(frozen=True, slots=True)
class CoreEffectValidation:
    """Advisory validation only; commit must recheck every bound field atomically."""

    effect_id: str
    request_digest: str
    writer: CoreWriterIdentity
    observed_generation: int
    allowed: bool
    reason: str

    def __post_init__(self) -> None:
        _text(self.effect_id, "effect_id")
        _digest(self.request_digest, "request_digest")
        if not isinstance(self.writer, CoreWriterIdentity):
            raise TypeError("writer must be CoreWriterIdentity")
        _nonnegative(self.observed_generation, "observed_generation")
        if not isinstance(self.allowed, bool):
            raise TypeError("allowed must be bool")
        _text(self.reason, "reason")


@dataclass(frozen=True, slots=True)
class CoreFenceValidation:
    """Advisory preflight; it neither reserves a transition nor proves authority."""

    transition_id: str
    request_digest: str
    writer: CoreWriterIdentity
    observed_generation: int
    allowed: bool
    reason: str

    def __post_init__(self) -> None:
        _text(self.transition_id, "transition_id")
        _digest(self.request_digest, "request_digest")
        if not isinstance(self.writer, CoreWriterIdentity):
            raise TypeError("writer must be CoreWriterIdentity")
        _nonnegative(self.observed_generation, "observed_generation")
        if not isinstance(self.allowed, bool):
            raise TypeError("allowed must be bool")
        _text(self.reason, "reason")


@dataclass(frozen=True, slots=True)
class CoreEffectCommitReceipt:
    """Core commit evidence, scoped to a specific writer, binding, request and sequence."""

    effect_id: str
    instance_id: str
    incarnation_id: str
    package_id: str
    package_digest: str
    writer: CoreWriterIdentity
    expected_generation: int
    canonical_payload_digest: str
    operation: str
    target_ids: tuple[str, ...]
    authority_refs: tuple[CoreAuthorityReference, ...]
    canonical_commit_sequence: int
    committed_at: datetime

    def __post_init__(self) -> None:
        _text(self.effect_id, "effect_id")
        for field in ("instance_id", "incarnation_id", "package_id", "operation"):
            _text(getattr(self, field), field)
        _digest(self.package_digest, "package_digest")
        if not isinstance(self.writer, CoreWriterIdentity):
            raise TypeError("writer must be CoreWriterIdentity")
        _nonnegative(self.expected_generation, "expected_generation")
        _digest(self.canonical_payload_digest, "canonical_payload_digest")
        _text_tuple(self.target_ids, "target_ids")
        _authority_tuple(self.authority_refs)
        _positive(self.canonical_commit_sequence, "canonical_commit_sequence")
        _aware(self.committed_at)

    def matches_request(self, request: CoreLifecycleEffect) -> bool:
        claim = request.lifecycle
        return (
            self.effect_id == request.effect_id
            and (self.instance_id, self.incarnation_id, self.package_id, self.package_digest)
            == (claim.instance_id, claim.incarnation_id, claim.package_id, claim.package_digest)
            and self.writer == request.writer
            and self.expected_generation == claim.expected_generation
            and self.canonical_payload_digest == request.canonical_payload_digest
            and self.operation == request.operation
            and self.target_ids == request.target_ids
            and self.authority_refs == request.authority_refs
        )

    def same_commit(self, other: CoreEffectCommitReceipt) -> bool:
        """True for an idempotent replay; changed binding/sequence is a conflict."""
        return self == other


@dataclass(frozen=True, slots=True)
class CoreFenceTransitionReceipt:
    transition_id: str
    instance_id: str
    incarnation_id: str
    package_id: str
    package_digest: str
    writer: CoreWriterIdentity
    previous_generation: int
    resulting_generation: int
    action: CoreLifecycleAction
    canonical_payload_digest: str
    authority_refs: tuple[CoreAuthorityReference, ...]
    canonical_commit_sequence: int
    committed_at: datetime

    def __post_init__(self) -> None:
        _text(self.transition_id, "transition_id")
        for field in ("instance_id", "incarnation_id", "package_id"):
            _text(getattr(self, field), field)
        _digest(self.package_digest, "package_digest")
        if not isinstance(self.writer, CoreWriterIdentity):
            raise TypeError("writer must be CoreWriterIdentity")
        _nonnegative(self.previous_generation, "previous_generation")
        _positive(self.resulting_generation, "resulting_generation")
        if self.resulting_generation != self.previous_generation + 1:
            raise ValueError("resulting_generation must advance exactly once")
        if not isinstance(self.action, CoreLifecycleAction):
            raise TypeError("action must be CoreLifecycleAction")
        _digest(self.canonical_payload_digest, "canonical_payload_digest")
        _authority_tuple(self.authority_refs)
        _positive(self.canonical_commit_sequence, "canonical_commit_sequence")
        _aware(self.committed_at)

    def matches_request(self, request: CoreFenceTransition) -> bool:
        claim = request.lifecycle
        return (
            self.transition_id == request.transition_id
            and (self.instance_id, self.incarnation_id, self.package_id, self.package_digest)
            == (claim.instance_id, claim.incarnation_id, claim.package_id, claim.package_digest)
            and self.writer == request.writer
            and self.previous_generation == claim.expected_generation
            and self.resulting_generation == request.resulting_generation
            and self.action is request.action
            and self.canonical_payload_digest == request.canonical_payload_digest
            and self.authority_refs == request.authority_refs
        )

    def same_commit(self, other: CoreFenceTransitionReceipt) -> bool:
        """True for an idempotent replay; changed binding/sequence is a conflict."""
        return self == other


class CoreEffectLookupStatus(StrEnum):
    MATCHED = "MATCHED"
    ABSENT_AT_READ = "ABSENT_AT_READ"
    CONFLICT = "CONFLICT"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class CoreSavedCommitReceipt:
    """A validated historical receipt and the digest of its complete request."""

    request_id: str
    request_digest: str
    kind: str
    receipt: CoreEffectCommitReceipt | CoreFenceTransitionReceipt

    def __post_init__(self) -> None:
        _text(self.request_id, "request_id")
        _digest(self.request_digest, "request_digest")
        if self.kind not in {"effect", "transition"}:
            raise ValueError("kind must be effect or transition")
        expected = CoreEffectCommitReceipt if self.kind == "effect" else CoreFenceTransitionReceipt
        if type(self.receipt) is not expected:
            raise TypeError("saved receipt type does not match kind")
        self.receipt.__post_init__()
        receipt_id = (
            self.receipt.effect_id
            if type(self.receipt) is CoreEffectCommitReceipt
            else self.receipt.transition_id
        )
        if receipt_id != self.request_id:
            raise ValueError("saved receipt ID does not match request_id")


@dataclass(frozen=True, slots=True)
class CoreEffectLookupEvidence:
    """Read-only lookup result, scoped to one validated commit/event snapshot."""

    status: CoreEffectLookupStatus
    effect_id: str
    request_digest: str
    observed_commit_sequence: int | None
    observed_event_sequence: int | None
    receipt: CoreEffectCommitReceipt | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.status, CoreEffectLookupStatus):
            raise TypeError("status must be CoreEffectLookupStatus")
        _text(self.effect_id, "effect_id")
        _digest(self.request_digest, "request_digest")
        for name in ("observed_commit_sequence", "observed_event_sequence"):
            value = getattr(self, name)
            if value is not None:
                _nonnegative(value, name)
        if self.status in {CoreEffectLookupStatus.MATCHED, CoreEffectLookupStatus.ABSENT_AT_READ}:
            if self.observed_commit_sequence is None or self.observed_event_sequence is None:
                raise ValueError("validated lookup results require observed sequence bounds")
        if (self.status is CoreEffectLookupStatus.MATCHED) != (self.receipt is not None):
            raise ValueError("only a matched lookup may expose a receipt")
        if self.receipt is not None:
            if type(self.receipt) is not CoreEffectCommitReceipt:
                raise TypeError("receipt must be CoreEffectCommitReceipt")
            self.receipt.__post_init__()
        _text(self.reason, "reason")


class CoreCommitStatus(StrEnum):
    COMMITTED = "COMMITTED"
    DENIED = "DENIED"
    UNRESOLVED = "UNRESOLVED"
    UNAVAILABLE = "UNAVAILABLE"


class CoreAtomicCommitRejected(RuntimeError):
    """The canonical transaction rejected the request before commit."""


class CoreAtomicCommitOutcomeUnknown(RuntimeError):
    """The canonical commit may have completed; callers must not retry the effect."""


@dataclass(frozen=True, slots=True)
class CoreStagedEffectCommit:
    receipt: CoreEffectCommitReceipt
    replayed: bool

    def __post_init__(self) -> None:
        if type(self.receipt) is not CoreEffectCommitReceipt:
            raise TypeError("receipt must be CoreEffectCommitReceipt")
        self.receipt.__post_init__()
        if type(self.replayed) is not bool:
            raise TypeError("replayed must be bool")


@dataclass(frozen=True, slots=True)
class CoreStagedTransitionCommit:
    receipt: CoreFenceTransitionReceipt
    replayed: bool

    def __post_init__(self) -> None:
        if type(self.receipt) is not CoreFenceTransitionReceipt:
            raise TypeError("receipt must be CoreFenceTransitionReceipt")
        self.receipt.__post_init__()
        if type(self.replayed) is not bool:
            raise TypeError("replayed must be bool")


class CoreCanonicalLifecycleTransaction(Protocol):
    """One canonical-store unit of work. Stage methods may not commit effects.

    Implementations must serialize by instance and perform writer-epoch,
    package/identity binding, generation CAS and idempotency checks against
    canonical state in this transaction. A read followed by a later write is
    insufficient. The canonical commit is the linearization point for both
    effect/fence changes and their events/receipt.
    The effect/transition ID is the idempotency key: the same ID and request
    digest returns the original receipt as a replay; the same ID with a different
    request digest is a conflict and must be rejected before commit.
    They must stage canonical state changes, fence, events and receipt together.
    Effects/risk class must be derived from canonical payload, operation and exact
    targets; caller labels are not authority. The service publishes a staged
    receipt only after `commit()` returns successfully. Staging never commits;
    callers must roll back on any stage failure. `commit()` must release locks and
    connections on both success and exception, without rolling back a commit that
    may already be durable. An exception from commit has an ambiguous outcome.
    """

    def check_and_stage_effect(self, request: CoreLifecycleEffect) -> CoreStagedEffectCommit: ...

    def check_and_stage_transition(
        self, request: CoreFenceTransition
    ) -> CoreStagedTransitionCommit: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class CoreLifecycleCommitPort(Protocol):
    """Adapter into the canonical Core transaction store; no shadow store allowed."""

    def begin_lifecycle_transaction(self) -> CoreCanonicalLifecycleTransaction: ...


@dataclass(frozen=True, slots=True)
class CoreLifecycleCommitResult:
    status: CoreCommitStatus
    receipt: CoreEffectCommitReceipt | CoreFenceTransitionReceipt | None
    replayed: bool
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, CoreCommitStatus):
            raise TypeError("status must be CoreCommitStatus")
        if type(self.replayed) is not bool:
            raise TypeError("replayed must be bool")
        _text(self.reason, "reason")
        if (self.status is CoreCommitStatus.COMMITTED) != (self.receipt is not None):
            raise ValueError("only a committed result may expose a receipt")
        if self.replayed and self.status is not CoreCommitStatus.COMMITTED:
            raise ValueError("only a committed result may be replayed")
        if self.receipt is not None:
            if type(self.receipt) not in {CoreEffectCommitReceipt, CoreFenceTransitionReceipt}:
                raise TypeError("receipt must be a concrete Core commit receipt")
            self.receipt.__post_init__()


class CoreLifecycleCommitCoordinator:
    """Coordinates one atomic canonical-store attempt and never retries it."""

    def __init__(self, port: CoreLifecycleCommitPort | None = None) -> None:
        self._port = port

    def commit_effect(self, request: CoreLifecycleEffect) -> CoreLifecycleCommitResult:
        return self._commit(request, is_effect=True)

    def commit_transition(self, request: CoreFenceTransition) -> CoreLifecycleCommitResult:
        return self._commit(request, is_effect=False)

    def _commit(
        self, request: CoreLifecycleEffect | CoreFenceTransition, *, is_effect: bool
    ) -> CoreLifecycleCommitResult:
        if self._port is None:
            return _result(
                CoreCommitStatus.UNAVAILABLE, "canonical Core transaction port unavailable"
            )
        try:
            transaction = self._port.begin_lifecycle_transaction()
        except Exception as exc:
            return _result(CoreCommitStatus.UNAVAILABLE, _safe_reason(exc))
        try:
            staged = (
                transaction.check_and_stage_effect(request)
                if is_effect
                else transaction.check_and_stage_transition(request)
            )
            expected_wrapper = CoreStagedEffectCommit if is_effect else CoreStagedTransitionCommit
            if type(staged) is not expected_wrapper:
                raise TypeError("canonical transaction returned an invalid staged response")
            staged.__post_init__()
            receipt = staged.receipt
            if is_effect and type(receipt) is not CoreEffectCommitReceipt:
                raise TypeError("canonical transaction returned the wrong receipt type")
            if not is_effect and type(receipt) is not CoreFenceTransitionReceipt:
                raise TypeError("canonical transaction returned the wrong receipt type")
            receipt.__post_init__()
            matches = receipt.matches_request(request)
            if not matches:
                rollback_error = _rollback(transaction)
                if rollback_error is not None:
                    return _result(CoreCommitStatus.UNRESOLVED, rollback_error)
                return _result(
                    CoreCommitStatus.DENIED,
                    "canonical transaction returned a receipt for a different request",
                )
        except CoreAtomicCommitRejected as exc:
            rollback_error = _rollback(transaction)
            if rollback_error is not None:
                return _result(CoreCommitStatus.UNRESOLVED, rollback_error)
            return _result(CoreCommitStatus.DENIED, _safe_reason(exc))
        except CoreAtomicCommitOutcomeUnknown as exc:
            rollback_error = _rollback(transaction)
            if rollback_error is not None:
                return _result(CoreCommitStatus.UNRESOLVED, rollback_error)
            return _result(CoreCommitStatus.UNAVAILABLE, _safe_reason(exc))
        except Exception as exc:
            rollback_error = _rollback(transaction)
            if rollback_error is not None:
                return _result(CoreCommitStatus.UNRESOLVED, rollback_error)
            return _result(CoreCommitStatus.UNAVAILABLE, _safe_reason(exc))
        try:
            transaction.commit()
        except Exception as exc:
            # The commit may have taken effect before an acknowledgement was lost.
            # Do not rollback or repeat; canonical idempotency resolves a later retry.
            return _result(CoreCommitStatus.UNRESOLVED, _safe_reason(exc))
        return CoreLifecycleCommitResult(
            CoreCommitStatus.COMMITTED,
            receipt,
            staged.replayed,
            "canonical transaction committed",
        )


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")


def _fingerprint(value: dict[str, object]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _claim_dict(claim: CoreLifecycleExpectedGeneration) -> dict[str, object]:
    return {
        "instance_id": claim.instance_id,
        "incarnation_id": claim.incarnation_id,
        "package_id": claim.package_id,
        "package_digest": claim.package_digest,
        "expected_generation": claim.expected_generation,
    }


def _authorities_dict(
    authorities: tuple[CoreAuthorityReference, ...],
) -> list[dict[str, str]]:
    return [
        {"authority": item.authority, "reference_id": item.reference_id, "digest": item.digest}
        for item in authorities
    ]


def _result(status: CoreCommitStatus, reason: str) -> CoreLifecycleCommitResult:
    return CoreLifecycleCommitResult(status, None, False, reason or status.value)


def _safe_reason(error: Exception) -> str:
    message = str(error).strip()
    return message[:240] if message else type(error).__name__


def _rollback(transaction: CoreCanonicalLifecycleTransaction) -> str | None:
    try:
        transaction.rollback()
    except Exception as exc:
        return _safe_reason(exc)
    return None


def _digest(value: str, name: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _nonnegative(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _positive(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _text_tuple(values: tuple[str, ...], name: str) -> None:
    if not isinstance(values, tuple):
        raise TypeError(f"{name} must be a tuple")
    for value in values:
        _text(value, name)
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must not contain duplicates")


def _authority_tuple(values: tuple[CoreAuthorityReference, ...]) -> None:
    if not isinstance(values, tuple) or any(
        not isinstance(value, CoreAuthorityReference) for value in values
    ):
        raise TypeError("authority_refs must be a tuple of CoreAuthorityReference")


def _aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("committed_at must be timezone-aware")
