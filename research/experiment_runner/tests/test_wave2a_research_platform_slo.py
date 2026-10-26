from __future__ import annotations

import hashlib
import importlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from modular_research_runner import (
    ExperimentIdentityInputs,
    configuration_id,
    run_id,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
STREAM_F_ROOT = REPOSITORY_ROOT / "reports" / "strategy-governance-wave2f"
STREAM_A_ROOT = REPOSITORY_ROOT / "reports" / "research-platform-wave2a"
COMPOSITION_ROOT = REPOSITORY_ROOT / "research" / "strategy_audit_validation" / "compositions"
BASELINE_SHA256 = (
    "80db925072f8a972336c93bde2c45e903fbf42ab5da7401bb4800a9286c6f3ab"  # pragma: allowlist secret
)


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _run_smoke(output_path: Path) -> dict[str, Any]:
    component_root = REPOSITORY_ROOT / "research" / "experiment_runner"
    sys.path.insert(0, str(component_root))
    try:
        benchmark = importlib.import_module("benchmarks.wave2a_verified_baseline")
    finally:
        sys.path.remove(str(component_root))
    report = benchmark.run_verified_baseline(output_path=output_path, profile="smoke")
    assert isinstance(report, dict)
    return report


def test_stream_f_checksums_and_canonical_composition_remain_fail_closed() -> None:
    checksums = _json(STREAM_F_ROOT / "evidence-checksums.json")
    files = checksums["files"]
    assert isinstance(files, dict)
    for relative_path, expected in files.items():
        assert isinstance(relative_path, str)
        assert isinstance(expected, str)
        payload = (REPOSITORY_ROOT / relative_path).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == expected

    governance_source = REPOSITORY_ROOT / "research" / "strategy_audit_validation" / "src"
    sys.path.insert(0, str(governance_source))
    try:
        governance = importlib.import_module("strategy_audit_validation.governance")
        errors = importlib.import_module("strategy_audit_validation.errors")
    finally:
        sys.path.remove(str(governance_source))

    registry = _json(COMPOSITION_ROOT / "module-contracts-v1.json")
    contract_items = registry["contracts"]
    assert isinstance(contract_items, list)
    contracts = {}
    for item in contract_items:
        assert isinstance(item, dict)
        contract = governance.ModuleContract(
            module_id=item["module_id"],
            module_version=item["module_version"],
            required_parameters=frozenset(item["required_parameters"]),
            allowed_timeframes=frozenset(item["allowed_timeframes"]),
            contract_complete=item["contract_complete"],
            source_path=item["source_path"],
            source_commit_sha=item["source_commit_sha"],
        )
        contracts[contract.module_id] = contract

    gap = _json(COMPOSITION_ROOT / "canonical-fvg-rt-final-15m-frozen-1h-4h.gap.json")
    composition = gap["composition"]
    assert isinstance(composition, dict)
    candidate = governance.load_composition(
        composition,
        contracts=contracts,
        require_canonical=False,
    )
    assert candidate.complete is False
    assert candidate.candidate_id == gap["candidate_id"]
    assert candidate.composition_hash == gap["composition_hash"]
    assert candidate.state.value == "INCOMPLETE"

    with pytest.raises(
        errors.EvidenceMalformedError,
        match="canonical composition is incomplete or misnamed",
    ):
        governance.load_composition(
            composition,
            contracts=contracts,
            require_canonical=True,
        )


def test_identity_changes_only_when_meaningful_inputs_change(specification) -> None:
    inputs = ExperimentIdentityInputs(
        specification=specification,
        backtest_engine_version="0.4.0",
        metric_schema_version="metrics-v1",
        module_api_version="module-api-v1-draft",
    )
    assert inputs.experiment_id() == inputs.experiment_id()

    changed_dataset = replace(specification.datasets[0], sha256="b" * 64)
    changed_specification = replace(specification, datasets=(changed_dataset,))
    changed_inputs = replace(inputs, specification=changed_specification)
    assert changed_inputs.experiment_id() != inputs.experiment_id()

    first_configuration = configuration_id({"candidate": "governance", "threshold": 1})
    assert first_configuration == configuration_id({"threshold": 1, "candidate": "governance"})
    second_configuration = configuration_id({"candidate": "governance", "threshold": 2})
    assert second_configuration != first_configuration

    first_run = run_id(
        experiment_id=inputs.experiment_id(),
        stage_id="train",
        split_id="fixture",
        configuration_sha256=first_configuration,
        scenario_id="paper-only",
        seed=7,
        repetition_index=0,
    )
    assert first_run == run_id(
        experiment_id=inputs.experiment_id(),
        stage_id="train",
        split_id="fixture",
        configuration_sha256=first_configuration,
        scenario_id="paper-only",
        seed=7,
        repetition_index=0,
    )
    assert first_run != run_id(
        experiment_id=inputs.experiment_id(),
        stage_id="train",
        split_id="fixture",
        configuration_sha256=second_configuration,
        scenario_id="paper-only",
        seed=7,
        repetition_index=0,
    )


def test_wave2a_committed_baseline_and_exact_head_smoke_are_fail_closed() -> None:
    baseline_path = STREAM_A_ROOT / "baseline-evidence.json"
    baseline_bytes = baseline_path.read_bytes()
    assert hashlib.sha256(baseline_bytes).hexdigest() == BASELINE_SHA256
    baseline = json.loads(baseline_bytes)

    assert baseline["workload_contract"] == {
        "dataset_id": "fixture-btcusdt-5m-governance-v1",
        "exchange_credentials_used": False,
        "id": "research-platform-wave2a-synthetic-v1",
        "live_trading_used": False,
        "network_used": False,
        "paper_only": True,
        "raw_dataset_published": False,
    }
    assert baseline["stream_f_contract"]["canonical_strategy_execution_claimed"] is False
    assert baseline["stream_f_contract"]["canonical_strategy_status"] == ("INCOMPLETE_FAIL_CLOSED")
    assert baseline["scheduler_sequential_1000"]["inserted_jobs"] == 1000
    assert baseline["scheduler_bounded_1000"]["inserted_jobs"] == 1000
    assert baseline["publication_1000"]["revision"] == 1000
    assert baseline["duplicate_admission"]["duplicate_suppression_count"] == 1000
    assert baseline["invalid_admission"]["rejected"] == 1000
    assert baseline["invalid_admission"]["backtest_side_effects"] == 0
    assert len(baseline["variance"]["scheduler_semantic_digests"]) == 1
    assert len(baseline["variance"]["publication_semantic_digests"]) == 1
    assert baseline["variance"]["scheduler_1000_candidates_per_second"]["sample_count"] == 3
    assert baseline["variance"]["publication_1000_packages_per_second"]["sample_count"] == 3

    conflict = baseline["publication_conflict"]
    assert conflict["rejected"] is True
    assert conflict["audit_revision_advanced"] is True
    assert conflict["original_entity_preserved"] is True
    assert conflict["conflicting_entity_visible"] is False
    assert conflict["quarantine_count"] >= 1
    assert baseline["restart_midway"]["recovered_leases"] == 1
    assert baseline["restart_midway"]["ordering_preserved"] is True
    assert baseline["cancellation"]["canceled_runs"] == 10
    assert baseline["cancellation"]["orphan_process_count"] == 0
    assert baseline["cancellation"]["orphan_artifact_count"] == 0

    smoke = _run_smoke(STREAM_A_ROOT / "smoke-evidence.json")
    assert smoke["profile"] == "smoke"
    assert smoke["workload_contract"]["network_used"] is False
    assert smoke["stream_f_contract"]["canonical_strategy_execution_claimed"] is False
    workloads = smoke["workloads"]
    assert workloads["scheduler-sequential-10"]["inserted_jobs"] == 10
    assert workloads["scheduler-bounded-10"]["inserted_jobs"] == 10
    assert workloads["publication-10"]["revision"] == 10
    assert workloads["mostly-invalid-1000"]["backtest_side_effects"] == 0
    assert workloads["restart-midway"]["ordering_preserved"] is True
    assert workloads["cancellation"]["orphan_process_count"] == 0
    smoke_conflict = workloads["publication-conflict"]
    assert smoke_conflict["original_entity_preserved"] is True
    assert smoke_conflict["conflicting_entity_visible"] is False
