from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from modular_control_plane.paper_positions import (
    PaperPositionFact,
    PaperPositionsAvailableSnapshot,
    PaperPositionsRecoveringSnapshot,
    PaperPositionsSnapshot,
    PaperPositionsStaleSnapshot,
    PaperPositionsUnavailableSnapshot,
    paper_positions_snapshot_adapter,
)


def _available(**overrides: object) -> PaperPositionsAvailableSnapshot:
    values: dict[str, object] = {
        "owner_id": "paper-execution",
        "composition_id": "composition-1",
        "portfolio_id": "portfolio-1",
        "owner_generation": "generation-2",
        "membership_version": "membership-8",
        "snapshot_version": "snapshot-17",
        "state_version": "state-23",
        "as_of": datetime(2026, 10, 21, 12, tzinfo=timezone(timedelta(hours=7))),
        "coverage": "complete",
        "positions": (),
    }
    values.update(overrides)
    return PaperPositionsAvailableSnapshot(**values)  # type: ignore[arg-type]


def _position(**overrides: object) -> PaperPositionFact:
    values: dict[str, object] = {
        "owner_position_id": "position-1",
        "instrument_id": "venue:instrument:ABC",
        "instrument_version": "instrument-v4",
        "side": "long",
        "quantity": "0.125",
        "quantity_unit": "contracts",
        "strategy_id": "strategy-1",
    }
    values.update(overrides)
    return PaperPositionFact(**values)  # type: ignore[arg-type]


def test_complete_empty_snapshot_is_explicitly_available_and_utc() -> None:
    snapshot = _available()

    assert snapshot.status == "available"
    assert snapshot.coverage == "complete"
    assert snapshot.positions == ()
    assert snapshot.as_of == datetime(2026, 10, 21, 5, tzinfo=UTC)


def test_available_snapshot_requires_explicit_coverage() -> None:
    payload = _available().model_dump(mode="python")
    del payload["coverage"]

    with pytest.raises(ValidationError, match="coverage"):
        PaperPositionsAvailableSnapshot(**payload)


@pytest.mark.parametrize(
    "field_name",
    [
        "owner_id",
        "composition_id",
        "portfolio_id",
        "owner_generation",
        "membership_version",
        "snapshot_version",
        "state_version",
    ],
)
def test_available_snapshot_requires_scope_and_version_identities(field_name: str) -> None:
    payload = _available().model_dump(mode="python")
    del payload[field_name]

    with pytest.raises(ValidationError, match=field_name):
        PaperPositionsAvailableSnapshot(**payload)


@pytest.mark.parametrize("include_composition_id", [False, True])
def test_available_snapshot_rejects_legacy_runtime_id(
    include_composition_id: bool,
) -> None:
    payload = _available().model_dump(mode="python")
    if not include_composition_id:
        del payload["composition_id"]
    payload["runtime_id"] = "runtime-1"

    with pytest.raises(ValidationError):
        PaperPositionsAvailableSnapshot(**payload)


@pytest.mark.parametrize(
    ("status", "expected_type"),
    [
        ("unavailable", PaperPositionsUnavailableSnapshot),
        ("recovering", PaperPositionsRecoveringSnapshot),
        ("stale", PaperPositionsStaleSnapshot),
    ],
)
def test_nonavailable_states_are_discriminated_and_never_flat(
    status: str, expected_type: type[object]
) -> None:
    result: PaperPositionsSnapshot = paper_positions_snapshot_adapter.validate_python(
        {"status": status, "reason_code": "owner_snapshot_not_current"}
    )

    assert isinstance(result, expected_type)
    assert result.status == status
    with pytest.raises(ValidationError):
        paper_positions_snapshot_adapter.validate_python(
            {"status": status, "reason_code": "not_ready", "positions": []}
        )


@pytest.mark.parametrize("status", ["unavailable", "recovering", "stale"])
@pytest.mark.parametrize("reason_code", [" ", "Owner is not ready", "not ready"])
def test_nonavailable_reason_codes_must_be_stable_identifiers(
    status: str, reason_code: str
) -> None:
    with pytest.raises(ValidationError):
        paper_positions_snapshot_adapter.validate_python(
            {"status": status, "reason_code": reason_code}
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"coverage": "partial"},
        {"as_of": datetime(2026, 10, 21)},
        {"owner_id": " owner "},
        {"composition_id": ""},
        {"portfolio_id": "  "},
        {"owner_generation": ""},
        {"membership_version": ""},
        {"membership_version": " membership-8 "},
        {"snapshot_version": ""},
        {"state_version": ""},
        {"positions": (), "next_page": "opaque-token"},
        {"positions": (), "mark_price": "1.2"},
        {"positions": (), "pnl": "0"},
    ],
)
def test_available_snapshot_rejects_incomplete_or_unowned_facts(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _available(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"owner_position_id": ""},
        {"instrument_id": " "},
        {"instrument_version": ""},
        {"side": "buy"},
        {"quantity": "0"},
        {"quantity": "-1"},
        {"quantity": "NaN"},
        {"quantity": "Infinity"},
        {"quantity": " 1 "},
        {"quantity": 1},
        {"quantity_unit": " "},
        {"strategy_id": " "},
        {"mark_price": "10"},
        {"notional": "10"},
        {"pnl": "10"},
    ],
)
def test_position_facts_reject_malformed_values_and_derived_fields(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _position(**overrides)


def test_available_snapshot_rejects_duplicate_owner_position_ids() -> None:
    first = _position()
    second = _position(instrument_id="venue:instrument:XYZ")

    with pytest.raises(ValidationError, match="unique"):
        _available(positions=(first, second))


def test_owner_position_identity_and_attribution_remain_owner_scoped_facts() -> None:
    first = _position(strategy_id=None)
    first_snapshot = _available(positions=(first,))
    second_snapshot = _available(
        snapshot_version="snapshot-18",
        positions=(first,),
    )

    assert first.owner_position_id == "position-1"
    assert first.strategy_id is None
    assert first_snapshot.positions == (first,)
    assert second_snapshot.positions == (first,)


def test_same_symbol_owner_positions_remain_distinct_facts() -> None:
    instrument = "venue:instrument:ABC"
    long_a = _position(
        owner_position_id="owner-pos-a",
        instrument_id=instrument,
        side="long",
        quantity="1",
        strategy_id="strategy-a",
    )
    short_b = _position(
        owner_position_id="owner-pos-b",
        instrument_id=instrument,
        side="short",
        quantity="2",
        strategy_id="strategy-b",
    )
    long_c = _position(
        owner_position_id="owner-pos-c",
        instrument_id=instrument,
        side="long",
        quantity="3",
        strategy_id="strategy-c",
    )

    snapshot = _available(positions=(long_a, short_b, long_c))

    assert snapshot.positions == (long_a, short_b, long_c)
    assert tuple(position.side for position in snapshot.positions) == (
        "long",
        "short",
        "long",
    )
    assert tuple(position.strategy_id for position in snapshot.positions) == (
        "strategy-a",
        "strategy-b",
        "strategy-c",
    )


def test_unattributed_owner_fact_remains_unknown_in_complete_inventory() -> None:
    unattributed = _position(owner_position_id="unattributed-owner-position", strategy_id=None)

    snapshot = _available(positions=(unattributed,))

    assert snapshot.coverage == "complete"
    assert snapshot.positions == (unattributed,)
    assert snapshot.positions[0].strategy_id is None


def test_as_of_has_no_contract_level_freshness_threshold() -> None:
    old_observation = datetime(2000, 1, 1, tzinfo=UTC)

    snapshot = _available(as_of=old_observation)

    assert snapshot.status == "available"
    assert snapshot.as_of == old_observation


def test_contract_and_positions_are_immutable() -> None:
    position = _position()
    snapshot = _available(positions=(position,))

    with pytest.raises(ValidationError):
        position.quantity = "2"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        snapshot.positions = ()  # type: ignore[misc]
    assert isinstance(snapshot.positions, tuple)


def test_snapshot_union_dispatches_available_variant() -> None:
    payload = _available().model_dump(mode="json")

    decoded = paper_positions_snapshot_adapter.validate_python(payload)

    assert isinstance(decoded, PaperPositionsAvailableSnapshot)
    assert decoded.positions == ()
