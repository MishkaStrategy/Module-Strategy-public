from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    DATASET_HASH,
    MANIFEST_HASH,
    NOW,
    RESULT_HASH,
    SIGNING_KEY,
    build_auditor,
)

from strategy_audit_validation import (
    AuditOutcome,
    AuditPolicy,
    AuditUnavailableError,
    EvidenceIdentityConflictError,
    EvidenceMalformedError,
    EvidenceTamperedError,
    FactoryAdmissionStatus,
    HmacDecisionSigner,
    LocalStrategyAuditPort,
    SQLiteAuditStore,
    StaticDigestDatasetVerifier,
    StaticDigestResultsVerifier,
    UnavailableDatasetLineageVerifier,
    UnavailableResultsEvidenceVerifier,
    VerificationResult,
)
from strategy_audit_validation.canonical import (
    CanonicalizationError,
    canonical_json_bytes,
    canonical_sha256,
    strict_json_loads,
)


def _factory_normalize(value: Any) -> Any:
    """Minimal independent copy of Strategy Factory canonical semantics."""

    if isinstance(value, Enum):
        return _factory_normalize(value.value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return {"$decimal": format(value, "f")}
    if isinstance(value, dict):
        return {key: _factory_normalize(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_factory_normalize(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(type(value).__name__)


def _factory_sha256(value: Any) -> str:
    encoded = json.dumps(
        _factory_normalize(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _valid_v2_evidence(valid_evidence: dict[str, Any]) -> dict[str, Any]:
    evidence = deepcopy(valid_evidence)
    evidence["research"]["random_seeds"] = [7]
    evidence["schema_version"] = 2
    holdout_period = evidence["research"]["periods"]["holdout"]
    request_context = {
        "request_id": "request-v2",
        "mode": "create",
        "symbol": "BTCUSDT",
        "exchange": "binance",
        "market_type": "linear_futures",
        "dataset": {
            "dataset_id": "binance-btcusdt-usdtm-v2",
            "source": "synthetic",
            "symbol": "BTCUSDT",
            "exchange": "binance",
            "market_type": "linear_futures",
            "base_timeframe": "5m",
            "available_timeframes": ["5m"],
            "period": {
                "start": "2020-01-01T00:00:00Z",
                "end": "2025-01-01T00:00:00Z",
            },
            "timezone": "UTC",
            "file_format": "csv",
            "uri": "synthetic://fixture/btcusdt.csv?version=2",
            "sha256": DATASET_HASH,
            "row_count": 100,
            "received_at": "2025-01-02T00:00:00Z",
            "schema_version": 1,
            "funding_uri": None,
            "funding_sha256": None,
            "exchange_info_sha256": None,
        },
        "allowed_timeframes": ["5m"],
        "initial_capital": {"$decimal": "10000"},
        "research_risk_fraction": {"$decimal": "0.02"},
        "maximum_drawdown_fraction": {"$decimal": "0.30"},
        "minimum_trade_count": 30,
        "objectives": {},
        "budget": {
            "maximum_experiments": 100,
            "maximum_architectures": 20,
            "maximum_finalists": 3,
            "maximum_duration_seconds": 3600,
            "maximum_memory_bytes": 1000000000,
            "maximum_cpu_workers": 4,
            "maximum_parallel_tasks": 2,
            "stage_experiment_limits": {
                "coarse_search": 40,
                "local_refinement": 30,
                "structural_screening": 30,
            },
            "schema_version": 1,
        },
        "random_seed": 7,
        "allowed_strategy_families": ["fvg"],
        "forbidden_module_ids": [],
        "baseline_strategy_id": None,
        "source_strategy_id": None,
        "adaptation_policy": {
            "allow_parameter_adaptation": True,
            "allowed_parameter_names": [],
            "neighborhood_points": 3,
            "require_source_baseline_control": True,
            "allow_direction_reduction": False,
            "schema_version": 1,
        },
        "improvement_policy": {
            "allowed_mutations": ["parameters"],
            "allowed_parameter_names": [],
            "neighborhood_points": 3,
            "maximum_runtime_module_changes": 0,
            "maximum_architectures": 8,
            "maximum_complexity_increase": {"$decimal": "0"},
            "require_baseline_control": True,
            "schema_version": 1,
        },
        "explicit_holdout": None,
        "created_at": evidence["created_at"],
        "schema_version": 1,
    }
    robustness_report = {
        "report_id": "robustness-v2",
        "candidate_id": "candidate-btc-001",
        "passed": True,
    }
    projection = {
        "schema_version": 1,
        "request_context": deepcopy(request_context),
        "strategy_id": f"strategy-btcusdt-{evidence['candidate']['configuration_hash'][:16]}",
        "version": "0.1.0",
        "symbol": "BTCUSDT",
        "exchange": "binance",
        "market_type": "linear_futures",
        "directions": ["long"],
        "architecture": {},
        "parameters": {},
        "risk_policy": {
            "research_risk_fraction": {"$decimal": "0.02"},
            "initial_capital": {"$decimal": "10000"},
        },
        "constraints": {
            "candidate_id": "candidate-btc-001",
            "candidate_configuration_hash": evidence["candidate"]["configuration_hash"],
            "factory_request_fingerprint": canonical_sha256(request_context),
            "maximum_drawdown_fraction": {"$decimal": "0.30"},
            "minimum_trade_count": 30,
        },
        "timeframes": ["5m"],
        "execution_assumptions": {"exchange": "binance", "market_type": "linear_futures"},
        "dataset_hashes": [DATASET_HASH],
        "research_periods": deepcopy(evidence["research"]["periods"]),
        "commit_sha": "a" * 40,
        "random_seeds": [7],
        "key_results": {"candidate_metrics": {"net_return": "0.2"}},
        "robustness_report": robustness_report,
        "holdout_report": {
            "report_id": "holdout-v2",
            "candidate_id": "candidate-btc-001",
            "period": deepcopy(holdout_period),
            "opened_once": True,
            "metrics": {"net_return": "0.2"},
        },
        "known_weaknesses": [],
        "disabled_regimes": [],
        "evidence_links": ["synthetic-result-reference"],
        "paper_configuration": {"execution_mode": "paper"},
    }
    evidence["package_intent"] = {
        "schema_version": 1,
        "request_fingerprint": canonical_sha256(request_context),
        "request_context": request_context,
        "package_projection": projection,
    }
    evidence["robustness"]["report_sha256"] = canonical_sha256(robustness_report)
    _reseal_v2_intent(evidence)
    return evidence


def _reseal_v2_intent(evidence: dict[str, Any]) -> None:
    intent = evidence["package_intent"]
    projection = intent["package_projection"]
    projection["request_context"] = deepcopy(intent["request_context"])
    projection["constraints"]["factory_request_fingerprint"] = canonical_sha256(
        intent["request_context"]
    )
    intent["request_fingerprint"] = canonical_sha256(intent["request_context"])
    _hash_v2_intent(evidence)


def _hash_v2_intent(evidence: dict[str, Any]) -> None:
    intent = evidence["package_intent"]
    body = {
        "schema_version": intent["schema_version"],
        "request_fingerprint": intent["request_fingerprint"],
        "request_context": intent["request_context"],
        "package_projection": intent["package_projection"],
    }
    intent.clear()
    intent.update(body, intent_hash=canonical_sha256(body))


def _set_path(value: dict[str, Any], path: tuple[str, ...], replacement: Any) -> None:
    parent = value
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = replacement


def test_valid_evidence_is_approved_and_factory_hash_compatible(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    auditor, store = build_auditor(tmp_path / "audit.sqlite")
    decision = auditor.audit(
        evidence_package_reference="strategy-factory-artifact:full-audit-001",
        evidence=valid_evidence,
    )

    assert decision.outcome is AuditOutcome.APPROVED_FOR_PAPER
    assert decision.factory_status is FactoryAdmissionStatus.APPROVED_FOR_PAPER
    assert decision.live_trading_authorized is False
    assert "ALL_INDEPENDENT_GATES_PASSED" in decision.reason_codes
    assert decision.decision_hash == _factory_sha256(decision.hash_payload())
    assert decision.as_factory_mapping()["status"] == "APPROVED_FOR_PAPER"
    store.verify_integrity()
    store.close()


def test_v2_signed_package_intent_withholds_paper_without_metric_source_proofs(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    evidence = _valid_v2_evidence(valid_evidence)

    auditor, store = build_auditor(
        tmp_path / "audit-v2.sqlite",
        contract_version="strategy-audit-port-v2",
    )
    decision = auditor.audit(
        evidence_package_reference="synthetic:v2-package-intent",
        evidence=evidence,
    )
    assert decision.contract_version == "strategy-audit-port-v2"
    assert decision.outcome is AuditOutcome.RESEARCH_ONLY
    assert "PACKAGE_INTENT_CONTENT_BOUND" in decision.reason_codes
    assert "RESULT_METRIC_SOURCE_EVIDENCE_UNAVAILABLE" in decision.reason_codes
    assert "HOLDOUT_METRIC_SOURCE_EVIDENCE_UNAVAILABLE" in decision.reason_codes
    assert auditor.verify_stored_decision(expected=decision) == decision

    unsupported = deepcopy(evidence)
    unsupported_intent = unsupported["package_intent"]
    unsupported_context = unsupported_intent["request_context"]
    unsupported_context["objectives"] = {"fee_model": "maker"}
    unsupported_fingerprint = canonical_sha256(unsupported_context)
    unsupported_projection = unsupported_intent["package_projection"]
    unsupported_projection["request_context"] = unsupported_context
    unsupported_projection["constraints"]["factory_request_fingerprint"] = unsupported_fingerprint
    unsupported_body = {
        "schema_version": unsupported_intent["schema_version"],
        "request_fingerprint": unsupported_fingerprint,
        "request_context": unsupported_context,
        "package_projection": unsupported_projection,
    }
    unsupported["package_intent"] = {
        **unsupported_body,
        "intent_hash": canonical_sha256(unsupported_body),
    }
    rejected = auditor.audit(
        evidence_package_reference="synthetic:v2-unsupported-objective",
        evidence=unsupported,
    )
    assert rejected.outcome is AuditOutcome.REJECTED
    store.close()


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("package_intent", "package_projection", "schema_version"), True),
        (("package_intent", "schema_version"), 1.0),
        (("package_intent", "request_context", "unexpected"), "value"),
        (("package_intent", "request_context", "schema_version"), True),
        (("package_intent", "request_context", "schema_version"), 2),
        (("package_intent", "request_context", "mode"), "mystery"),
        (("package_intent", "request_context", "market_type"), "perpetual"),
        (("package_intent", "request_context", "allowed_timeframes"), ["1d"]),
        (("package_intent", "request_context", "random_seed"), True),
        (("package_intent", "request_context", "minimum_trade_count"), 30.0),
        (("package_intent", "request_context", "minimum_trade_count"), 0),
        (("package_intent", "request_context", "initial_capital"), 10000),
        (("package_intent", "request_context", "initial_capital"), {"$decimal": "1.00"}),
        (("package_intent", "request_context", "initial_capital"), {"$decimal": 1}),
        (("package_intent", "request_context", "allowed_strategy_families"), []),
        (("package_intent", "request_context", "allowed_strategy_families"), ["z", "a"]),
        (("package_intent", "request_context", "baseline_strategy_id"), 7),
        (("package_intent", "request_context", "created_at"), "2026-07-29T13:30:00+00:00"),
        (("package_intent", "request_context", "created_at"), "2026-07-29T13:26:00Z"),
        (("package_intent", "request_context", "budget"), {}),
        (("package_intent", "request_context", "budget", "schema_version"), 2),
        (
            ("package_intent", "request_context", "budget", "maximum_memory_bytes"),
            True,
        ),
        (
            ("package_intent", "request_context", "budget", "stage_experiment_limits"),
            {"unknown_stage": 1},
        ),
        (("package_intent", "request_context", "adaptation_policy"), {}),
        (("package_intent", "request_context", "adaptation_policy", "schema_version"), 2),
        (
            ("package_intent", "request_context", "adaptation_policy", "allowed_parameter_names"),
            ["z.module", "a.module"],
        ),
        (
            (
                "package_intent",
                "request_context",
                "adaptation_policy",
                "allow_parameter_adaptation",
            ),
            1,
        ),
        (("package_intent", "request_context", "improvement_policy"), {}),
        (("package_intent", "request_context", "improvement_policy", "schema_version"), 2),
        (
            ("package_intent", "request_context", "improvement_policy", "allowed_parameter_names"),
            ["z.module", "a.module"],
        ),
        (
            (
                "package_intent",
                "request_context",
                "improvement_policy",
                "maximum_complexity_increase",
            ),
            {"$decimal": "-1"},
        ),
        (
            ("package_intent", "request_context", "improvement_policy", "allowed_mutations"),
            ["entry"],
        ),
        (
            ("package_intent", "request_context", "improvement_policy", "allowed_mutations"),
            ["unknown"],
        ),
        (
            ("package_intent", "request_context", "improvement_policy", "neighborhood_points"),
            22,
        ),
        (
            ("package_intent", "request_context", "improvement_policy", "allowed_mutations"),
            ["directions", "parameters"],
        ),
        (
            (
                "package_intent",
                "request_context",
                "improvement_policy",
                "maximum_complexity_increase",
            ),
            "0",
        ),
        (
            ("package_intent", "request_context", "dataset", "row_count"),
            True,
        ),
        (("package_intent", "request_context", "dataset", "base_timeframe"), "1m"),
        (("package_intent", "request_context", "dataset", "available_timeframes"), ["1m"]),
        (("package_intent", "request_context", "dataset", "timezone"), "America/New_York"),
        (("package_intent", "request_context", "dataset", "file_format"), "json"),
        (("package_intent", "request_context", "dataset", "uri"), "  "),
        (
            ("package_intent", "request_context", "dataset", "uri"),
            "https://user:credential@example.test/data.csv",
        ),
        (
            ("package_intent", "request_context", "dataset", "uri"),
            "https://example.test/data.csv?%61pi%5Fkey=credential",
        ),
        (("package_intent", "request_context", "dataset", "schema_version"), 2),
        (("package_intent", "request_context", "dataset", "received_at"), None),
        (
            ("package_intent", "request_context", "dataset", "market_type"),
            "spot",
        ),
        (
            ("package_intent", "request_context", "dataset", "available_timeframes"),
            ["5m", "5m"],
        ),
        (
            ("package_intent", "request_context", "dataset", "funding_uri"),
            "synthetic://funding/data.json",
        ),
        (("package_intent", "request_context", "dataset", "funding_uri"), " "),
        (("package_intent", "request_context", "dataset", "funding_sha256"), "bad"),
        (("package_intent", "request_context", "dataset", "exchange_info_sha256"), "bad"),
        (
            ("package_intent", "request_context", "dataset", "period", "end"),
            "2019-01-01T00:00:00Z",
        ),
        (
            ("package_intent", "request_context", "objectives", "api-key"),
            "synthetic-secret",
        ),
        (
            ("package_intent", "request_context", "mode"),
            "validate",
        ),
        (("package_intent", "request_context", "mode"), "adapt"),
        (("package_intent", "request_context", "mode"), "improve"),
        (("package_intent", "request_context", "allowed_strategy_families"), [1]),
        (("package_intent", "request_context", "request_id"), " "),
        (("package_intent", "request_context", "symbol"), "ETHUSDT"),
        (("package_intent", "package_projection", "version"), "9.9"),
        (("package_intent", "package_projection", "timeframes"), ["1d"]),
        (("package_intent", "package_projection", "constraints", "minimum_trade_count"), 31),
        (("package_intent", "request_context", "forbidden_module_ids"), ["z", "a"]),
        (("package_intent", "request_context", "allowed_timeframes"), []),
        (("package_intent", "request_context", "initial_capital"), {"$decimal": "0"}),
        (("package_intent", "request_context", "research_risk_fraction"), {"$decimal": "2"}),
        (
            ("package_intent", "request_context", "budget", "stage_experiment_limits"),
            {"coarse_search": 101},
        ),
        (
            ("package_intent", "request_context", "explicit_holdout"),
            {"start": "2019-01-01T00:00:00Z", "end": "2019-02-01T00:00:00Z"},
        ),
        (
            ("package_intent", "request_context", "explicit_holdout"),
            {"start": "2024-12-31T00:00:00Z", "end": "2025-01-02T00:00:00Z"},
        ),
        (
            ("package_intent", "request_context", "objectives"),
            {"walk_forward": {"unknown_window": 20}},
        ),
        (
            ("package_intent", "request_context", "objectives"),
            {"risk_policy": {"unknown_limit": 0.1}},
        ),
        (
            ("package_intent", "request_context", "objectives"),
            {"fee_model": "maker"},
        ),
        (
            ("package_intent", "request_context", "objectives"),
            {"access_token": "synthetic-secret"},
        ),
        (("package_intent", "request_context", "objectives"), {"strategy_version": " "}),
        (("package_intent", "request_context", "objectives"), {"known_weaknesses": [""]}),
        (("package_intent", "request_context", "objectives"), {"disabled_regimes": "illiquid"}),
        (
            ("package_intent", "request_context", "objectives"),
            {"walk_forward": {"mode": "random"}},
        ),
        (
            ("package_intent", "request_context", "objectives"),
            {"walk_forward": {"train_seconds": True}},
        ),
        (
            ("package_intent", "request_context", "objectives"),
            {"cost_scenarios": [{"scenario_id": "fee-stress-v1"}]},
        ),
        (
            ("package_intent", "request_context", "objectives"),
            {
                "cost_scenarios": [
                    {
                        "scenario_id": "cost-double-v1",
                        "roles": ["train"],
                        "execution_cost_multiplier": 2,
                    }
                ]
            },
        ),
        (
            ("package_intent", "request_context", "objectives"),
            {
                "cost_scenarios": [
                    {
                        "scenario_id": "cost-double-v1",
                        "roles": ["oos"],
                        "execution_cost_multiplier": 3,
                    }
                ]
            },
        ),
        (
            ("package_intent", "request_context", "objectives"),
            {"robustness_scenarios": [{"name": "delay"}]},
        ),
        (
            ("package_intent", "request_context", "objectives"),
            {"risk_policy": {" ": "value"}},
        ),
        (
            ("package_intent", "request_context", "objectives"),
            {"risk_policy": {"research_risk_fraction": {"$decimal": ""}}},
        ),
        (
            ("package_intent", "request_context", "objectives"),
            {"risk_policy": {"research_risk_fraction": [0.1]}},
        ),
        (
            ("package_intent", "request_context", "objectives"),
            {
                "cost_scenarios": [
                    {"scenario_id": "unknown", "roles": ["oos"], "execution_cost_multiplier": 2}
                ]
            },
        ),
        (("package_intent", "request_context", "objectives", "risk_policy"), []),
        (
            ("package_intent", "package_projection", "dataset_hashes"),
            ["f" * 64],
        ),
        (
            ("package_intent", "package_projection", "constraints", "candidate_id"),
            "candidate-other",
        ),
        (
            ("package_intent", "package_projection", "symbol"),
            "ETHUSDT",
        ),
        (
            ("package_intent", "package_projection", "risk_policy", "research_risk_fraction"),
            {"$decimal": "0.03"},
        ),
        (
            ("package_intent", "package_projection", "risk_policy", "unexpected"),
            "extra",
        ),
        (
            ("package_intent", "package_projection", "risk_policy", "research_risk_fraction"),
            {"$decimal": "not-a-number"},
        ),
        (
            ("package_intent", "package_projection", "risk_policy", "initial_capital"),
            {"$decimal": "not-a-number"},
        ),
        (
            ("package_intent", "package_projection", "execution_assumptions", "unexpected"),
            "extra",
        ),
        (
            ("package_intent", "package_projection", "execution_assumptions", "market_type"),
            "spot",
        ),
        (("execution_assumptions", "market_type"), "spot"),
        (
            ("package_intent", "package_projection", "robustness_report", "passed"),
            False,
        ),
        (
            ("package_intent", "package_projection", "holdout_report", "candidate_id"),
            "candidate-other",
        ),
        (
            ("package_intent", "package_projection", "holdout_report", "opened_once"),
            False,
        ),
        (
            ("package_intent", "package_projection", "random_seeds"),
            [7, 19, 42],
        ),
        (
            ("package_intent", "package_projection", "research_periods", "test", "end"),
            "2026-01-01T00:00:00Z",
        ),
        (
            ("package_intent", "package_projection", "constraints", "maximum_drawdown_fraction"),
            {"$decimal": "0.31"},
        ),
        (
            ("package_intent", "package_projection", "constraints", "maximum_drawdown_fraction"),
            {"$decimal": "bad"},
        ),
        (
            ("package_intent", "request_context", "maximum_drawdown_fraction"),
            {"$decimal": "bad"},
        ),
        (("risk_policy", "maximum_risk_fraction"), "not-a-number"),
        (("research", "random_seeds"), [7, 19, 42]),
        (
            ("risk_policy", "maximum_risk_fraction"),
            "0.03",
        ),
    ],
)
def test_v2_signed_intent_refuses_resealed_semantic_mismatches(
    tmp_path: Path,
    valid_evidence: dict[str, Any],
    path: tuple[str, ...],
    replacement: Any,
) -> None:
    evidence = _valid_v2_evidence(valid_evidence)
    _set_path(evidence, path, replacement)
    _reseal_v2_intent(evidence)
    auditor, store = build_auditor(
        tmp_path / f"audit-v2-negative-{path[-1]}.sqlite",
        contract_version="strategy-audit-port-v2",
    )
    decision = auditor.audit(
        evidence_package_reference=f"synthetic:v2-negative:{path[-1]}",
        evidence=evidence,
    )
    assert decision.outcome is AuditOutcome.REJECTED
    assert "EVIDENCE_MALFORMED" in decision.reason_codes
    assert "PAPER_ADMISSION_REJECTED" in decision.reason_codes
    assert auditor.verify_stored_decision(expected=decision) == decision
    store.close()


@pytest.mark.parametrize(
    "funding_uri",
    [
        "https://user:credential@example.test/funding.csv",
        "https://example.test/funding.csv?PASSWORD=credential",
        "https://example.test/funding.csv?%61pi%5Fkey=credential",
    ],
)
def test_v2_auditor_rejects_credential_bearing_funding_uris(
    tmp_path: Path, valid_evidence: dict[str, Any], funding_uri: str
) -> None:
    evidence = _valid_v2_evidence(valid_evidence)
    dataset = evidence["package_intent"]["request_context"]["dataset"]
    dataset["funding_uri"] = funding_uri
    dataset["funding_sha256"] = "f" * 64
    _reseal_v2_intent(evidence)
    auditor, store = build_auditor(
        tmp_path / "audit-v2-funding-uri.sqlite",
        contract_version="strategy-audit-port-v2",
    )
    decision = auditor.audit(
        evidence_package_reference="synthetic:v2-funding-uri-credential",
        evidence=evidence,
    )
    assert decision.outcome is AuditOutcome.REJECTED
    assert "EVIDENCE_MALFORMED" in decision.reason_codes
    assert auditor.verify_stored_decision(expected=decision) == decision
    store.close()


def test_v2_auditor_accepts_paired_synthetic_funding_reference(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    evidence = _valid_v2_evidence(valid_evidence)
    dataset = evidence["package_intent"]["request_context"]["dataset"]
    dataset["funding_uri"] = "synthetic://fixture/funding.csv?version=2"
    dataset["funding_sha256"] = "f" * 64
    evidence["package_intent"]["package_projection"]["dataset_hashes"].append("f" * 64)
    _reseal_v2_intent(evidence)
    auditor, store = build_auditor(
        tmp_path / "audit-v2-funding-uri-valid.sqlite",
        contract_version="strategy-audit-port-v2",
    )
    decision = auditor.audit(
        evidence_package_reference="synthetic:v2-funding-uri-valid",
        evidence=evidence,
    )
    assert decision.outcome is AuditOutcome.RESEARCH_ONLY
    assert "PACKAGE_INTENT_CONTENT_BOUND" in decision.reason_codes
    assert "RESULT_METRIC_SOURCE_EVIDENCE_UNAVAILABLE" in decision.reason_codes
    assert "HOLDOUT_METRIC_SOURCE_EVIDENCE_UNAVAILABLE" in decision.reason_codes
    store.close()


def test_v2_auditor_accepts_only_the_supported_typed_objective_subset(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    supported_objectives = [
        {
            "net_return": "maximize",
            "strategy_version": "2.1",
            "known_weaknesses": ["limited crisis coverage"],
        },
        {"drawdown": "minimize", "disabled_regimes": ["illiquid"]},
        {"walk_forward": None},
        {
            "walk_forward": {
                "mode": "rolling",
                "train_seconds": 10,
                "validation_seconds": 5,
                "oos_seconds": 5,
                "step_seconds": 5,
                "purge_seconds": 0,
                "embargo_seconds": 0,
                "minimum_history_seconds": 0,
            }
        },
        {
            "risk_policy": {
                "research_risk_fraction": {"$decimal": "0.02"},
                "initial_capital": {"$decimal": "10000"},
            }
        },
        {"execution_assumptions": {"exchange": "binance", "market_type": "linear_futures"}},
        {
            "cost_scenarios": [
                {
                    "scenario_id": "cost-double-v1",
                    "roles": ["oos"],
                    "execution_cost_multiplier": 2,
                }
            ],
            "robustness_scenarios": [],
        },
    ]
    auditor, store = build_auditor(
        tmp_path / "audit-v2-supported-objectives.sqlite",
        contract_version="strategy-audit-port-v2",
    )
    for index, objectives in enumerate(supported_objectives):
        evidence = _valid_v2_evidence(valid_evidence)
        evidence["package_intent"]["request_context"]["objectives"] = objectives
        projection = evidence["package_intent"]["package_projection"]
        if "strategy_version" in objectives:
            projection["version"] = objectives["strategy_version"]
        if "known_weaknesses" in objectives:
            projection["known_weaknesses"] = objectives["known_weaknesses"]
        if "disabled_regimes" in objectives:
            projection["disabled_regimes"] = objectives["disabled_regimes"]
        if index == 0:
            # Exact decimal strings compare numerically with canonical request tags.
            evidence["package_intent"]["package_projection"]["risk_policy"][
                "research_risk_fraction"
            ] = "0.02"
        _reseal_v2_intent(evidence)
        decision = auditor.audit(
            evidence_package_reference=f"synthetic:v2-supported-objective:{index}",
            evidence=evidence,
        )
        assert decision.outcome is AuditOutcome.RESEARCH_ONLY
        assert "PACKAGE_INTENT_CONTENT_BOUND" in decision.reason_codes
        assert "RESULT_METRIC_SOURCE_EVIDENCE_UNAVAILABLE" in decision.reason_codes
        assert auditor.verify_stored_decision(expected=decision) == decision
    store.close()


def test_v2_intent_fingerprint_and_projection_context_are_independently_bound(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    auditor, store = build_auditor(
        tmp_path / "audit-v2-intent-digest.sqlite",
        contract_version="strategy-audit-port-v2",
    )
    invalid_cases = []

    bad_fingerprint = _valid_v2_evidence(valid_evidence)
    bad_fingerprint["package_intent"]["request_fingerprint"] = "0" * 64
    _hash_v2_intent(bad_fingerprint)
    invalid_cases.append(bad_fingerprint)

    bad_hash = _valid_v2_evidence(valid_evidence)
    bad_hash["package_intent"]["intent_hash"] = "0" * 64
    invalid_cases.append(bad_hash)

    mismatched_projection_context = _valid_v2_evidence(valid_evidence)
    mismatched_projection = mismatched_projection_context["package_intent"]["package_projection"]
    mismatched_projection["request_context"] = deepcopy(
        mismatched_projection_context["package_intent"]["request_context"]
    )
    mismatched_projection["request_context"]["request_id"] = "other-request"
    _hash_v2_intent(mismatched_projection_context)
    invalid_cases.append(mismatched_projection_context)

    for index, evidence in enumerate(invalid_cases):
        decision = auditor.audit(
            evidence_package_reference=f"synthetic:v2-intent-digest:{index}",
            evidence=evidence,
        )
        assert decision.outcome is AuditOutcome.REJECTED
        assert "EVIDENCE_MALFORMED" in decision.reason_codes
        assert auditor.verify_stored_decision(expected=decision) == decision
    store.close()


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("created_at",), "not-an-iso-date"),
        (("created_at",), "2026-01-01T00:00:00"),
        (("created_at",), 123),
        (("dataset_lineage", "registry_version"), 3),
        (("dataset_lineage", "manifest_sha256"), "not-a-hash"),
        (("dataset_lineage", "funding_lineage_sha256"), "not-a-hash"),
        (("dataset_lineage", "timeframes"), "5m"),
        (("dataset_lineage", "timeframes"), []),
        (("dataset_lineage", "immutable"), False),
        (("research", "random_seeds"), []),
        (("research", "periods", "train", "end"), "2019-01-01T00:00:00Z"),
        (("research", "periods", "train"), {"start": "2020-01-01T00:00:00Z"}),
        (("research", "results_evidence", "registry_package_hashes"), []),
        (("risk_policy", "maximum_risk_fraction"), True),
        (("risk_policy", "maximum_drawdown_fraction"), "NaN"),
        (("execution_assumptions", "mode"), "live"),
        (("execution_assumptions", "order_placement"), True),
    ],
)
def test_legacy_malformed_evidence_is_durably_rejected(
    tmp_path: Path,
    valid_evidence: dict[str, Any],
    path: tuple[str, ...],
    replacement: Any,
) -> None:
    evidence = deepcopy(valid_evidence)
    _set_path(evidence, path, replacement)
    auditor, store = build_auditor(tmp_path / "legacy-malformed.sqlite")
    decision = auditor.audit(
        evidence_package_reference=f"synthetic:legacy-malformed:{path[-1]}",
        evidence=evidence,
    )
    assert decision.outcome is AuditOutcome.REJECTED
    assert "EVIDENCE_MALFORMED" in decision.reason_codes
    assert auditor.verify_stored_decision(expected=decision) == decision
    store.close()


def test_invalid_candidate_identity_fails_closed_without_fabricated_decision(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    evidence = deepcopy(valid_evidence)
    evidence["candidate"]["candidate_id"] = " contains spaces "
    auditor, store = build_auditor(tmp_path / "invalid-candidate-identity.sqlite")
    port = LocalStrategyAuditPort(auditor=auditor, evidence_resolver=lambda _reference: evidence)
    with pytest.raises(EvidenceMalformedError, match="candidate_id must be a portable identifier"):
        port.audit("synthetic:invalid-candidate-identity")
    assert store._connection.execute("SELECT COUNT(*) FROM audit_decisions").fetchone()[0] == 0
    store.verify_integrity()
    store.close()


def test_signature_is_hmac_bound_to_factory_payload(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    auditor, store = build_auditor(tmp_path / "audit.sqlite")
    decision = auditor.audit(evidence_package_reference="artifact:a", evidence=valid_evidence)
    raw = hmac.new(
        SIGNING_KEY,
        canonical_json_bytes(decision.signature_payload()),
        hashlib.sha256,
    ).hexdigest()
    assert decision.signature == f"hmac-sha256:{raw}"
    store.close()


def test_exact_replay_survives_restart_and_later_clock(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    path = tmp_path / "audit.sqlite"
    first, first_store = build_auditor(path, now=NOW)
    original = first.audit(evidence_package_reference="artifact:replay", evidence=valid_evidence)
    first_store.close()

    restarted, restarted_store = build_auditor(path, now=NOW + timedelta(days=2))
    replay = restarted.audit(evidence_package_reference="artifact:replay", evidence=valid_evidence)

    assert replay == original
    assert replay.issued_at == NOW
    restarted_store.verify_integrity()
    restarted_store.close()


def test_same_reference_different_content_is_rejected(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    auditor, store = build_auditor(tmp_path / "audit.sqlite")
    auditor.audit(evidence_package_reference="artifact:stable-id", evidence=valid_evidence)
    changed = deepcopy(valid_evidence)
    changed["risk_policy"]["maximum_risk_fraction"] = "0.03"

    with pytest.raises(EvidenceIdentityConflictError, match="reused"):
        auditor.audit(evidence_package_reference="artifact:stable-id", evidence=changed)
    store.close()


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        (
            lambda evidence: evidence["candidate"].__setitem__("configuration_hash", "f" * 64),
            "EVIDENCE_MALFORMED",
        ),
        (
            lambda evidence: evidence["execution_assumptions"].__setitem__("mode", "live"),
            "EVIDENCE_MALFORMED",
        ),
        (
            lambda evidence: evidence["execution_assumptions"].__setitem__("order_placement", True),
            "EVIDENCE_MALFORMED",
        ),
        (
            lambda evidence: evidence["dataset_lineage"].pop("funding_lineage_sha256"),
            "EVIDENCE_MALFORMED",
        ),
        (
            lambda evidence: evidence["research"].__setitem__("holdout_opened_once", False),
            "EVIDENCE_MALFORMED",
        ),
        (
            lambda evidence: evidence["research"]["periods"]["validation"].__setitem__(
                "start", "2021-06-01T00:00:00Z"
            ),
            "EVIDENCE_MALFORMED",
        ),
    ],
)
def test_hard_contract_violations_create_durable_rejection(
    tmp_path: Path,
    valid_evidence: dict[str, Any],
    mutation: Any,
    expected_reason: str,
) -> None:
    mutation(valid_evidence)
    auditor, store = build_auditor(tmp_path / "audit.sqlite")
    decision = auditor.audit(evidence_package_reference="artifact:invalid", evidence=valid_evidence)

    assert decision.outcome is AuditOutcome.REJECTED
    assert decision.factory_status is FactoryAdmissionStatus.REJECTED
    assert expected_reason in decision.reason_codes
    store.verify_integrity()
    store.close()


def test_stale_evidence_is_rejected(tmp_path: Path, valid_evidence: dict[str, Any]) -> None:
    valid_evidence["created_at"] = (NOW - timedelta(days=8)).isoformat()
    auditor, store = build_auditor(tmp_path / "audit.sqlite")
    decision = auditor.audit(evidence_package_reference="artifact:stale", evidence=valid_evidence)

    assert decision.outcome is AuditOutcome.REJECTED
    assert "EVIDENCE_STALE" in decision.reason_codes
    store.close()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda evidence: evidence["robustness"].__setitem__("passed", False),
        lambda evidence: evidence["risk_policy"].__setitem__("maximum_risk_fraction", "0.06"),
        lambda evidence: evidence["risk_policy"].__setitem__("maximum_drawdown_fraction", "0.60"),
    ],
)
def test_soft_policy_failures_remain_research_only(
    tmp_path: Path, valid_evidence: dict[str, Any], mutation: Any
) -> None:
    mutation(valid_evidence)
    auditor, store = build_auditor(tmp_path / "audit.sqlite")
    decision = auditor.audit(
        evidence_package_reference="artifact:research", evidence=valid_evidence
    )

    assert decision.outcome is AuditOutcome.RESEARCH_ONLY
    assert decision.factory_status is FactoryAdmissionStatus.RESEARCH_ONLY
    assert "PAPER_ADMISSION_WITHHELD" in decision.reason_codes
    store.close()


def test_unknown_external_evidence_is_research_only(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    auditor, store = build_auditor(
        tmp_path / "audit.sqlite",
        manifest_hashes={"a" * 64},
        result_hashes={"b" * 64},
    )
    decision = auditor.audit(evidence_package_reference="artifact:unknown", evidence=valid_evidence)
    assert decision.outcome is AuditOutcome.RESEARCH_ONLY
    assert "DATASET_REGISTRY_MANIFEST_UNKNOWN" in decision.reason_codes
    assert "RESULTS_REGISTRY_PACKAGES_UNKNOWN" in decision.reason_codes
    store.close()


@pytest.mark.parametrize(
    ("dataset_verifier", "results_verifier"),
    [
        (UnavailableDatasetLineageVerifier(), StaticDigestResultsVerifier({RESULT_HASH})),
        (StaticDigestDatasetVerifier({MANIFEST_HASH}), UnavailableResultsEvidenceVerifier()),
    ],
)
def test_unavailable_mandatory_verifier_maps_to_insufficient_evidence(
    tmp_path: Path,
    valid_evidence: dict[str, Any],
    dataset_verifier: Any,
    results_verifier: Any,
) -> None:
    auditor, store = build_auditor(
        tmp_path / "audit.sqlite",
        dataset_verifier=dataset_verifier,
        results_verifier=results_verifier,
    )
    decision = auditor.audit(
        evidence_package_reference="artifact:unavailable", evidence=valid_evidence
    )

    assert decision.outcome is AuditOutcome.UNAVAILABLE
    assert decision.factory_status is FactoryAdmissionStatus.INSUFFICIENT_EVIDENCE
    assert decision.as_factory_mapping()["status"] == "INSUFFICIENT_EVIDENCE"
    store.close()


def test_wrong_signing_key_rejects_replay(tmp_path: Path, valid_evidence: dict[str, Any]) -> None:
    path = tmp_path / "audit.sqlite"
    original, store = build_auditor(path)
    original.audit(evidence_package_reference="artifact:key", evidence=valid_evidence)
    store.close()

    wrong_key, wrong_store = build_auditor(
        path, key=b"different-audit-key-that-is-at-least-32-bytes"
    )
    with pytest.raises(EvidenceTamperedError, match="signature"):
        wrong_key.audit(evidence_package_reference="artifact:key", evidence=valid_evidence)
    wrong_store.close()


def test_tampered_evidence_row_is_detected_before_new_audit(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    path = tmp_path / "audit.sqlite"
    auditor, store = build_auditor(path)
    auditor.audit(evidence_package_reference="artifact:tamper-evidence", evidence=valid_evidence)
    store.close()

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE audit_evidence SET payload_json = ? WHERE reference = ?",
        (b'{"candidate":{"candidate_id":"candidate-btc-001"}}', "artifact:tamper-evidence"),
    )
    connection.commit()
    connection.close()

    restarted, restarted_store = build_auditor(path)
    with pytest.raises(EvidenceTamperedError, match="content hash"):
        restarted.audit(evidence_package_reference="artifact:new", evidence=valid_evidence)
    restarted_store.close()


def test_tampered_decision_chain_is_detected(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    path = tmp_path / "audit.sqlite"
    auditor, store = build_auditor(path)
    auditor.audit(evidence_package_reference="artifact:tamper-decision", evidence=valid_evidence)
    store.close()

    connection = sqlite3.connect(path)
    connection.execute("UPDATE audit_decisions SET record_hash = ?", ("0" * 64,))
    connection.commit()
    connection.close()

    reopened = SQLiteAuditStore(path)
    with pytest.raises(EvidenceTamperedError, match="record hash"):
        reopened.verify_integrity()
    reopened.close()


def test_port_resolves_and_returns_factory_mapping(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    auditor, store = build_auditor(tmp_path / "audit.sqlite")
    port = LocalStrategyAuditPort(
        auditor=auditor,
        evidence_resolver=lambda reference: valid_evidence if reference == "artifact:port" else {},
    )
    mapping = port.audit("artifact:port")
    assert mapping["status"] == "APPROVED_FOR_PAPER"
    assert mapping["contract_version"] == "strategy-audit-port-v1"
    store.close()


def test_port_preserves_domain_errors_and_wraps_unknown_resolver_errors(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    auditor, store = build_auditor(tmp_path / "audit.sqlite")

    def unavailable(_: str) -> dict[str, Any]:
        raise OSError("offline")

    port = LocalStrategyAuditPort(auditor=auditor, evidence_resolver=unavailable)
    with pytest.raises(AuditUnavailableError, match="resolver"):
        port.audit("artifact:missing")
    with pytest.raises(EvidenceMalformedError):
        port.audit("")
    store.close()


def test_verification_result_is_immutable_and_normalized() -> None:
    result = VerificationResult(
        verified=True,
        reason_codes=("B", "A", "B"),
        evidence={"digest": "a" * 64},
    )
    assert result.reason_codes == ("A", "B")
    with pytest.raises(TypeError):
        result.evidence["changed"] = True  # type: ignore[index]


def test_policy_validation_and_fingerprint_are_deterministic() -> None:
    first = AuditPolicy()
    second = AuditPolicy()
    assert first.fingerprint == second.fingerprint
    with pytest.raises(EvidenceMalformedError):
        AuditPolicy(maximum_risk_fraction=Decimal("0"))
    with pytest.raises(EvidenceMalformedError):
        AuditPolicy(minimum_results_packages=0)


def test_signer_validates_key_size() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        HmacDecisionSigner(b"short")


def test_store_requires_timezone_aware_timestamp(tmp_path: Path) -> None:
    store = SQLiteAuditStore(tmp_path / "audit.sqlite")
    with pytest.raises(ValueError, match="timezone-aware"):
        store.register_evidence(
            reference="artifact:naive",
            payload={"candidate": {"candidate_id": "candidate"}},
            candidate_id="candidate",
            observed_at=datetime(2026, 1, 1),
        )
    store.close()


def test_canonical_codec_matches_factory_datetime_and_rejects_bad_json() -> None:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    assert canonical_sha256({"issued_at": timestamp}) == _factory_sha256({"issued_at": timestamp})
    with pytest.raises(CanonicalizationError, match="timezone-aware"):
        canonical_json_bytes({"issued_at": datetime(2026, 1, 1)})
    with pytest.raises(CanonicalizationError, match="duplicate JSON key"):
        strict_json_loads('{"a":1,"a":2}')
    with pytest.raises(CanonicalizationError, match="invalid JSON constant"):
        strict_json_loads('{"a":NaN}')


def test_auditor_has_no_live_authorization_surface() -> None:
    assert "LIVE" not in AuditOutcome.__members__
    assert not hasattr(LocalStrategyAuditPort, "authorize_live")
    assert not hasattr(LocalStrategyAuditPort, "place_order")
    assert not hasattr(LocalStrategyAuditPort, "load_credentials")
