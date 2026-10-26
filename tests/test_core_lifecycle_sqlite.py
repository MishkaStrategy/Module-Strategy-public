from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import pytest


from modular_core import (
    CoreAuthorityReference,
    CoreCommitStatus,
    CoreEffectLookupStatus,
    CoreFenceTransition,
    CoreLifecycleAction,
    CoreLifecycleCommitCoordinator,
    CoreLifecycleEffect,
    CoreLifecycleExpectedGeneration,
    CoreWriterIdentity,
    EngineConfig,
    ExecutionConfig,
    InstrumentSpec,
    Journal,
)
from modular_core.execution import BrokerSimulator
from modular_core.broker_state import capture_broker_state, restore_broker_state
from modular_core.lifecycle_sqlite import _restore_private_broker
from modular_core.lifecycle_sqlite import SQLiteCoreLifecycleCommitPort

_DIGEST = hashlib.sha256(b"core lifecycle sqlite tests").hexdigest()
_NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)


def _store(
    path,
    *,
    risk_enabled: bool = True,
    authorities=True,
    writer=None,
    execution=None,
    config=None,
):
    binding = CoreLifecycleExpectedGeneration(
        "instance-1", "incarnation-1", "package-1", _DIGEST, 0
    )
    writer = writer or CoreWriterIdentity("core-owner-1", 1)
    refs = (
        (CoreAuthorityReference("paper-admission", "admission-1", _DIGEST),) if authorities else ()
    )
    broker = BrokerSimulator(
        config
        or EngineConfig(
            close_open_position_at_end=False,
            execution=execution or ExecutionConfig(),
        ),
        Journal(run_id="bootstrap"),
        InstrumentSpec("BTCUSDT"),
    )
    port = SQLiteCoreLifecycleCommitPort(
        path,
        binding=binding,
        writer=writer,
        initial_broker=broker,
        trusted_authority_refs=refs,
        initial_risk_enabled=risk_enabled,
        clock=lambda: _NOW,
    )
    return port, binding, writer, refs


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _custom_effect(binding, writer, refs, effect_id, payload, targets=()):
    encoded = _canonical(payload)
    return CoreLifecycleEffect(
        effect_id,
        binding,
        writer,
        payload["operation"],
        hashlib.sha256(encoded).hexdigest(),
        encoded,
        targets,
        refs,
    )


def _effect(
    binding,
    writer,
    refs,
    *,
    effect_id="effect-1",
    operation="submit_order",
    payload=None,
    targets=("entry-1",),
):
    value = payload or {
        "operation": "submit_order",
        "request": {
            "side": "buy",
            "order_type": "market",
            "quantity": 1.0,
            "stop_loss": 90.0,
            "take_profit": 120.0,
            "client_order_id": "entry-1",
        },
        "source_module": "test-strategy",
        "created_at": _NOW.isoformat(),
        "current_index": 0,
        "mark_price": 100.0,
    }
    return _custom_effect(binding, writer, refs, effect_id, value, targets)


def _suspend(binding, writer, refs, *, transition_id="suspend-1"):
    encoded = _canonical({"action": "SUSPEND"})
    return CoreFenceTransition(
        transition_id,
        binding,
        writer,
        binding.expected_generation + 1,
        CoreLifecycleAction.SUSPEND,
        hashlib.sha256(encoded).hexdigest(),
        encoded,
        refs,
    )


def _seed_partial_entry_suspended(path):
    execution = ExecutionConfig(max_fill_fraction_per_bar=0.5)
    port, binding, writer, refs = _store(path, execution=execution)
    coordinator = CoreLifecycleCommitCoordinator(port)
    entry = {
        "operation": "submit_order",
        "request": {
            "side": "buy",
            "order_type": "market",
            "quantity": 2.0,
            "stop_loss": 90.0,
            "take_profit": 120.0,
            "client_order_id": "remainder-entry",
        },
        "source_module": "test-strategy",
        "created_at": _NOW.isoformat(),
        "current_index": 0,
        "mark_price": 100.0,
    }
    assert coordinator.commit_effect(
        _custom_effect(binding, writer, refs, "submit-remainder", entry, ("remainder-entry",))
    ).status is CoreCommitStatus.COMMITTED
    bar = {
        "timestamp": _NOW.isoformat(),
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 10.0,
        "symbol": "BTCUSDT",
        "timeframe": "1m",
    }
    assert coordinator.commit_effect(
        _custom_effect(
            binding,
            writer,
            refs,
            "fill-remainder-entry",
            {"operation": "process_bar", "bar": bar, "index": 1},
        )
    ).status is CoreCommitStatus.COMMITTED
    assert coordinator.commit_transition(_suspend(binding, writer, refs)).status is (
        CoreCommitStatus.COMMITTED
    )
    return port, binding, writer, refs, coordinator, execution, bar


def test_effect_receipt_and_broker_events_commit_atomically_and_replay(tmp_path):
    path = tmp_path / "core.sqlite"
    port, binding, writer, refs = _store(path)
    coordinator = CoreLifecycleCommitCoordinator(port)
    request = _effect(binding, writer, refs)

    committed = coordinator.commit_effect(request)
    replayed = coordinator.commit_effect(request)

    assert committed.status is CoreCommitStatus.COMMITTED
    assert committed.receipt is not None
    assert committed.replayed is False
    assert replayed.status is CoreCommitStatus.COMMITTED
    assert replayed.receipt == committed.receipt
    assert replayed.replayed is True
    snapshot = port.snapshot()
    assert snapshot.commit_sequence == 1
    assert snapshot.lifecycle.expected_generation == 0
    assert len(snapshot.broker_state.orders) == 1
    assert snapshot.broker_state.orders[0]["client_order_id"] == "entry-1"
    assert [event.sequence for event in snapshot.events] == [1, 2]
    assert [event.event_type for event in snapshot.events] == [
        "order.submitted",
        "core.effect.committed",
    ]

    reopened, _, _, _ = _store(path)
    restored = reopened.snapshot()
    assert restored.broker_state == snapshot.broker_state
    assert restored.events == snapshot.events
    assert restored.commit_sequence == snapshot.commit_sequence


def test_same_id_with_changed_payload_is_rejected_without_mutation(tmp_path):
    port, binding, writer, refs = _store(tmp_path / "core.sqlite")
    coordinator = CoreLifecycleCommitCoordinator(port)
    first = _effect(binding, writer, refs)
    assert coordinator.commit_effect(first).status is CoreCommitStatus.COMMITTED

    changed_payload = {
        "operation": "submit_order",
        "request": {
            "side": "buy",
            "order_type": "market",
            "quantity": 2.0,
            "stop_loss": 90.0,
            "take_profit": 120.0,
            "client_order_id": "entry-1",
        },
        "source_module": "test-strategy",
        "created_at": _NOW.isoformat(),
        "current_index": 0,
        "mark_price": 100.0,
    }
    conflict = _effect(binding, writer, refs, payload=changed_payload)
    rejected = coordinator.commit_effect(conflict)

    assert rejected.status is CoreCommitStatus.DENIED
    assert "idempotency" in rejected.reason
    assert port.snapshot().commit_sequence == 1
    assert len(port.snapshot().broker_state.orders) == 1


def test_suspend_generation_fence_prevents_pending_entry_fill_and_replays(tmp_path):
    port, binding, writer, refs = _store(tmp_path / "core.sqlite")
    coordinator = CoreLifecycleCommitCoordinator(port)
    assert (
        coordinator.commit_effect(_effect(binding, writer, refs)).status
        is CoreCommitStatus.COMMITTED
    )

    transition = _suspend(binding, writer, refs)
    suspended = coordinator.commit_transition(transition)
    replay = coordinator.commit_transition(transition)

    assert suspended.status is CoreCommitStatus.COMMITTED
    assert replay.status is CoreCommitStatus.COMMITTED
    assert replay.replayed is True
    snapshot = port.snapshot()
    assert snapshot.risk_enabled is False
    assert snapshot.lifecycle.expected_generation == 1
    assert snapshot.events[-1].event_type == "core.lifecycle.suspended"
    assert snapshot.events[-1].payload["reconciliation_required"] is False

    bar_payload = {
        "operation": "process_bar",
        "bar": {
            "timestamp": _NOW.isoformat(),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 10.0,
            "symbol": "BTCUSDT",
            "timeframe": "1m",
        },
        "index": 1,
    }
    blocked = _effect(
        snapshot.lifecycle,
        writer,
        refs,
        effect_id="bar-after-suspend",
        operation="process_bar",
        payload=bar_payload,
        targets=(),
    )
    processed = coordinator.commit_effect(blocked)
    assert processed.status is CoreCommitStatus.COMMITTED
    final = port.snapshot()
    assert final.broker_state.fill_sequence == 0
    assert final.broker_state.orders[0]["status"] == "pending"
    assert final.commit_sequence == 3


def test_closed_fence_bar_runs_existing_take_profit_and_preserves_entry_remainder(tmp_path):
    path = tmp_path / "core.sqlite"
    port, binding, writer, refs = _store(
        path,
        execution=ExecutionConfig(max_fill_fraction_per_bar=0.5),
    )
    coordinator = CoreLifecycleCommitCoordinator(port)
    entry = {
        "operation": "submit_order",
        "request": {
            "side": "buy",
            "order_type": "market",
            "quantity": 2.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
            "client_order_id": "partial-entry",
        },
        "source_module": "test-strategy",
        "created_at": _NOW.isoformat(),
        "current_index": 0,
        "mark_price": 100.0,
    }
    assert coordinator.commit_effect(
        _custom_effect(binding, writer, refs, "submit-partial-entry", entry, ("partial-entry",))
    ).status is CoreCommitStatus.COMMITTED
    entry_bar = {
        "timestamp": _NOW.isoformat(),
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 10.0,
        "symbol": "BTCUSDT",
        "timeframe": "1m",
    }
    assert coordinator.commit_effect(
        _custom_effect(
            binding,
            writer,
            refs,
            "partial-entry-fill",
            {"operation": "process_bar", "bar": entry_bar, "index": 1},
        )
    ).status is CoreCommitStatus.COMMITTED
    partial = port.snapshot().broker_state
    assert partial.position is not None
    assert partial.position["quantity"] == 1.0
    assert partial.orders[0]["filled_quantity"] == 1.0
    assert partial.orders[0]["status"] == "partially_filled"

    assert coordinator.commit_transition(_suspend(binding, writer, refs)).status is (
        CoreCommitStatus.COMMITTED
    )
    suspended = port.snapshot().lifecycle
    target_bar = {
        **entry_bar,
        "timestamp": datetime(2026, 10, 25, 12, 1, tzinfo=UTC).isoformat(),
        "high": 111.0,
        "close": 109.0,
    }
    target_effect = _custom_effect(
        suspended,
        writer,
        refs,
        "take-profit-after-suspend",
        {"operation": "process_bar", "bar": target_bar, "index": 2},
    )
    closed = coordinator.commit_effect(target_effect)
    assert closed.status is CoreCommitStatus.COMMITTED
    assert closed.receipt is not None
    final = port.snapshot()
    assert final.risk_enabled is False
    assert final.broker_state.position is None
    pending_entry = final.broker_state.orders[0]
    assert pending_entry["status"] == "partially_filled"
    assert pending_entry["filled_quantity"] == 1.0

    stale = _custom_effect(
        binding,
        writer,
        refs,
        "stale-generation-bar",
        {"operation": "process_bar", "bar": target_bar, "index": 3},
    )
    assert coordinator.commit_effect(stale).status is CoreCommitStatus.DENIED
    reopened, _, _, _ = _store(
        path,
        execution=ExecutionConfig(max_fill_fraction_per_bar=0.5),
    )
    replay = CoreLifecycleCommitCoordinator(reopened).commit_effect(target_effect)
    assert replay.status is CoreCommitStatus.COMMITTED
    assert replay.replayed is True
    assert replay.receipt == closed.receipt
    assert reopened.snapshot() == final


def test_closed_fence_bar_applies_explicit_reducer_with_entry_remainder(tmp_path):
    port, binding, writer, refs = _store(
        tmp_path / "core.sqlite",
        execution=ExecutionConfig(max_fill_fraction_per_bar=0.5),
    )
    coordinator = CoreLifecycleCommitCoordinator(port)
    entry = {
        "operation": "submit_order",
        "request": {
            "side": "buy",
            "order_type": "market",
            "quantity": 2.0,
            "stop_loss": 90.0,
            "take_profit": 120.0,
            "client_order_id": "remainder-entry",
        },
        "source_module": "test-strategy",
        "created_at": _NOW.isoformat(),
        "current_index": 0,
        "mark_price": 100.0,
    }
    assert coordinator.commit_effect(
        _custom_effect(binding, writer, refs, "submit-remainder", entry, ("remainder-entry",))
    ).status is CoreCommitStatus.COMMITTED
    bar = {
        "timestamp": _NOW.isoformat(),
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 10.0,
        "symbol": "BTCUSDT",
        "timeframe": "1m",
    }
    assert coordinator.commit_effect(
        _custom_effect(binding, writer, refs, "fill-remainder-entry", {
            "operation": "process_bar", "bar": bar, "index": 1
        })
    ).status is CoreCommitStatus.COMMITTED
    assert coordinator.commit_transition(_suspend(binding, writer, refs)).status is (
        CoreCommitStatus.COMMITTED
    )
    suspended = port.snapshot().lifecycle
    assert coordinator.commit_effect(
        _custom_effect(
            suspended,
            writer,
            refs,
            "submit-explicit-reducer",
            {
                "operation": "submit_order",
                "request": {
                    "side": "sell",
                    "order_type": "market",
                    "quantity": 0.25,
                    "reduce_only": True,
                    "position_side": "long",
                    "client_order_id": "explicit-reducer",
                },
                "source_module": "test-reducer",
                "created_at": _NOW.isoformat(),
                "current_index": 1,
                "mark_price": 100.0,
            },
            ("explicit-reducer",),
        )
    ).status is CoreCommitStatus.COMMITTED

    next_bar = {**bar, "timestamp": datetime(2026, 10, 25, 12, 1, tzinfo=UTC).isoformat()}
    reduced = coordinator.commit_effect(
        _custom_effect(
            port.snapshot().lifecycle,
            writer,
            refs,
            "apply-scheduled-and-explicit-reductions",
            {"operation": "process_bar", "bar": next_bar, "index": 2},
        )
    )
    assert reduced.status is CoreCommitStatus.COMMITTED
    final = port.snapshot()
    assert final.broker_state.position is not None
    assert final.broker_state.position["quantity"] == 0.875
    by_client_id = {order["client_order_id"]: order for order in final.broker_state.orders}
    assert by_client_id["remainder-entry"]["status"] == "partially_filled"
    assert by_client_id["remainder-entry"]["filled_quantity"] == 1.0
    assert by_client_id["explicit-reducer"]["filled_quantity"] == 0.125


def test_closed_fence_pair_is_core_only_and_survives_reopen(tmp_path):
    path = tmp_path / "core.sqlite"
    port, _, writer, refs, coordinator, execution, _ = _seed_partial_entry_suspended(path)
    suspended = port.snapshot().lifecycle
    reducer_payload = {
        "operation": "submit_order",
        "request": {
            "side": "sell",
            "order_type": "market",
            "quantity": 0.25,
            "reduce_only": True,
            "position_side": "long",
            "client_order_id": "core-only-reducer",
        },
        "source_module": "test-reducer",
        "created_at": _NOW.isoformat(),
        "current_index": 1,
        "mark_price": 100.0,
    }
    accepted = coordinator.commit_effect(
        _custom_effect(
            suspended,
            writer,
            refs,
            "core-only-reducer",
            reducer_payload,
            ("core-only-reducer",),
        )
    )
    assert accepted.status is CoreCommitStatus.COMMITTED
    saved = port.snapshot()
    assert saved.risk_enabled is False
    assert len(saved.broker_state.pending_order_ids) == 2

    reopened, _, _, _ = _store(path, execution=execution)
    assert reopened.snapshot() == saved

    core_broker, _ = _restore_private_broker(
        saved.broker_state,
        saved.events,
        reopened.binding,
        risk_enabled=False,
    )
    with pytest.raises(ValueError, match="one-active-order"):
        capture_broker_state(core_broker)

    strict_target = BrokerSimulator(
        EngineConfig(close_open_position_at_end=False, execution=execution),
        Journal(run_id="strict-default-restore"),
        InstrumentSpec("BTCUSDT"),
    )
    with pytest.raises(ValueError, match="one-active-order"):
        restore_broker_state(strict_target, saved.broker_state)

    state_before_bad_attempts = reopened.snapshot()
    for suffix, side in (("second", "sell"), ("wrong-direction", "buy")):
        invalid_payload = {
            **reducer_payload,
            "request": {
                **reducer_payload["request"],
                "client_order_id": f"invalid-{suffix}",
                "side": side,
            },
        }
        invalid = CoreLifecycleCommitCoordinator(reopened).commit_effect(
            _custom_effect(
                state_before_bad_attempts.lifecycle,
                writer,
                refs,
                f"invalid-{suffix}",
                invalid_payload,
                (f"invalid-{suffix}",),
            )
        )
        assert invalid.status is CoreCommitStatus.DENIED
        assert reopened.snapshot() == state_before_bad_attempts


def test_closed_fence_pair_clamps_reducer_to_open_position_volume(tmp_path):
    port, _, writer, refs, coordinator, _, _ = _seed_partial_entry_suspended(
        tmp_path / "core.sqlite"
    )
    suspended = port.snapshot().lifecycle
    position_quantity = port.snapshot().broker_state.position["quantity"]
    oversized = {
        "operation": "submit_order",
        "request": {
            "side": "sell",
            "order_type": "market",
            "quantity": 50.0,
            "reduce_only": True,
            "position_side": "long",
            "client_order_id": "bounded-reducer",
        },
        "source_module": "test-reducer",
        "created_at": _NOW.isoformat(),
        "current_index": 1,
        "mark_price": 100.0,
    }
    assert coordinator.commit_effect(
        _custom_effect(
            suspended,
            writer,
            refs,
            "bounded-reducer",
            oversized,
            ("bounded-reducer",),
        )
    ).status is CoreCommitStatus.COMMITTED
    saved = port.snapshot()
    reducer = next(
        order for order in saved.broker_state.orders if order["client_order_id"] == "bounded-reducer"
    )
    assert reducer["quantity"] <= position_quantity


def test_open_fence_keeps_default_single_active_order_guard(tmp_path):
    path = tmp_path / "open.sqlite"
    execution = ExecutionConfig(max_fill_fraction_per_bar=0.5)
    port, binding, writer, refs = _store(path, execution=execution)
    coordinator = CoreLifecycleCommitCoordinator(port)
    entry = {
        "operation": "submit_order",
        "request": {
            "side": "buy",
            "order_type": "market",
            "quantity": 2.0,
            "stop_loss": 90.0,
            "take_profit": 120.0,
            "client_order_id": "open-entry-remainder",
        },
        "source_module": "test-strategy",
        "created_at": _NOW.isoformat(),
        "current_index": 0,
        "mark_price": 100.0,
    }
    assert coordinator.commit_effect(
        _custom_effect(binding, writer, refs, "submit-open-entry", entry, ("open-entry-remainder",))
    ).status is CoreCommitStatus.COMMITTED
    bar = {
        "timestamp": _NOW.isoformat(),
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 10.0,
        "symbol": "BTCUSDT",
        "timeframe": "1m",
    }
    assert coordinator.commit_effect(
        _custom_effect(
            binding,
            writer,
            refs,
            "fill-open-entry-partially",
            {"operation": "process_bar", "bar": bar, "index": 1},
        )
    ).status is CoreCommitStatus.COMMITTED
    before = port.snapshot()
    reducer = {
        "operation": "submit_order",
        "request": {
            "side": "sell",
            "order_type": "market",
            "quantity": 0.25,
            "reduce_only": True,
            "position_side": "long",
            "client_order_id": "open-fence-reducer",
        },
        "source_module": "test-reducer",
        "created_at": _NOW.isoformat(),
        "current_index": 1,
        "mark_price": 100.0,
    }
    denied = coordinator.commit_effect(
        _custom_effect(
            before.lifecycle,
            writer,
            refs,
            "open-fence-reducer",
            reducer,
            ("open-fence-reducer",),
        )
    )
    assert denied.status is CoreCommitStatus.DENIED
    assert port.snapshot() == before


def test_closed_fence_mutual_exclusion_between_pending_close_and_reducer(tmp_path):
    for pending_first in ("close", "reducer"):
        path = tmp_path / f"{pending_first}.sqlite"
        port, _, writer, refs, coordinator, _, _ = _seed_partial_entry_suspended(path)
        suspended = port.snapshot().lifecycle
        position_id = port.snapshot().broker_state.position["id"]
        close = _custom_effect(
            suspended,
            writer,
            refs,
            "scheduled-close",
            {
                "operation": "schedule_close",
                "request": {"fraction": 0.5, "reason": "operator_reduce"},
                "source_module": "test-operator",
                "timestamp": _NOW.isoformat(),
                "current_index": 1,
            },
            (position_id,),
        )
        reducer = _custom_effect(
            suspended,
            writer,
            refs,
            "pending-reducer",
            {
                "operation": "submit_order",
                "request": {
                    "side": "sell",
                    "order_type": "market",
                    "quantity": 0.25,
                    "reduce_only": True,
                    "position_side": "long",
                    "client_order_id": "pending-reducer",
                },
                "source_module": "test-reducer",
                "created_at": _NOW.isoformat(),
                "current_index": 1,
                "mark_price": 100.0,
            },
            ("pending-reducer",),
        )
        first, second = (close, reducer) if pending_first == "close" else (reducer, close)
        assert coordinator.commit_effect(first).status is CoreCommitStatus.COMMITTED
        before_conflict = port.snapshot()
        assert coordinator.commit_effect(second).status is CoreCommitStatus.DENIED
        assert port.snapshot() == before_conflict


def test_closed_fence_scheduled_partial_close_preserves_entry_remainder(tmp_path):
    port, _, writer, refs, coordinator, _, bar = _seed_partial_entry_suspended(
        tmp_path / "core.sqlite"
    )
    suspended = port.snapshot().lifecycle
    position_id = port.snapshot().broker_state.position["id"]
    scheduled = coordinator.commit_effect(
        _custom_effect(
            suspended,
            writer,
            refs,
            "scheduled-partial-close",
            {
                "operation": "schedule_close",
                "request": {"fraction": 0.5, "reason": "operator_reduce"},
                "source_module": "test-operator",
                "timestamp": _NOW.isoformat(),
                "current_index": 1,
            },
            (position_id,),
        )
    )
    assert scheduled.status is CoreCommitStatus.COMMITTED
    next_bar = {**bar, "timestamp": datetime(2026, 10, 25, 12, 1, tzinfo=UTC).isoformat()}
    reduced = coordinator.commit_effect(
        _custom_effect(
            port.snapshot().lifecycle,
            writer,
            refs,
            "execute-scheduled-partial-close",
            {"operation": "process_bar", "bar": next_bar, "index": 2},
        )
    )
    assert reduced.status is CoreCommitStatus.COMMITTED
    final = port.snapshot()
    assert final.broker_state.position["quantity"] == 0.5
    entry_order = next(
        order for order in final.broker_state.orders if order["client_order_id"] == "remainder-entry"
    )
    assert entry_order["status"] == "partially_filled"
    assert entry_order["filled_quantity"] == 1.0


def test_incompatible_suspend_commits_quarantine_and_replays_after_reopen(tmp_path):
    path = tmp_path / "quarantined.sqlite"
    config = EngineConfig(one_active_order=False, close_open_position_at_end=False)
    port, binding, writer, refs = _store(path, config=config)
    coordinator = CoreLifecycleCommitCoordinator(port)
    for suffix, price in (("one", 90.0), ("two", 91.0)):
        payload = {
            "operation": "submit_order",
            "request": {
                "side": "buy",
                "order_type": "limit",
                "quantity": 1.0,
                "price": price,
                "stop_loss": 80.0,
                "take_profit": 120.0,
                "client_order_id": f"ambiguous-entry-{suffix}",
            },
            "source_module": "test-strategy",
            "created_at": _NOW.isoformat(),
            "current_index": 0,
            "mark_price": 100.0,
        }
        assert coordinator.commit_effect(
            _custom_effect(
                binding,
                writer,
                refs,
                f"submit-ambiguous-{suffix}",
                payload,
                (f"ambiguous-entry-{suffix}",),
            )
        ).status is CoreCommitStatus.COMMITTED

    before_suspend = port.snapshot()
    caller_marker_payload = {"action": "SUSPEND", "reconciliation_required": True}
    caller_marker_encoded = _canonical(caller_marker_payload)
    caller_marker_transition = CoreFenceTransition(
        "caller-selected-quarantine",
        before_suspend.lifecycle,
        writer,
        before_suspend.lifecycle.expected_generation + 1,
        CoreLifecycleAction.SUSPEND,
        hashlib.sha256(caller_marker_encoded).hexdigest(),
        caller_marker_encoded,
        refs,
    )
    assert coordinator.commit_transition(caller_marker_transition).status is (
        CoreCommitStatus.DENIED
    )
    assert port.snapshot() == before_suspend

    transition = _suspend(binding, writer, refs)
    suspended = coordinator.commit_transition(transition)
    replay = coordinator.commit_transition(transition)
    assert suspended.status is CoreCommitStatus.COMMITTED
    assert suspended.receipt is not None
    assert replay.status is CoreCommitStatus.COMMITTED
    assert replay.replayed is True
    assert replay.receipt == suspended.receipt
    saved = port.snapshot()
    assert saved.risk_enabled is False
    assert saved.events[-1].payload["reconciliation_required"] is True
    assert len(saved.broker_state.pending_order_ids) == 2

    # Simulate the previous event shape where SUSPEND had no disposition field.
    connection = sqlite3.connect(path)
    try:
        sequence, encoded = connection.execute(
            "SELECT sequence,event_json FROM core_lifecycle_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        legacy_event = json.loads(encoded)
        legacy_event["payload"].pop("reconciliation_required")
        legacy_encoded = json.dumps(legacy_event, sort_keys=True, separators=(",", ":"))
        connection.execute(
            "UPDATE core_lifecycle_events SET event_json=?,event_sha256=? WHERE sequence=?",
            (
                legacy_encoded,
                hashlib.sha256(legacy_encoded.encode()).hexdigest(),
                sequence,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    reopened, _, _, _ = _store(path, config=config)
    legacy_snapshot = reopened.snapshot()
    assert legacy_snapshot.risk_enabled is False
    assert legacy_snapshot.broker_state == saved.broker_state
    assert legacy_snapshot.commit_sequence == saved.commit_sequence
    assert "reconciliation_required" not in legacy_snapshot.events[-1].payload
    transition_replay = CoreLifecycleCommitCoordinator(reopened).commit_transition(transition)
    assert transition_replay.status is CoreCommitStatus.COMMITTED
    assert transition_replay.replayed is True
    assert transition_replay.receipt == suspended.receipt
    before = reopened.snapshot()
    rejected_payloads = (
        (
            "quarantine-new-entry",
            {
                "operation": "submit_order",
                "request": {
                    "side": "buy",
                    "order_type": "market",
                    "quantity": 1.0,
                    "client_order_id": "forbidden-entry",
                },
                "source_module": "test-strategy",
                "created_at": _NOW.isoformat(),
                "current_index": 0,
                "mark_price": 100.0,
            },
            ("forbidden-entry",),
        ),
        (
            "quarantine-no-position-bar",
            {
                "operation": "process_bar",
                "bar": {
                    "timestamp": datetime(2026, 10, 25, 12, 1, tzinfo=UTC).isoformat(),
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 10.0,
                    "symbol": "BTCUSDT",
                    "timeframe": "1m",
                },
                "index": 1,
            },
            (),
        ),
        (
            "quarantine-reducer",
            {
                "operation": "submit_order",
                "request": {
                    "side": "sell",
                    "order_type": "market",
                    "quantity": 0.5,
                    "reduce_only": True,
                    "position_side": "long",
                    "client_order_id": "forbidden-reducer",
                },
                "source_module": "test-reducer",
                "created_at": _NOW.isoformat(),
                "current_index": 0,
                "mark_price": 100.0,
            },
            ("forbidden-reducer",),
        ),
        (
            "quarantine-cancel",
            {
                "operation": "cancel_order",
                "request": {"client_order_id": "ambiguous-entry-one"},
                "source_module": "test-operator",
                "timestamp": _NOW.isoformat(),
            },
            ("ambiguous-entry-one",),
        ),
    )
    for effect_id, payload, targets in rejected_payloads:
        denied = CoreLifecycleCommitCoordinator(reopened).commit_effect(
            _custom_effect(before.lifecycle, writer, refs, effect_id, payload, targets)
        )
        assert denied.status is CoreCommitStatus.DENIED
        assert reopened.snapshot() == before
    resume = CoreFenceTransition(
        "forbidden-resume",
        before.lifecycle,
        writer,
        before.lifecycle.expected_generation + 1,
        CoreLifecycleAction.RESUME,
        hashlib.sha256(_canonical({"action": "RESUME"})).hexdigest(),
        _canonical({"action": "RESUME"}),
        refs,
    )
    assert CoreLifecycleCommitCoordinator(reopened).commit_transition(resume).status is (
        CoreCommitStatus.DENIED
    )
    assert reopened.snapshot() == before
    stale = _custom_effect(
        binding,
        writer,
        refs,
        "stale-generation-quarantine-command",
        rejected_payloads[0][1],
        rejected_payloads[0][2],
    )
    assert CoreLifecycleCommitCoordinator(reopened).commit_effect(stale).status is (
        CoreCommitStatus.DENIED
    )
    assert reopened.snapshot() == before


def test_quarantine_position_sltp_preserves_ambiguous_orders_and_reopens(tmp_path):
    path = tmp_path / "quarantine-exit.sqlite"
    config = EngineConfig(
        one_active_order=False,
        allow_scale_in=True,
        close_open_position_at_end=False,
        execution=ExecutionConfig(max_fill_fraction_per_bar=0.5),
    )
    port, binding, writer, refs = _store(path, config=config)
    coordinator = CoreLifecycleCommitCoordinator(port)
    first_entry = {
        "operation": "submit_order",
        "request": {
            "side": "buy",
            "order_type": "market",
            "quantity": 2.0,
            "stop_loss": 90.0,
            "take_profit": 120.0,
            "client_order_id": "position-entry-remainder",
        },
        "source_module": "test-strategy",
        "created_at": _NOW.isoformat(),
        "current_index": 0,
        "mark_price": 100.0,
    }
    assert coordinator.commit_effect(
        _custom_effect(
            binding,
            writer,
            refs,
            "submit-position-entry",
            first_entry,
            ("position-entry-remainder",),
        )
    ).status is CoreCommitStatus.COMMITTED
    entry_bar = {
        "timestamp": _NOW.isoformat(),
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 10.0,
        "symbol": "BTCUSDT",
        "timeframe": "1m",
    }
    assert coordinator.commit_effect(
        _custom_effect(
            binding,
            writer,
            refs,
            "partial-position-entry-fill",
            {"operation": "process_bar", "bar": entry_bar, "index": 1},
        )
    ).status is CoreCommitStatus.COMMITTED
    second_entry = {
        "operation": "submit_order",
        "request": {
            "side": "buy",
            "order_type": "limit",
            "quantity": 1.0,
            "price": 90.0,
            "stop_loss": 80.0,
            "take_profit": 130.0,
            "client_order_id": "ambiguous-scale-in",
        },
        "source_module": "test-scale-in",
        "created_at": _NOW.isoformat(),
        "current_index": 1,
        "mark_price": 100.0,
    }
    assert coordinator.commit_effect(
        _custom_effect(
            port.snapshot().lifecycle,
            writer,
            refs,
            "submit-ambiguous-scale-in",
            second_entry,
            ("ambiguous-scale-in",),
        )
    ).status is CoreCommitStatus.COMMITTED
    assert coordinator.commit_transition(
        _suspend(port.snapshot().lifecycle, writer, refs)
    ).status is CoreCommitStatus.COMMITTED
    suspended = port.snapshot()
    assert suspended.events[-1].payload["reconciliation_required"] is True
    before_orders = suspended.broker_state.orders

    target_bar = {
        **entry_bar,
        "timestamp": datetime(2026, 10, 25, 12, 1, tzinfo=UTC).isoformat(),
        "high": 121.0,
        "close": 119.0,
    }
    exit_effect = _custom_effect(
        suspended.lifecycle,
        writer,
        refs,
        "quarantine-position-take-profit",
        {"operation": "process_bar", "bar": target_bar, "index": 2},
    )
    exited = coordinator.commit_effect(exit_effect)
    assert exited.status is CoreCommitStatus.COMMITTED
    assert exited.receipt is not None
    final = port.snapshot()
    assert final.broker_state.position is None
    assert final.broker_state.orders == before_orders
    assert final.broker_state.pending_order_ids == suspended.broker_state.pending_order_ids

    reopened, _, _, _ = _store(path, config=config)
    assert reopened.snapshot() == final
    replay = CoreLifecycleCommitCoordinator(reopened).commit_effect(exit_effect)
    assert replay.status is CoreCommitStatus.COMMITTED
    assert replay.replayed is True
    assert replay.receipt == exited.receipt
    assert reopened.snapshot() == final


def test_quarantine_sltp_preserves_pending_close_and_reduce_order(tmp_path):
    path = tmp_path / "quarantine-close-reducer.sqlite"
    config = EngineConfig(one_active_order=False, close_open_position_at_end=False)
    port, binding, writer, refs = _store(path, config=config)
    coordinator = CoreLifecycleCommitCoordinator(port)
    assert coordinator.commit_effect(_effect(binding, writer, refs)).status is (
        CoreCommitStatus.COMMITTED
    )
    entry_bar = {
        "timestamp": _NOW.isoformat(),
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 10.0,
        "symbol": "BTCUSDT",
        "timeframe": "1m",
    }
    assert coordinator.commit_effect(
        _custom_effect(
            binding,
            writer,
            refs,
            "fill-position-before-ambiguous-close",
            {"operation": "process_bar", "bar": entry_bar, "index": 1},
        )
    ).status is CoreCommitStatus.COMMITTED
    before_ambiguous = port.snapshot()
    position_id = before_ambiguous.broker_state.position["id"]
    assert coordinator.commit_effect(
        _custom_effect(
            before_ambiguous.lifecycle,
            writer,
            refs,
            "schedule-ambiguous-close",
            {
                "operation": "schedule_close",
                "request": {"fraction": 0.5, "reason": "unresolved_operator_close"},
                "source_module": "test-operator",
                "timestamp": _NOW.isoformat(),
                "current_index": 1,
            },
            (position_id,),
        )
    ).status is CoreCommitStatus.COMMITTED
    assert coordinator.commit_effect(
        _custom_effect(
            port.snapshot().lifecycle,
            writer,
            refs,
            "submit-ambiguous-reducer",
            {
                "operation": "submit_order",
                "request": {
                    "side": "sell",
                    "order_type": "market",
                    "quantity": 0.25,
                    "reduce_only": True,
                    "position_side": "long",
                    "client_order_id": "unresolved-reducer",
                },
                "source_module": "test-reducer",
                "created_at": _NOW.isoformat(),
                "current_index": 1,
                "mark_price": 100.0,
            },
            ("unresolved-reducer",),
        )
    ).status is CoreCommitStatus.COMMITTED

    transition = _suspend(port.snapshot().lifecycle, writer, refs)
    assert coordinator.commit_transition(transition).status is CoreCommitStatus.COMMITTED
    # Exercise the old markerless SUSPEND format. The first Core quarantine
    # effect must persist the inferred disposition for future reopen/replay.
    connection = sqlite3.connect(path)
    try:
        sequence, encoded = connection.execute(
            "SELECT sequence,event_json FROM core_lifecycle_events "
            "WHERE event_json LIKE '%core.lifecycle.suspended%' ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        legacy_event = json.loads(encoded)
        legacy_event["payload"].pop("reconciliation_required")
        legacy_encoded = json.dumps(legacy_event, sort_keys=True, separators=(",", ":"))
        connection.execute(
            "UPDATE core_lifecycle_events SET event_json=?,event_sha256=? WHERE sequence=?",
            (legacy_encoded, hashlib.sha256(legacy_encoded.encode()).hexdigest(), sequence),
        )
        connection.commit()
    finally:
        connection.close()
    quarantined = port.snapshot()
    assert "reconciliation_required" not in quarantined.events[-1].payload
    orders_before = quarantined.broker_state.orders
    pending_close_before = quarantined.broker_state.pending_close
    assert pending_close_before is not None

    reopened, _, _, _ = _store(path, config=config)
    assert reopened.snapshot() == quarantined
    exit_bar = {
        **entry_bar,
        "timestamp": datetime(2026, 10, 25, 12, 1, tzinfo=UTC).isoformat(),
        "high": 121.0,
        "close": 119.0,
    }
    exit_effect = _custom_effect(
        quarantined.lifecycle,
        writer,
        refs,
        "quarantine-sltp-with-ambiguous-close",
        {"operation": "process_bar", "bar": exit_bar, "index": 2},
    )
    assert CoreLifecycleCommitCoordinator(reopened).commit_effect(exit_effect).status is (
        CoreCommitStatus.COMMITTED
    )
    final = reopened.snapshot()
    effect_marker = next(
        event
        for event in reversed(final.events)
        if event.event_type == "core.effect.committed"
        and event.payload["request_id"] == exit_effect.effect_id
    )
    assert effect_marker.payload["state_validation_mode"] == "quarantine"
    assert final.broker_state.position is None
    assert final.broker_state.orders == orders_before
    assert final.broker_state.pending_close == pending_close_before
    assert final.broker_state.pending_order_ids == quarantined.broker_state.pending_order_ids

    after_commit_sequence = final.commit_sequence
    after_event_count = len(final.events)
    reopened_again, _, _, _ = _store(path, config=config)
    assert reopened_again.snapshot() == final
    replay = CoreLifecycleCommitCoordinator(reopened_again).commit_effect(exit_effect)
    assert replay.status is CoreCommitStatus.COMMITTED
    assert replay.replayed is True
    assert replay.receipt == next(
        item.receipt
        for item in final.saved_receipts
        if item.request_id == exit_effect.effect_id
    )
    assert reopened_again.snapshot().commit_sequence == after_commit_sequence
    assert len(reopened_again.snapshot().events) == after_event_count


@pytest.mark.parametrize("tamper", ["unknown-mode", "unlinked-marker"])
def test_reopen_rejects_invalid_quarantine_effect_marker(tmp_path, tamper):
    path = tmp_path / f"bad-quarantine-marker-{tamper}.sqlite"
    config = EngineConfig(one_active_order=False, close_open_position_at_end=False)
    port, binding, writer, refs = _store(path, config=config)
    coordinator = CoreLifecycleCommitCoordinator(port)
    assert coordinator.commit_effect(_effect(binding, writer, refs)).status is (
        CoreCommitStatus.COMMITTED
    )
    entry_bar = {
        "timestamp": _NOW.isoformat(),
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 10.0,
        "symbol": "BTCUSDT",
        "timeframe": "1m",
    }
    assert coordinator.commit_effect(
        _custom_effect(
            port.snapshot().lifecycle,
            writer,
            refs,
            "marker-test-fill-position",
            {"operation": "process_bar", "bar": entry_bar, "index": 1},
        )
    ).status is CoreCommitStatus.COMMITTED
    current = port.snapshot()
    assert coordinator.commit_effect(
        _custom_effect(
            current.lifecycle,
            writer,
            refs,
            "marker-test-pending-close",
            {
                "operation": "schedule_close",
                "request": {"fraction": 0.5, "reason": "unresolved"},
                "source_module": "test-operator",
                "timestamp": _NOW.isoformat(),
                "current_index": 1,
            },
            (current.broker_state.position["id"],),
        )
    ).status is CoreCommitStatus.COMMITTED
    transition = _suspend(port.snapshot().lifecycle, writer, refs)
    assert coordinator.commit_transition(transition).status is CoreCommitStatus.COMMITTED
    bar = {
        "timestamp": datetime(2026, 10, 25, 12, 1, tzinfo=UTC).isoformat(),
        "open": 100.0,
        "high": 121.0,
        "low": 99.0,
        "close": 119.0,
        "volume": 10.0,
        "symbol": "BTCUSDT",
        "timeframe": "1m",
    }
    effect = _custom_effect(
        port.snapshot().lifecycle,
        writer,
        refs,
        "quarantine-marker-corruption",
        {"operation": "process_bar", "bar": bar, "index": 2},
    )
    assert coordinator.commit_effect(effect).status is CoreCommitStatus.COMMITTED
    connection = sqlite3.connect(path)
    try:
        sequence, encoded = connection.execute(
            "SELECT sequence,event_json FROM core_lifecycle_events "
            "WHERE event_json LIKE '%core.effect.committed%' ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        event = json.loads(encoded)
        if tamper == "unknown-mode":
            event["payload"]["state_validation_mode"] = "future-mode"
        else:
            event["payload"]["request_id"] = "unlinked-request"
        changed = json.dumps(event, sort_keys=True, separators=(",", ":"))
        connection.execute(
            "UPDATE core_lifecycle_events SET event_json=?,event_sha256=? WHERE sequence=?",
            (changed, hashlib.sha256(changed.encode()).hexdigest(), sequence),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ValueError):
        _store(path, config=config)


@pytest.mark.parametrize("expiry_mode", ["ioc", "gtd"])
def test_closed_fence_bar_blocks_losing_reduce_only_protection_with_open_position(
    tmp_path, expiry_mode
):
    port, binding, writer, refs = _store(tmp_path / f"{expiry_mode}.sqlite")
    coordinator = CoreLifecycleCommitCoordinator(port)
    assert coordinator.commit_effect(_effect(binding, writer, refs)).status is (
        CoreCommitStatus.COMMITTED
    )
    entry_bar = {
        "timestamp": _NOW.isoformat(),
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 10.0,
        "symbol": "BTCUSDT",
        "timeframe": "1m",
    }
    assert coordinator.commit_effect(
        _custom_effect(
            binding,
            writer,
            refs,
            "fill-entry-before-protection-expiry",
            {"operation": "process_bar", "bar": entry_bar, "index": 1},
        )
    ).status is CoreCommitStatus.COMMITTED
    assert coordinator.commit_transition(_suspend(binding, writer, refs)).status is (
        CoreCommitStatus.COMMITTED
    )
    suspended = port.snapshot().lifecycle
    protection_request = {
        "side": "sell",
        "order_type": "limit",
        "quantity": 0.5,
        "price": 120.0,
        "reduce_only": True,
        "position_side": "long",
        "client_order_id": f"protect-{expiry_mode}",
        "time_in_force": "ioc" if expiry_mode == "ioc" else "gtd",
    }
    if expiry_mode == "gtd":
        protection_request["good_till_time"] = datetime(
            2026, 10, 25, 12, 1, tzinfo=UTC
        ).isoformat()
    assert coordinator.commit_effect(
        _custom_effect(
            suspended,
            writer,
            refs,
            f"submit-protection-{expiry_mode}",
            {
                "operation": "submit_order",
                "request": protection_request,
                "source_module": "test-protection",
                "created_at": _NOW.isoformat(),
                "current_index": 1,
                "mark_price": 100.0,
            },
            (f"protect-{expiry_mode}",),
        )
    ).status is CoreCommitStatus.COMMITTED

    before = port.snapshot()
    expiry_bar = {
        **entry_bar,
        "timestamp": datetime(2026, 10, 25, 12, 1, tzinfo=UTC).isoformat(),
    }
    denied = coordinator.commit_effect(
        _custom_effect(
            before.lifecycle,
            writer,
            refs,
            f"blocked-protection-{expiry_mode}",
            {"operation": "process_bar", "bar": expiry_bar, "index": 2},
        )
    )
    assert denied.status is CoreCommitStatus.DENIED
    assert "risk fence" in denied.reason
    assert port.snapshot() == before


def test_failed_event_insert_rolls_back_snapshot_and_request_receipt(tmp_path):
    path = tmp_path / "core.sqlite"
    port, binding, writer, refs = _store(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TRIGGER reject_core_event BEFORE INSERT ON core_lifecycle_events "
            "BEGIN SELECT RAISE(ABORT, 'injected event failure'); END"
        )
        connection.commit()
    finally:
        connection.close()

    result = CoreLifecycleCommitCoordinator(port).commit_effect(_effect(binding, writer, refs))
    assert result.status is CoreCommitStatus.UNAVAILABLE
    snapshot = port.snapshot()
    assert snapshot.commit_sequence == 0
    assert snapshot.broker_state.orders == ()
    assert snapshot.events == ()


def test_two_connections_serialize_effect_and_suspend(tmp_path):
    path = tmp_path / "core.sqlite"
    port_a, binding, writer, refs = _store(path)
    port_b, _, _, _ = _store(path)
    barrier = threading.Barrier(2)

    def run_effect():
        barrier.wait(timeout=5)
        return CoreLifecycleCommitCoordinator(port_a).commit_effect(
            _effect(binding, writer, refs, effect_id="racing-effect")
        )

    def run_suspend():
        barrier.wait(timeout=5)
        return CoreLifecycleCommitCoordinator(port_b).commit_transition(
            _suspend(binding, writer, refs, transition_id="racing-suspend")
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        effect_future = pool.submit(run_effect)
        suspend_future = pool.submit(run_suspend)
        effect_result = effect_future.result(timeout=10)
        suspend_result = suspend_future.result(timeout=10)

    assert suspend_result.status is CoreCommitStatus.COMMITTED
    assert effect_result.status in {CoreCommitStatus.COMMITTED, CoreCommitStatus.DENIED}
    snapshot = port_a.snapshot()
    assert snapshot.risk_enabled is False
    assert snapshot.lifecycle.expected_generation == 1
    assert snapshot.events[-1].event_type == "core.lifecycle.suspended"
    if effect_result.status is CoreCommitStatus.COMMITTED:
        assert snapshot.commit_sequence == 2
        assert [event.event_type for event in snapshot.events] == [
            "order.submitted",
            "core.effect.committed",
            "core.lifecycle.suspended",
        ]
    else:
        assert snapshot.commit_sequence == 1
        assert [event.event_type for event in snapshot.events] == [
            "core.lifecycle.suspended",
        ]


def test_commit_ack_loss_is_unresolved_then_same_id_replays(tmp_path):
    port, binding, writer, refs = _store(tmp_path / "core.sqlite")
    request = _effect(binding, writer, refs)

    class CommitThenLoseAck:
        def __init__(self, inner):
            self._inner = inner

        def check_and_stage_effect(self, value):
            return self._inner.check_and_stage_effect(value)

        def check_and_stage_transition(self, value):
            return self._inner.check_and_stage_transition(value)

        def commit(self):
            self._inner.commit()
            raise OSError("injected acknowledgement loss")

        def rollback(self):
            self._inner.rollback()

    class CommitThenLoseAckPort:
        def begin_lifecycle_transaction(self):
            return CommitThenLoseAck(port.begin_lifecycle_transaction())

    unresolved = CoreLifecycleCommitCoordinator(CommitThenLoseAckPort()).commit_effect(request)
    replay = CoreLifecycleCommitCoordinator(port).commit_effect(request)

    assert unresolved.status is CoreCommitStatus.UNRESOLVED
    assert unresolved.receipt is None
    assert replay.status is CoreCommitStatus.COMMITTED
    assert replay.replayed is True
    assert port.snapshot().commit_sequence == 1
    assert len(port.snapshot().events) == 2


def test_quiet_bar_commits_progress_even_without_broker_events(tmp_path):
    port, binding, writer, refs = _store(tmp_path / "core.sqlite")
    bar = {
        "timestamp": _NOW.isoformat(),
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 10.0,
        "symbol": "BTCUSDT",
        "timeframe": "1m",
    }
    request = _custom_effect(
        binding,
        writer,
        refs,
        "quiet-bar",
        {"operation": "process_bar", "bar": bar, "index": 0},
    )

    result = CoreLifecycleCommitCoordinator(port).commit_effect(request)

    assert result.status is CoreCommitStatus.COMMITTED
    snapshot = port.snapshot()
    assert snapshot.broker_state.last_processed_bar["index"] == 0
    assert len(snapshot.events) == 1
    marker = snapshot.events[0]
    assert marker.event_type == "core.effect.committed"
    assert marker.payload["broker_event_count"] == 0
    assert marker.payload["broker_event_start"] is None
    assert marker.payload["broker_event_end"] is None


def test_reopen_rejects_committed_state_after_canonical_event_tail_is_deleted(tmp_path):
    path = tmp_path / "core.sqlite"
    port, binding, writer, refs = _store(path)
    assert (
        CoreLifecycleCommitCoordinator(port).commit_effect(_effect(binding, writer, refs)).status
        is CoreCommitStatus.COMMITTED
    )
    connection = sqlite3.connect(path)
    try:
        connection.execute("DELETE FROM core_lifecycle_events")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ValueError, match="canonical event marker"):
        _store(path)


def test_historical_protection_and_partial_exit_work_after_suspend(tmp_path):
    port, binding, writer, refs = _store(tmp_path / "core.sqlite")
    coordinator = CoreLifecycleCommitCoordinator(port)
    assert (
        coordinator.commit_effect(_effect(binding, writer, refs)).status
        is CoreCommitStatus.COMMITTED
    )
    entry_bar = {
        "timestamp": _NOW.isoformat(),
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 10.0,
        "symbol": "BTCUSDT",
        "timeframe": "1m",
    }
    assert (
        coordinator.commit_effect(
            _custom_effect(
                binding,
                writer,
                refs,
                "fill-entry",
                {"operation": "process_bar", "bar": entry_bar, "index": 1},
            )
        ).status
        is CoreCommitStatus.COMMITTED
    )
    protective = {
        "operation": "submit_order",
        "request": {
            "side": "sell",
            "order_type": "stop_market",
            "close_position": True,
            "reduce_only": True,
            "trigger_price": 95.0,
            "position_side": "long",
            "client_order_id": "protect-1",
        },
        "source_module": "test-strategy",
        "created_at": _NOW.isoformat(),
        "current_index": 1,
        "mark_price": 100.0,
    }
    assert (
        coordinator.commit_effect(
            _custom_effect(binding, writer, refs, "add-protection", protective, ("protect-1",))
        ).status
        is CoreCommitStatus.COMMITTED
    )
    cancel = {
        "operation": "cancel_order",
        "request": {"client_order_id": "protect-1"},
        "source_module": "test-strategy",
        "timestamp": _NOW.isoformat(),
    }
    assert (
        coordinator.commit_effect(
            _custom_effect(binding, writer, refs, "cancel-protection", cancel, ("protect-1",))
        ).status
        is CoreCommitStatus.COMMITTED
    )
    assert (
        coordinator.commit_transition(_suspend(binding, writer, refs)).status
        is CoreCommitStatus.COMMITTED
    )

    after_suspend = port.snapshot().lifecycle
    position_id = port.snapshot().broker_state.position["id"]
    scheduled = coordinator.commit_effect(
        _custom_effect(
            after_suspend,
            writer,
            refs,
            "schedule-partial-close",
            {
                "operation": "schedule_close",
                "request": {"fraction": 0.5, "reason": "operator_reduce"},
                "source_module": "test-operator",
                "timestamp": _NOW.isoformat(),
                "current_index": 1,
            },
            (position_id,),
        )
    )
    assert scheduled.status is CoreCommitStatus.COMMITTED
    after_schedule = port.snapshot().lifecycle
    quiet_bar = {**entry_bar, "timestamp": datetime(2026, 10, 25, 12, 1, tzinfo=UTC).isoformat()}
    continued = coordinator.commit_effect(
        _custom_effect(
            after_schedule,
            writer,
            refs,
            "quiet-after-suspend",
            {"operation": "process_bar", "bar": quiet_bar, "index": 2},
        )
    )
    assert continued.status is CoreCommitStatus.COMMITTED
    final = port.snapshot()
    assert final.risk_enabled is False
    assert final.broker_state.position is not None
    assert final.broker_state.position["quantity"] == 0.5


def test_filled_reduce_only_order_can_reduce_position_after_suspend(tmp_path):
    port, binding, writer, refs = _store(tmp_path / "core.sqlite")
    coordinator = CoreLifecycleCommitCoordinator(port)
    entry_payload = {
        "operation": "submit_order",
        "request": {
            "side": "buy",
            "order_type": "market",
            "quantity": 2.0,
            "stop_loss": 90.0,
            "take_profit": 120.0,
            "client_order_id": "entry-2",
        },
        "source_module": "test-strategy",
        "created_at": _NOW.isoformat(),
        "current_index": 0,
        "mark_price": 100.0,
    }
    assert (
        coordinator.commit_effect(
            _custom_effect(binding, writer, refs, "submit-two", entry_payload, ("entry-2",))
        ).status
        is CoreCommitStatus.COMMITTED
    )
    first_bar = {
        "timestamp": _NOW.isoformat(),
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 10.0,
        "symbol": "BTCUSDT",
        "timeframe": "1m",
    }
    assert (
        coordinator.commit_effect(
            _custom_effect(
                binding,
                writer,
                refs,
                "fill-two",
                {"operation": "process_bar", "bar": first_bar, "index": 1},
            )
        ).status
        is CoreCommitStatus.COMMITTED
    )
    assert (
        coordinator.commit_transition(_suspend(binding, writer, refs)).status
        is CoreCommitStatus.COMMITTED
    )

    suspended_binding = port.snapshot().lifecycle
    reduce_order = {
        "operation": "submit_order",
        "request": {
            "side": "sell",
            "order_type": "market",
            "quantity": 1.0,
            "reduce_only": True,
            "position_side": "long",
            "client_order_id": "reduce-1",
        },
        "source_module": "test-reducer",
        "created_at": datetime(2026, 10, 25, 12, 1, tzinfo=UTC).isoformat(),
        "current_index": 1,
        "mark_price": 100.0,
    }
    assert (
        coordinator.commit_effect(
            _custom_effect(
                suspended_binding,
                writer,
                refs,
                "submit-reducer",
                reduce_order,
                ("reduce-1",),
            )
        ).status
        is CoreCommitStatus.COMMITTED
    )
    second_bar = {
        **first_bar,
        "timestamp": datetime(2026, 10, 25, 12, 1, tzinfo=UTC).isoformat(),
    }
    reduced = coordinator.commit_effect(
        _custom_effect(
            port.snapshot().lifecycle,
            writer,
            refs,
            "fill-reducer",
            {"operation": "process_bar", "bar": second_bar, "index": 2},
        )
    )

    assert reduced.status is CoreCommitStatus.COMMITTED
    final = port.snapshot()
    assert final.risk_enabled is False
    assert final.broker_state.position is not None
    assert final.broker_state.position["quantity"] == 1.0
    reduce_order_state = next(
        order for order in final.broker_state.orders if order["client_order_id"] == "reduce-1"
    )
    assert reduce_order_state["status"] == "filled"


def test_store_without_authority_references_fails_closed(tmp_path):
    port, binding, writer, refs = _store(
        tmp_path / "core.sqlite", risk_enabled=False, authorities=False
    )
    assert refs == ()
    result = CoreLifecycleCommitCoordinator(port).commit_effect(_effect(binding, writer, refs))
    assert result.status is CoreCommitStatus.DENIED
    assert port.snapshot().commit_sequence == 0


def test_resume_and_stale_generation_remain_closed(tmp_path):
    port, binding, writer, refs = _store(tmp_path / "core.sqlite")
    coordinator = CoreLifecycleCommitCoordinator(port)
    suspend = _suspend(binding, writer, refs)
    assert coordinator.commit_transition(suspend).status is CoreCommitStatus.COMMITTED

    stale_effect = _effect(binding, writer, refs, effect_id="stale-effect")
    assert coordinator.commit_effect(stale_effect).status is CoreCommitStatus.DENIED

    encoded = _canonical({"action": "RESUME"})
    resume = CoreFenceTransition(
        "resume-1",
        port.snapshot().lifecycle,
        writer,
        2,
        CoreLifecycleAction.RESUME,
        hashlib.sha256(encoded).hexdigest(),
        encoded,
        refs,
    )
    result = coordinator.commit_transition(resume)
    assert result.status is CoreCommitStatus.DENIED
    assert port.snapshot().risk_enabled is False
    assert port.snapshot().lifecycle.expected_generation == 1


def test_effect_lookup_matches_historical_receipt_after_generation_advances(tmp_path):
    port, binding, writer, refs = _store(tmp_path / "core.sqlite")
    coordinator = CoreLifecycleCommitCoordinator(port)
    request = _effect(binding, writer, refs)
    committed = coordinator.commit_effect(request)
    assert committed.status is CoreCommitStatus.COMMITTED
    assert committed.receipt is not None

    suspended = coordinator.commit_transition(_suspend(binding, writer, refs))
    assert suspended.status is CoreCommitStatus.COMMITTED
    assert port.snapshot().lifecycle.expected_generation == 1

    evidence = port.lookup_effect(request)
    assert evidence.status is CoreEffectLookupStatus.MATCHED
    assert evidence.receipt == committed.receipt
    assert evidence.observed_commit_sequence == 2
    assert evidence.observed_event_sequence == len(port.snapshot().events)


def test_effect_lookup_absence_is_scoped_and_read_only(tmp_path):
    path = tmp_path / "core.sqlite"
    port, binding, writer, refs = _store(path)
    request = _effect(binding, writer, refs)
    connection = sqlite3.connect(path)
    before = connection.execute(
        "SELECT commit_sequence FROM core_lifecycle_instance"
    ).fetchone()[0]

    evidence = port.lookup_effect(request)

    after = connection.execute("SELECT commit_sequence FROM core_lifecycle_instance").fetchone()[0]
    event_count = connection.execute("SELECT COUNT(*) FROM core_lifecycle_events").fetchone()[0]
    connection.close()
    assert evidence.status is CoreEffectLookupStatus.ABSENT_AT_READ
    assert evidence.observed_commit_sequence == before == after == 0
    assert evidence.observed_event_sequence == event_count == 0


def test_effect_lookup_foreign_aggregate_is_conflict_and_old_writer_absence_is_bounded(tmp_path):
    port, binding, writer, refs = _store(tmp_path / "core.sqlite")
    other_writer = CoreWriterIdentity("core-owner-2", 2)
    absent_from_old_epoch = _effect(
        binding, other_writer, refs, effect_id="absent-old-epoch"
    )
    before = port.snapshot()

    absent = port.lookup_effect(absent_from_old_epoch)
    foreign_binding = CoreLifecycleExpectedGeneration(
        "foreign-instance", binding.incarnation_id, binding.package_id,
        binding.package_digest, binding.expected_generation,
    )
    foreign = _effect(foreign_binding, writer, refs, effect_id="foreign-effect")
    conflict = port.lookup_effect(foreign)

    assert absent.status is CoreEffectLookupStatus.ABSENT_AT_READ
    assert absent.observed_commit_sequence == before.commit_sequence
    assert absent.observed_event_sequence == (before.events[-1].sequence if before.events else 0)
    assert conflict.status is CoreEffectLookupStatus.CONFLICT
    assert conflict.observed_commit_sequence == before.commit_sequence
    assert conflict.observed_event_sequence == (before.events[-1].sequence if before.events else 0)


def test_effect_lookup_matches_historical_receipt_after_writer_epoch_changes(tmp_path):
    path = tmp_path / "core.sqlite"
    port, binding, writer, refs = _store(path)
    request = _effect(binding, writer, refs)
    committed = CoreLifecycleCommitCoordinator(port).commit_effect(request)
    assert committed.status is CoreCommitStatus.COMMITTED
    port.close()

    new_writer = CoreWriterIdentity("core-owner-2", 2)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE core_lifecycle_instance SET writer_owner_id=?,writer_epoch=? WHERE singleton=1",
            (new_writer.owner_id, new_writer.epoch),
        )
    reopened, _, _, _ = _store(path, writer=new_writer)

    evidence = reopened.lookup_effect(request)

    assert reopened.snapshot().writer == new_writer
    assert evidence.status is CoreEffectLookupStatus.MATCHED
    assert evidence.receipt == committed.receipt


def test_effect_lookup_classifies_same_id_different_request_as_conflict(tmp_path):
    port, binding, writer, refs = _store(tmp_path / "core.sqlite")
    original = _effect(binding, writer, refs)
    assert CoreLifecycleCommitCoordinator(port).commit_effect(original).status is CoreCommitStatus.COMMITTED
    changed_payload = {
        "operation": "submit_order",
        "request": {
            "side": "buy", "order_type": "market", "quantity": 2.0,
            "stop_loss": 90.0, "take_profit": 120.0, "client_order_id": "entry-1",
        },
        "source_module": "test-strategy",
        "created_at": _NOW.isoformat(),
        "current_index": 0,
        "mark_price": 100.0,
    }
    conflicting = _custom_effect(binding, writer, refs, original.effect_id, changed_payload, ("entry-1",))

    evidence = port.lookup_effect(conflicting)

    assert evidence.status is CoreEffectLookupStatus.CONFLICT
    assert evidence.receipt is None


def test_effect_lookup_reports_corrupt_or_closed_store_as_unavailable(tmp_path):
    corrupt_path = tmp_path / "corrupt.sqlite"
    corrupt_port, binding, writer, refs = _store(corrupt_path)
    request = _effect(binding, writer, refs)
    assert CoreLifecycleCommitCoordinator(corrupt_port).commit_effect(request).status is CoreCommitStatus.COMMITTED
    connection = sqlite3.connect(corrupt_path)
    connection.execute("UPDATE core_lifecycle_requests SET receipt_sha256=?", ("0" * 64,))
    connection.commit()
    connection.close()
    assert corrupt_port.lookup_effect(request).status is CoreEffectLookupStatus.UNAVAILABLE

    closed_port, closed_binding, closed_writer, closed_refs = _store(tmp_path / "closed.sqlite")
    closed_request = _effect(closed_binding, closed_writer, closed_refs)
    closed_port.close()
    assert closed_port.lookup_effect(closed_request).status is CoreEffectLookupStatus.UNAVAILABLE
