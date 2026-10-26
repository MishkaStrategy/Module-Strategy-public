from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
COMPOSITIONS = REPOSITORY_ROOT / "research" / "strategy_audit_validation" / "compositions"
HANDOFF = REPOSITORY_ROOT / "reports" / "strategy-governance-wave2f" / "handoff-to-stream-a.json"
BENCHMARK_ROOT = REPOSITORY_ROOT / "research" / "experiment_runner" / "benchmarks"
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "canonical-strategy-v2-backtest.yml"
EXPECTED_HASH = (
    "51659c6ed582f2843357bd33fbff35059f9a535dd27f05a585a9e7dfa78365c9"  # pragma: allowlist secret
)
EXPECTED_CANDIDATE = "strategy-candidate-51659c6ed582f2843357bd33"
EXPECTED_DIGEST = (
    "63a2c102601cec5a613ff3749754d31955d6513c654c73cd3dd7e09bbc5cefdb"  # pragma: allowlist secret
)


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _wave2a_benchmark():
    sys.path.insert(0, str(BENCHMARK_ROOT))
    try:
        return importlib.import_module("wave2a_canonical_strategy")
    finally:
        sys.path.remove(str(BENCHMARK_ROOT))


def test_wave2a_consumes_complete_variant2_identity() -> None:
    source = REPOSITORY_ROOT / "research" / "strategy_audit_validation" / "src"
    sys.path.insert(0, str(source))
    try:
        governance = importlib.import_module("strategy_audit_validation.governance")
    finally:
        sys.path.remove(str(source))

    registry = _json(COMPOSITIONS / "module-contracts-v2.json")
    contracts = {}
    for item in registry["contracts"]:
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

    composition = _json(COMPOSITIONS / "canonical-fvg-rt-final-15m-frozen-1h-4h.v2.json")
    candidate = governance.load_composition(
        composition,
        contracts=contracts,
        require_canonical=True,
    )
    handoff = _json(HANDOFF)

    assert candidate.complete is True
    assert candidate.composition_hash == EXPECTED_HASH
    assert candidate.candidate_id == EXPECTED_CANDIDATE
    assert handoff["composition_hash"] == candidate.composition_hash
    assert handoff["candidate_id"] == candidate.candidate_id
    reference = handoff["expected_strategy_reference"]
    assert reference["semantic_digest"] == EXPECTED_DIGEST
    assert reference["semantic_digest_contract"] == "canonical-strategy-v2-result-v1"
    assert handoff["status"] == ("CANONICAL_STRATEGY_READY_FOR_RESEARCH_PLATFORM_CONSUMPTION")


def test_authoritative_subprocess_failure_diagnostic_is_sanitized() -> None:
    benchmark = _wave2a_benchmark()

    assert benchmark._subprocess_failure_message(
        "Traceback (most recent call last):\nRuntimeError: ledger[7].quantity: 1.0 != 2.0\n"
    ) == ("authoritative parity subprocess failed: RuntimeError: ledger[7].quantity: 1.0 != 2.0")
    assert (
        benchmark._subprocess_failure_message(
            "FileNotFoundError: missing /home/runner/private/evidence.json\n"
        )
        == "authoritative parity subprocess failed: FileNotFoundError"
    )
    assert (
        benchmark._subprocess_failure_message("RuntimeError: token=do-not-print-this\n")
        == "authoritative parity subprocess failed: RuntimeError"
    )
    assert benchmark._subprocess_failure_message("") == (
        "authoritative parity subprocess failed without sanitized diagnostic"
    )


def test_canonical_workflow_validates_authoritative_parity_evidence_inventory() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    required_fragments = (
        '"authoritative-parity",',
        '"authoritative-comparison-ledger.csv",',
        '"parity-report.json",',
        'authoritative_report != report.get("authoritative_historical_parity")',
        '"AUTHORITATIVE_CORE_BACKTEST_HISTORICAL_PARITY"',
        'authoritative_report.get("funding") != "disabled"',
        'authoritative_report.get("valuation_policy") != "last"',
        'authoritative_report.get("bars_processed") != 721597',
        'authoritative_report.get("trade_count") != 438',
        'authoritative_report.get("restart_verified") is not True',
        'authoritative_report.get("ledger_exact") is not True',
        'authoritative_report.get("metrics_exact") is not True',
        '"final_equity": 15266.31855203826,',
        '"max_closed_equity_drawdown": 0.1164174394297347,',
        '"net_return": 0.526631855203826,',
        '"profit_factor": 1.328366621740268,',
        '"win_rate": 0.3515981735159817,',
    )
    for fragment in required_fragments:
        assert fragment in workflow
