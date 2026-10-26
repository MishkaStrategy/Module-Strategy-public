from __future__ import annotations

from pathlib import Path

import pytest

from modular_system_acceptance.fault_recovery import (
    CIVerificationStatus,
    FaultClass,
    ObservabilityKind,
    ScenarioSuite,
    assert_recovered_state_matches,
    factual_state_fingerprint,
    load_fault_recovery_matrix,
    load_residual_risk_register,
    sanitized_evidence_record,
)

ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "fault_recovery_matrix.v1.json"
RESIDUAL_PATH = ROOT / "fault_recovery_residual_risks.v1.json"
CANDIDATE_SHA = "a" * 40
FINANCIAL_EFFECT_MARKERS = {
    "duplicate_fill",
    "duplicate_order",
    "duplicate_position_transition",
    "new_order_under_pressure",
    "new_order_while_stale",
}


def test_matrix_covers_every_required_fault_class_and_both_process_kill_windows() -> None:
    matrix = load_fault_recovery_matrix(MATRIX_PATH)

    assert matrix.schema_version == "1"
    assert matrix.fault_classes == frozenset(FaultClass)
    assert len(matrix.scenarios) >= len(FaultClass)
    assert {scenario.suite for scenario in matrix.scenarios} == {
        ScenarioSuite.SMOKE,
        ScenarioSuite.EXTENDED,
    }
    process_kills = [
        scenario for scenario in matrix.scenarios if scenario.fault_class is FaultClass.PROCESS_KILL
    ]
    assert {scenario.injector for scenario in process_kills} == {
        "process_kill_before_owner_commit",
        "process_kill_after_owner_commit",
    }


def test_residual_register_covers_every_matrix_scenario_exactly_once() -> None:
    matrix = load_fault_recovery_matrix(MATRIX_PATH)
    register = load_residual_risk_register(RESIDUAL_PATH)

    assert register.schema_version == "1"
    assert {item.scenario_id for item in register.entries} == {
        item.scenario_id for item in matrix.scenarios
    }
    for item in register.entries:
        if item.verification_status is CIVerificationStatus.RESIDUAL_NOT_FULLY_REPRODUCED:
            assert item.residual_risk_id is not None
        else:
            assert item.residual_risk_id is None
            assert not item.physical_required

    sqlite = register.entry("acceptance_sqlite_busy_wal")
    assert sqlite.verification_status is CIVerificationStatus.OWNER_BACKED_FAST
    assert sqlite.residual_risk_id is None
    assert not sqlite.physical_required

    for scenario_id in (
        "runner_process_kill",
        "runner_process_kill_before_checkpoint",
        "market_data_stale_interrupt",
        "out_of_order_owner_evidence",
        "signal_bot_unavailable",
        "control_plane_restart",
        "operator_retry_after_timeout",
    ):
        composed = register.entry(scenario_id)
        assert composed.verification_status is CIVerificationStatus.COMPOSED_CONTRACT_FAST
        assert composed.residual_risk_id is None
        assert not composed.physical_required


def test_matrix_distinguishes_component_state_from_control_plane_transport() -> None:
    matrix = load_fault_recovery_matrix(MATRIX_PATH)

    transport = [
        scenario
        for scenario in matrix.scenarios
        if scenario.observability.kind is ObservabilityKind.TRANSPORT
    ]
    assert [scenario.scenario_id for scenario in transport] == ["control_plane_restart"]
    assert transport[0].observability.subject == "control_plane_transport"


def test_runtime_risk_faults_explicitly_forbid_duplicate_financial_effects() -> None:
    matrix = load_fault_recovery_matrix(MATRIX_PATH)
    runtime_targets = {"risk_supervisor", "trading_runner", "market_data", "system_recovery"}

    for scenario in matrix.scenarios:
        if scenario.target_owner not in runtime_targets:
            continue
        assert FINANCIAL_EFFECT_MARKERS.intersection(scenario.forbidden_side_effects), (
            scenario.scenario_id,
            scenario.forbidden_side_effects,
        )


def test_factual_state_fingerprint_matches_continuous_and_recovered_paths() -> None:
    matrix = load_fault_recovery_matrix(MATRIX_PATH)
    scenario = matrix.scenario("runner_process_kill")
    continuous = {
        "order_count": 4,
        "fill_count": 2,
        "position_revision": 8,
        "runner_checkpoint_seq": 15,
        "ignored_runtime_cache": "different-on-purpose",
    }
    recovered = {
        "order_count": 4,
        "fill_count": 2,
        "position_revision": 8,
        "runner_checkpoint_seq": 15,
        "ignored_runtime_cache": "rebuilt-cache",
    }

    left, right = assert_recovered_state_matches(
        continuous,
        recovered,
        fields=scenario.factual_state_fields,
    )

    assert left == right
    assert left.digest_sha256 == right.digest_sha256
    assert "ignored_runtime_cache" not in left.field_names


def test_factual_state_comparison_fails_closed_on_owner_state_divergence() -> None:
    scenario = load_fault_recovery_matrix(MATRIX_PATH).scenario("operator_retry_after_timeout")
    continuous = {
        "order_count": 1,
        "fill_count": 0,
        "risk_decision_revision": 5,
        "operator_request_count": 1,
    }
    recovered = dict(continuous, operator_request_count=2)

    with pytest.raises(AssertionError, match="recovered factual state"):
        assert_recovered_state_matches(
            continuous,
            recovered,
            fields=scenario.factual_state_fields,
        )


def test_evidence_record_is_fixed_schema_digest_only_and_truthfully_classified() -> None:
    matrix = load_fault_recovery_matrix(MATRIX_PATH)
    register = load_residual_risk_register(RESIDUAL_PATH)
    scenario = matrix.scenario("control_plane_restart")
    residual = register.entry(scenario.scenario_id)
    state = {
        "acceptance_run_id": "acceptance-run-1",
        "acceptance_status": "SUCCEEDED",
        "acceptance_phase": "FINAL_VERIFICATION",
        "acceptance_active_test_time": 12.5,
        "acceptance_restart_count": 0,
        "acceptance_identity": {
            "full_rc_sha": "b" * 40,
            "dataset_id": "dataset-v1",
        },
    }
    fingerprint = factual_state_fingerprint(state, fields=scenario.factual_state_fields)

    evidence = sanitized_evidence_record(
        scenario,
        residual,
        candidate_sha=CANDIDATE_SHA,
        continuous=fingerprint,
        recovered=fingerprint,
    )

    assert frozenset(evidence) == frozenset(scenario.evidence_fields)
    assert evidence["verification_status"] == "composed_contract_fast"
    assert evidence["residual_risk_id"] is None
    assert evidence["observability_kind"] == "transport"
    rendered = repr(dict(evidence)).lower()
    for forbidden in (
        "/users/",
        "token=",
        "password",
        "hostname",
        "runner_name",
        "raw_market_data",
        "raw_payload",
    ):
        assert forbidden not in rendered
    assert evidence["continuous_state_sha256"] == evidence["recovered_state_sha256"]


def test_final_physical_soak_is_not_smuggled_into_deterministic_matrix() -> None:
    matrix = load_fault_recovery_matrix(MATRIX_PATH)
    register = load_residual_risk_register(RESIDUAL_PATH)

    assert {scenario.suite.value for scenario in matrix.scenarios} <= {"smoke", "extended"}
    assert any(item.physical_required for item in register.entries)
