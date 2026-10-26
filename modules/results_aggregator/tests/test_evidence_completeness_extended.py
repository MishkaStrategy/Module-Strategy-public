from __future__ import annotations

from dataclasses import replace

import pytest

from conftest import make_package
from modular_results_registry.errors import IntegrityError, ValidationError
from modular_results_registry.evidence import (
    EvidenceCompletenessResult,
    EvidenceRequirement,
    evaluate_evidence_completeness,
)
from modular_results_registry.models import CompletenessStatus, EvidenceStatus


def _evidence(status: EvidenceStatus, *, evidence_type: str = "bootstrap"):
    return replace(make_package().evidence[0], status=status, evidence_type=evidence_type)


@pytest.mark.parametrize(
    ("status", "expected", "category", "blocking"),
    [
        (EvidenceStatus.PASS, CompletenessStatus.COMPLETE, "satisfied", ()),
        (EvidenceStatus.PARTIAL, CompletenessStatus.BLOCKED, "partial", ("PARTIAL:bootstrap",)),
        (EvidenceStatus.FAIL, CompletenessStatus.BLOCKED, "failed", ("FAILED:bootstrap",)),
        (
            EvidenceStatus.CONTAMINATED,
            CompletenessStatus.CONTAMINATED,
            "contaminated",
            ("CONTAMINATED:bootstrap",),
        ),
        (
            EvidenceStatus.INCOMPATIBLE,
            CompletenessStatus.INCOMPATIBLE,
            "incompatible",
            ("INCOMPATIBLE:bootstrap",),
        ),
        (EvidenceStatus.MISSING, CompletenessStatus.BLOCKED, "missing", ("MISSING:bootstrap",)),
    ],
)
def test_all_evidence_statuses(status, expected, category, blocking) -> None:
    result = evaluate_evidence_completeness(
        (_evidence(status),),
        (EvidenceRequirement("bootstrap"),),
    )
    assert result.status is expected
    assert getattr(result, category) == ("bootstrap",)
    assert result.blocking_reasons == blocking


def test_partial_requirement_can_accept_partial() -> None:
    result = evaluate_evidence_completeness(
        (_evidence(EvidenceStatus.PARTIAL),),
        (EvidenceRequirement("bootstrap", minimum_status=EvidenceStatus.PARTIAL),),
    )
    assert result.status is CompletenessStatus.COMPLETE
    assert result.satisfied == ("bootstrap",)


def test_nonblocking_failures_are_insufficient_or_partial_not_blocked() -> None:
    failed = evaluate_evidence_completeness(
        (_evidence(EvidenceStatus.FAIL),),
        (EvidenceRequirement("bootstrap", blocking=False),),
    )
    assert failed.status is CompletenessStatus.INSUFFICIENT
    assert failed.blocking_reasons == ()
    partial = evaluate_evidence_completeness(
        (_evidence(EvidenceStatus.PARTIAL),),
        (EvidenceRequirement("bootstrap", blocking=False),),
    )
    assert partial.status is CompletenessStatus.PARTIAL


def test_missing_nonblocking_requirement_is_insufficient() -> None:
    result = evaluate_evidence_completeness(
        (),
        (EvidenceRequirement("bootstrap", blocking=False),),
    )
    assert result.status is CompletenessStatus.INSUFFICIENT
    assert result.missing == ("bootstrap",)


def test_status_priority_contamination_then_incompatible() -> None:
    evidence = (
        replace(
            _evidence(EvidenceStatus.INCOMPATIBLE, evidence_type="monte_carlo"),
            evidence_id="evidence-002",
        ),
        _evidence(EvidenceStatus.CONTAMINATED, evidence_type="bootstrap"),
    )
    result = evaluate_evidence_completeness(
        evidence,
        (EvidenceRequirement("bootstrap"), EvidenceRequirement("monte_carlo")),
    )
    assert result.status is CompletenessStatus.CONTAMINATED
    assert result.contaminated == ("bootstrap",)
    assert result.incompatible == ("monte_carlo",)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: EvidenceRequirement(""),
        lambda: EvidenceRequirement("bootstrap", blocking=1),
        lambda: EvidenceRequirement("bootstrap", minimum_status="PASS"),
        lambda: EvidenceRequirement("bootstrap", minimum_status=EvidenceStatus.FAIL),
    ],
)
def test_requirement_validation(factory) -> None:
    with pytest.raises(ValidationError):
        factory()


def test_evaluate_input_validation() -> None:
    evidence = _evidence(EvidenceStatus.PASS)
    requirement = EvidenceRequirement("bootstrap")
    for evidence_value, requirements_value in (
        ([evidence], (requirement,)),
        ((object(),), (requirement,)),
        ((evidence,), [requirement]),
        ((evidence,), (object(),)),
        ((evidence,), ()),
        ((evidence, evidence), (requirement,)),
        ((evidence,), (requirement, requirement)),
    ):
        with pytest.raises(ValidationError):
            evaluate_evidence_completeness(evidence_value, requirements_value)  # type: ignore[arg-type]


def test_result_integrity_and_shape_validation() -> None:
    result = evaluate_evidence_completeness(
        (_evidence(EvidenceStatus.PASS),),
        (EvidenceRequirement("bootstrap"),),
    )
    with pytest.raises(IntegrityError):
        replace(result, result_id="completeness-" + "0" * 48)
    for changes in (
        {"status": "COMPLETE"},
        {"satisfied": ["bootstrap"]},
        {"satisfied": ("",)},
        {"satisfied": ("bootstrap", "bootstrap")},
        {"missing": ("bootstrap",)},
        {"blocking_reasons": ["x"]},
        {"blocking_reasons": ("",)},
        {"blocking_reasons": ("x", "x")},
    ):
        with pytest.raises((ValidationError, IntegrityError)):
            replace(result, **changes)


def test_result_constructor_rejects_category_overlap() -> None:
    with pytest.raises(ValidationError, match="disjoint"):
        EvidenceCompletenessResult(
            status=CompletenessStatus.COMPLETE,
            satisfied=("bootstrap",),
            missing=("bootstrap",),
            failed=(),
            partial=(),
            contaminated=(),
            incompatible=(),
            blocking_reasons=(),
            result_id="completeness-" + "0" * 48,
        )
