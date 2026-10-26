from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from strategy_factory.artifacts import ApprovedStrategyPackageLoader, ContentAddressedArtifactStore
from strategy_factory.canonical import canonical_sha256
from strategy_factory.contracts import (
    AdmissionStatus,
    CandidateEvaluation,
    CandidateStrategy,
    HoldoutReport,
    RobustnessReport,
    StrategyAuditDecision,
    TimeRange,
)
from strategy_factory.errors import CheckpointError
from strategy_factory.package import StrategyPackageBuilder


def _approved_package(factory_request: Any, architecture: Any) -> object:
    candidate_id = "candidate-restart"
    parameters = {
        "fixed_fractional.risk_fraction": "0.005",
        "fixed_r.reward_r": 4,
    }
    candidate = CandidateStrategy(
        candidate_id=candidate_id,
        architecture=architecture,
        parameters=parameters,
        configuration_hash=canonical_sha256(parameters),
        lineage_references=("run-restart",),
        evaluation=CandidateEvaluation(
            evaluation_id="eval-restart",
            candidate_id=candidate_id,
            metrics={"net_return": "0.20", "max_drawdown": "0.08"},
            constraints={"registry_clean": True},
            pareto_front=0,
            instability_flags=(),
            complexity=Decimal("2"),
            evidence_references=("evidence-restart",),
        ),
    )
    period = factory_request.dataset.period
    quarter = (period.end - period.start) / 4
    periods = {
        "train": TimeRange(period.start, period.start + quarter),
        "validation": TimeRange(period.start + quarter, period.start + 2 * quarter),
        "test": TimeRange(period.start + 2 * quarter, period.start + 3 * quarter),
        "holdout": TimeRange(period.start + 3 * quarter, period.end),
    }
    generated_at = period.end + timedelta(hours=1)
    return StrategyPackageBuilder().build(
        request=factory_request,
        candidate=candidate,
        strategy_id="restart-strategy",
        version="1.0.0",
        commit_sha="c" * 40,
        research_periods=periods,
        robustness=RobustnessReport(
            report_id="robustness-restart",
            candidate_id=candidate_id,
            passed=True,
            scenario_results={"cost_stress": "pass"},
            failure_reasons=(),
            generated_at=generated_at,
        ),
        holdout=HoldoutReport(
            report_id="holdout-restart",
            candidate_id=candidate_id,
            period=periods["holdout"],
            dataset_fingerprint=factory_request.dataset.fingerprint,
            metrics={"profit_factor": "1.25"},
            passed=True,
            opened_once=True,
            generated_at=generated_at + timedelta(hours=1),
        ),
        key_results={"net_return": "0.20"},
        known_weaknesses=("restart fixture",),
        disabled_regimes=("illiquid",),
        evidence_links=("evidence-restart",),
        paper_configuration={"symbol": factory_request.symbol},
        risk_policy={"risk_fraction": "0.005"},
        execution_assumptions={"maker_fee": "0.0002"},
        audit_decision=StrategyAuditDecision.issue(
            audit_id="audit-restart",
            candidate_id=candidate_id,
            status=AdmissionStatus.APPROVED_FOR_PAPER,
            evidence_package_reference="evidence:restart",
            evidence_hash="a" * 64,
            reason_codes=("INDEPENDENT_AUDIT_PASSED",),
            issued_at=generated_at + timedelta(hours=2),
            auditor_id="strategy-audit-restart",
            contract_version="strategy-audit-port-v1",
            signature="restart-signature",
        ),
    )


def _persist_package(
    root: Path, factory_request: object, architecture: object
) -> tuple[ContentAddressedArtifactStore, str, object]:
    package = _approved_package(factory_request, architecture)
    store = ContentAddressedArtifactStore(root)
    artifact_id = store.put(
        artifact_type="strategy_package",
        value={
            "schema_version": 1,
            "strategy_package": package,
            "admission_status": package.admission_status.value,
            "audit_decision_hash": package.constraints["audit_decision_hash"],
        },
    )
    return store, artifact_id, package


def test_loads_only_configured_verified_paper_package(
    tmp_path: Path, factory_request: object, architecture: object
) -> None:
    store, artifact_id, package = _persist_package(tmp_path, factory_request, architecture)

    loaded = ApprovedStrategyPackageLoader(
        store,
        artifact_id=artifact_id,
        strategy_id=package.strategy_id,
        package_hash=package.package_hash,
    ).load()

    assert loaded == package


@pytest.mark.parametrize("field", ["strategy_id", "package_hash"])
def test_configured_strategy_identity_mismatch_fails_closed(
    tmp_path: Path,
    factory_request: object,
    architecture: object,
    field: str,
) -> None:
    store, artifact_id, package = _persist_package(tmp_path, factory_request, architecture)
    configured = {
        "strategy_id": package.strategy_id,
        "package_hash": package.package_hash,
    }
    configured[field] = "other-strategy" if field == "strategy_id" else "f" * 64

    with pytest.raises(CheckpointError, match="identity mismatch"):
        ApprovedStrategyPackageLoader(store, artifact_id=artifact_id, **configured).load()


def test_corrupt_or_live_strategy_state_fails_closed(
    tmp_path: Path, factory_request: object, architecture: object
) -> None:
    store, artifact_id, package = _persist_package(tmp_path, factory_request, architecture)
    path = store.path_for(artifact_id)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["payload"]["strategy_package"]["live_trading_authorized"] = True
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(CheckpointError):
        ApprovedStrategyPackageLoader(
            store,
            artifact_id=artifact_id,
            strategy_id=package.strategy_id,
            package_hash=package.package_hash,
        ).load()


def test_strategy_artifact_audit_identity_mismatch_fails_closed(
    tmp_path: Path, factory_request: object, architecture: object
) -> None:
    package = _approved_package(factory_request, architecture)
    store = ContentAddressedArtifactStore(tmp_path)
    artifact_id = store.put(
        artifact_type="strategy_package",
        value={
            "schema_version": 1,
            "strategy_package": package,
            "admission_status": package.admission_status.value,
            "audit_decision_hash": "f" * 64,
        },
    )

    with pytest.raises(CheckpointError, match="audit decision identity mismatch"):
        ApprovedStrategyPackageLoader(
            store,
            artifact_id=artifact_id,
            strategy_id=package.strategy_id,
            package_hash=package.package_hash,
        ).load()


def test_rejected_package_is_never_restart_authoritative(
    tmp_path: Path, factory_request: object, architecture: object
) -> None:
    store, _, package = _persist_package(tmp_path, factory_request, architecture)
    rejected = replace(package, admission_status=package.admission_status.REJECTED)
    rejected = replace(rejected, package_hash=StrategyPackageBuilder().compute_hash(rejected))
    artifact_id = store.put(
        artifact_type="strategy_package",
        value={
            "schema_version": 1,
            "strategy_package": rejected,
            "admission_status": rejected.admission_status.value,
            "audit_decision_hash": rejected.constraints["audit_decision_hash"],
        },
    )

    with pytest.raises(CheckpointError, match="approved for PAPER"):
        ApprovedStrategyPackageLoader(
            store,
            artifact_id=artifact_id,
            strategy_id=rejected.strategy_id,
            package_hash=rejected.package_hash,
        ).load()
