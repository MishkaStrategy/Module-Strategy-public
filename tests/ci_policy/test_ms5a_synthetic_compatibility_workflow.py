from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ms5a-owner-search-preflight.yml"

EXPECTED_TESTS = (
    "tests/ci_policy/test_ms5a_synthetic_compatibility_workflow.py",
    "modules/risk_request_composer/tests/test_runtime_context.py",
    "research/strategy_factory/tests/test_multi_strategy_runtime_closure.py",
    "research/strategy_factory/tests/test_multi_strategy_owner.py",
    "research/strategy_factory/tests/test_multi_strategy_search.py",
    "research/strategy_factory/tests/test_multi_strategy_search_surface.py",
    "research/strategy_factory/tests/test_family_neutral_planning.py",
)


def test_ms5a_automatic_workflow_is_synthetic_only() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "name: MS5A synthetic compatibility\n" in text
    triggers = text.split("on:\n", 1)[1].split("\npermissions:", 1)[0]
    assert "  pull_request:\n" in triggers
    assert "  push:\n" in triggers
    for path in (
        "research/strategy_factory/**",
        "modules/market_data_ingestion/**",
        "modules/risk_request_composer/**",
        "modules/trade_plan_router/**",
        "tests/ci_policy/test_ms5a_synthetic_compatibility_workflow.py",
    ):
        assert triggers.count(f'      - "{path}"') == 2

    assert "  synthetic-compatibility:\n" in text
    assert "    name: MS5A synthetic compatibility / Python 3.13\n" in text
    assert "    runs-on: [self-hosted, fast]\n" in text
    assert '          role: fast\n          python-version: "3.13"\n' in text

    for test_path in EXPECTED_TESTS:
        assert test_path in text
    assert "cd modules/trade_plan_router\n" in text
    assert "python -m pytest -q tests/test_core_order_bridge.py" in text
    for forbidden in (
        "MARKET_DATA_ROOT",
        "ModuleStrategyData",
        "--market-data-root",
        "freeze_ms5a_request",
        "scripts/ms5a_owner_search_preflight.py",
        "assert_governed_host_maintenance_clear",
        "host-maintenance.json",
        "actions/upload-artifact",
        "reports/ms5a-owner-search-preflight",
        "$HOME",
    ):
        assert forbidden not in text
