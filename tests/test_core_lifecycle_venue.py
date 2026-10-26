from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from modular_core import (
    Candle,
    CancelRequest,
    ClosePositionRequest,
    CoreAuthorityReference,
    CoreCommitStatus,
    CoreEffectLookupStatus,
    CoreFenceTransition,
    CoreLifecycleAction,
    CoreLifecycleCommitCoordinator,
    CoreLifecycleCommitResult,
    CoreLifecycleEffect,
    CoreLifecycleExpectedGeneration,
    CoreWriterIdentity,
    EngineConfig,
    InstrumentSpec,
    Journal,
    JsonLinesEventStore,
    OrderRequest,
    OrderType,
    PositionUpdateRequest,
    Side,
)
from modular_core.execution import BrokerSimulator
from modular_core.lifecycle_sqlite import SQLiteCoreLifecycleCommitPort
from modular_core.lifecycle_venue import (
    CorePaperCommittedProjectionUnavailable,
    CorePaperContextUnavailable,
    CoreInvocationRef,
    CorePaperEffectNotCommitted,
    SQLiteCorePaperVenue,
    canonical_core_paper_payload,
    prepare_local_core_paper_binding,
)
from modular_core.serialization import to_jsonable
from paper_execution.core_attempt_evidence import (
    PaperAttemptInspectionStatus,
    PaperIntentCorrespondence,
    inspect_pending_attempts,
    persist_pending_attempt_link,
)
from paper_execution.journal import ExecutionEventJournal

NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)
DIGEST = hashlib.sha256(b"synthetic local bridge fixture").hexdigest()


@pytest.fixture(autouse=True)
def _local_caller_packages(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "modules/paper_execution/src"))
    monkeypatch.syspath_prepend(str(root / "applications/trading_runner/src"))


class SavedEffects:
    """Synthetic trusted source with a real temporary durable request database."""

    def __init__(self, path):
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS effects(id TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS bar_effects(event_id TEXT PRIMARY KEY, "
                "payload_hash TEXT NOT NULL, event_index INTEGER NOT NULL, effect_id TEXT NOT NULL)"
            )

    def save_bar_reference(self, event, index, effect_id):
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO bar_effects VALUES(?,?,?,?)",
                (event.event_id, event.payload_hash, index, effect_id),
            )

    def lookup_bar_effect_id(self, *, event_id, payload_hash, index):
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT payload_hash,event_index,effect_id FROM bar_effects WHERE event_id=?",
                (event_id,),
            ).fetchone()
        if row is None or row[:2] != (payload_hash, index):
            raise CorePaperContextUnavailable("no exact saved bar effect mapping")
        return row[2]

    def save(self, request):
        value = {
            "effect_id": request.effect_id,
            "lifecycle": to_jsonable(request.lifecycle),
            "writer": to_jsonable(request.writer),
            "operation": request.operation,
            "canonical_payload_digest": request.canonical_payload_digest,
            "canonical_payload": request.canonical_payload.decode(),
            "target_ids": request.target_ids,
            "authority_refs": to_jsonable(request.authority_refs),
        }
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO effects VALUES(?,?)", (request.effect_id, json.dumps(value)))

    def load_effect(self, effect_id):
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT value FROM effects WHERE id=?", (effect_id,)).fetchone()
        if row is None:
            raise CorePaperContextUnavailable("synthetic saved effect is absent")
        value = json.loads(row[0])
        return CoreLifecycleEffect(
            value["effect_id"],
            CoreLifecycleExpectedGeneration(**value["lifecycle"]),
            CoreWriterIdentity(**value["writer"]),
            value["operation"],
            value["canonical_payload_digest"],
            value["canonical_payload"].encode(),
            tuple(value["target_ids"]),
            tuple(CoreAuthorityReference(**item) for item in value["authority_refs"]),
        )


def store(path, *, authorities=True):
    binding = CoreLifecycleExpectedGeneration("instance", "incarnation", "package", DIGEST, 0)
    writer = CoreWriterIdentity("synthetic-owner", 1)
    refs = (
        (CoreAuthorityReference("synthetic-admission", "fixture", DIGEST),) if authorities else ()
    )
    port = SQLiteCoreLifecycleCommitPort(
        path,
        binding=binding,
        writer=writer,
        initial_broker=BrokerSimulator(
            EngineConfig(close_open_position_at_end=False),
            Journal(),
            InstrumentSpec("BTCUSDT"),
        ),
        trusted_authority_refs=refs,
        initial_risk_enabled=authorities,
        clock=lambda: NOW,
    )
    return port, binding, writer, refs


def save_effect(source, binding, writer, refs, effect_id, operation, targets=(), **fields):
    payload = canonical_core_paper_payload(operation, **fields)
    request = CoreLifecycleEffect(
        effect_id,
        binding,
        writer,
        operation,
        hashlib.sha256(payload).hexdigest(),
        payload,
        targets,
        refs,
    )
    source.save(request)
    return request


def order(client_id, *, quantity=2.0, reduce_only=False):
    return OrderRequest(
        Side.SELL if reduce_only else Side.BUY,
        OrderType.MARKET,
        quantity=quantity,
        stop_loss=None if reduce_only else 90.0,
        take_profit=None if reduce_only else 120.0,
        client_order_id=client_id,
        reduce_only=reduce_only,
    )


def bar(minute=0):
    return Candle(NOW + timedelta(minutes=minute), 100.0, 101.0, 99.0, 100.0, 10.0, "BTCUSDT", "1m")


def test_all_supported_venue_mutations_use_ledger_and_detached_projections(tmp_path):
    port, binding, writer, refs = store(tmp_path / "core.sqlite")
    source = SavedEffects(tmp_path / "effects.sqlite")
    venue = SQLiteCorePaperVenue(port, effect_source=source)

    def bound(effect_id, operation, targets=(), **fields):
        save_effect(source, binding, writer, refs, effect_id, operation, targets, **fields)
        return venue.for_effect(effect_id)

    def submit(client_id, *, timestamp=NOW, index=0, quantity=2.0, reduce_only=False):
        request = order(client_id, quantity=quantity, reduce_only=reduce_only)
        fields = dict(
            request=request,
            source_module="synthetic-paper",
            created_at=timestamp,
            current_index=index,
            mark_price=100.0,
        )
        capability = bound("submit:" + client_id, "submit_order", (client_id,), **fields)
        invocation = capability.invocation_ref("submit_order", **fields)
        result = capability.submit(**fields)
        receipt = capability.receipt_for_invocation(invocation)
        assert receipt is not None
        assert receipt.matches_request(source.load_effect(invocation.effect_id))
        commit_sequence = port.snapshot().commit_sequence
        replayed = capability.submit(**fields)
        assert replayed.id == result.id
        assert capability.receipt_for_invocation(invocation) == receipt
        assert port.snapshot().commit_sequence == commit_sequence
        return result

    result = submit("entry")
    result.quantity = 999.0
    assert venue.pending_snapshots()[0].quantity == 2.0
    bound("bar:1", "process_bar", bar=bar(), index=1).process_bar(bar(), 1)
    position = venue.position_snapshot(100.0)
    assert position is not None and position.quantity == 2.0

    update = dict(
        request=PositionUpdateRequest(95.0, 120.0),
        timestamp=NOW,
        source_module="synthetic-repair",
        mark_price=100.0,
    )
    assert bound("repair:1", "update_position", (position.id,), **update).update_position(**update)
    assert venue.position_snapshot(100.0).stop_loss == 95.0

    close = dict(
        request=ClosePositionRequest(quantity=1.0),
        timestamp=NOW,
        current_index=1,
        source_module="synthetic-close",
    )
    assert bound("close:1", "schedule_close", (position.id,), **close).schedule_close(**close)
    bound("bar:2", "process_bar", bar=bar(1), index=2).process_bar(bar(1), 2)
    assert venue.position_snapshot(100.0).quantity == 1.0

    submit("reducer", timestamp=NOW + timedelta(minutes=1), index=2, quantity=1.0, reduce_only=True)
    bound("bar:3", "process_bar", bar=bar(2), index=3).process_bar(bar(2), 3)
    assert venue.position_snapshot(100.0) is None

    submit("cancelled", timestamp=NOW + timedelta(minutes=3), index=3)
    cancel = dict(
        request=CancelRequest(client_order_id="cancelled"),
        timestamp=NOW + timedelta(minutes=3),
        source_module="synthetic-oco",
    )
    assert bound("oco:cancel", "cancel_order", ("cancelled",), **cancel).cancel(**cancel) == 1
    submit("cancel-all", timestamp=NOW + timedelta(minutes=4), index=4)
    cancel_all = dict(timestamp=NOW + timedelta(minutes=4), reason="synthetic_cleanup")
    bound("cleanup:all", "cancel_all", **cancel_all).cancel_all(**cancel_all)
    assert venue.pending_snapshots() == ()
    snapshot = port.snapshot()
    evidence = venue.snapshot_evidence(SimpleNamespace(candle=bar(4)))
    assert evidence["core_commit_sequence"] == snapshot.commit_sequence == 11
    assert evidence["core_event_sequence"] == snapshot.events[-1].sequence
    assert venue.event_store.read() == snapshot.events
    with pytest.raises(CorePaperContextUnavailable):
        venue.event_store.append(snapshot.events[0])


def test_missing_context_and_unqualified_composition_cannot_mutate(tmp_path):
    port, _, _, _ = store(tmp_path / "core.sqlite")
    binding = prepare_local_core_paper_binding(port)
    venue = binding.venue
    calls = (
        lambda: venue.submit(
            order("entry"), source_module="paper", created_at=NOW, current_index=0, mark_price=100.0
        ),
        lambda: venue.cancel(
            CancelRequest(client_order_id="entry"), timestamp=NOW, source_module="oco"
        ),
        lambda: venue.update_position(
            PositionUpdateRequest(95.0, 120.0),
            timestamp=NOW,
            source_module="repair",
            mark_price=100.0,
        ),
        lambda: venue.schedule_close(
            ClosePositionRequest(), timestamp=NOW, current_index=0, source_module="close"
        ),
        lambda: venue.process_bar(bar(), 0),
        lambda: venue.cancel_all(NOW),
        lambda: venue.close_position(NOW, 100.0, "direct"),
        lambda: venue.for_effect("not-persisted"),
        binding.require_runtime_qualified,
    )
    for call in calls:
        with pytest.raises(CorePaperContextUnavailable):
            call()
    assert port.snapshot().commit_sequence == 0
    assert binding.qualification_complete is False


def test_bound_payload_cannot_change_or_call_a_different_operation(tmp_path):
    port, binding, writer, refs = store(tmp_path / "core.sqlite")
    source = SavedEffects(tmp_path / "effects.sqlite")
    save_effect(source, binding, writer, refs, "saved-bar", "process_bar", bar=bar(), index=0)
    venue = SQLiteCorePaperVenue(port, effect_source=source).for_effect("saved-bar")
    with pytest.raises(CorePaperContextUnavailable, match="differs"):
        venue.process_bar(bar(), 1)
    with pytest.raises(CorePaperContextUnavailable, match="differs"):
        venue.cancel_all(NOW)
    assert port.snapshot().commit_sequence == 0


def test_lost_ack_reopens_same_durable_context_without_rebasing(tmp_path, monkeypatch):
    path, effects_path = tmp_path / "core.sqlite", tmp_path / "effects.sqlite"
    port, binding, writer, refs = store(path)
    source = SavedEffects(effects_path)
    save_effect(source, binding, writer, refs, "saved-bar", "process_bar", bar=bar(), index=0)
    venue = SQLiteCorePaperVenue(port, effect_source=source).for_effect("saved-bar")
    original = CoreLifecycleCommitCoordinator.commit_effect

    def lost_ack(self, request):
        assert original(self, request).status is CoreCommitStatus.COMMITTED
        return CoreLifecycleCommitResult(CoreCommitStatus.UNRESOLVED, None, False, "lost ack")

    with monkeypatch.context() as patch:
        patch.setattr(CoreLifecycleCommitCoordinator, "commit_effect", lost_ack)
        with pytest.raises(CorePaperEffectNotCommitted) as error:
            venue.process_bar(bar(), 0)
        assert error.value.status is CoreCommitStatus.UNRESOLVED
    assert venue.committed_receipt is None
    port.close()

    reopened, _, _, _ = store(path)
    restored_source = SavedEffects(effects_path)
    restored = SQLiteCorePaperVenue(reopened, effect_source=restored_source).for_effect("saved-bar")
    restored.process_bar(bar(), 0)
    assert restored.committed_receipt is not None
    assert reopened.snapshot().commit_sequence == 1
    assert restored_source.load_effect("saved-bar").lifecycle == binding


def test_suspended_generation_blocks_stale_context_and_pending_entry_fill(tmp_path):
    port, binding, writer, refs = store(tmp_path / "core.sqlite")
    source = SavedEffects(tmp_path / "effects.sqlite")
    venue = SQLiteCorePaperVenue(port, effect_source=source)
    fields = dict(
        request=order("entry"),
        source_module="synthetic",
        created_at=NOW,
        current_index=0,
        mark_price=100.0,
    )
    save_effect(source, binding, writer, refs, "entry", "submit_order", ("entry",), **fields)
    venue.for_effect("entry").submit(**fields)
    save_effect(source, binding, writer, refs, "stale-bar", "process_bar", bar=bar(), index=1)
    payload = b'{"action":"SUSPEND"}'
    transition = CoreFenceTransition(
        "suspend",
        binding,
        writer,
        1,
        CoreLifecycleAction.SUSPEND,
        hashlib.sha256(payload).hexdigest(),
        payload,
        refs,
    )
    assert (
        CoreLifecycleCommitCoordinator(port).commit_transition(transition).status
        is CoreCommitStatus.COMMITTED
    )
    with pytest.raises(CorePaperEffectNotCommitted, match="stale"):
        venue.for_effect("stale-bar").process_bar(bar(), 1)
    current = port.snapshot().lifecycle
    save_effect(source, current, writer, refs, "fenced-bar", "process_bar", bar=bar(), index=1)
    with pytest.raises(CorePaperEffectNotCommitted, match="risk fence"):
        venue.for_effect("fenced-bar").process_bar(bar(), 1)
    assert port.snapshot().broker_state.fill_sequence == 0


def test_explicit_source_does_not_authorize_missing_authorities(tmp_path):
    port, binding, writer, refs = store(tmp_path / "core.sqlite", authorities=False)
    source = SavedEffects(tmp_path / "effects.sqlite")
    save_effect(source, binding, writer, refs, "bar", "process_bar", bar=bar(), index=0)
    local = prepare_local_core_paper_binding(port, effect_source=source)
    with pytest.raises(CorePaperEffectNotCommitted, match="authority"):
        local.venue.for_effect("bar").process_bar(bar(), 0)
    with pytest.raises(CorePaperContextUnavailable, match="Paper plan"):
        local.require_runtime_qualified()
    assert port.snapshot().commit_sequence == 0


def test_existing_runner_bar_call_cannot_invent_context_but_bound_effect_commits(tmp_path):
    from modular_core import InMemoryStateRepository
    from modular_trading_runner.core_bridge import ConservativePaperBarReconciliation
    from modular_trading_runner.cycle import PaperBarExecutor
    from modular_trading_runner.market_data import MarketEventEnvelope

    port, binding, writer, refs = store(tmp_path / "core.sqlite")
    source = SavedEffects(tmp_path / "effects.sqlite")
    venue = SQLiteCorePaperVenue(port, effect_source=source)
    candle = bar()
    event = MarketEventEnvelope(
        event_id="synthetic-market:1",
        source_id="synthetic-source",
        market="linear_futures",
        sequence=1,
        available_at=candle.close_time,
        received_at=candle.close_time,
        candle=candle,
    )
    repository = InMemoryStateRepository()

    def executor(target):
        return PaperBarExecutor(
            run_id="synthetic-runner",
            config_hash=DIGEST,
            state_repository=repository,
            venue=target,
            evidence=venue,
            reconciliation=ConservativePaperBarReconciliation(),
        )

    with pytest.raises(CorePaperContextUnavailable, match="saved Core effect"):
        executor(venue).execute(event, index=0)
    assert port.snapshot().commit_sequence == 0

    # The test host explicitly supplies its persisted causal ID. The legacy
    # Runner call itself still cannot choose/bind this context in production.
    save_effect(source, binding, writer, refs, event.event_id, "process_bar", bar=candle, index=0)
    bound_executor = executor(venue.for_effect(event.event_id))
    result = bound_executor.execute(event, index=0)
    assert result.after_evidence["core_commit_sequence"] == 1
    assert result.after_evidence["core_event_sequence"] == port.snapshot().events[-1].sequence
    assert bound_executor.execute(event, index=0).duplicate is True
    assert port.snapshot().commit_sequence == 1


def ledger_runner(tmp_path, port, source):
    from modular_core import AtomicJsonStateRepository
    from modular_trading_runner.core_lifecycle_bar import CoreLedgerPaperBarExecutor

    return CoreLedgerPaperBarExecutor(
        run_id="synthetic-runner",
        config_hash=DIGEST,
        state_repository=AtomicJsonStateRepository(tmp_path / "runner-state"),
        venue=SQLiteCorePaperVenue(port, effect_source=source),
        effect_lookup=source,
    )


def runner_event():
    from modular_trading_runner.market_data import MarketEventEnvelope

    candle = bar()
    return MarketEventEnvelope(
        "original-market-event",
        "synthetic-source",
        "linear_futures",
        1,
        candle.close_time,
        candle.close_time,
        candle,
    )


def test_opt_in_runner_requires_exact_saved_event_context_before_mutation(tmp_path):
    port, binding, writer, refs = store(tmp_path / "core.sqlite")
    source = SavedEffects(tmp_path / "effects.sqlite")
    executor, event = ledger_runner(tmp_path, port, source), runner_event()
    with pytest.raises(CorePaperContextUnavailable, match="no exact saved"):
        executor.execute(event, index=0)
    assert not executor.recovery_required
    assert port.snapshot().commit_sequence == 0
    save_effect(
        source,
        binding,
        writer,
        refs,
        "external-effect-ID",
        "process_bar",
        bar=event.candle,
        index=0,
    )
    source.save_bar_reference(event, 0, "external-effect-ID")
    with pytest.raises(CorePaperContextUnavailable, match="no exact saved"):
        executor.execute(event, index=1)
    result = executor.execute(event, index=0)
    assert result.event_id == event.event_id
    assert result.after_evidence["core_commit_sequence"] == 1
    reopened = ledger_runner(tmp_path, port, SavedEffects(source.path))
    assert reopened.execute(event, index=0).duplicate
    assert port.snapshot().commit_sequence == 1


def test_opt_in_runner_rejects_existing_legacy_bar_state_without_migration(tmp_path):
    from modular_core import AtomicJsonStateRepository

    port, _, _, _ = store(tmp_path / "core.sqlite")
    source = SavedEffects(tmp_path / "effects.sqlite")
    repository = AtomicJsonStateRepository(tmp_path / "runner-state")
    repository.save("trading-runner-paper-bars:synthetic-runner", {"legacy": "owner-state"})
    with pytest.raises(CorePaperContextUnavailable, match="owner migration"):
        ledger_runner(tmp_path, port, source)
    assert port.snapshot().commit_sequence == 0


def test_opt_in_runner_reopens_lost_ack_with_original_effect_and_no_relookup(tmp_path, monkeypatch):
    path = tmp_path / "core.sqlite"
    port, binding, writer, refs = store(path)
    source, event = SavedEffects(tmp_path / "effects.sqlite"), runner_event()
    request = save_effect(
        source,
        binding,
        writer,
        refs,
        "durable-bar-effect",
        "process_bar",
        bar=event.candle,
        index=0,
    )
    source.save_bar_reference(event, 0, request.effect_id)
    executor = ledger_runner(tmp_path, port, source)
    original = CoreLifecycleCommitCoordinator.commit_effect

    def lost_ack(self, effect):
        assert original(self, effect).status is CoreCommitStatus.COMMITTED
        return CoreLifecycleCommitResult(CoreCommitStatus.UNRESOLVED, None, False, "lost Core ack")

    with monkeypatch.context() as patch:
        patch.setattr(CoreLifecycleCommitCoordinator, "commit_effect", lost_ack)
        with pytest.raises(CorePaperEffectNotCommitted):
            executor.execute(event, index=0)
    assert executor.recovery_required
    assert executor._pending["core_effect_id"] == request.effect_id
    assert executor._pending["core_request_digest"] == request.request_digest
    port.close()
    recovered_port, _, _, _ = store(path)
    recovered_source = SavedEffects(source.path)

    def no_relookup(**_kwargs):
        pytest.fail("pending recovery must use the persisted effect reference")

    monkeypatch.setattr(recovered_source, "lookup_bar_effect_id", no_relookup)
    recovered = ledger_runner(tmp_path, recovered_port, recovered_source)
    result = recovered.recover()
    assert result.recovered and result.event_id == event.event_id
    assert not recovered.recovery_required
    assert recovered_port.snapshot().commit_sequence == 1


def test_opt_in_runner_retains_stale_context_and_rejects_rebased_saved_request(tmp_path):
    port, binding, writer, refs = store(tmp_path / "core.sqlite")
    source, event = SavedEffects(tmp_path / "effects.sqlite"), runner_event()
    request = save_effect(
        source, binding, writer, refs, "stale-context", "process_bar", bar=event.candle, index=0
    )
    source.save_bar_reference(event, 0, request.effect_id)
    payload = b'{"action":"SUSPEND"}'
    transition = CoreFenceTransition(
        "suspend",
        binding,
        writer,
        1,
        CoreLifecycleAction.SUSPEND,
        hashlib.sha256(payload).hexdigest(),
        payload,
        refs,
    )
    assert (
        CoreLifecycleCommitCoordinator(port).commit_transition(transition).status
        is CoreCommitStatus.COMMITTED
    )
    executor = ledger_runner(tmp_path, port, source)
    with pytest.raises(CorePaperEffectNotCommitted, match="stale"):
        executor.execute(event, index=0)
    assert executor.recovery_required
    assert port.snapshot().commit_sequence == 1

    # Simulate a trusted source violating its immutability contract. A well-typed
    # replacement still must not rebase a persisted Runner/Core attempt.
    with sqlite3.connect(source.path) as db:
        value = json.loads(
            db.execute("SELECT value FROM effects WHERE id=?", (request.effect_id,)).fetchone()[0]
        )
        value["lifecycle"]["expected_generation"] = 1
        db.execute("UPDATE effects SET value=? WHERE id=?", (json.dumps(value), request.effect_id))
    with pytest.raises(CorePaperContextUnavailable, match="context changed"):
        ledger_runner(tmp_path, port, source)
    assert port.snapshot().commit_sequence == 1


@pytest.mark.parametrize("core_outcome", ("absent", "denied", "unresolved"))
def test_actual_paper_coordinator_retains_unresolved_intent_without_compensation(
    tmp_path, monkeypatch, core_outcome
):
    from decimal import Decimal
    from paper_execution import (
        ExecutableTradePlan,
        ExecutionIntent,
        IntentRole,
        PaperExecutionConfig,
        PaperExecutionCoordinator,
        LifecycleState,
        PlanAction,
        ReconciliationBlocked,
        StateVersions,
        VenueMetadataSnapshot,
    )
    from modular_core import TimeInForce
    from modular_core.events import JsonLinesEventStore

    port, binding, writer, refs = store(tmp_path / "core.sqlite")
    source = SavedEffects(tmp_path / "effects.sqlite")
    venue = SQLiteCorePaperVenue(port, effect_source=source)
    config, versions = PaperExecutionConfig(), StateVersions(0, 0, 0)
    metadata = VenueMetadataSnapshot(
        venue_id="synthetic",
        instrument="BTCUSDT",
        market_type="linear_futures",
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        max_quantity=Decimal("100"),
        min_notional=Decimal("1"),
        max_notional=Decimal("1000000"),
        contract_multiplier=Decimal("1"),
        permitted_order_types=tuple(OrderType),
        permitted_time_in_force=tuple(TimeInForce),
        post_only_supported=True,
        reduce_only_supported=True,
        metadata_version="synthetic-v1",
        effective_from=NOW - timedelta(days=1),
        source_hash=DIGEST,
    )

    class Metadata:
        def snapshot(self, instrument, timestamp):
            return metadata

    coordinator = PaperExecutionCoordinator(
        config=config,
        venue=venue,
        metadata_provider=Metadata(),
        run_id="synthetic-paper",
        event_store=JsonLinesEventStore(tmp_path / "paper.events.jsonl", fsync=True),
    )
    plan = ExecutableTradePlan.create(
        plan_id="synthetic-plan",
        created_at=NOW,
        valid_from=NOW,
        expires_at=NOW + timedelta(minutes=5),
        portfolio_id="synthetic-portfolio",
        instrument="BTCUSDT",
        market="linear_futures",
        action=PlanAction.OPEN_POSITION,
        intents=(
            ExecutionIntent(
                "entry", IntentRole.ENTRY, Side.BUY, OrderType.MARKET, quantity=Decimal("1")
            ),
        ),
        expected_versions=versions,
        metadata_version="synthetic-v1",
        configuration_hash=config.canonical_hash,
    )
    if core_outcome != "absent":
        # Test-only host saves the actual deterministic Paper preflight result.
        prepared = coordinator._prepare_plan(
            plan=plan,
            metadata=metadata,
            now=NOW,
            mark_price=Decimal("100"),
            scope=(plan.portfolio_id, plan.instrument),
        )[0]
        save_effect(
            source,
            binding,
            writer,
            refs,
            "saved-paper-effect",
            "submit_order",
            (prepared.client_order_id,),
            request=prepared.normalized.request,
            source_module=coordinator.MODULE_ID,
            created_at=NOW,
            current_index=0,
            mark_price=100.0,
        )
        coordinator.venue = venue.for_effect("saved-paper-effect")
        original = CoreLifecycleCommitCoordinator.commit_effect

        def unresolved_after_commit(self, request):
            if core_outcome == "denied":
                return CoreLifecycleCommitResult(
                    CoreCommitStatus.DENIED, None, False, "known denial"
                )
            assert original(self, request).status is CoreCommitStatus.COMMITTED
            return CoreLifecycleCommitResult(CoreCommitStatus.UNRESOLVED, None, False, "lost ack")

        monkeypatch.setattr(
            CoreLifecycleCommitCoordinator, "commit_effect", unresolved_after_commit
        )

    def dispatch(target):
        return target.accept_plan(
            plan,
            now=NOW,
            current_index=0,
            mark_price=Decimal("100"),
            versions=versions,
        )

    if core_outcome != "unresolved":
        # Existing absent-context and known-denied behavior remains terminal.
        result = dispatch(coordinator)
        assert not result.accepted and result.terminal
        assert port.snapshot().commit_sequence == 0
        assert venue.pending_snapshots() == ()
        assert coordinator.journal.store.read()[-1].event_type == "execution.failed"
        return

    def no_compensation(*_args, **_kwargs):
        pytest.fail("UNRESOLVED Core outcomes must not become failure/compensation")

    monkeypatch.setattr(coordinator, "_handle_execution_failure", no_compensation)
    monkeypatch.setattr(coordinator, "_compensate_submitted_orders", no_compensation)
    with pytest.raises(ReconciliationBlocked, match="Core effect outcome is unresolved"):
        dispatch(coordinator)
    assert port.snapshot().commit_sequence == 1
    assert len(venue.pending_snapshots()) == 1
    assert coordinator.lifecycle is LifecycleState.BLOCKED
    assert (plan.portfolio_id, plan.instrument) in coordinator.blocked_scopes
    assert plan.plan_id not in coordinator._plan_results
    assert plan.plan_id not in coordinator._plan_hashes
    assert [item.event_type for item in coordinator.journal.store.read()] == [
        "execution.intent_recorded"
    ]
    with pytest.raises(ReconciliationBlocked):
        dispatch(coordinator)
    assert port.snapshot().commit_sequence == 1
    port.close()

    reopened_port, _, _, _ = store(tmp_path / "core.sqlite")
    reopened_venue = SQLiteCorePaperVenue(
        reopened_port, effect_source=SavedEffects(source.path)
    ).for_effect("saved-paper-effect")
    reopened = PaperExecutionCoordinator(
        config=config,
        venue=reopened_venue,
        metadata_provider=Metadata(),
        run_id="synthetic-paper",
        event_store=JsonLinesEventStore(tmp_path / "paper.events.jsonl", fsync=True),
    )
    assert reopened.lifecycle is LifecycleState.BLOCKED
    with pytest.raises(ReconciliationBlocked):
        dispatch(reopened)
    assert reopened_port.snapshot().commit_sequence == 1
    assert plan.plan_id not in reopened._plan_results
    assert [item.event_type for item in reopened.journal.store.read()] == [
        "execution.intent_recorded"
    ]


def _saved_paper_intent(
    journal, *, plan_id="plan-1", intent_id="intent-1", client_id="paper-client"
):
    return journal.append(
        timestamp=NOW,
        event_type="execution.intent_recorded",
        aggregate_id=plan_id,
        payload={
            "plan_id": plan_id,
            "content_hash": DIGEST,
            "portfolio_id": "portfolio-1",
            "instrument": "BTCUSDT",
            "intents": [{"intent_id": intent_id, "role": "entry", "client_order_id": client_id}],
        },
        correlation_id=plan_id,
        symbol="BTCUSDT",
    )


def _saved_paper_submit(source, binding, writer, refs):
    return save_effect(
        source,
        binding,
        writer,
        refs,
        "paper-effect-1",
        "submit_order",
        ("paper-client",),
        request=order("paper-client"),
        source_module="paper_execution_venue_reconciliation",
        created_at=NOW,
        current_index=0,
        mark_price=100.0,
    )


def _paper_coordinator_with_saved_submit(tmp_path, event_store):
    from decimal import Decimal
    from modular_core import TimeInForce
    from paper_execution import (
        ExecutableTradePlan,
        ExecutionIntent,
        IntentRole,
        PaperExecutionConfig,
        PaperExecutionCoordinator,
        PlanAction,
        StateVersions,
        VenueMetadataSnapshot,
    )

    port, binding, writer, refs = store(tmp_path / "core.sqlite")
    source = SavedEffects(tmp_path / "effects.sqlite")
    venue = SQLiteCorePaperVenue(port, effect_source=source)
    config, versions = PaperExecutionConfig(), StateVersions(0, 0, 0)
    metadata = VenueMetadataSnapshot(
        venue_id="synthetic",
        instrument="BTCUSDT",
        market_type="linear_futures",
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        max_quantity=Decimal("100"),
        min_notional=Decimal("1"),
        max_notional=Decimal("1000000"),
        contract_multiplier=Decimal("1"),
        permitted_order_types=tuple(OrderType),
        permitted_time_in_force=tuple(TimeInForce),
        post_only_supported=True,
        reduce_only_supported=True,
        metadata_version="synthetic-v1",
        effective_from=NOW - timedelta(days=1),
        source_hash=DIGEST,
    )

    class Metadata:
        def snapshot(self, instrument, timestamp):
            return metadata

    coordinator = PaperExecutionCoordinator(
        config=config,
        venue=venue,
        metadata_provider=Metadata(),
        run_id="synthetic-paper",
        event_store=event_store,
    )
    plan = ExecutableTradePlan.create(
        plan_id="synthetic-plan",
        created_at=NOW,
        valid_from=NOW,
        expires_at=NOW + timedelta(minutes=5),
        portfolio_id="synthetic-portfolio",
        instrument="BTCUSDT",
        market="linear_futures",
        action=PlanAction.OPEN_POSITION,
        intents=(
            ExecutionIntent(
                "entry", IntentRole.ENTRY, Side.BUY, OrderType.MARKET, quantity=Decimal("1")
            ),
        ),
        expected_versions=versions,
        metadata_version="synthetic-v1",
        configuration_hash=config.canonical_hash,
    )
    prepared = coordinator._prepare_plan(
        plan=plan,
        metadata=metadata,
        now=NOW,
        mark_price=Decimal("100"),
        scope=(plan.portfolio_id, plan.instrument),
    )[0]
    request = save_effect(
        source,
        binding,
        writer,
        refs,
        "saved-paper-effect",
        "submit_order",
        (prepared.client_order_id,),
        request=prepared.normalized.request,
        source_module=coordinator.MODULE_ID,
        created_at=NOW,
        current_index=0,
        mark_price=100.0,
    )
    coordinator.venue = venue.for_effect(request.effect_id)
    return (
        port, source, coordinator, plan, versions, config,
        coordinator.metadata_provider, request,
    )


class _AppendFaultEventStore:
    def __init__(self, delegate, event_type, *, after_write):
        self.delegate = delegate
        self.event_type = event_type
        self.after_write = after_write
        self.failed = False

    def append(self, event):
        if not self.failed and event.event_type == self.event_type:
            self.failed = True
            if not self.after_write:
                raise OSError("synthetic append failure before write")
            self.delegate.append(event)
            raise OSError("synthetic fsync acknowledgement lost after write")
        self.delegate.append(event)

    def append_many(self, events):
        for event in events:
            self.append(event)

    def read(self, *, after_sequence=0):
        return self.delegate.read(after_sequence=after_sequence)


@pytest.mark.parametrize(
    ("failure_point", "after_write"),
    (
        ("projection", False),
        ("order.submitted", False),
        ("order.submitted", True),
        ("plan.accepted", False),
        ("plan.accepted", True),
    ),
)
def test_core_commit_followup_failures_block_without_compensation_or_terminal_result(
    tmp_path, monkeypatch, failure_point, after_write
):
    from decimal import Decimal
    from paper_execution import PaperExecutionCoordinator, ReconciliationBlocked, LifecycleState

    raw_store = JsonLinesEventStore(tmp_path / "paper.events.jsonl", fsync=True)
    event_store = (
        raw_store
        if failure_point == "projection"
        else _AppendFaultEventStore(raw_store, failure_point, after_write=after_write)
    )
    (
        port, source, coordinator, plan, versions, config,
        metadata_provider, _request,
    ) = _paper_coordinator_with_saved_submit(tmp_path, event_store)
    cancel_calls = []

    def unexpected_cancel(*args, **kwargs):
        cancel_calls.append((args, kwargs))
        raise AssertionError("Core-committed Paper failure must not compensate")

    monkeypatch.setattr(coordinator.venue, "cancel", unexpected_cancel)
    if failure_point == "projection":
        import modular_core.lifecycle_venue as venue_module

        def broken_projection(_order):
            raise ValueError("synthetic projection failure")

        monkeypatch.setattr(venue_module, "_decode_order", broken_projection)

    with pytest.raises(ReconciliationBlocked) as caught:
        coordinator.accept_plan(
            plan,
            now=NOW,
            current_index=0,
            mark_price=Decimal("100"),
            versions=versions,
        )

    if failure_point == "projection":
        committed_error = caught.value.__cause__
        assert isinstance(committed_error, CorePaperCommittedProjectionUnavailable)
        assert committed_error.effect_id == _request.effect_id
        assert committed_error.request_digest == _request.request_digest
        assert committed_error.receipt.effect_id == _request.effect_id
        assert committed_error.invocation_ref == CoreInvocationRef.from_request(_request)
        with pytest.raises(AttributeError):
            committed_error.effect_id = "replacement"
    assert cancel_calls == []
    assert port.snapshot().commit_sequence == 1
    assert (plan.portfolio_id, plan.instrument) in coordinator.blocked_scopes
    assert coordinator.lifecycle is LifecycleState.BLOCKED
    assert plan.plan_id not in coordinator._plan_results
    assert plan.plan_id not in coordinator._plan_hashes
    event_types = [event.event_type for event in raw_store.read()]
    assert "execution.failed" not in event_types
    if failure_point == "order.submitted" and after_write:
        assert "order.submitted" in event_types
    if failure_point == "plan.accepted" and after_write:
        assert "plan.accepted" in event_types

    port.close()
    reopened_port, _, _, _ = store(tmp_path / "core.sqlite")
    reopened = PaperExecutionCoordinator(
        config=config,
        venue=SQLiteCorePaperVenue(reopened_port, effect_source=SavedEffects(source.path)),
        metadata_provider=metadata_provider,
        run_id="synthetic-paper",
        event_store=JsonLinesEventStore(tmp_path / "paper.events.jsonl", fsync=True),
    )
    assert reopened.lifecycle is LifecycleState.BLOCKED
    with pytest.raises(ReconciliationBlocked):
        reopened.accept_plan(
            plan,
            now=NOW,
            current_index=0,
            mark_price=Decimal("100"),
            versions=versions,
        )
    assert reopened_port.snapshot().commit_sequence == 1


def test_bound_core_success_without_exact_receipt_blocks_without_terminal_failure(tmp_path, monkeypatch):
    from decimal import Decimal
    from paper_execution import ReconciliationBlocked

    raw_store = JsonLinesEventStore(tmp_path / "paper.events.jsonl", fsync=True)
    port, source, coordinator, plan, versions, _config, _metadata_provider, _request = (
        _paper_coordinator_with_saved_submit(tmp_path, raw_store)
    )
    monkeypatch.setattr(coordinator.venue, "receipt_for_invocation", lambda _ref: None)
    cancel_calls = []
    monkeypatch.setattr(
        coordinator.venue,
        "cancel",
        lambda *args, **kwargs: cancel_calls.append((args, kwargs)),
    )

    with pytest.raises(ReconciliationBlocked, match="Core mutation committed"):
        coordinator.accept_plan(
            plan,
            now=NOW,
            current_index=0,
            mark_price=Decimal("100"),
            versions=versions,
        )

    assert port.snapshot().commit_sequence == 1
    assert cancel_calls == []
    assert plan.plan_id not in coordinator._plan_results
    assert plan.plan_id not in coordinator._plan_hashes
    assert "execution.failed" not in [event.event_type for event in raw_store.read()]


def test_receipt_aware_forwarding_wrapper_correlates_exact_core_receipt(tmp_path):
    from decimal import Decimal

    raw_store = JsonLinesEventStore(tmp_path / "paper.events.jsonl", fsync=True)
    port, _source, coordinator, plan, versions, *_rest = _paper_coordinator_with_saved_submit(
        tmp_path, raw_store
    )
    inner = coordinator.venue

    class ForwardingReceiptAwareVenue:
        core_receipt_aware = True

        def __getattr__(self, name):
            return getattr(inner, name)

    coordinator.venue = ForwardingReceiptAwareVenue()

    result = coordinator.accept_plan(
        plan,
        now=NOW,
        current_index=0,
        mark_price=Decimal("100"),
        versions=versions,
    )

    assert result.accepted
    assert port.snapshot().commit_sequence == 1
    assert plan.plan_id in coordinator._plan_results


def test_marked_core_wrapper_missing_reference_api_is_rejected_before_mutation(
    tmp_path,
):
    from decimal import Decimal

    raw_store = JsonLinesEventStore(tmp_path / "paper.events.jsonl", fsync=True)
    port, _source, coordinator, plan, versions, *_rest = _paper_coordinator_with_saved_submit(
        tmp_path, raw_store
    )
    inner = coordinator.venue
    mutation_calls = []

    class UnsupportedCoreWrapper:
        core_receipt_aware = True

        def __getattr__(self, name):
            if name in {"invocation_ref", "receipt_for_invocation"}:
                raise AttributeError(name)
            if name == "submit":
                def submit(*args, **kwargs):
                    mutation_calls.append((args, kwargs))
                    return inner.submit(*args, **kwargs)

                return submit
            return getattr(inner, name)

    coordinator.venue = UnsupportedCoreWrapper()

    result = coordinator.accept_plan(
        plan,
        now=NOW,
        current_index=0,
        mark_price=Decimal("100"),
        versions=versions,
    )

    assert mutation_calls == []
    assert port.snapshot().commit_sequence == 0
    assert not result.accepted


def test_legacy_wrapper_stale_receipt_does_not_turn_rejection_into_core_commit(tmp_path):
    from decimal import Decimal

    port, binding, writer, refs = store(tmp_path / "core.sqlite")
    source = SavedEffects(tmp_path / "effects.sqlite")
    venue = SQLiteCorePaperVenue(port, effect_source=source)
    stale_request = save_effect(
        source,
        binding,
        writer,
        refs,
        "unrelated-bar",
        "process_bar",
        bar=bar(),
        index=1,
    )
    stale_capability = venue.for_effect(stale_request.effect_id)
    stale_capability.process_bar(bar(), 1)
    stale_receipt = stale_capability.committed_receipt
    assert stale_receipt is not None

    class LegacyVenueWrapper:
        committed_receipt = stale_receipt

        def pending_snapshots(self):
            return ()

        def submit(self, *_args, **_kwargs):
            return None

        def cancel(self, *_args, **_kwargs):
            pytest.fail("a rejected pre-commit legacy wrapper must not compensate")

    raw_store = JsonLinesEventStore(tmp_path / "paper.events.jsonl", fsync=True)
    _, _, coordinator, plan, versions, *_rest = _paper_coordinator_with_saved_submit(
        tmp_path / "paper", raw_store
    )
    coordinator.venue = LegacyVenueWrapper()

    result = coordinator.accept_plan(
        plan,
        now=NOW,
        current_index=0,
        mark_price=Decimal("100"),
        versions=versions,
    )

    assert not result.accepted
    assert result.terminal
    assert [event.event_type for event in raw_store.read()][-1] == "execution.failed"
    assert port.snapshot().commit_sequence == 1


def test_pending_paper_link_is_durable_and_inspector_only_reads_core_and_paper(tmp_path):
    core, binding, writer, refs = store(tmp_path / "core.sqlite")
    source = SavedEffects(tmp_path / "requests.sqlite")
    request = _saved_paper_submit(source, binding, writer, refs)
    paper_store = JsonLinesEventStore(tmp_path / "paper.events.jsonl", fsync=True)
    journal = ExecutionEventJournal("paper-run", paper_store)
    intent_event = _saved_paper_intent(journal)

    link_event = persist_pending_attempt_link(
        journal,
        source,
        request,
        plan_id="plan-1",
        plan_content_hash=DIGEST,
        intent_id="intent-1",
    )

    assert link_event.event_type == "execution.core_effect_pending"
    assert journal.sequence == 2
    reopened = ExecutionEventJournal(
        "paper-run", JsonLinesEventStore(tmp_path / "paper.events.jsonl", fsync=True)
    )
    assert reopened.store.read()[-1] == link_event
    committed = CoreLifecycleCommitCoordinator(core).commit_effect(request)
    assert committed.status is CoreCommitStatus.COMMITTED
    assert core.lookup_effect(request).status is CoreEffectLookupStatus.MATCHED

    core_sequence = core.snapshot().commit_sequence
    paper_sequence = reopened.sequence
    evidence = inspect_pending_attempts(reopened, source, core)

    assert len(evidence) == 1
    assert evidence[0].status is PaperAttemptInspectionStatus.CORE_RECEIPT_MATCHED
    assert evidence[0].intent_correspondence is PaperIntentCorrespondence.UNVERIFIED
    assert evidence[0].plan_id == "plan-1"
    assert evidence[0].intent_id == "intent-1"
    assert evidence[0].effect_id == request.effect_id
    assert evidence[0].request_digest == request.request_digest
    assert evidence[0].observed_commit_sequence == core_sequence == 1
    assert evidence[0].observed_event_sequence == len(core.snapshot().events)
    assert core.snapshot().commit_sequence == core_sequence
    assert reopened.sequence == paper_sequence == 2
    assert intent_event.event_id == link_event.causation_id


def test_legacy_paper_intent_without_attempt_link_remains_blocked(tmp_path):
    core, binding, writer, refs = store(tmp_path / "core.sqlite")
    source = SavedEffects(tmp_path / "requests.sqlite")
    _saved_paper_submit(source, binding, writer, refs)
    journal = ExecutionEventJournal(
        "paper-run", JsonLinesEventStore(tmp_path / "paper.events.jsonl", fsync=True)
    )
    _saved_paper_intent(journal)

    evidence = inspect_pending_attempts(journal, source, core)

    assert len(evidence) == 1
    assert evidence[0].status is PaperAttemptInspectionStatus.BLOCKED_LEGACY
    assert "no durable Core attempt link" in evidence[0].reason
    assert core.snapshot().commit_sequence == 0
    assert journal.sequence == 1


def test_terminal_accepted_intent_without_link_is_not_pending(tmp_path):
    core, _, _, _ = store(tmp_path / "core.sqlite")
    source = SavedEffects(tmp_path / "requests.sqlite")
    journal = ExecutionEventJournal(
        "paper-run", JsonLinesEventStore(tmp_path / "paper.events.jsonl", fsync=True)
    )
    _saved_paper_intent(journal)
    journal.append(
        timestamp=NOW,
        event_type="plan.accepted",
        aggregate_id="plan-1",
        payload={"plan_id": "plan-1", "content_hash": DIGEST},
        correlation_id="plan-1",
    )

    assert inspect_pending_attempts(journal, source, core) == ()
    assert core.snapshot().commit_sequence == 0
    assert journal.sequence == 2


def test_terminal_failed_outcome_conflicts_with_matched_core_submit_receipt(tmp_path):
    core, binding, writer, refs = store(tmp_path / "core.sqlite")
    source = SavedEffects(tmp_path / "requests.sqlite")
    request = _saved_paper_submit(source, binding, writer, refs)
    journal = ExecutionEventJournal(
        "paper-run", JsonLinesEventStore(tmp_path / "paper.events.jsonl", fsync=True)
    )
    _saved_paper_intent(journal)
    persist_pending_attempt_link(
        journal, source, request,
        plan_id="plan-1", plan_content_hash=DIGEST, intent_id="intent-1",
    )
    assert CoreLifecycleCommitCoordinator(core).commit_effect(request).status is CoreCommitStatus.COMMITTED
    journal.append(
        timestamp=NOW,
        event_type="execution.failed",
        aggregate_id="plan-1",
        payload={"plan_id": "plan-1", "content_hash": DIGEST},
        correlation_id="plan-1",
    )

    evidence = inspect_pending_attempts(journal, source, core)

    assert len(evidence) == 1
    assert evidence[0].status is PaperAttemptInspectionStatus.CONFLICT
    assert "contradicts the terminal Paper outcome" in evidence[0].reason
    assert core.snapshot().commit_sequence == 1
    assert journal.sequence == 3


def test_pending_inspector_distinguishes_missing_request_from_link_conflict(tmp_path):
    core, binding, writer, refs = store(tmp_path / "core.sqlite")
    source = SavedEffects(tmp_path / "requests.sqlite")
    request = _saved_paper_submit(source, binding, writer, refs)
    wrong_request = save_effect(
        source,
        binding,
        writer,
        refs,
        "different-effect-id",
        "submit_order",
        ("paper-client",),
        request=order("paper-client"),
        source_module="paper_execution_venue_reconciliation",
        created_at=NOW,
        current_index=0,
        mark_price=100.0,
    )
    journal = ExecutionEventJournal(
        "paper-run", JsonLinesEventStore(tmp_path / "paper.events.jsonl", fsync=True)
    )
    _saved_paper_intent(journal)
    persist_pending_attempt_link(
        journal, source, request,
        plan_id="plan-1", plan_content_hash=DIGEST, intent_id="intent-1",
    )

    class MissingRequestSource:
        def load_effect(self, _effect_id):
            raise FileNotFoundError("fixture missing")

    class ConflictingRequestSource:
        def load_effect(self, _effect_id):
            return wrong_request

    missing = inspect_pending_attempts(journal, MissingRequestSource(), core)
    conflicting = inspect_pending_attempts(journal, ConflictingRequestSource(), core)

    assert missing[0].status is PaperAttemptInspectionStatus.UNAVAILABLE
    assert "unavailable" in missing[0].reason
    assert conflicting[0].status is PaperAttemptInspectionStatus.CONFLICT
    assert "conflicts" in conflicting[0].reason
    assert core.snapshot().commit_sequence == 0
    assert journal.sequence == 2


def test_pending_link_refuses_non_durable_store_and_mismatched_request(tmp_path):
    core, binding, writer, refs = store(tmp_path / "core.sqlite")
    source = SavedEffects(tmp_path / "requests.sqlite")
    request = _saved_paper_submit(source, binding, writer, refs)
    memory_journal = ExecutionEventJournal("paper-memory")
    _saved_paper_intent(memory_journal)
    with pytest.raises(ValueError, match="fsync-backed"):
        persist_pending_attempt_link(
            memory_journal,
            source,
            request,
            plan_id="plan-1",
            plan_content_hash=DIGEST,
            intent_id="intent-1",
        )
    assert memory_journal.sequence == 1

    durable_journal = ExecutionEventJournal(
        "paper-run", JsonLinesEventStore(tmp_path / "paper.events.jsonl", fsync=True)
    )
    _saved_paper_intent(durable_journal, client_id="another-client")
    with pytest.raises(ValueError, match="does not match saved Paper client target"):
        persist_pending_attempt_link(
            durable_journal,
            source,
            request,
            plan_id="plan-1",
            plan_content_hash=DIGEST,
            intent_id="intent-1",
        )
    assert durable_journal.sequence == 1
    assert core.snapshot().commit_sequence == 0
