from __future__ import annotations

import threading
from collections.abc import Callable

from .contracts import AcceptanceOwnerError, TERMINAL_STATUSES
from .owner import SystemAcceptanceOwner


class SystemAcceptanceDaemon:
    """Browser-independent owner loop for durable acceptance progress."""

    def __init__(
        self,
        owner: SystemAcceptanceOwner,
        *,
        poll_seconds: float = 1.0,
        on_error: Callable[[AcceptanceOwnerError], None] | None = None,
    ) -> None:
        if not isinstance(owner, SystemAcceptanceOwner):
            raise TypeError("owner must be SystemAcceptanceOwner")
        if (
            isinstance(poll_seconds, bool)
            or not isinstance(poll_seconds, (int, float))
            or not 0.05 <= float(poll_seconds) <= 60.0
        ):
            raise ValueError("poll_seconds must be between 0.05 and 60")
        self.owner = owner
        self.poll_seconds = float(poll_seconds)
        self.on_error = on_error
        self._stop = threading.Event()

    def run_forever(self) -> None:
        """Recover persisted work once, then execute bounded owner slices until stopped."""
        try:
            self.owner.recover_after_restart()
        except AcceptanceOwnerError as exc:
            self._report(exc)
        while not self._stop.is_set():
            try:
                snapshot = self.owner.step()
            except AcceptanceOwnerError as exc:
                self._report(exc)
                self._stop.wait(self.poll_seconds)
                continue
            if snapshot is None or snapshot.status in TERMINAL_STATUSES:
                self._stop.wait(self.poll_seconds)
                continue
            self._stop.wait(self.poll_seconds)

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def _report(self, error: AcceptanceOwnerError) -> None:
        if self.on_error is not None:
            self.on_error(error)
