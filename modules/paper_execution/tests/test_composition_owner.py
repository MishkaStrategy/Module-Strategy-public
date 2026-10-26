from __future__ import annotations

import threading
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from modular_core import (
    Candle,
    EngineConfig,
    InstrumentSpec,
    OrderRequest,
    OrderSnapshot,
    OrderType,
    PositionUpdateRequest,
    PositionSide,
    Side,
)
from composition_owner_reference import (
    PaperCompositionMemberConfig,
    PaperCompositionOwner,
    PaperCompositionStateRoot,
    PaperCompositionUnavailable,
)

UTC = timezone.utc
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
LEFT = "strategy-instance-alpha"
RIGHT = "strategy-instance-beta"


def _member(strategy_instance_id: str, symbol: str = "BTCUSDT") -> PaperCompositionMemberConfig:
    return PaperCompositionMemberConfig(
        strategy_instance_id=strategy_instance_id,
        engine_config=EngineConfig(close_open_position_at_end=False),
        instrument=InstrumentSpec(symbol=symbol),
    )


def _member_ids(count: int) -> tuple[str, ...]:
    return tuple(f"strategy-instance-{index:02d}" for index in range(count))


def _owner(
    *member_ids: str,
    composition_id: str = "composition-paper-reference",
    owner_generation: str | None = None,
) -> PaperCompositionOwner:
    configured_ids = member_ids or (LEFT, RIGHT)
    return PaperCompositionOwner(
        composition_id,
        tuple(_member(member_id) for member_id in configured_ids),
        owner_generation=owner_generation,
        utc_clock=lambda: NOW,
    )


def _bar(timestamp: datetime, symbol: str = "BTCUSDT") -> Candle:
    return Candle(
        timestamp=timestamp,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=1.0,
        symbol=symbol,
        timeframe="1m",
    )


def _open_position(
    owner: PaperCompositionOwner,
    strategy_instance_id: str,
    side: Side,
) -> None:
    order = owner.submit(
        strategy_instance_id,
        OrderRequest(side=side, order_type=OrderType.MARKET, quantity=1.0),
        source_module=strategy_instance_id,
        created_at=NOW,
        current_index=0,
        mark_price=100.0,
    )
    assert order is not None
    assert order.status.value == "pending"
    owner.process_bars(
        {member_id: _bar(NOW, "BTCUSDT") for member_id in owner.membership},
        index=1,
    )


def _queue_market_order(owner: PaperCompositionOwner, strategy_instance_id: str) -> None:
    order = owner.submit(
        strategy_instance_id,
        OrderRequest(side=Side.BUY, order_type=OrderType.MARKET, quantity=1.0),
        source_module=strategy_instance_id,
        created_at=NOW,
        current_index=0,
        mark_price=100.0,
    )
    assert order is not None
    assert order.status.value == "pending"


@pytest.mark.parametrize("member_count", [1, 2, 4])
def test_owner_accepts_explicit_nonempty_membership_and_restores_every_member(
    member_count: int,
) -> None:
    member_ids = _member_ids(member_count)
    source = _owner(*reversed(member_ids))
    root = source.capture_state()
    candidate = _owner(*member_ids)

    assert source.membership == tuple(sorted(member_ids))
    assert source.snapshot().membership == source.membership
    assert tuple(member.strategy_instance_id for member in root.members) == source.membership

    candidate.restore_state(root)
    restored = candidate.snapshot()
    assert restored.coverage == "complete"
    assert restored.membership == source.membership
    assert restored.positions == ()


def test_owner_rejects_empty_and_duplicate_membership() -> None:
    with pytest.raises(ValueError, match="non-empty tuple"):
        PaperCompositionOwner("empty-composition", ())

    with pytest.raises(ValueError, match="unique"):
        PaperCompositionOwner(
            "duplicate-composition",
            (_member(LEFT), _member(RIGHT), _member(LEFT)),
        )


def test_snapshot_and_state_root_reject_empty_or_duplicate_membership() -> None:
    owner = _owner()
    snapshot = owner.snapshot()
    with pytest.raises(ValueError, match="at least one member"):
        replace(snapshot, membership=())
    with pytest.raises(ValueError, match="unique and sorted"):
        replace(snapshot, membership=(LEFT, LEFT))
    with pytest.raises(ValueError, match="unique and sorted"):
        replace(snapshot, membership=(RIGHT, LEFT))

    root = owner.capture_state()
    with pytest.raises(ValueError, match="at least one member state"):
        PaperCompositionStateRoot.create(
            composition_id=root.composition_id,
            source_owner_generation=root.source_owner_generation,
            membership_version=root.membership_version,
            state_version=root.state_version,
            members=(),
        )
    with pytest.raises(ValueError, match="unique and sorted"):
        PaperCompositionStateRoot.create(
            composition_id=root.composition_id,
            source_owner_generation=root.source_owner_generation,
            membership_version=root.membership_version,
            state_version=root.state_version,
            members=(root.members[0], root.members[0]),
        )
    with pytest.raises(ValueError, match="unique and sorted"):
        PaperCompositionStateRoot.create(
            composition_id=root.composition_id,
            source_owner_generation=root.source_owner_generation,
            membership_version=root.membership_version,
            state_version=root.state_version,
            members=tuple(reversed(root.members)),
        )


def test_membership_digest_is_order_independent_and_binds_config_and_instrument() -> None:
    alpha = _member("strategy-alpha")
    beta = _member("strategy-beta")
    baseline = PaperCompositionOwner("digest-composition", (beta, alpha))
    reordered = PaperCompositionOwner("digest-composition", (alpha, beta))
    changed_config = PaperCompositionOwner(
        "digest-composition",
        (
            replace(
                alpha,
                engine_config=EngineConfig(
                    initial_cash=20_000.0,
                    close_open_position_at_end=False,
                ),
            ),
            beta,
        ),
    )
    changed_instrument = PaperCompositionOwner(
        "digest-composition",
        (replace(alpha, instrument=InstrumentSpec(symbol="ETHUSDT")), beta),
    )

    assert baseline.membership == reordered.membership
    assert baseline.membership_version == reordered.membership_version
    assert baseline.membership_version != changed_config.membership_version
    assert baseline.membership_version != changed_instrument.membership_version


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_same_symbol_long_short_positions_are_isolated_by_strategy_instance(side: Side) -> None:
    owner = _owner()
    other_side = Side.SELL if side is Side.BUY else Side.BUY
    _open_position(owner, LEFT, side)
    _open_position(owner, RIGHT, other_side)

    snapshot = owner.snapshot()

    assert snapshot.coverage == "complete"
    assert snapshot.membership == (LEFT, RIGHT)
    assert len(snapshot.positions or ()) == 2
    positions = {item.identity.strategy_instance_id: item for item in snapshot.positions or ()}
    assert positions[LEFT].instrument == positions[RIGHT].instrument == "BTCUSDT"
    assert positions[LEFT].identity.core_position_id == positions[RIGHT].identity.core_position_id
    assert positions[LEFT].side is (PositionSide.LONG if side is Side.BUY else PositionSide.SHORT)
    assert positions[RIGHT].side is (PositionSide.SHORT if side is Side.BUY else PositionSide.LONG)
    assert positions[LEFT].identity != positions[RIGHT].identity


def test_same_symbol_positions_remain_independent_with_four_members() -> None:
    member_ids = _member_ids(4)
    owner = _owner(*member_ids)

    for index, member_id in enumerate(member_ids):
        side = Side.BUY if index % 2 == 0 else Side.SELL
        _open_position(owner, member_id, side)

    snapshot = owner.snapshot()
    positions = snapshot.positions or ()

    assert snapshot.coverage == "complete"
    assert snapshot.membership == member_ids
    assert len(positions) == len(member_ids)
    assert {item.identity.strategy_instance_id for item in positions} == set(member_ids)
    assert {item.instrument for item in positions} == {"BTCUSDT"}
    assert len({item.identity for item in positions}) == len(member_ids)


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_same_symbol_same_side_positions_are_isolated_by_strategy_instance(side: Side) -> None:
    owner = _owner()
    _open_position(owner, LEFT, side)
    _open_position(owner, RIGHT, side)

    snapshot = owner.snapshot()

    assert snapshot.coverage == "complete"
    assert len(snapshot.positions or ()) == 2
    assert len({item.identity for item in snapshot.positions or ()}) == 2
    assert {item.side for item in snapshot.positions or ()} == {
        PositionSide.LONG if side is Side.BUY else PositionSide.SHORT
    }


def test_same_symbol_close_and_protection_mutations_are_member_local() -> None:
    owner = _owner()
    for strategy_instance_id in owner.membership:
        order = owner.submit(
            strategy_instance_id,
            OrderRequest(side=Side.BUY, order_type=OrderType.MARKET, quantity=1.0),
            source_module=strategy_instance_id,
            created_at=NOW,
            current_index=0,
            mark_price=100.0,
        )
        assert order is not None
        assert order.status.value == "pending"
    owner.process_bars({member_id: _bar(NOW) for member_id in owner.membership}, 1)
    assert len(owner.snapshot().positions or ()) == 2

    assert owner.update_position(
        LEFT,
        PositionUpdateRequest(stop_loss=95.0, take_profit=110.0),
        timestamp=NOW,
        source_module=LEFT,
        mark_price=100.0,
    )
    assert owner.update_position(
        RIGHT,
        PositionUpdateRequest(stop_loss=90.0, take_profit=120.0),
        timestamp=NOW,
        source_module=RIGHT,
        mark_price=100.0,
    )
    protected = {
        item.identity.strategy_instance_id: item for item in owner.snapshot().positions or ()
    }
    assert (protected[LEFT].stop_loss, protected[LEFT].take_profit) == (95.0, 110.0)
    assert (protected[RIGHT].stop_loss, protected[RIGHT].take_profit) == (90.0, 120.0)

    close_snapshot = owner.snapshot()
    left_position = next(
        item
        for item in close_snapshot.positions or ()
        if item.identity.strategy_instance_id == LEFT
    )
    closed = owner.close_position(
        close_snapshot,
        left_position,
        NOW + timedelta(minutes=1),
        105.0,
        "synthetic_close",
    )
    assert closed is not None and closed.position_closed
    remaining = owner.snapshot().positions
    assert remaining is not None and len(remaining) == 1
    assert remaining[0].identity.strategy_instance_id == RIGHT
    assert (remaining[0].stop_loss, remaining[0].take_profit) == (90.0, 120.0)


def test_snapshot_close_rejects_stale_position_after_replacement() -> None:
    owner = _owner()
    _open_position(owner, LEFT, Side.BUY)
    snapshot_a = owner.snapshot()
    position_a = next(
        item for item in snapshot_a.positions or () if item.identity.strategy_instance_id == LEFT
    )

    closed_a = owner.close_position(
        snapshot_a,
        position_a,
        NOW + timedelta(minutes=1),
        105.0,
        "replace",
    )
    assert closed_a is not None and closed_a.position_closed
    _open_position(owner, LEFT, Side.SELL)
    snapshot_b = owner.snapshot()
    position_b = next(
        item for item in snapshot_b.positions or () if item.identity.strategy_instance_id == LEFT
    )
    assert position_b.identity.core_position_id != position_a.identity.core_position_id
    state_version_before_stale_close = owner.state_version

    with pytest.raises(TypeError):
        owner.close_position(LEFT, NOW, 95.0, "unscoped-close")  # type: ignore[call-arg]

    with pytest.raises(PaperCompositionUnavailable, match="snapshot is stale"):
        owner.close_position(
            snapshot_a,
            position_a,
            NOW + timedelta(minutes=2),
            95.0,
            "stale-close",
        )

    after = owner.snapshot()
    remaining_b = next(
        item for item in after.positions or () if item.identity.strategy_instance_id == LEFT
    )
    assert remaining_b == position_b
    assert owner.state_version == state_version_before_stale_close


def test_complete_empty_snapshot_means_flat_and_is_immutable() -> None:
    owner = _owner()

    snapshot = owner.snapshot()

    assert snapshot.coverage == "complete"
    assert snapshot.positions == ()
    assert owner.is_current(snapshot)
    assert snapshot.membership_version == owner.membership_version
    with pytest.raises(AttributeError):
        getattr(owner, "broker")
    with pytest.raises(AttributeError):
        getattr(owner, "portfolio")
    with pytest.raises(AttributeError):
        getattr(owner, "position")


def test_snapshot_position_read_failure_returns_no_partial_positions_and_restores_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _owner(LEFT, RIGHT)
    _open_position(owner, LEFT, Side.BUY)
    _open_position(owner, RIGHT, Side.SELL)
    complete_before_failure = owner.snapshot()
    assert complete_before_failure.coverage == "complete"
    assert complete_before_failure.positions is not None
    assert {item.identity.strategy_instance_id for item in complete_before_failure.positions} == {
        LEFT,
        RIGHT,
    }
    saved_root = owner.capture_state()

    position_reads: list[str] = []

    class _PositionReadProbe:
        def __init__(self, member_id: str, position: Any, *, fail: bool = False) -> None:
            self.member_id = member_id
            self._position = position
            self.fail = fail

        @property
        def position(self) -> Any:
            position_reads.append(self.member_id)
            if self.fail:
                raise RuntimeError(f"synthetic position read failure for {self.member_id}")
            return self._position

    for member_id in owner.membership:
        broker = owner._brokers[member_id]  # type: ignore[attr-defined]
        original_portfolio = broker.portfolio
        monkeypatch.setattr(
            broker,
            "portfolio",
            _PositionReadProbe(
                member_id,
                original_portfolio.position,
                fail=member_id == RIGHT,
            ),
        )

    state_version_before_failure = owner.state_version
    failed_snapshot = owner.snapshot()

    assert position_reads == [LEFT, RIGHT]
    assert failed_snapshot.coverage == "recovering"
    assert failed_snapshot.positions is None
    assert not owner.is_current(failed_snapshot)
    assert owner.state_version > state_version_before_failure

    recovering_state_version = owner.state_version
    with pytest.raises(PaperCompositionUnavailable):
        owner.capture_state()
    with pytest.raises(PaperCompositionUnavailable):
        owner.process_bar(LEFT, _bar(NOW), 2)
    assert owner.state_version == recovering_state_version

    candidate = _owner(LEFT, RIGHT)
    candidate.restore_state(saved_root)
    restored_snapshot = candidate.snapshot()
    assert restored_snapshot.coverage == "complete"
    assert restored_snapshot.positions == complete_before_failure.positions


def test_public_mutation_results_are_detached_immutable_snapshots() -> None:
    owner = _owner()
    order = owner.submit(
        LEFT,
        OrderRequest(side=Side.BUY, order_type=OrderType.MARKET, quantity=1.0),
        source_module=LEFT,
        created_at=NOW,
        current_index=0,
        mark_price=100.0,
    )
    assert order is not None
    with pytest.raises(FrozenInstanceError):
        order.status = order.status  # type: ignore[misc]
    pending = owner.pending_snapshots(LEFT)
    assert isinstance(pending, tuple)
    assert pending[0] is not order
    with pytest.raises(FrozenInstanceError):
        pending[0].status = pending[0].status  # type: ignore[misc]

    _open_position(owner, RIGHT, Side.BUY)
    position = owner.snapshot().positions
    assert position is not None
    with pytest.raises(FrozenInstanceError):
        position[0].quantity = position[0].quantity  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        position[0].identity.strategy_instance_id = LEFT  # type: ignore[misc]


class _NotifyingRLock:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.target_thread: int | None = None
        self.attempting = threading.Event()

    def __enter__(self) -> _NotifyingRLock:
        if threading.get_ident() == self.target_thread:
            self.attempting.set()
        self._lock.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self._lock.release()


def test_snapshot_waits_for_entire_multibroker_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    member_ids = _member_ids(4)
    owner = _owner(*member_ids)
    for member_id in member_ids:
        _queue_market_order(owner, member_id)
    first_processed = threading.Event()
    finish_batch = threading.Event()
    lock = _NotifyingRLock()
    owner._lock = lock  # type: ignore[attr-defined]
    original = PaperCompositionOwner._process_one_locked

    def pause_after_first(
        current_owner: PaperCompositionOwner,
        strategy_instance_id: str,
        bar: Candle,
        index: int,
    ) -> None:
        original(current_owner, strategy_instance_id, bar, index)
        if strategy_instance_id == member_ids[0]:
            first_processed.set()
            assert finish_batch.wait(timeout=2)

    monkeypatch.setattr(PaperCompositionOwner, "_process_one_locked", pause_after_first)
    bars = {member_id: _bar(NOW) for member_id in owner.membership}
    worker = threading.Thread(target=lambda: owner.process_bars(bars, 1))
    worker.start()
    try:
        assert first_processed.wait(timeout=2)

        result: list[Any] = []

        def read_snapshot() -> None:
            lock.target_thread = threading.get_ident()
            result.append(owner.snapshot())

        reader = threading.Thread(target=read_snapshot)
        reader.start()
        assert lock.attempting.wait(timeout=2)
        assert result == []
    finally:
        finish_batch.set()
        worker.join(timeout=2)
        if "reader" in locals():
            reader.join(timeout=2)

    assert not worker.is_alive()
    assert result and result[0].coverage == "complete"
    assert result[0].state_version == len(member_ids) + 1
    assert {item.identity.strategy_instance_id for item in result[0].positions or ()} == set(
        member_ids
    )


def test_submit_projection_is_created_before_releasing_composition_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _owner()
    lock = _NotifyingRLock()
    owner._lock = lock  # type: ignore[attr-defined]
    projection_started = threading.Event()
    finish_projection = threading.Event()
    calls = 0
    original = OrderSnapshot.from_order

    def block_on_public_projection(cls: type[OrderSnapshot], order: Any) -> OrderSnapshot:
        nonlocal calls
        calls += 1
        if calls == 2:
            projection_started.set()
            assert finish_projection.wait(timeout=2)
        return original(order)

    monkeypatch.setattr(OrderSnapshot, "from_order", classmethod(block_on_public_projection))
    submitted: list[OrderSnapshot | None] = []

    def submit_order() -> None:
        submitted.append(
            owner.submit(
                LEFT,
                OrderRequest(side=Side.BUY, order_type=OrderType.MARKET, quantity=1.0),
                source_module=LEFT,
                created_at=NOW,
                current_index=0,
                mark_price=100.0,
            )
        )

    writer = threading.Thread(target=submit_order)
    writer.start()
    reader_result: list[Any] = []
    try:
        assert projection_started.wait(timeout=2)

        def read_snapshot() -> None:
            lock.target_thread = threading.get_ident()
            reader_result.append(owner.snapshot())

        reader = threading.Thread(target=read_snapshot)
        reader.start()
        assert lock.attempting.wait(timeout=2)
        assert reader_result == []
    finally:
        finish_projection.set()
        writer.join(timeout=2)
        if "reader" in locals():
            reader.join(timeout=2)

    assert not writer.is_alive()
    assert submitted and submitted[0] is not None
    assert reader_result and reader_result[0].coverage == "complete"


@pytest.mark.parametrize(
    ("failure_index", "failure_point"),
    [(0, "first"), (2, "middle"), (3, "last")],
    ids=["after-first-member", "after-middle-member", "after-last-member"],
)
def test_failure_after_each_batch_member_fences_snapshot_until_fresh_restore(
    monkeypatch: pytest.MonkeyPatch,
    failure_index: int,
    failure_point: str,
) -> None:
    member_ids = _member_ids(4)
    owner = _owner(*member_ids)
    baseline = owner.capture_state()
    for member_id in member_ids:
        _queue_market_order(owner, member_id)
    original = PaperCompositionOwner._process_one_locked
    failing_member = member_ids[failure_index]
    processed_members: list[str] = []

    def fail_after_member(
        current_owner: PaperCompositionOwner,
        strategy_instance_id: str,
        bar: Candle,
        index: int,
    ) -> None:
        original(current_owner, strategy_instance_id, bar, index)
        processed_members.append(strategy_instance_id)
        if strategy_instance_id == failing_member:
            raise RuntimeError(f"synthetic failure after {failure_point} member")

    monkeypatch.setattr(PaperCompositionOwner, "_process_one_locked", fail_after_member)
    with pytest.raises(RuntimeError, match=f"after {failure_point} member"):
        owner.process_bars({member_id: _bar(NOW) for member_id in owner.membership}, 1)

    assert processed_members == list(member_ids[: failure_index + 1])
    failed_snapshot = owner.snapshot()
    assert failed_snapshot.coverage == "recovering"
    assert failed_snapshot.positions is None
    with pytest.raises(PaperCompositionUnavailable):
        owner.capture_state()
    with pytest.raises(PaperCompositionUnavailable):
        owner.process_bar(member_ids[0], _bar(NOW), 1)

    candidate = _owner(*member_ids)
    candidate.restore_state(baseline)
    restored = candidate.snapshot()
    assert restored.coverage == "complete"
    assert restored.positions == ()


def test_snapshot_version_state_version_and_owner_generation_fence_staleness() -> None:
    owner = _owner()
    before = owner.snapshot()
    assert owner.is_current(before)

    owner.process_bars({member_id: _bar(NOW) for member_id in owner.membership}, 1)
    after = owner.snapshot()

    assert after.snapshot_version > before.snapshot_version
    assert after.state_version > before.state_version
    assert after.membership_version == before.membership_version
    assert not owner.is_current(before)
    replacement = _owner()
    assert not replacement.is_current(after)


@pytest.mark.parametrize("member_count", [1, 2, 4])
def test_batch_requires_exact_membership_before_any_mutation(member_count: int) -> None:
    member_ids = _member_ids(member_count)
    owner = _owner(*member_ids)
    complete_batch = {member_id: _bar(NOW) for member_id in member_ids}
    incomplete_batch = dict(complete_batch)
    incomplete_batch.pop(member_ids[-1])
    extra_member_batch = {**complete_batch, "outside-membership": _bar(NOW)}

    for batch in (incomplete_batch, extra_member_batch):
        with pytest.raises(ValueError, match="exact composition membership"):
            owner.process_bars(batch, 1)
        assert owner.state_version == 0
        assert owner.snapshot().positions == ()


@pytest.mark.parametrize("member_count", [1, 2, 4])
def test_batch_validates_each_member_symbol_before_first_mutation(member_count: int) -> None:
    member_ids = _member_ids(member_count)
    owner = _owner(*member_ids)
    batch = {member_id: _bar(NOW) for member_id in member_ids}
    batch[member_ids[-1]] = _bar(NOW, "ETHUSDT")

    with pytest.raises(ValueError, match="exact member instrument"):
        owner.process_bars(batch, 1)

    assert owner.state_version == 0
    assert owner.snapshot().positions == ()


def test_single_bar_validates_symbol_before_mutation() -> None:
    owner = _owner()

    with pytest.raises(ValueError, match="exact member instrument"):
        owner.process_bar(LEFT, _bar(NOW, "ETHUSDT"), 1)

    assert owner.state_version == 0
    assert owner.snapshot().positions == ()


def test_clock_reentrant_mutation_cannot_produce_mixed_snapshot() -> None:
    owner = _owner()
    _queue_market_order(owner, LEFT)
    mutated = False

    def reentrant_clock() -> datetime:
        nonlocal mutated
        if not mutated:
            mutated = True
            owner.process_bars({member_id: _bar(NOW) for member_id in owner.membership}, 1)
        return NOW

    owner._clock = reentrant_clock  # type: ignore[attr-defined]

    snapshot = owner.snapshot()

    assert snapshot.coverage == "complete"
    assert snapshot.state_version == owner.state_version == 2
    assert snapshot.positions is not None and len(snapshot.positions) == 1
    assert snapshot.positions[0].identity.strategy_instance_id == LEFT
    assert owner.is_current(snapshot)


def test_restore_rejects_membership_or_composition_mismatch_without_mutation() -> None:
    source = _owner()
    root = source.capture_state()

    wrong_membership = _owner("strategy-other-a", "strategy-other-b")
    with pytest.raises(ValueError, match="membership"):
        wrong_membership.restore_state(root)
    assert wrong_membership.snapshot().coverage == "complete"
    assert wrong_membership.snapshot().positions == ()

    wrong_composition = _owner(composition_id="different-composition")
    with pytest.raises(ValueError, match="composition identity"):
        wrong_composition.restore_state(root)
    assert wrong_composition.snapshot().positions == ()


def test_restore_checks_exact_member_ids_before_any_core_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _owner("strategy-instance-a", "strategy-instance-c")
    candidate = _owner("strategy-instance-a", "strategy-instance-b")
    source_root = source.capture_state()
    mismatched_root = PaperCompositionStateRoot.create(
        composition_id=source_root.composition_id,
        source_owner_generation=source_root.source_owner_generation,
        membership_version=candidate.membership_version,
        state_version=source_root.state_version,
        members=source_root.members,
    )
    import composition_owner_reference as owner_module

    restore_calls = 0

    def record_restore_call(broker: Any, broker_state: Any) -> None:
        nonlocal restore_calls
        restore_calls += 1
        raise AssertionError("Core restore must not begin for mismatched member IDs")

    monkeypatch.setattr(owner_module, "restore_broker_state", record_restore_call)
    with pytest.raises(ValueError, match="state root membership mismatch"):
        candidate.restore_state(mismatched_root)

    assert restore_calls == 0
    assert candidate.state_version == 0
    snapshot = candidate.snapshot()
    assert snapshot.coverage == "complete"
    assert snapshot.positions == ()
    assert candidate.capture_state().state_version == 0


def test_restore_requires_a_new_owner_generation() -> None:
    source = _owner(owner_generation="same-incarnation")
    old_snapshot = source.snapshot()
    root = source.capture_state()
    same_generation = _owner(owner_generation="same-incarnation")

    with pytest.raises(ValueError, match="new owner generation"):
        same_generation.restore_state(root)
    assert same_generation.snapshot().coverage == "complete"
    assert not same_generation.is_current(old_snapshot)


@pytest.mark.parametrize("corrupt_index", [0, 2, 3], ids=["first", "middle", "last"])
def test_restore_rejects_corrupt_member_digest_before_changing_candidate(
    monkeypatch: pytest.MonkeyPatch,
    corrupt_index: int,
) -> None:
    member_ids = _member_ids(4)
    source = _owner(*member_ids)
    root = source.capture_state()
    members = list(root.members)
    members[corrupt_index] = replace(members[corrupt_index], state_digest="0" * 64)
    corrupt_root = PaperCompositionStateRoot.create(
        composition_id=root.composition_id,
        source_owner_generation=root.source_owner_generation,
        membership_version=root.membership_version,
        state_version=root.state_version,
        members=tuple(members),
    )
    candidate = _owner(*member_ids)
    import composition_owner_reference as owner_module

    restore_calls = 0

    def record_restore_call(broker: Any, broker_state: Any) -> None:
        nonlocal restore_calls
        restore_calls += 1
        raise AssertionError("Core restore must not begin for corrupt member data")

    monkeypatch.setattr(owner_module, "restore_broker_state", record_restore_call)

    with pytest.raises(ValueError, match="member state digest"):
        candidate.restore_state(corrupt_root)

    assert restore_calls == 0
    assert candidate.state_version == 0
    snapshot = candidate.snapshot()
    assert snapshot.coverage == "complete"
    assert snapshot.positions == ()
    assert candidate.capture_state().state_version == 0


def test_state_root_rejects_corrupt_root_digest() -> None:
    root = _owner().capture_state()

    with pytest.raises(ValueError, match="root digest"):
        replace(root, root_digest="f" * 64)


@pytest.mark.parametrize(
    ("failure_index", "failure_point"),
    [(0, "first"), (2, "middle"), (3, "last")],
    ids=["after-first-member", "after-middle-member", "after-last-member"],
)
def test_restore_failure_after_each_member_does_not_publish_partial_state(
    monkeypatch: pytest.MonkeyPatch,
    failure_index: int,
    failure_point: str,
) -> None:
    member_ids = _member_ids(4)
    source = _owner(*member_ids)
    for member_id in member_ids:
        _open_position(source, member_id, Side.BUY)
    root = source.capture_state()
    candidate = _owner(*member_ids)

    import composition_owner_reference as owner_module

    original = owner_module.restore_broker_state
    failing_member = member_ids[failure_index]
    failing_broker_id = id(candidate._brokers[failing_member])  # type: ignore[attr-defined]

    def fail_after_member_restore(broker: Any, broker_state: Any) -> None:
        original(broker, broker_state)
        if id(broker) == failing_broker_id:
            raise RuntimeError(f"synthetic restore failure after {failure_point} member")

    monkeypatch.setattr(owner_module, "restore_broker_state", fail_after_member_restore)

    with pytest.raises(RuntimeError, match=f"after {failure_point} member"):
        candidate.restore_state(root)

    snapshot = candidate.snapshot()
    assert snapshot.coverage == "recovering"
    assert snapshot.positions is None


def test_restore_requires_pristine_candidate_owner() -> None:
    source = _owner()
    root = source.capture_state()
    candidate = _owner()
    candidate.cancel_all(LEFT, NOW, "no-op mutation still advances owner state")

    with pytest.raises(PaperCompositionUnavailable, match="pristine"):
        candidate.restore_state(root)
