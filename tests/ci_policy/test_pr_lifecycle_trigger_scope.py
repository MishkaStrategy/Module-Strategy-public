from pathlib import Path


WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _pull_request_section(name: str) -> str:
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    return text.split("  pull_request:\n", 1)[1].split("    paths:\n", 1)[0]


def test_strategy_factory_only_reruns_for_tree_changing_pr_events() -> None:
    section = _pull_request_section("strategy-factory-ci.yml")

    assert "types: [opened, reopened, synchronize]" in section
    assert "ready_for_review" not in section
    assert "converted_to_draft" not in section


def test_decision_router_gates_only_rerun_for_tree_changing_pr_events() -> None:
    for name in (
        "decision-router-final-gate.yml",
        "decision-router-core-v042-compatibility.yml",
    ):
        section = _pull_request_section(name)
        assert "types: [opened, reopened, synchronize]" in section
        assert "ready_for_review" not in section
        assert "converted_to_draft" not in section
