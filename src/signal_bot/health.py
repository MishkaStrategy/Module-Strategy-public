from __future__ import annotations

import hmac
import ipaddress
import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class HealthServer:
    """Small dependency-free health and Prometheus-text endpoint."""

    def __init__(
        self,
        health: Callable[[], dict[str, object]],
        *,
        host: str = "127.0.0.1",
        port: int = 8080,
        auth_token: str | None = None,
    ) -> None:
        if not isinstance(host, str):
            raise ValueError("health server bind host must be a string")
        normalized_host = host.strip().lower()
        if not normalized_host or any(character.isspace() for character in normalized_host):
            raise ValueError("health server bind host must be non-empty and contain no whitespace")
        if ":" in normalized_host:
            raise ValueError("IPv6 health binds are not supported")
        if normalized_host == "localhost":
            # ThreadingHTTPServer uses AF_INET and must not resolve localhost to ::1.
            normalized_host = "127.0.0.1"
        try:
            is_loopback = ipaddress.ip_address(normalized_host).is_loopback
        except ValueError:
            is_loopback = False

        if auth_token is not None and not isinstance(auth_token, str):
            raise ValueError("health authorization token must be a string")
        normalized_token = auth_token if auth_token is not None else ""
        if normalized_token and (
            len(normalized_token) < 32
            or not normalized_token.isascii()
            or any(character.isspace() for character in normalized_token)
        ):
            raise ValueError("health authorization token must be at least 32 ASCII characters")
        if not is_loopback and not normalized_token:
            raise ValueError("non-loopback health binds require SIGNAL_BOT_HEALTH_TOKEN")

        self.health = health
        self._auth_token = normalized_token or None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if owner._auth_token is not None:
                    authorization = self.headers.get("Authorization", "")
                    provided = authorization.encode("utf-8", "surrogatepass")
                    expected = f"Bearer {owner._auth_token}".encode("ascii")
                    if not hmac.compare_digest(provided, expected):
                        body = b"unauthorized\n"
                        self.send_response(401)
                        self.send_header("WWW-Authenticate", "Bearer")
                        self.send_header("Content-Type", "text/plain")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                status, content_type, body = owner.response(self.path)
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer((normalized_host, port), Handler)
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="signal-bot-health", daemon=True
        )
        self._thread.start()

    def response(self, path: str) -> tuple[int, str, bytes]:
        snapshot = self.health()
        if path == "/health":
            body = json.dumps(snapshot, default=_json_default, sort_keys=True).encode()
            return (
                200 if snapshot.get("status") == "ok" else 503,
                "application/json",
                body,
            )
        if path == "/metrics":
            return 200, "text/plain; version=0.0.4", self._metrics(snapshot).encode()
        return 404, "text/plain", b"not found"

    def close(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @staticmethod
    def _metrics(snapshot: dict[str, object]) -> str:
        rows = []
        for key in ("cycles", "pairs_processed", "signals_sent", "pair_errors"):
            value = snapshot.get(key, 0)
            rows.append(f"signal_bot_{key} {int(value) if isinstance(value, (int, bool)) else 0}")
        rows.append(f"signal_bot_healthy {1 if snapshot.get('status') == 'ok' else 0}")
        return "\n".join(rows) + "\n"
