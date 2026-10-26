from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from signal_bot.health import HealthServer, _json_default

UTC = timezone.utc
NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def test_health_server_http_lifecycle_and_fail_closed_status() -> None:
    snapshot: dict[str, object] = {
        "status": "ok",
        "cycles": 3,
        "pairs_processed": True,
        "signals_sent": 2,
        "pair_errors": "invalid",
        "last_cycle_at": NOW,
    }

    assert _json_default(NOW) == NOW.isoformat()
    assert _json_default(3) == "3"

    server = HealthServer(lambda: snapshot, port=0)
    server.start()
    server.start()
    host, port = server.address

    try:
        with urlopen(f"http://{host}:{port}/health", timeout=5) as response:
            assert response.status == 200
            assert response.headers["Content-Type"] == "application/json"
            payload = json.loads(response.read())
            assert payload["last_cycle_at"] == NOW.isoformat()

        with urlopen(f"http://{host}:{port}/metrics", timeout=5) as response:
            metrics = response.read().decode()
            assert "signal_bot_cycles 3" in metrics
            assert "signal_bot_pairs_processed 1" in metrics
            assert "signal_bot_pair_errors 0" in metrics
            assert "signal_bot_healthy 1" in metrics

        with pytest.raises(HTTPError) as caught:
            urlopen(f"http://{host}:{port}/missing", timeout=5)
        assert caught.value.code == 404

        snapshot["status"] = "degraded"
        status, content_type, body = server.response("/health")
        assert status == 503
        assert content_type == "application/json"
        assert json.loads(body)["status"] == "degraded"

        status, content_type, body = server.response("/metrics")
        assert status == 200
        assert content_type == "text/plain; version=0.0.4"
        assert b"signal_bot_healthy 0" in body

        assert server.response("/missing") == (404, "text/plain", b"not found")
    finally:
        server.close()


def test_health_server_authenticates_before_reading_health() -> None:
    token = "a" * 64
    snapshots_read = 0

    def health() -> dict[str, object]:
        nonlocal snapshots_read
        snapshots_read += 1
        return {"status": "ok"}

    server = HealthServer(health, port=0, auth_token=token)
    server.start()
    host, port = server.address
    endpoint = f"http://{host}:{port}/health"

    try:
        with pytest.raises(HTTPError) as caught:
            urlopen(endpoint, timeout=5)
        assert caught.value.code == 401
        assert snapshots_read == 0

        request = Request(endpoint, headers={"Authorization": f"Bearer {token}"})
        with urlopen(request, timeout=5) as response:
            assert response.status == 200
            assert json.loads(response.read()) == {"status": "ok"}
        assert snapshots_read == 1
    finally:
        server.close()
