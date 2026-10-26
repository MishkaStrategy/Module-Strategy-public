"""Platform-neutral lifecycle primitives for trusted Stage-1 service adapters.

The core accepts only already-constructed start/close callbacks. It does not load
owners, interpret paths, select commands, or grant deployment/acceptance authority.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TypeVar

LifecycleAction = Callable[[], object]
ResourceT = TypeVar("ResourceT")


class Stage1Lifecycle:
    """Start typed resources and close every acquired resource in reverse order.

    This primitive does not drain resource factories running concurrently with
    ``close``. A coordinating owner such as ``Stage1MacServiceManager`` must
    serialize bootstrap and shutdown. Start and close callbacks must be
    synchronous, finite, and must not re-enter the owning manager.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._closers: list[LifecycleAction] = []
        self._closed = False

    def start(self, start: LifecycleAction, close: LifecycleAction) -> None:
        """Register cleanup before startup so partially started resources are closed."""
        if not callable(start) or not callable(close):
            raise TypeError("start and close actions must be callable")
        with self._lock:
            if self._closed:
                raise RuntimeError("Stage-1 lifecycle is closed")
            self._closers.append(close)
            start()

    def acquire(
        self,
        factory: Callable[[], ResourceT],
        *,
        start: Callable[[ResourceT], object],
        close: Callable[[ResourceT], object],
    ) -> ResourceT:
        """Construct a resource and immediately register its rollback and shutdown.

        A factory that fails before returning a resource must clean up any partial
        construction itself; the lifecycle cannot register an object it never gets.
        Once returned, the resource is registered before ``start`` is called.
        """
        if not callable(factory) or not callable(start) or not callable(close):
            raise TypeError("factory, start, and close actions must be callable")
        with self._lock:
            if self._closed:
                raise RuntimeError("Stage-1 lifecycle is closed")
        resource = factory()
        with self._lock:
            if self._closed:
                close(resource)
                raise RuntimeError("Stage-1 lifecycle closed during resource construction")
            self._closers.append(lambda: close(resource))
            start(resource)
        return resource

    def wait_for_stop(
        self,
        stop_event: threading.Event,
        *,
        is_running: Callable[[], bool],
        wait_seconds: float,
        failure_message: str,
    ) -> None:
        """Wait for an adapter-owned stop event and fail if a resource exits early."""
        if not isinstance(stop_event, threading.Event):
            raise TypeError("stop_event must be threading.Event")
        if not callable(is_running):
            raise TypeError("is_running must be callable")
        if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)):
            raise TypeError("wait_seconds must be numeric")
        if not 0.05 <= float(wait_seconds) <= 60.0:
            raise ValueError("wait_seconds must be between 0.05 and 60")
        while not stop_event.wait(float(wait_seconds)):
            if not is_running():
                raise RuntimeError(failure_message)

    def close(self) -> None:
        """Close all registered resources in reverse order, attempting every closer.

        Callers that run ``acquire`` concurrently must coordinate factory drain
        themselves before invoking this method.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            closers = tuple(reversed(self._closers))
            self._closers.clear()
        errors: list[BaseException] = []
        for close in closers:
            try:
                close()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise BaseExceptionGroup("Stage-1 lifecycle cleanup failed", errors)

    def __repr__(self) -> str:
        return "Stage1Lifecycle(<private resources>)"


__all__ = ["LifecycleAction", "ResourceT", "Stage1Lifecycle"]
