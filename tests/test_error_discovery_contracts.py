from __future__ import annotations

from dataclasses import fields
import inspect
from pathlib import Path

from modular_core import (
    CheckpointManager,
    ExecutionStateSnapshot,
    ReconciliationReport,
    reconcile_execution_state,
)
from modular_core.extensions import AtomicJsonStateRepository
from modular_core.serialization import to_jsonable


def test_reconciliation_report_preserves_original_positional_field_prefix() -> None:
    names = tuple(field.name for field in fields(ReconciliationReport))
    assert names[:6] == (
        "matched",
        "missing_local_order_ids",
        "missing_remote_order_ids",
        "position_mismatch",
        "safe_halt_required",
        "diagnostics",
    )
    assert names[6:] == (
        "missing_local_client_order_ids",
        "missing_remote_client_order_ids",
    )

    legacy_positional = ReconciliationReport(True, (), (), False, False, {})
    assert legacy_positional.matched


def test_checkpoint_manager_keeps_repository_positional_and_new_option_keyword_only() -> None:
    signature = inspect.signature(CheckpointManager)
    parameters = tuple(signature.parameters.values())
    assert parameters[0].name == "repository"
    assert parameters[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert parameters[1].name == "supported_schema_versions"
    assert parameters[1].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters[1].default == ("1",)


def test_new_reconciliation_fields_are_serializable_additions() -> None:
    snapshot = ExecutionStateSnapshot(
        __import__("datetime").datetime(2026, 7, 21, tzinfo=__import__("datetime").timezone.utc),
        client_order_ids=("client-1",),
    )
    report = reconcile_execution_state(snapshot, snapshot)
    payload = to_jsonable(report)
    assert payload["matched"] is True
    assert payload["missing_local_client_order_ids"] == []
    assert payload["missing_remote_client_order_ids"] == []


def test_checkpoint_manager_default_constructor_remains_valid(tmp_path: Path) -> None:
    manager = CheckpointManager(AtomicJsonStateRepository(tmp_path))
    assert manager.load("missing") is None


def test_kernel_source_does_not_branch_on_integrated_strategy_names() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in sorted(Path("src/modular_core").glob("*.py"))
    )
    forbidden = (
        "session range",
        "breakout-return",
        "fair value gap",
        "liquidity sweep",
        "market regime",
        "btcusdt",
        "binance",
    )
    assert all(name not in source for name in forbidden)
