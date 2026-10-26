from __future__ import annotations

import threading

from .daemon import SystemAcceptanceDaemon
from .trusted_http import PinnedSystemAcceptanceHttpServer


class SystemAcceptanceService:
    """Process-level composition that owns daemon and Control Plane transport lifecycles."""

    def __init__(
        self,
        daemon: SystemAcceptanceDaemon,
        http: PinnedSystemAcceptanceHttpServer,
    ) -> None:
        if not isinstance(daemon, SystemAcceptanceDaemon):
            raise TypeError("daemon must be SystemAcceptanceDaemon")
        if not isinstance(http, PinnedSystemAcceptanceHttpServer):
            raise TypeError("http must be PinnedSystemAcceptanceHttpServer")
        self._daemon = daemon
        self._http = http
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._closed = False

    def start(self) -> None:
        """Start owner progress before accepting Control Plane requests."""
        with self._lock:
            if self._closed:
                raise RuntimeError("System Acceptance service cannot restart after close")
            if self._thread is not None:
                return
            thread = threading.Thread(
                target=self._daemon.run_forever,
                name="system-acceptance-daemon",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            try:
                self._http.start()
            except BaseException:
                self._closed = True
                self._daemon.stop()
                try:
                    self._http.close()
                finally:
                    thread.join(timeout=5)
                    self._thread = None
                raise

    def close(self) -> None:
        """Stop accepting requests, then stop and join the owner loop."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._http.close()
            self._daemon.stop()
            thread = self._thread
            self._thread = None
        if thread is not None:
            thread.join(timeout=5)

    @property
    def running(self) -> bool:
        thread = self._thread
        return (
            not self._closed
            and thread is not None
            and thread.is_alive()
            and not self._daemon.stopped
        )
