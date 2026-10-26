from __future__ import annotations

import json
from pathlib import Path

from modular_results_registry.evidence import (
    EvidenceRequirement,
    StrategyAuditPackageInput,
    build_strategy_audit_package,
    evaluate_evidence_completeness,
)
from modular_results_registry.reporting import (
    ResearchReportInput,
    render_csv,
    render_html_report,
    render_json_report,
    render_markdown_report,
    write_report_atomic,
)


def report_input(package) -> ResearchReportInput:
    completeness = evaluate_evidence_completeness(
        package.evidence,
        (EvidenceRequirement("artifact_integrity"),),
    )
    audit = build_strategy_audit_package(
        StrategyAuditPackageInput(
            registry_revision=1,
            registry_state_hash="a" * 64,
            strategy_id="strategy-001",
            baseline_ids=(package.configurations[0].configuration_id,),
            candidate_ids=(),
            experiment_ids=(package.experiments[0].experiment_id,),
            run_ids=(package.runs[0].run_id,),
            configuration_ids=(package.configurations[0].configuration_id,),
            dataset_ids=(package.datasets[0].dataset_id,),
            dataset_hashes=(package.datasets[0].sha256,),
            commit_shas=(package.experiments[0].commit_sha,),
            module_versions={"core": "0.4.0"},
            backtest_engine_versions=("0.4.0",),
            metric_definition_ids=(package.metric_definitions[0].definition_id,),
            metric_definition_hashes=(package.metric_definitions[0].content_hash,),
            metric_definition_versions=("core.metrics.v0.4.0",),
            split_information={"test": "untouched"},
            metrics=({"name": "profit_factor", "value": 1.42},),
            artifact_references=(package.artifacts[0].artifact_id,),
            comparison_tables=({"candidate": "none", "status": "baseline-only"},),
            slices={"SIDE": ({"key": "LONG", "trade_count": 10},)},
            pareto_result={"frontier": []},
            evidence=package.evidence,
            completeness=completeness,
            reproducibility_status="PASS",
            registry_integrity_status="PASS",
            known_limitations=("authoritative checkout unavailable",),
        )
    )
    return ResearchReportInput(
        title="Results Registry Research Report",
        executive_summary="Verified baseline-only evidence.",
        audit_package=audit,
        next_step_recommendation="Submit to Strategy Audit only after source integration.",
    )


def test_json_markdown_html_are_deterministic_and_hash_bound(package) -> None:
    data = report_input(package)
    for renderer in (render_json_report, render_markdown_report, render_html_report):
        left = renderer(data)
        right = renderer(data)
        assert left.content == right.content
        assert left.content_hash == right.content_hash
        assert left.report_id == right.report_id
    payload = json.loads(render_json_report(data).content)
    assert payload["audit_package"]["package_hash"] == data.audit_package.package_hash
    assert b"Pareto ranking" in render_markdown_report(data).content
    assert b"<!doctype html>" in render_html_report(data).content


def test_report_style_does_not_change_quantitative_package(package) -> None:
    data = report_input(package)
    before = data.audit_package.content_hash
    render_markdown_report(data)
    render_html_report(data)
    assert data.audit_package.content_hash == before


def test_csv_formula_injection_and_line_endings_are_safe() -> None:
    content = render_csv(
        (
            {"id": "1", "value": '=HYPERLINK("bad")'},
            {"id": "2", "value": " +SUM(A1:A2)"},
            {"id": "3", "value": "safe\r\ntext"},
        )
    ).decode("utf-8")
    assert "'=HYPERLINK" in content
    assert "' +SUM" in content
    assert "\r" not in content


def test_atomic_write_replaces_target(package, tmp_path: Path) -> None:
    report = render_json_report(report_input(package))
    target = tmp_path / "nested" / "report.json"
    target.parent.mkdir()
    target.write_text("old")
    assert write_report_atomic(report, target) == target
    assert target.read_bytes() == report.content
