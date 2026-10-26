from pathlib import Path


WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "market-data-external-corpus-registration-ci.yml"
)


def test_legacy_external_corpus_pr_trigger_is_owner_scoped() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    pull_request = text.split("  pull_request:\n", 1)[1].split("  workflow_dispatch:\n", 1)[0]

    assert (
        '      - ".github/workflows/market-data-external-corpus-registration-ci.yml"\n'
        in pull_request
    )
    assert '      - ".github/actions/python-runner-preflight/**"\n' in pull_request
    assert "modules/market_data_ingestion/" in pull_request
    assert "scripts/audit_runner_routing.py" not in pull_request
    assert "tests/ci_policy/test_runner_routing_policy.py" not in pull_request


def test_legacy_external_corpus_keeps_manual_and_owner_branch_guards() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "  workflow_dispatch:\n" in text
    assert "github.event_name == 'workflow_dispatch'" in text
    assert "github.event.pull_request.head.ref == 'integration/historical-data-lineage'" in text
    assert "runs-on: [self-hosted, backtester]" in text
