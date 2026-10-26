from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from modular_core import OrderType, Side

from paper_execution import (
    ExecutableTradePlan,
    ExecutionIntent,
    IntentRole,
    LiveExecutionNotSupported,
    PaperExecutionConfig,
    PaperReasonCode,
    PlanValidationError,
    ReconciliationMode,
    StateVersions,
)


def test_live_mode_is_technically_rejected() -> None:
    with pytest.raises(LiveExecutionNotSupported) as exc:
        PaperExecutionConfig(execution_mode="live")
    assert exc.value.reason_code is PaperReasonCode.LIVE_EXECUTION_NOT_SUPPORTED


def test_unknown_execution_mode_is_rejected_as_live_attempt() -> None:
    with pytest.raises(LiveExecutionNotSupported):
        PaperExecutionConfig(execution_mode="production")


def test_config_hash_is_deterministic() -> None:
    assert PaperExecutionConfig().canonical_hash == PaperExecutionConfig().canonical_hash
    assert (
        PaperExecutionConfig(module_namespace="x").canonical_hash
        != PaperExecutionConfig().canonical_hash
    )


def test_plan_factory_produces_valid_hash(plan, now, versions) -> None:
    assert len(plan.content_hash) == 64
    plan.validate(now, versions)


def test_plan_hash_tampering_is_rejected(plan, now, versions) -> None:
    tampered = replace(plan, portfolio_id="other")
    with pytest.raises(PlanValidationError) as exc:
        tampered.validate(now, versions)
    assert exc.value.reason_code is PaperReasonCode.PLAN_HASH_MISMATCH


def test_plan_not_yet_valid_is_retryable(plan, now, versions) -> None:
    future = ExecutableTradePlan.create(
        plan_id="future",
        created_at=now,
        valid_from=now + timedelta(minutes=1),
        expires_at=now + timedelta(minutes=2),
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
        market=plan.market,
        action=plan.action,
        intents=plan.intents,
        expected_versions=versions,
        metadata_version=plan.metadata_version,
        configuration_hash=plan.configuration_hash,
    )
    with pytest.raises(PlanValidationError) as exc:
        future.validate(now, versions)
    assert exc.value.reason_code is PaperReasonCode.PLAN_NOT_YET_VALID
    assert exc.value.retryable


def test_stale_plan_is_rejected(plan, now, versions) -> None:
    with pytest.raises(PlanValidationError) as exc:
        plan.validate(now + timedelta(hours=1), versions)
    assert exc.value.reason_code is PaperReasonCode.STALE_PLAN


def test_state_version_mismatch_is_rejected(plan, now) -> None:
    with pytest.raises(PlanValidationError) as exc:
        plan.validate(now, StateVersions(3, 4, 6))
    assert exc.value.reason_code is PaperReasonCode.STATE_VERSION_MISMATCH


def test_cancel_intent_requires_selector() -> None:
    with pytest.raises(ValueError, match="cancel_client_order_id"):
        ExecutionIntent(intent_id="cancel", role=IntentRole.CANCEL)


def test_protection_must_be_reduce_only() -> None:
    with pytest.raises(ValueError, match="reduce_only"):
        ExecutionIntent(
            intent_id="stop",
            role=IntentRole.STOP,
            side=Side.SELL,
            order_type=OrderType.STOP_MARKET,
            quantity=Decimal("1"),
            trigger_price=Decimal("90"),
        )


def test_public_manifest_is_paper_only_execution_module() -> None:
    from paper_execution.manifest import build_manifest

    manifest = build_manifest()
    assert manifest.name == "paper_execution_venue_reconciliation"
    assert manifest.version == "0.1.0"
    assert "execution.paper.result.v1" in manifest.produced_features
    assert manifest.parameters_schema["execution_mode"]["default"] == "paper"


def test_reconciliation_mode_string_is_normalized() -> None:
    config = PaperExecutionConfig(reconciliation_mode="report_only")
    assert config.reconciliation_mode is ReconciliationMode.REPORT_ONLY


def test_metadata_rejects_zero_tick_and_inverted_maximums(metadata) -> None:
    from dataclasses import replace

    with pytest.raises(ValueError, match="tick_size"):
        replace(metadata, tick_size=Decimal("0"))
    with pytest.raises(ValueError, match="max_quantity"):
        replace(metadata, max_quantity=Decimal("0.0001"))
    with pytest.raises(ValueError, match="max_notional"):
        replace(metadata, max_notional=Decimal("1"))
