from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from strategy_audit_validation.canonical_runtime import (
    MODULE_RULES,
    ContractBoundAdapter,
    ModuleRules,
    SetupRules,
    SignalSnapshot,
    build_checkpoint,
    validate_checkpoint,
)
from strategy_audit_validation.errors import EvidenceMalformedError, EvidenceTamperedError

_DIGEST = "a" * 64
_COMPOSITION = "b" * 64
_DATASET_HASH = "c" * 64
_AVAILABLE = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)


def _snapshot(module_id: str, setup_id: str) -> SignalSnapshot:
    rules = MODULE_RULES[module_id]
    setup = rules.setups[setup_id]
    entry = 100.0
    stop = 95.0 if setup.side == "long" else 105.0
    return SignalSnapshot(
        module_id=module_id,
        module_version=rules.module_version,
        setup_id=setup_id,
        signal_available_at=_AVAILABLE,
        first_eligible_5m_open=_AVAILABLE,
        conditions={name: True for name in setup.required_conditions},
        completed_timeframes=setup.required_completed_timeframes,
        entry_price=entry,
        stop_price=stop,
        semantic_input_digest=_DIGEST,
    )


@pytest.mark.parametrize(
    ("module_id", "setup_id", "expected_target", "priority"),
    [
        ("fvg_rt_final_15m", "long", 122.5, 1),
        ("fvg_rt_final_15m", "short_a", 75.0, 1),
        ("fvg_rt_final_15m", "short_b", 70.0, 1),
        ("frozen_1h_short", "short", 70.0, 2),
        ("frozen_4h_short", "short", 70.0, 3),
    ],
)
def test_adapter_builds_deterministic_plans(
    module_id: str, setup_id: str, expected_target: float, priority: int
) -> None:
    adapter = ContractBoundAdapter(module_id)
    snapshot = _snapshot(module_id, setup_id)

    first = adapter.build_plan(snapshot)
    second = adapter.build_plan(snapshot)

    assert first == second
    assert first.output_contract == "canonical-strategy-order-plan-v1"
    assert first.target_price == expected_target
    assert first.priority == priority
    assert first.fill_rule == "touch_inclusive_at_configured_limit"
    assert first.same_bar_priority == ("stop", "full_target", "close_based_no_progress_exit")
    assert len(first.plan_digest) == 64


def test_rule_constructors_reject_invalid_contracts() -> None:
    with pytest.raises(EvidenceMalformedError, match="condition"):
        SetupRules("short", frozenset(), frozenset({"5m"}), 1.0, 1, 2, 1, 0.1)
    with pytest.raises(EvidenceMalformedError, match="completed timeframes"):
        SetupRules("short", frozenset({"x"}), frozenset(), 1.0, 1, 2, 1, 0.1)
    with pytest.raises(EvidenceMalformedError, match="target_r"):
        SetupRules("short", frozenset({"x"}), frozenset({"5m"}), 0.0, 1, 2, 1, 0.1)
    with pytest.raises(EvidenceMalformedError, match="positive integer"):
        SetupRules("short", frozenset({"x"}), frozenset({"5m"}), 1.0, 0, 2, 1, 0.1)
    with pytest.raises(EvidenceMalformedError, match="precede"):
        SetupRules("short", frozenset({"x"}), frozenset({"5m"}), 1.0, 1, 2, 2, 0.1)
    valid = SetupRules("short", frozenset({"x"}), frozenset({"5m"}), 1.0, 1, 2, 1, 0.1)
    with pytest.raises(EvidenceMalformedError, match="module_id"):
        ModuleRules("", "1.0.0", 1, {"short": valid})
    with pytest.raises(EvidenceMalformedError, match="module_version"):
        ModuleRules("module", " ", 1, {"short": valid})
    with pytest.raises(EvidenceMalformedError, match="priority"):
        ModuleRules("module", "1.0.0", 0, {"short": valid})
    with pytest.raises(EvidenceMalformedError, match="at least one setup"):
        ModuleRules("module", "1.0.0", 1, {})


def test_adapter_rejects_unknown_and_mismatched_identity() -> None:
    with pytest.raises(EvidenceMalformedError, match="unknown canonical module"):
        ContractBoundAdapter("missing")
    adapter = ContractBoundAdapter("frozen_1h_short")
    snapshot = _snapshot("frozen_1h_short", "short")
    with pytest.raises(EvidenceMalformedError, match="module_id mismatch"):
        adapter.build_plan(replace(snapshot, module_id="frozen_4h_short"))
    with pytest.raises(EvidenceMalformedError, match="module_version mismatch"):
        adapter.build_plan(replace(snapshot, module_version="9.9.9"))
    with pytest.raises(EvidenceMalformedError, match="unknown setup"):
        adapter.build_plan(replace(snapshot, setup_id="missing"))


def test_adapter_rejects_causality_and_condition_drift() -> None:
    adapter = ContractBoundAdapter("frozen_1h_short")
    snapshot = _snapshot("frozen_1h_short", "short")
    with pytest.raises(EvidenceMalformedError, match="timezone-aware"):
        adapter.build_plan(replace(snapshot, signal_available_at=_AVAILABLE.replace(tzinfo=None)))
    plus_three = timezone(timedelta(hours=3))
    with pytest.raises(EvidenceMalformedError, match="must use UTC"):
        adapter.build_plan(replace(snapshot, signal_available_at=_AVAILABLE.astimezone(plus_three)))
    with pytest.raises(EvidenceMalformedError, match="first eligible"):
        adapter.build_plan(
            replace(snapshot, first_eligible_5m_open=_AVAILABLE + timedelta(minutes=5))
        )
    with pytest.raises(EvidenceMalformedError, match="timeframe identity"):
        adapter.build_plan(replace(snapshot, completed_timeframes=frozenset({"5m", "1h"})))
    with pytest.raises(EvidenceMalformedError, match="condition identity"):
        adapter.build_plan(replace(snapshot, conditions={"unexpected": True}))
    failed = dict(snapshot.conditions)
    failed[next(iter(failed))] = False
    with pytest.raises(EvidenceMalformedError, match="failed condition"):
        adapter.build_plan(replace(snapshot, conditions=failed))


def test_adapter_rejects_price_and_digest_errors() -> None:
    long_adapter = ContractBoundAdapter("fvg_rt_final_15m")
    long_snapshot = _snapshot("fvg_rt_final_15m", "long")
    with pytest.raises(EvidenceMalformedError, match="long stop"):
        long_adapter.build_plan(replace(long_snapshot, stop_price=105.0))
    short_adapter = ContractBoundAdapter("frozen_4h_short")
    short_snapshot = _snapshot("frozen_4h_short", "short")
    with pytest.raises(EvidenceMalformedError, match="short stop"):
        short_adapter.build_plan(replace(short_snapshot, stop_price=95.0))
    with pytest.raises(EvidenceMalformedError, match="entry_price"):
        short_adapter.build_plan(replace(short_snapshot, entry_price=float("nan")))
    with pytest.raises(EvidenceMalformedError, match="target price"):
        short_adapter.build_plan(replace(short_snapshot, entry_price=1.0, stop_price=100.0))
    with pytest.raises(EvidenceMalformedError, match="semantic_input_digest"):
        replace(short_snapshot, semantic_input_digest="bad")


def test_checkpoint_round_trip_and_identity_fence() -> None:
    checkpoint = build_checkpoint(
        composition_hash=_COMPOSITION,
        dataset_id="binance_usdtm_btcusdt_5m_v2",
        dataset_version=2,
        dataset_hash=_DATASET_HASH,
        last_processed_5m_open=_AVAILABLE,
        state_sequence=7,
        pending_order_digest=_DIGEST,
    )
    assert checkpoint.schema_version == "canonical-strategy-state-v1"
    assert checkpoint.candidate_id == f"strategy-candidate-{_COMPOSITION[:24]}"
    validate_checkpoint(
        checkpoint,
        expected_composition_hash=_COMPOSITION,
        expected_dataset_id="binance_usdtm_btcusdt_5m_v2",
        expected_dataset_version=2,
        expected_dataset_hash=_DATASET_HASH,
        minimum_state_sequence=7,
    )
    with pytest.raises(EvidenceTamperedError, match="sequence regressed"):
        validate_checkpoint(
            checkpoint,
            expected_composition_hash=_COMPOSITION,
            expected_dataset_id="binance_usdtm_btcusdt_5m_v2",
            expected_dataset_version=2,
            expected_dataset_hash=_DATASET_HASH,
            minimum_state_sequence=8,
        )
    with pytest.raises(EvidenceTamperedError, match="identity or digest"):
        validate_checkpoint(
            checkpoint,
            expected_composition_hash="d" * 64,
            expected_dataset_id="binance_usdtm_btcusdt_5m_v2",
            expected_dataset_version=2,
            expected_dataset_hash=_DATASET_HASH,
        )
    with pytest.raises(EvidenceTamperedError, match="schema or symbol"):
        validate_checkpoint(
            replace(checkpoint, symbol="ETHUSDT"),
            expected_composition_hash=_COMPOSITION,
            expected_dataset_id="binance_usdtm_btcusdt_5m_v2",
            expected_dataset_version=2,
            expected_dataset_hash=_DATASET_HASH,
        )
    with pytest.raises(EvidenceTamperedError, match="identity or digest"):
        validate_checkpoint(
            replace(checkpoint, checkpoint_digest="e" * 64),
            expected_composition_hash=_COMPOSITION,
            expected_dataset_id="binance_usdtm_btcusdt_5m_v2",
            expected_dataset_version=2,
            expected_dataset_hash=_DATASET_HASH,
        )


def test_checkpoint_builder_rejects_malformed_inputs() -> None:
    kwargs = {
        "composition_hash": _COMPOSITION,
        "dataset_id": "dataset",
        "dataset_version": 1,
        "dataset_hash": _DATASET_HASH,
        "last_processed_5m_open": _AVAILABLE,
        "state_sequence": 0,
    }
    with pytest.raises(EvidenceMalformedError, match="composition_hash"):
        build_checkpoint(**{**kwargs, "composition_hash": "bad"})
    with pytest.raises(EvidenceMalformedError, match="dataset_hash"):
        build_checkpoint(**{**kwargs, "dataset_hash": "bad"})
    with pytest.raises(EvidenceMalformedError, match="dataset_version"):
        build_checkpoint(**{**kwargs, "dataset_version": 0})
    with pytest.raises(EvidenceMalformedError, match="dataset_id"):
        build_checkpoint(**{**kwargs, "dataset_id": " "})
    with pytest.raises(EvidenceMalformedError, match="state_sequence"):
        build_checkpoint(**{**kwargs, "state_sequence": -1})
    with pytest.raises(EvidenceMalformedError, match="pending_order_digest"):
        build_checkpoint(**kwargs, pending_order_digest="bad")
    with pytest.raises(EvidenceMalformedError, match="timezone-aware"):
        build_checkpoint(**{**kwargs, "last_processed_5m_open": _AVAILABLE.replace(tzinfo=None)})
