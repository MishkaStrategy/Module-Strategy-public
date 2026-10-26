from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from modular_control_plane.paper_positions import (
    PaperPositionFact,
    PaperPositionsAvailableSnapshot,
)
from modular_control_plane.server import ServerSettings, create_server
from modular_control_plane.service import ControlPlaneService

NOW = datetime(2026, 10, 23, 5, 0, tzinfo=UTC)
READER = "paper-position-reader-token"
PATH = "/api/control/v1/paper/positions"


def _position(
    owner_position_id: str = "position-1",
    *,
    instrument_id: str = "venue:ABC",
    side: str = "long",
    quantity: str = "1.25",
    strategy_id: str | None = "strategy-1",
) -> dict[str, object]:
    return {
        "owner_position_id": owner_position_id,
        "instrument_id": instrument_id,
        "instrument_version": "instrument-v1",
        "side": side,
        "quantity": quantity,
        "quantity_unit": "contracts",
        "strategy_id": strategy_id,
    }


def _snapshot(
    *,
    positions: tuple[dict[str, object], ...] = (),
    status: str = "available",
    **overrides: object,
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "status": status,
        "owner_id": "paper-execution",
        "composition_id": "composition-1",
        "portfolio_id": "portfolio-1",
        "owner_generation": "generation-2",
        "membership_version": "membership-8",
        "snapshot_version": "snapshot-17",
        "state_version": "state-23",
        "as_of": "2026-10-23T05:00:00Z",
        "coverage": "complete",
        "positions": positions,
    }
    if status != "available":
        value = {"schema_version": 1, "status": status, "reason_code": "owner_not_ready"}
    value.update(overrides)
    return value


def _client(reader: Callable[[], object] | None = None) -> TestClient:
    return TestClient(
        create_server(
            ServerSettings(reader_token=READER),
            service=ControlPlaneService(clock=lambda: NOW),
            paper_positions_reader=reader,
        )
    )


def test_unattached_positions_reader_returns_sanitized_no_store_503() -> None:
    response = _client().get(PATH, headers={"Authorization": f"Bearer {READER}"})

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["error"]["code"] == "CONTROL_PAPER_POSITIONS_UNAVAILABLE"
    assert response.json()["error"]["technical_details"] == {}


def test_reader_authorization_runs_before_owner_snapshot_reader() -> None:
    calls = 0

    def reader() -> object:
        nonlocal calls
        calls += 1
        return _snapshot()

    response = _client(reader).get(PATH)

    assert response.status_code == 401
    assert calls == 0


def test_positions_refuse_to_read_without_reader_auth_configuration() -> None:
    calls = 0

    def reader() -> object:
        nonlocal calls
        calls += 1
        return _snapshot()

    client = TestClient(
        create_server(
            ServerSettings(),
            service=ControlPlaneService(clock=lambda: NOW),
            paper_positions_reader=reader,
        )
    )
    response = client.get(PATH)

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["error"]["code"] == "CONTROL_PAPER_POSITIONS_AUTH_NOT_CONFIGURED"
    assert calls == 0


def test_complete_empty_snapshot_is_not_confused_with_unavailable() -> None:
    response = _client(_snapshot).get(PATH, headers={"Authorization": f"Bearer {READER}"})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    expected = _snapshot()
    expected["positions"] = []
    assert response.json() == expected


@pytest.mark.parametrize("status", ["unavailable", "recovering", "stale"])
def test_owner_statuses_are_preserved(status: str) -> None:
    response = _client(lambda: _snapshot(status=status)).get(
        PATH, headers={"Authorization": f"Bearer {READER}"}
    )

    assert response.status_code == 200
    assert response.json() == _snapshot(status=status)


def test_same_symbol_positions_and_unknown_strategy_attribution_are_preserved() -> None:
    rows = (
        _position("position-long", side="long", strategy_id=None),
        _position("position-short", side="short", quantity="2", strategy_id="strategy-2"),
    )
    response = _client(lambda: _snapshot(positions=rows)).get(
        PATH, headers={"Authorization": f"Bearer {READER}"}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["positions"] == [
        {"schema_version": 1, **rows[0]},
        {"schema_version": 1, **rows[1]},
    ]
    assert payload["positions"][0]["strategy_id"] is None
    assert payload["positions"][1]["instrument_id"] == payload["positions"][0]["instrument_id"]


@pytest.mark.parametrize(
    "snapshot",
    [
        _snapshot(
            positions=(_position("duplicate"), _position("duplicate", instrument_id="venue:XYZ"))
        ),
        _snapshot(coverage="partial"),
        _snapshot(positions=(_position(quantity="NaN"),)),
        _snapshot(positions=(_position(quantity="0"),)),
        _snapshot(runtime_id="runtime-1"),
    ],
)
def test_malformed_incomplete_or_legacy_owner_data_fails_closed(
    snapshot: dict[str, object],
) -> None:
    response = _client(lambda: snapshot).get(PATH, headers={"Authorization": f"Bearer {READER}"})

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["error"]["code"] == "CONTROL_PAPER_POSITIONS_UNAVAILABLE"
    assert "runtime-1" not in response.text


def test_position_count_limit_rejects_without_truncating() -> None:
    rows = tuple(_position(f"position-{index}") for index in range(501))
    response = _client(lambda: _snapshot(positions=rows)).get(
        PATH, headers={"Authorization": f"Bearer {READER}"}
    )

    assert response.status_code == 503
    assert "positions" not in response.json()


def test_serialized_response_limit_rejects_without_truncating() -> None:
    oversized = tuple(
        _position(
            f"{index:04d}-" + "x" * 248,
            instrument_id="i" * 256,
            strategy_id="s" * 256,
        )
        | {"instrument_version": "v" * 256, "quantity_unit": "u" * 64}
        for index in range(400)
    )
    response = _client(lambda: _snapshot(positions=oversized)).get(
        PATH, headers={"Authorization": f"Bearer {READER}"}
    )

    assert response.status_code == 503
    assert "positions" not in response.json()


@pytest.mark.parametrize("construction", ["model_copy", "model_construct"])
def test_snapshot_model_instances_are_revalidated(construction: str) -> None:
    valid = PaperPositionsAvailableSnapshot.model_validate(_snapshot())
    if construction == "model_copy":
        invalid = valid.model_copy(update={"coverage": "partial"})
    else:
        raw = _snapshot(coverage="partial")
        raw["as_of"] = NOW
        invalid = PaperPositionsAvailableSnapshot.model_construct(
            **raw  # type: ignore[arg-type]
        )

    response = _client(lambda: invalid).get(PATH, headers={"Authorization": f"Bearer {READER}"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CONTROL_PAPER_POSITIONS_UNAVAILABLE"


@pytest.mark.parametrize("construction", ["model_copy", "model_construct"])
def test_nested_position_model_instances_are_revalidated(construction: str) -> None:
    valid = PaperPositionFact.model_validate(_position())
    if construction == "model_copy":
        invalid = valid.model_copy(update={"quantity": "NaN"})
    else:
        invalid = PaperPositionFact.model_construct(
            **(_position(quantity="NaN"))  # type: ignore[arg-type]
        )
    if construction == "model_copy":
        snapshot = PaperPositionsAvailableSnapshot.model_validate(
            _snapshot(positions=(_position(),))
        ).model_copy(update={"positions": (invalid,)})
    else:
        raw = _snapshot()
        raw["as_of"] = NOW
        raw["positions"] = (invalid,)
        snapshot = PaperPositionsAvailableSnapshot.model_construct(
            **raw  # type: ignore[arg-type]
        )

    response = _client(lambda: snapshot).get(PATH, headers={"Authorization": f"Bearer {READER}"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CONTROL_PAPER_POSITIONS_UNAVAILABLE"


def test_position_generator_is_rejected_without_consumption() -> None:
    consumed = 0

    def rows():
        nonlocal consumed
        consumed += 1
        yield _position()

    raw = _snapshot()
    raw["positions"] = rows()

    response = _client(lambda: raw).get(PATH, headers={"Authorization": f"Bearer {READER}"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CONTROL_PAPER_POSITIONS_UNAVAILABLE"
    assert consumed == 0


def test_owner_transport_failure_is_sanitized() -> None:
    def fail() -> object:
        raise RuntimeError("private /Users/secret/positions.json")

    response = _client(fail).get(PATH, headers={"Authorization": f"Bearer {READER}"})

    assert response.status_code == 503
    assert "private" not in response.text
    assert "/Users/secret" not in response.text
