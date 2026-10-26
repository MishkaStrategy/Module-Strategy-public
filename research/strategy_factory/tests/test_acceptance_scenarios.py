from __future__ import annotations

import csv
import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_coordinator import (
    test_blocked_stage_resumes_without_double_budget_or_batch as _assert_resume_without_duplicate_batch,
)
from test_coordinator import (
    test_budget_exhaustion_stops_before_external_work as _assert_budget_exhaustion,
)
from test_grammar import (
    test_missing_funding_module_is_not_eligible as _assert_missing_funding_ineligible,
)
from test_ranking_package import (
    audit_decision,
    candidate,
    research_periods,
)
from test_ranking_package import (
    test_package_hash_and_paper_only_gate as _assert_package_paper_only,
)
from test_ranking_package import (
    test_pareto_ranking_and_simplicity_tiebreak as _assert_pareto_simplicity,
)
from test_recovery import test_checkpoint_corruption_detected as _assert_checkpoint_corruption
from test_results_publication_edges import (
    test_publish_experiment_requires_terminal_success as _assert_terminal_publication,
)
from test_run import analyzer as run_analyzer
from test_run import make_run
from test_validation import _candidate, _policy

from strategy_factory.canonical import canonical_sha256
from strategy_factory.contracts import (
    AdmissionStatus,
    DatasetGateStatus,
    HoldoutReport,
    MarketType,
    RobustnessReport,
)
from strategy_factory.dataset import DatasetAnalyzer, DatasetValidationPolicy
from strategy_factory.errors import CompatibilityError, ContractError, IdempotencyConflictError
from strategy_factory.idempotency import IdempotencyLedger
from strategy_factory.package import StrategyPackageBuilder
from strategy_factory.validation import CandidateGate


def _dataset_analyzer(*, minimum_history_days: str = "0") -> DatasetAnalyzer:
    return DatasetAnalyzer(
        DatasetValidationPolicy(
            interval_seconds=300,
            minimum_rows=10,
            minimum_history_days=Decimal(minimum_history_days),
        )
    )


def _rewrite_rows(descriptor, tmp_path: Path, rows: list[dict[str, str]], name: str):
    path = tmp_path / name
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    return replace(
        descriptor,
        uri=str(path),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        row_count=len(rows),
    )


def test_acceptance_01_new_liquid_instrument_with_full_history(descriptor) -> None:
    result = _dataset_analyzer().analyze(descriptor)
    assert result.validation.status is DatasetGateStatus.ACCEPTED
    assert result.profile is not None
    assert result.profile.observations == descriptor.row_count


def test_acceptance_02_short_history_instrument_is_rejected(descriptor) -> None:
    result = _dataset_analyzer(minimum_history_days="30").analyze(descriptor)
    assert result.validation.status is DatasetGateStatus.REJECTED
    assert "INSUFFICIENT_HISTORY" in result.validation.reason_codes
    assert result.profile is None


def test_acceptance_03_dataset_with_gaps_is_rejected(descriptor, tmp_path: Path) -> None:
    source = Path(descriptor.uri)
    rows = list(csv.DictReader(source.open(encoding="utf-8")))
    rows.pop(5)
    changed = _rewrite_rows(descriptor, tmp_path, rows, "gap.csv")
    result = _dataset_analyzer().analyze(changed)
    assert result.validation.status is DatasetGateStatus.REJECTED
    assert "DATA_GAPS" in result.validation.reason_codes


def test_acceptance_04_dataset_with_duplicates_is_rejected(descriptor, tmp_path: Path) -> None:
    source = Path(descriptor.uri)
    rows = list(csv.DictReader(source.open(encoding="utf-8")))
    rows.append(dict(rows[-1]))
    changed = _rewrite_rows(descriptor, tmp_path, rows, "duplicate.csv")
    result = _dataset_analyzer().analyze(changed)
    assert result.validation.status is DatasetGateStatus.REJECTED
    assert "DUPLICATE_TIMESTAMPS" in result.validation.reason_codes


def test_acceptance_05_spot_data_cannot_satisfy_futures_request(
    factory_request,
) -> None:
    spot = replace(factory_request.dataset, market_type=MarketType.SPOT)
    with pytest.raises(ContractError, match="market_type"):
        replace(factory_request, dataset=spot)


def test_acceptance_06_missing_funding_history_excludes_dependent_modules(descriptor) -> None:
    _assert_missing_funding_ineligible(descriptor)


def test_acceptance_07_no_candidate_meets_minimum_criteria(architecture) -> None:
    value = _candidate(architecture, "candidate-none")
    assert value.evaluation is not None
    metrics = {
        **dict(value.evaluation.metrics),
        "trade_count": 5,
        "walk_forward_efficiency": "0.10",
        "cost_stress_profit_factor": "0.70",
        "year_stability": "0.10",
    }
    result = CandidateGate(_policy()).evaluate(
        replace(value, evaluation=replace(value.evaluation, metrics=metrics))
    )
    assert not result.eligible


def test_acceptance_08_several_close_candidates_use_explainable_tiebreak(architecture) -> None:
    _assert_pareto_simplicity(architecture)


def test_acceptance_09_highly_profitable_but_overfit_candidate_is_rejected(architecture) -> None:
    value = _candidate(architecture, "candidate-overfit")
    assert value.evaluation is not None
    metrics = {
        **dict(value.evaluation.metrics),
        "profit_factor": "3.50",
        "walk_forward_efficiency": "0.15",
        "year_stability": "0.15",
    }
    result = CandidateGate(_policy()).evaluate(
        replace(value, evaluation=replace(value.evaluation, metrics=metrics))
    )
    assert not result.eligible
    assert {"UNSTABLE_WALK_FORWARD", "UNSTABLE_YEARLY_RESULTS"} <= set(result.reason_codes)


def test_acceptance_10_stable_less_profitable_candidate_remains_eligible(architecture) -> None:
    value = _candidate(architecture, "candidate-stable")
    assert value.evaluation is not None
    metrics = {**dict(value.evaluation.metrics), "profit_factor": "1.15"}
    result = CandidateGate(_policy()).evaluate(
        replace(value, evaluation=replace(value.evaluation, metrics=metrics))
    )
    assert result.eligible


def test_acceptance_11_candidate_with_insufficient_trades_is_rejected(architecture) -> None:
    value = _candidate(architecture, "candidate-few-trades")
    assert value.evaluation is not None
    metrics = {**dict(value.evaluation.metrics), "trade_count": 12}
    result = CandidateGate(_policy()).evaluate(
        replace(value, evaluation=replace(value.evaluation, metrics=metrics))
    )
    assert not result.eligible
    assert "LOW_TRADE_COUNT" in result.reason_codes


def test_acceptance_12_commission_sensitive_strategy_is_rejected(architecture) -> None:
    value = _candidate(architecture, "candidate-cost-sensitive")
    assert value.evaluation is not None
    metrics = {**dict(value.evaluation.metrics), "cost_stress_profit_factor": "0.80"}
    result = CandidateGate(_policy()).evaluate(
        replace(value, evaluation=replace(value.evaluation, metrics=metrics))
    )
    assert not result.eligible
    assert "COST_SENSITIVITY" in result.reason_codes


def test_acceptance_13_one_year_only_strategy_is_rejected(architecture) -> None:
    value = _candidate(architecture, "candidate-one-year")
    assert value.evaluation is not None
    metrics = {
        **dict(value.evaluation.metrics),
        "time_coverage": "0.30",
        "year_stability": "0.20",
    }
    result = CandidateGate(_policy()).evaluate(
        replace(value, evaluation=replace(value.evaluation, metrics=metrics))
    )
    assert not result.eligible
    assert {"INSUFFICIENT_TIME_COVERAGE", "UNSTABLE_YEARLY_RESULTS"} <= set(result.reason_codes)


def test_acceptance_14_crash_after_batch_submit_resumes_without_duplicate(
    factory_request, tmp_path: Path
) -> None:
    _assert_resume_without_duplicate_batch(factory_request, tmp_path)


def test_acceptance_15_partial_research_runner_result_is_not_publishable(
    factory_request, tmp_path: Path
) -> None:
    snapshot = SimpleNamespace(has_nonterminal_runs=True, status="completed")
    _assert_terminal_publication(factory_request, tmp_path, snapshot)


def test_acceptance_16_duplicate_completion_event_is_idempotent(
    factory_request, tmp_path: Path
) -> None:
    run = make_run(factory_request, tmp_path)
    run.initialize()
    assert run.process_event("completion-event")
    assert not run.process_event("completion-event")


def test_acceptance_17_dataset_hash_change_during_run_is_rejected(
    factory_request, tmp_path: Path
) -> None:
    path = Path(factory_request.dataset.uri)
    original = path.read_bytes()
    path.write_bytes(original + b"\n")
    try:
        result = run_analyzer().analyze(factory_request.dataset)
    finally:
        path.write_bytes(original)
    assert result.validation.status is DatasetGateStatus.REJECTED
    assert "DATASET_HASH_MISMATCH" in result.validation.reason_codes


def test_acceptance_18_corrupted_checkpoint_is_detected(factory_request, tmp_path: Path) -> None:
    _assert_checkpoint_corruption(factory_request, tmp_path)


def test_acceptance_19_budget_exhaustion_prevents_external_work(
    factory_request, tmp_path: Path
) -> None:
    _assert_budget_exhaustion(factory_request, tmp_path)


def test_acceptance_20_cancel_then_resume_is_rejected_safely(
    factory_request, tmp_path: Path
) -> None:
    run = make_run(factory_request, tmp_path)
    run.initialize()
    run.cancel()
    with pytest.raises(CompatibilityError, match="paused or blocked"):
        run.resume()


def test_acceptance_21_negative_holdout_cannot_authorize_paper(
    factory_request, architecture
) -> None:
    value = candidate(
        architecture,
        "candidate-negative-holdout",
        {"net_return": "0.20", "max_drawdown": "0.08"},
        "2",
    )
    periods = research_periods(factory_request.dataset.period)
    now = datetime(2026, 1, 2, tzinfo=UTC)
    package = StrategyPackageBuilder().build(
        request=factory_request,
        candidate=value,
        strategy_id="negative-holdout",
        version="1.0.0",
        commit_sha="c" * 40,
        research_periods=periods,
        robustness=RobustnessReport("robustness-negative", value.candidate_id, True, {}, (), now),
        holdout=HoldoutReport(
            "holdout-negative",
            value.candidate_id,
            periods["holdout"],
            factory_request.dataset.fingerprint,
            {"profit_factor": "0.80"},
            False,
            True,
            now,
        ),
        key_results={"net_return": "0.20"},
        known_weaknesses=("negative untouched holdout",),
        disabled_regimes=(),
        evidence_links=("evidence-negative",),
        paper_configuration={"symbol": factory_request.symbol},
        risk_policy={"risk_fraction": "0.005"},
        execution_assumptions={"maker_fee": "0.0002"},
        audit_decision=audit_decision(value.candidate_id, issued_at=now.replace(day=3)),
    )
    assert package.admission_status is not AdmissionStatus.APPROVED_FOR_PAPER
    assert not package.live_trading_authorized


def test_acceptance_22_final_candidate_forms_verified_strategy_package(
    factory_request, architecture
) -> None:
    _assert_package_paper_only(factory_request, architecture)


def test_acceptance_23_live_promotion_without_permission_is_impossible(
    factory_request, architecture
) -> None:
    value = candidate(
        architecture,
        "candidate-paper-only",
        {"net_return": "0.20", "max_drawdown": "0.08"},
        "2",
    )
    periods = research_periods(factory_request.dataset.period)
    now = datetime(2026, 1, 2, tzinfo=UTC)
    package = StrategyPackageBuilder().build(
        request=factory_request,
        candidate=value,
        strategy_id="paper-only",
        version="1.0.0",
        commit_sha="d" * 40,
        research_periods=periods,
        robustness=RobustnessReport("robustness-paper", value.candidate_id, True, {}, (), now),
        holdout=HoldoutReport(
            "holdout-paper",
            value.candidate_id,
            periods["holdout"],
            factory_request.dataset.fingerprint,
            {"profit_factor": "1.20"},
            True,
            True,
            now,
        ),
        key_results={"net_return": "0.20"},
        known_weaknesses=(),
        disabled_regimes=(),
        evidence_links=("evidence-paper",),
        paper_configuration={"execution_mode": "live", "live_trading_authorized": True},
        risk_policy={"risk_fraction": "0.005"},
        execution_assumptions={"maker_fee": "0.0002"},
        audit_decision=audit_decision(value.candidate_id, issued_at=now.replace(day=3)),
    )
    assert package.paper_configuration["execution_mode"] == "paper"
    assert package.paper_configuration["live_trading_authorized"] is False
    assert package.live_trading_authorized is False


def test_acceptance_24_identical_request_id_is_exact_replay(tmp_path: Path) -> None:
    ledger = IdempotencyLedger(tmp_path / "idempotency.sqlite3")
    try:
        digest = canonical_sha256({"request": "same"})
        assert ledger.claim(scope="factory", operation_id="request-24", content_hash=digest)
        assert not ledger.claim(scope="factory", operation_id="request-24", content_hash=digest)
    finally:
        ledger.close()


def test_acceptance_25_same_id_with_different_content_is_rejected(tmp_path: Path) -> None:
    ledger = IdempotencyLedger(tmp_path / "idempotency.sqlite3")
    try:
        assert ledger.claim(
            scope="factory",
            operation_id="request-25",
            content_hash=canonical_sha256({"request": "first"}),
        )
        with pytest.raises(IdempotencyConflictError, match="different content"):
            ledger.claim(
                scope="factory",
                operation_id="request-25",
                content_hash=canonical_sha256({"request": "changed"}),
            )
    finally:
        ledger.close()
