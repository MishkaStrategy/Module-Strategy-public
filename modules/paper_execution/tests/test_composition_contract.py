from __future__ import annotations

import builtins
import os
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from paper_execution._composition_contract import (
    CONTRACT_VERSION,
    REQUIRED_COMPONENTS,
    AggregateState,
    ComponentEvidenceReceipt,
    ComponentKind,
    CompositionCheckpoint,
    CompositionEvidence,
    CompositionScope,
    CompositionSnapshot,
    CompositionVersion,
    EvidenceState,
    OwnerFence,
    require_complete_checkpoint,
    require_complete_snapshot,
    validate_checkpoint_bindings,
    validate_snapshot_binding,
)


def _scope(membership_version: str = "members-7") -> CompositionScope:
    return CompositionScope("paper-a", membership_version, ("strategy-a", "strategy-b"))


def _receipts(
    *,
    scope: CompositionScope | None = None,
    owner_id: str = "owner-x",
    owner_generation: str = "generation-4",
    version: CompositionVersion | None = None,
) -> tuple[ComponentEvidenceReceipt, ...]:
    bound_scope = scope or _scope()
    bound_version = version or CompositionVersion(12, 19)
    return tuple(
        ComponentEvidenceReceipt(
            component=component,
            scope=bound_scope,
            owner_id=owner_id,
            owner_generation=owner_generation,
            version=bound_version,
            covered_members=bound_scope.members,
            evidence_digest=f"{index + 1:064x}",
        )
        for index, component in enumerate(REQUIRED_COMPONENTS)
    )


def _evidence(
    receipts: tuple[ComponentEvidenceReceipt, ...] | None = None,
    *,
    unavailable: frozenset[ComponentKind] = frozenset(),
) -> CompositionEvidence:
    receipt_values = receipts if receipts is not None else _receipts()
    return CompositionEvidence(
        child_positions=_status(ComponentKind.CHILD_POSITIONS, unavailable),
        child_orders=_status(ComponentKind.CHILD_ORDERS, unavailable),
        child_fills=_status(ComponentKind.CHILD_FILLS, unavailable),
        shared_accounting=_status(ComponentKind.SHARED_ACCOUNTING, unavailable),
        reservations=_status(ComponentKind.RESERVATIONS, unavailable),
        allocation=_status(ComponentKind.ALLOCATION, unavailable),
        replay=_status(ComponentKind.REPLAY, unavailable),
        risk=_status(ComponentKind.RISK, unavailable),
        receipts=receipt_values,
    )


def _status(component: ComponentKind, unavailable: frozenset[ComponentKind]) -> EvidenceState:
    return EvidenceState.UNCONFIGURED if component in unavailable else EvidenceState.AVAILABLE


def _snapshot(
    *,
    scope: CompositionScope | None = None,
    owner_id: str = "owner-x",
    owner_generation: str = "generation-4",
    version: CompositionVersion | None = None,
    state: AggregateState = AggregateState.COMPLETE,
    evidence: CompositionEvidence | None = None,
    observed_at: datetime = datetime(2026, 10, 23, 10, 0, tzinfo=UTC),
) -> CompositionSnapshot:
    bound_scope = scope or _scope()
    bound_version = version or CompositionVersion(12, 19)
    return CompositionSnapshot(
        contract_version=CONTRACT_VERSION,
        scope=bound_scope,
        owner_id=owner_id,
        owner_generation=owner_generation,
        version=bound_version,
        observed_at=observed_at,
        state=state,
        evidence=evidence
        or _evidence(
            _receipts(
                scope=bound_scope,
                owner_id=owner_id,
                owner_generation=owner_generation,
                version=bound_version,
            )
        ),
    )


def _checkpoint(
    *,
    scope: CompositionScope | None = None,
    generation: str = "generation-4",
    version: CompositionVersion | None = None,
    evidence: CompositionEvidence | None = None,
) -> CompositionCheckpoint:
    bound_scope = scope or _scope()
    bound_version = version or CompositionVersion(12, 19)
    return CompositionCheckpoint.create(
        scope=bound_scope,
        source_owner_id="owner-x",
        source_owner_generation=generation,
        version=bound_version,
        evidence=evidence
        or _evidence(
            _receipts(
                scope=bound_scope,
                owner_id="owner-x",
                owner_generation=generation,
                version=bound_version,
            )
        ),
    )


def test_scope_requires_exact_stable_unique_membership() -> None:
    with pytest.raises(ValueError, match="unique and sorted"):
        CompositionScope("paper-a", "members-7", ("strategy-b", "strategy-a"))
    with pytest.raises(ValueError, match="unique and sorted"):
        CompositionScope("paper-a", "members-7", ("strategy-a", "strategy-a"))


def test_snapshot_binding_rejects_generation_membership_and_version_mismatch() -> None:
    fence = OwnerFence("owner-x", "generation-4", CompositionVersion(12, 19))
    valid = _snapshot()
    validate_snapshot_binding(valid, expected_scope=_scope(), expected_fence=fence)

    with pytest.raises(ValueError, match="scope or membership"):
        validate_snapshot_binding(
            _snapshot(scope=_scope("members-8")),
            expected_scope=_scope(),
            expected_fence=fence,
        )
    with pytest.raises(ValueError, match="generation"):
        validate_snapshot_binding(
            _snapshot(owner_generation="generation-3"),
            expected_scope=_scope(),
            expected_fence=fence,
        )
    with pytest.raises(ValueError, match="version"):
        validate_snapshot_binding(
            _snapshot(version=CompositionVersion(12, 20)),
            expected_scope=_scope(),
            expected_fence=fence,
        )


def test_complete_snapshot_requires_every_component_receipt() -> None:
    for component in REQUIRED_COMPONENTS:
        missing = tuple(receipt for receipt in _receipts() if receipt.component is not component)
        incomplete = _evidence(missing, unavailable=frozenset({component}))
        snapshot = _snapshot(state=AggregateState.UNAVAILABLE, evidence=incomplete)
        with pytest.raises(ValueError, match="not complete"):
            require_complete_snapshot(snapshot)
        with pytest.raises(ValueError, match="full required component receipt set"):
            _snapshot(evidence=incomplete)


def test_duplicate_component_receipt_is_rejected() -> None:
    receipts = _receipts()
    with pytest.raises(ValueError, match="unique and canonically ordered"):
        _evidence((*receipts, receipts[0]))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("scope", _scope("members-6"), "scope binding"),
        ("owner_generation", "generation-3", "generation binding"),
        ("version", CompositionVersion(11, 19), "state version binding"),
    ],
)
def test_snapshot_rejects_receipt_with_mismatched_scope_generation_or_version(
    field: str,
    value: object,
    message: str,
) -> None:
    receipts = list(_receipts())
    receipts[0] = replace(receipts[0], **{field: value})
    with pytest.raises(ValueError, match=message):
        _snapshot(evidence=_evidence(tuple(receipts)))


def test_snapshot_and_checkpoint_reject_receipts_from_different_owner() -> None:
    foreign_owner_evidence = _evidence(_receipts(owner_id="owner-y"))
    with pytest.raises(ValueError, match="receipt owner binding mismatch"):
        _snapshot(evidence=foreign_owner_evidence)
    with pytest.raises(ValueError, match="receipt owner binding mismatch"):
        _checkpoint(evidence=foreign_owner_evidence)


def test_snapshot_observation_time_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _snapshot(observed_at=datetime(2026, 10, 23, 10, 0))


def test_receipt_requires_canonical_coverage_of_exact_membership() -> None:
    with pytest.raises(ValueError, match="exact stable membership"):
        ComponentEvidenceReceipt(
            component=ComponentKind.CHILD_POSITIONS,
            scope=_scope(),
            owner_id="owner-x",
            owner_generation="generation-4",
            version=CompositionVersion(12, 19),
            covered_members=("strategy-a",),
            evidence_digest="a" * 64,
        )


def test_checkpoint_manifest_digest_and_expected_bindings_fail_closed() -> None:
    checkpoint = _checkpoint()
    assert (
        validate_checkpoint_bindings(
            checkpoint,
            expected_scope=_scope(),
            expected_source_owner_id="owner-x",
            expected_source_owner_generation="generation-4",
            expected_version=CompositionVersion(12, 19),
        )
        is checkpoint
    )

    with pytest.raises(ValueError, match="manifest digest mismatch"):
        replace(checkpoint, manifest_digest="f" * 64)
    with pytest.raises(ValueError, match="scope or membership"):
        validate_checkpoint_bindings(
            checkpoint,
            expected_scope=_scope("members-8"),
            expected_source_owner_id="owner-x",
            expected_source_owner_generation="generation-4",
            expected_version=CompositionVersion(12, 19),
        )
    with pytest.raises(ValueError, match="generation is stale"):
        validate_checkpoint_bindings(
            checkpoint,
            expected_scope=_scope(),
            expected_source_owner_id="owner-x",
            expected_source_owner_generation="generation-3",
            expected_version=CompositionVersion(12, 19),
        )
    with pytest.raises(ValueError, match="state version is stale"):
        validate_checkpoint_bindings(
            checkpoint,
            expected_scope=_scope(),
            expected_source_owner_id="owner-x",
            expected_source_owner_generation="generation-4",
            expected_version=CompositionVersion(11, 19),
        )


def test_partial_checkpoint_cannot_be_staged_as_complete() -> None:
    missing = ComponentKind.REPLAY
    receipts = tuple(receipt for receipt in _receipts() if receipt.component is not missing)
    partial = _checkpoint(
        evidence=_evidence(receipts, unavailable=frozenset({missing})),
    )
    with pytest.raises(ValueError, match="full required component receipt set"):
        validate_checkpoint_bindings(
            partial,
            expected_scope=_scope(),
            expected_source_owner_id="owner-x",
            expected_source_owner_generation="generation-4",
            expected_version=CompositionVersion(12, 19),
        )
    with pytest.raises(ValueError, match="full required component receipt set"):
        require_complete_checkpoint(partial)


def test_value_construction_and_checkpoint_validation_have_no_filesystem_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("contract construction attempted filesystem I/O")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    scope = _scope()
    version = CompositionVersion(12, 19)
    fence = OwnerFence("owner-x", "generation-4", version)
    snapshot = _snapshot(scope=scope)
    checkpoint = _checkpoint(scope=scope)
    validate_snapshot_binding(snapshot, expected_scope=scope, expected_fence=fence)
    require_complete_snapshot(snapshot)
    validate_checkpoint_bindings(
        checkpoint,
        expected_scope=scope,
        expected_source_owner_id=fence.owner_id,
        expected_source_owner_generation=fence.owner_generation,
        expected_version=version,
    )
