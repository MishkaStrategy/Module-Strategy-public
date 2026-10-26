from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path
import sys
from types import ModuleType

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_policy() -> ModuleType:
    path = PROJECT_ROOT / "scripts" / "check_coverage_thresholds.py"
    spec = importlib.util.spec_from_file_location("check_coverage_thresholds", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_POLICY = _load_policy()
CoveragePolicyError = _POLICY.CoveragePolicyError
evaluate = _POLICY.evaluate


def _payload(
    *,
    covered_lines: int = 85,
    statements: int = 100,
    covered_branches: int = 75,
    branches: int = 100,
) -> dict[str, object]:
    return {
        "totals": {
            "covered_lines": covered_lines,
            "num_statements": statements,
            "covered_branches": covered_branches,
            "num_branches": branches,
        }
    }


def test_accepts_exact_statement_and_branch_thresholds() -> None:
    assert evaluate(
        _payload(),
        minimum_statement=Decimal(85),
        minimum_branch=Decimal(75),
    ) == (Decimal(85), Decimal(75))


def test_rejects_statement_regression() -> None:
    with pytest.raises(CoveragePolicyError, match="statement coverage"):
        evaluate(
            _payload(covered_lines=84),
            minimum_statement=Decimal(85),
            minimum_branch=Decimal(75),
        )


def test_rejects_branch_regression() -> None:
    with pytest.raises(CoveragePolicyError, match="branch coverage"):
        evaluate(
            _payload(covered_branches=74),
            minimum_statement=Decimal(85),
            minimum_branch=Decimal(75),
        )


def test_accepts_exact_combined_threshold_with_no_branch_obligations() -> None:
    assert evaluate(
        _payload(covered_lines=90, statements=100, covered_branches=0, branches=0),
        minimum_combined=Decimal(90),
    ) == (Decimal(90), Decimal(100))


def test_rejects_true_combined_coverage_below_rounded_report_threshold() -> None:
    # 89.73% can display as 90% in the zero-decimal coverage report.
    payload = _payload(
        covered_lines=8900,
        statements=9900,
        covered_branches=73,
        branches=100,
    )
    with pytest.raises(CoveragePolicyError, match="combined coverage 89.73%"):
        evaluate(payload, minimum_combined=Decimal(90))


def test_combined_threshold_uses_exact_integer_counts_beyond_decimal_precision() -> None:
    total = 10**30
    payload = _payload(
        covered_lines=(9 * total // 10) - 1,
        statements=total,
        covered_branches=0,
        branches=0,
    )
    with pytest.raises(CoveragePolicyError, match=r"below 90\.00%"):
        evaluate(payload, minimum_combined=Decimal(90))


@pytest.mark.parametrize(
    "payload",
    [
        _payload(covered_branches=1, branches=0),
        _payload(covered_branches=-1),
        _payload(branches=-1),
        _payload(covered_lines=True),
    ],
)
def test_combined_only_policy_still_validates_each_coverage_total(
    payload: dict[str, object],
) -> None:
    with pytest.raises(CoveragePolicyError):
        evaluate(payload, minimum_combined=Decimal(90))


def test_combined_only_cli_reports_exact_combined_percentage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    evidence = tmp_path / "coverage.json"
    evidence.write_text(
        '{"totals":{"covered_lines":90,"num_statements":100,"covered_branches":0,"num_branches":0}}'
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_coverage_thresholds.py", str(evidence), "--minimum-combined", "90"],
    )
    assert _POLICY.main() == 0
    assert "combined=90.00% (90/100)" in capsys.readouterr().out


def test_strategy_factory_workflow_routes_and_uses_exact_combined_gate() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "strategy-factory-ci.yml").read_text()
    assert workflow.count('"scripts/check_coverage_thresholds.py"') == 2
    assert workflow.count('"tests/ci_policy/test_coverage_policy.py"') == 2
    coverage_step = workflow.split("- name: Branch-aware coverage gate", 1)[1].split(
        "- name: Bandit security gate", 1
    )[0]
    assert "coverage report --show-missing" in coverage_step
    assert "coverage report --fail-under=90" not in coverage_step
    assert "python ../../scripts/check_coverage_thresholds.py" in coverage_step
    assert "--minimum-combined 90" in coverage_step


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"totals": []},
        _payload(covered_lines=-1),
        _payload(statements=0),
        _payload(covered_lines=101),
        _payload(covered_branches=-1),
        _payload(branches=0),
        _payload(covered_branches=101),
    ],
)
def test_rejects_malformed_coverage_evidence(payload: dict[str, object]) -> None:
    with pytest.raises(CoveragePolicyError):
        evaluate(
            payload,
            minimum_statement=Decimal(85),
            minimum_branch=Decimal(75),
        )
