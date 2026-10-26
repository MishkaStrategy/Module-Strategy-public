from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from modular_research_runner.artifacts import AtomicJsonArtifactStore
from modular_research_runner.engine import BacktestJob, BacktestRunReference
from modular_research_runner.models import ExperimentStatus, SpecificationError
from modular_research_runner.ranking import CandidateMetrics, RankingPolicy
from modular_research_runner.reporting import ExperimentReporter
from modular_research_runner.repository import SqliteExperimentRepository
from modular_research_runner.results import LocalResultSink, RunResultEnvelope
from modular_research_runner.state import FailureCategory, FailureRecord

NOW = datetime(2026, 5, 1, tzinfo=UTC)


def test_ranking_filters_scores_and_tie_breaks_stably() -> None:
    policy = RankingPolicy.from_mappings(
        {
            "objectives": [
                {"metric": "profit_factor", "direction": "maximize", "weight": 2},
                {"metric": "drawdown", "direction": "minimize", "weight": "0.1"},
            ]
        },
        {"minimum_trades": 100, "maximums": {"drawdown": 20}, "top_k": 1},
    )
    ranked = policy.rank(
        [
            CandidateMetrics(
                "cfg-b",
                {"profit_factor": Decimal("1.5"), "drawdown": 10, "trades": 150},
                ("run-2",),
            ),
            CandidateMetrics(
                "cfg-a",
                {"profit_factor": Decimal("1.5"), "drawdown": 10, "trades": 150},
                ("run-1",),
            ),
            CandidateMetrics(
                "cfg-rejected",
                {"profit_factor": 9, "drawdown": 50, "trades": 200},
            ),
        ]
    )
    assert [item.configuration_sha256 for item in ranked] == ["cfg-a", "cfg-b"]
    assert ranked[0].promoted
    assert not ranked[1].promoted
    assert ranked[0].score == Decimal("2.0")


def test_ranking_rejects_ambiguous_inputs() -> None:
    with pytest.raises(SpecificationError, match="unsupported"):
        RankingPolicy.from_mappings({"metric": "x", "direction": "sideways"}, {})
    with pytest.raises(SpecificationError, match="unknown promotion"):
        RankingPolicy.from_mappings({"metric": "x"}, {"mystery": 1})
    policy = RankingPolicy.from_mappings({"metric": "x"}, {})
    with pytest.raises(SpecificationError, match="missing metrics"):
        policy.rank([CandidateMetrics("cfg", {"y": 1})])
    with pytest.raises(SpecificationError, match="unique"):
        policy.rank([CandidateMetrics("cfg", {"x": 1}), CandidateMetrics("cfg", {"x": 2})])
    with pytest.raises(SpecificationError, match="finite"):
        CandidateMetrics("cfg", {"x": float("nan")})


def test_result_envelope_sink_does_not_embed_or_recalculate_metrics(tmp_path: Path) -> None:
    engine = BacktestRunReference("run-1", "engine-1", tmp_path / "engine", 100, True)
    envelope = RunResultEnvelope(
        experiment_id="exp-1",
        run_id="run-1",
        configuration_sha256="a" * 64,
        stage_id="stage-1",
        stage_role="test",
        split_id="split-1",
        dataset_id="data-1",
        dataset_sha256="b" * 64,
        scenario_id="base",
        seed=1,
        engine=engine,
        metrics_schema_version="metrics-v1",
        metrics_artifact=None,
        metadata={"note": "reference-only"},
        created_at=NOW,
    )
    sink = LocalResultSink(tmp_path / "sink")
    reference = sink.publish(envelope)
    payload = sink.store.read_json(reference)
    assert payload["metrics_artifact"] is None
    assert "metrics" not in payload
    assert payload["engine"]["engine_run_id"] == "engine-1"
    with pytest.raises(SpecificationError, match="does not belong"):
        RunResultEnvelope(
            experiment_id="exp-1",
            run_id="other",
            configuration_sha256="a",
            stage_id="s",
            stage_role="test",
            split_id="sp",
            dataset_id="d",
            dataset_sha256="b",
            scenario_id="base",
            seed=1,
            engine=engine,
            metrics_schema_version="v1",
            metrics_artifact=None,
            metadata={},
            created_at=NOW,
        )


def test_reporter_summarizes_durable_states_and_exports(tmp_path: Path) -> None:
    repository = SqliteExperimentRepository(tmp_path / "state.sqlite3")
    repository.register_experiment(
        experiment_id="exp",
        specification_sha256="a" * 64,
        manifest={"v": 1},
        created_at=NOW,
    )
    jobs = [
        BacktestJob(f"run-{index}", "exp", "stage", "split", "data", "base", index, {}, tmp_path)
        for index in (1, 2)
    ]
    repository.register_jobs(jobs, max_attempts=1, created_at=NOW)
    repository.queue_planned("exp", at=NOW)
    first_claim = repository.claim_next(
        experiment_id="exp",
        worker_id="w1",
        session_id="session-w1",
        lease_token="token-w1",
        at=NOW,
        lease_seconds=10,
    )
    assert first_claim is not None
    first_lease = first_claim[2]
    repository.succeed(
        "run-1",
        worker_id=first_lease.worker_id,
        session_id=first_lease.session_id,
        lease_token=first_lease.lease_token,
        expected_revision=first_lease.revision,
        result=BacktestRunReference("run-1", "engine", tmp_path, 1, True),
        at=NOW,
    )
    second_claim = repository.claim_next(
        experiment_id="exp",
        worker_id="w2",
        session_id="session-w2",
        lease_token="token-w2",
        at=NOW,
        lease_seconds=10,
    )
    assert second_claim is not None
    second_lease = second_claim[2]
    repository.fail(
        "run-2",
        worker_id=second_lease.worker_id,
        session_id=second_lease.session_id,
        lease_token=second_lease.lease_token,
        expected_revision=second_lease.revision,
        failure=FailureRecord(FailureCategory.INPUT, "bad", NOW, False),
        retry_at=None,
        at=NOW,
    )
    repository.set_experiment_status("exp", ExperimentStatus.COMPLETED_WITH_FAILURES, at=NOW)
    reporter = ExperimentReporter(repository)
    report = reporter.build("exp", generated_at=NOW)
    assert report.run_counts == {"SUCCEEDED": 1, "FAILED": 1}
    assert report.failure_counts == {"INPUT": 1}
    store = AtomicJsonArtifactStore(tmp_path / "artifacts")
    persisted = store.read_json(reporter.write_json(report, store))
    assert persisted["status"] == "COMPLETED_WITH_FAILURES"
    csv_path = tmp_path / "report.csv"
    reporter.write_csv(report, csv_path)
    assert "run_status,FAILED,1" in csv_path.read_text(encoding="utf-8")


def test_ranking_model_validation_edges() -> None:
    from modular_research_runner.ranking import (
        MetricConstraint,
        MetricDirection,
        MetricObjective,
        RankingPolicy,
    )

    with pytest.raises(SpecificationError, match="numeric"):
        RankingPolicy.from_mappings({"metric": "x", "weight": "bad"}, {})
    with pytest.raises(SpecificationError, match="finite"):
        RankingPolicy.from_mappings({"metric": "x", "weight": "NaN"}, {})
    with pytest.raises(SpecificationError, match="non-empty"):
        MetricObjective("", MetricDirection.MAXIMIZE)
    with pytest.raises(SpecificationError, match="positive"):
        MetricObjective("x", MetricDirection.MAXIMIZE, Decimal(0))
    with pytest.raises(SpecificationError, match="non-empty"):
        MetricConstraint("", minimum=0)
    with pytest.raises(SpecificationError, match="requires"):
        MetricConstraint("x")
    with pytest.raises(SpecificationError, match="exceeds"):
        MetricConstraint("x", minimum=2, maximum=1)
    with pytest.raises(SpecificationError, match="configuration hash"):
        CandidateMetrics("", {"x": 1})
    with pytest.raises(SpecificationError, match="metric names"):
        CandidateMetrics("cfg", {"": 1})
    with pytest.raises(SpecificationError, match="at least one"):
        RankingPolicy(())
    objective = MetricObjective("x", MetricDirection.MAXIMIZE)
    with pytest.raises(SpecificationError, match="unique"):
        RankingPolicy((objective, objective))
    with pytest.raises(SpecificationError, match="top_k"):
        RankingPolicy((objective,), top_k=0)
    with pytest.raises(SpecificationError, match="sequence"):
        RankingPolicy.from_mappings({"objectives": "x"}, {})
    with pytest.raises(SpecificationError, match="must be a mapping"):
        RankingPolicy.from_mappings({"objectives": [1]}, {})
    with pytest.raises(SpecificationError, match="unknown ranking"):
        RankingPolicy.from_mappings({"metric": "x", "extra": 1}, {})
    with pytest.raises(SpecificationError, match="promotion top_k"):
        RankingPolicy.from_mappings({"metric": "x"}, {"top_k": True})
    with pytest.raises(SpecificationError, match="must be mappings"):
        RankingPolicy.from_mappings({"metric": "x"}, {"minimums": []})
    policy = RankingPolicy.from_mappings({"metric": "x"}, {"maximum_x": 5, "top_k": 2})
    assert policy.rank([CandidateMetrics("cfg", {"x": 6})]) == ()
