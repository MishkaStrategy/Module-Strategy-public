from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from signal_bot.health import HealthServer


def test_metrics_payload() -> None:
    snapshot = {
        "status": "ok",
        "cycles": 2,
        "pairs_processed": 4,
        "signals_sent": 1,
        "pair_errors": 0,
    }
    metrics = HealthServer._metrics(snapshot)
    assert "signal_bot_cycles 2" in metrics
    assert "signal_bot_pairs_processed 4" in metrics
    assert "signal_bot_healthy 1" in metrics


def test_health_response_and_lifecycle_without_real_socket() -> None:
    fake_server = MagicMock()
    fake_server.server_address = ("127.0.0.1", 8080)
    with patch("signal_bot.health.ThreadingHTTPServer", return_value=fake_server):
        server = HealthServer(lambda: {"status": "ok", "cycles": 1})
        status, content_type, body = server.response("/health")
        assert status == 200 and content_type == "application/json" and b'"ok"' in body
        assert server.response("/metrics")[0] == 200
        assert server.response("/missing") == (404, "text/plain", b"not found")
        assert server.address == ("127.0.0.1", 8080)
        server.start()
        server.start()
        server.close()
        fake_server.server_close.assert_called_once()

    with patch("signal_bot.health.ThreadingHTTPServer", return_value=fake_server):
        degraded = HealthServer(lambda: {"status": "degraded"})
        assert degraded.response("/health")[0] == 503


def test_close_unstarted_health_server_releases_socket_without_shutdown() -> None:
    fake_server = MagicMock()
    fake_server.server_address = ("127.0.0.1", 8080)
    with patch("signal_bot.health.ThreadingHTTPServer", return_value=fake_server):
        server = HealthServer(lambda: {"status": "ok"})
        server.close()

    fake_server.shutdown.assert_not_called()
    fake_server.server_close.assert_called_once()


@pytest.mark.parametrize("host", ("0.0.0.0", "192.0.2.10", "health.example.com"))
def test_health_server_rejects_remote_hosts_without_token_before_binding(host: str) -> None:
    with patch("signal_bot.health.ThreadingHTTPServer") as server_factory:
        with pytest.raises(ValueError, match="require SIGNAL_BOT_HEALTH_TOKEN"):
            HealthServer(lambda: {"status": "ok"}, host=host)
    server_factory.assert_not_called()


@pytest.mark.parametrize("host", ("127.0.0.1", "localhost", " LOCALHOST "))
def test_health_server_accepts_loopback_hosts(host: str) -> None:
    fake_server = MagicMock()
    expected_host = "127.0.0.1"
    fake_server.server_address = (expected_host, 8080)
    with patch("signal_bot.health.ThreadingHTTPServer", return_value=fake_server) as server_factory:
        server = HealthServer(lambda: {"status": "ok"}, host=host)

    server_factory.assert_called_once()
    assert server_factory.call_args.args[0] == (expected_host, 8080)
    assert server.address == fake_server.server_address


@pytest.mark.parametrize("host", ("0.0.0.0", "192.0.2.10", "health.example.com"))
def test_health_server_allows_remote_hosts_with_valid_token(host: str) -> None:
    fake_server = MagicMock()
    fake_server.server_address = (host, 8080)
    token = "a" * 64
    with patch("signal_bot.health.ThreadingHTTPServer", return_value=fake_server) as factory:
        server = HealthServer(lambda: {"status": "ok"}, host=host, auth_token=token)

    factory.assert_called_once()
    assert factory.call_args.args[0] == (host, 8080)
    assert server._auth_token == token


@pytest.mark.parametrize("token", ("short", "contains whitespace", "é" * 40))
def test_health_server_rejects_weak_token(token: str) -> None:
    with patch("signal_bot.health.ThreadingHTTPServer") as server_factory:
        with pytest.raises(ValueError, match="at least 32 ASCII characters"):
            HealthServer(lambda: {"status": "ok"}, host="0.0.0.0", auth_token=token)
    server_factory.assert_not_called()


def test_health_server_rejects_ipv6_bind_until_supported() -> None:
    with patch("signal_bot.health.ThreadingHTTPServer") as server_factory:
        with pytest.raises(ValueError, match="IPv6 health binds are not supported"):
            HealthServer(lambda: {"status": "ok"}, host="::1", auth_token="a" * 64)
    server_factory.assert_not_called()
