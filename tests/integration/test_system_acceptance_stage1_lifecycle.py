from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import system_acceptance_stage1_service_manager as manager_module
from scripts.system_acceptance_stage1_lifecycle import Stage1Lifecycle
from scripts.system_acceptance_stage1_service_manager import (
    ReconstructedStage1Owners,
    Stage1MacServiceManager,
    Stage1MacServiceManagerConfig,
)


def test_lifecycle_closes_started_resources_in_reverse_order() -> None:
    events: list[str] = []
    lifecycle = Stage1Lifecycle()
    for name in ("owner", "transport", "host"):
        lifecycle.start(
            lambda name=name: events.append(f"start:{name}"),
            lambda name=name: events.append(f"close:{name}"),
        )

    lifecycle.close()
    lifecycle.close()

    assert events == [
        "start:owner",
        "start:transport",
        "start:host",
        "close:host",
        "close:transport",
        "close:owner",
    ]
    assert "owner" not in repr(lifecycle)


def test_lifecycle_rolls_back_partial_start_in_reverse_order() -> None:
    events: list[str] = []
    lifecycle = Stage1Lifecycle()
    lifecycle.start(lambda: events.append("start:owner"), lambda: events.append("close:owner"))

    def fail_start() -> None:
        events.append("start:host-partial")
        raise RuntimeError("synthetic startup failure")

    with pytest.raises(RuntimeError, match="synthetic startup failure"):
        lifecycle.start(fail_start, lambda: events.append("close:host-partial"))
    lifecycle.close()

    assert events == [
        "start:owner",
        "start:host-partial",
        "close:host-partial",
        "close:owner",
    ]


def test_lifecycle_acquire_rolls_back_if_next_constructor_fails() -> None:
    events: list[str] = []
    lifecycle = Stage1Lifecycle()
    lifecycle.acquire(
        lambda: "trading",
        start=lambda name: events.append(f"start:{name}"),
        close=lambda name: events.append(f"close:{name}"),
    )

    def fail_factory() -> str:
        events.append("construct:research-failed")
        raise RuntimeError("synthetic constructor failure")

    with pytest.raises(RuntimeError, match="synthetic constructor failure"):
        lifecycle.acquire(
            fail_factory,
            start=lambda name: events.append(f"start:{name}"),
            close=lambda name: events.append(f"close:{name}"),
        )
    lifecycle.close()

    assert events == ["start:trading", "construct:research-failed", "close:trading"]


def test_lifecycle_acquire_closes_partial_resource_when_start_fails() -> None:
    events: list[str] = []
    lifecycle = Stage1Lifecycle()

    def fail_start(name: str) -> None:
        events.append(f"start-partial:{name}")
        raise RuntimeError("synthetic start failure")

    with pytest.raises(RuntimeError, match="synthetic start failure"):
        lifecycle.acquire(
            lambda: "transport",
            start=fail_start,
            close=lambda name: events.append(f"close:{name}"),
        )
    lifecycle.close()

    assert events == ["start-partial:transport", "close:transport"]


def test_lifecycle_stop_event_returns_without_wall_clock_work() -> None:
    stop_event = threading.Event()
    stop_event.set()
    calls: list[str] = []

    Stage1Lifecycle().wait_for_stop(
        stop_event,
        is_running=lambda: calls.append("check") is None,
        wait_seconds=0.05,
        failure_message="synthetic process stopped",
    )

    assert calls == []


def test_mac_adapter_sigterm_requests_stop_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered: dict[object, object] = {}
    restored: list[tuple[object, object]] = []
    manager = Stage1MacServiceManager.__new__(Stage1MacServiceManager)
    manager.config = SimpleNamespace(
        acceptance=SimpleNamespace(host_wait_seconds=0.05),
    )
    manager._lifecycle = Stage1Lifecycle()
    closed: list[bool] = []

    def fake_signal(member: object, handler: object) -> object:
        registered[member] = handler
        restored.append((member, handler))
        return None

    monkeypatch.setattr(manager_module.signal, "getsignal", lambda member: f"prior:{member}")
    monkeypatch.setattr(manager_module.signal, "signal", fake_signal)
    monkeypatch.setattr(manager, "close", lambda: closed.append(True))

    def request_sigterm() -> None:
        handler = registered[manager_module.signal.SIGTERM]
        assert callable(handler)
        handler(manager_module.signal.SIGTERM, None)  # type: ignore[operator]

    monkeypatch.setattr(manager, "start", request_sigterm)
    manager.run()

    assert closed == [True]
    assert len(restored) == 4


def test_mac_adapter_restores_signals_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered: dict[object, object] = {}
    restored: list[tuple[object, object]] = []
    manager = Stage1MacServiceManager.__new__(Stage1MacServiceManager)
    manager.config = SimpleNamespace(acceptance=SimpleNamespace(host_wait_seconds=0.05))
    manager._lifecycle = Stage1Lifecycle()

    def fake_signal(member: object, handler: object) -> object:
        registered[member] = handler
        restored.append((member, handler))
        return None

    monkeypatch.setattr(manager_module.signal, "getsignal", lambda member: f"prior:{member}")
    monkeypatch.setattr(manager_module.signal, "signal", fake_signal)

    def fail_cleanup() -> None:
        raise BaseExceptionGroup("synthetic cleanup failure", [RuntimeError("close")])

    monkeypatch.setattr(manager, "close", fail_cleanup)
    monkeypatch.setattr(
        manager,
        "start",
        lambda: registered[manager_module.signal.SIGTERM](manager_module.signal.SIGTERM, None),
    )

    with pytest.raises(BaseExceptionGroup, match="synthetic cleanup failure"):
        manager.run()

    assert restored[-2:] == [
        (manager_module.signal.SIGTERM, f"prior:{manager_module.signal.SIGTERM}"),
        (manager_module.signal.SIGINT, f"prior:{manager_module.signal.SIGINT}"),
    ]


def test_run_preserves_primary_error_with_shutdown_baseexception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = Stage1MacServiceManager.__new__(Stage1MacServiceManager)
    manager.config = SimpleNamespace(acceptance=SimpleNamespace(host_wait_seconds=0.05))
    manager._lifecycle = Stage1Lifecycle()

    def fail_start() -> None:
        raise ValueError("primary run failure")

    def fail_close() -> None:
        raise KeyboardInterrupt("shutdown failure")

    monkeypatch.setattr(threading, "current_thread", lambda: object())
    monkeypatch.setattr(manager, "start", fail_start)
    monkeypatch.setattr(manager, "close", fail_close)
    with pytest.raises(BaseExceptionGroup, match="run and shutdown") as captured:
        manager.run()
    assert isinstance(captured.value.exceptions[0], ValueError)
    assert isinstance(captured.value.exceptions[1], KeyboardInterrupt)


def test_manager_proof_write_does_not_log_private_values(tmp_path: Path) -> None:
    from scripts.system_acceptance_stage1_service_manager import _write_sanitized_proof

    path = tmp_path / "private" / "proof.json"
    token = "private-token-that-must-not-appear"
    _write_sanitized_proof(path, '{"status":"ready"}')

    assert path.read_text(encoding="utf-8") == '{"status":"ready"}\n'
    assert token not in path.read_text(encoding="utf-8")
    assert path.stat().st_mode & 0o777 == 0o600


class _Runner:
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def _owners() -> ReconstructedStage1Owners:
    value = object.__new__(ReconstructedStage1Owners)
    object.__setattr__(value, "strategy_package", object())
    object.__setattr__(value, "research_runner", object())
    object.__setattr__(
        value, "research_prepared", SimpleNamespace(manifest=SimpleNamespace(experiment_id="e"))
    )
    object.__setattr__(value, "trading", SimpleNamespace(runner=_Runner(), lifecycle=object()))
    return value


def _manager_config(tmp_path: Path) -> Stage1MacServiceManagerConfig:
    value = object.__new__(Stage1MacServiceManagerConfig)
    object.__setattr__(value, "acceptance", SimpleNamespace(port=8781, host_wait_seconds=0.05))
    object.__setattr__(value, "proof_path", tmp_path / "proof.json")
    object.__setattr__(value, "trading_reader_token", "trading-reader-token-123")
    object.__setattr__(value, "trading_operator_token", "trading-operator-token-456")
    object.__setattr__(value, "research_reader_token", "research-reader-token-123")
    object.__setattr__(value, "research_operator_token", "research-operator-token-456")
    object.__setattr__(value, "trading_port", 8780)
    object.__setattr__(value, "research_port", 8782)
    return value


def _install_fake_runtime(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    class Server:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.name = "server"

        def start(self) -> None:
            events.append("start:server")

        def close(self) -> None:
            events.append("close:server")

    class Host:
        def __init__(self, **_kwargs: object) -> None:
            self.prestart_proof = SimpleNamespace(to_json=lambda: "{}")

        def start(self) -> None:
            events.append("start:host")

        def close(self) -> None:
            events.append("close:host")

    monkeypatch.setattr(manager_module, "TradingRunnerLifecycleHttpServer", Server)
    monkeypatch.setattr(manager_module, "ResearchRunnerOwnerHttpServer", Server)
    monkeypatch.setattr(manager_module, "ResearchRunnerOwnerHttpClient", lambda *_a, **_k: object())
    monkeypatch.setattr(
        manager_module, "TradingRunnerAcceptanceHttpQuery", lambda *_a, **_k: object()
    )
    monkeypatch.setattr(manager_module, "materialize_stage1_deployment", lambda _sources: object())
    monkeypatch.setattr(manager_module, "Stage1MacServiceHost", Host)
    monkeypatch.setattr(
        manager_module, "_write_sanitized_proof", lambda *_a: events.append("proof")
    )


def _register(lifecycle: Stage1Lifecycle, events: list[str], name: str) -> str:
    return lifecycle.acquire(
        lambda: name,
        start=lambda resource: events.append(f"start:{resource}"),
        close=lambda resource: events.append(f"close:{resource}"),
    )


def test_managed_and_legacy_bootstrap_modes_are_explicit_and_exclusive(tmp_path: Path) -> None:
    config = _manager_config(tmp_path)
    with pytest.raises(TypeError, match="exactly one"):
        Stage1MacServiceManager(config=config)
    with pytest.raises(TypeError, match="exactly one"):
        Stage1MacServiceManager(
            config=config,
            load_owners=lambda: _owners(),
            build_sources=lambda *_args: object(),
            managed_load_owners=lambda _lifecycle: _owners(),
            managed_build_sources=lambda *_args: object(),
        )


def test_legacy_bootstrap_pair_still_starts_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    _install_fake_runtime(monkeypatch, events)
    manager = Stage1MacServiceManager(
        config=_manager_config(tmp_path),
        load_owners=_owners,
        build_sources=lambda *_args: object(),
    )
    manager.start()
    manager.start()
    manager.close()
    assert events.count("start:host") == 1
    assert events.count("close:host") == 1


@pytest.mark.parametrize("failure_stage", ["loader", "sources", "invalid-owners"])
def test_managed_bootstrap_rolls_back_multiple_acquisitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str
) -> None:
    events: list[str] = []
    _install_fake_runtime(monkeypatch, events)

    def load(lifecycle: Stage1Lifecycle) -> ReconstructedStage1Owners:
        _register(lifecycle, events, "loader-one")
        _register(lifecycle, events, "loader-two")
        if failure_stage == "loader":
            raise ValueError("loader failed")
        return object() if failure_stage == "invalid-owners" else _owners()  # type: ignore[return-value]

    def build(lifecycle: Stage1Lifecycle, *_args: object) -> object:
        _register(lifecycle, events, "sources-one")
        _register(lifecycle, events, "sources-two")
        raise LookupError("sources failed")

    manager = Stage1MacServiceManager(
        config=_manager_config(tmp_path),
        managed_load_owners=load,
        managed_build_sources=build,
    )
    error = (
        ValueError
        if failure_stage == "loader"
        else TypeError
        if failure_stage == "invalid-owners"
        else LookupError
    )
    with pytest.raises(error):
        manager.start()
    assert events.count("close:loader-one") == 1
    assert events.count("close:loader-two") == 1
    if failure_stage == "sources":
        assert events.count("close:sources-one") == 1
        assert events.count("close:sources-two") == 1
        assert [
            event
            for event in events
            if event.startswith("close:sources") or event.startswith("close:loader")
        ] == [
            "close:sources-two",
            "close:sources-one",
            "close:loader-two",
            "close:loader-one",
        ]
    else:
        assert events[-2:] == ["close:loader-two", "close:loader-one"]


def test_managed_bootstrap_rolls_back_host_start_and_groups_cleanup_baseexceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    _install_fake_runtime(monkeypatch, events)

    class Host:
        def __init__(self, **_kwargs: object) -> None:
            self.prestart_proof = SimpleNamespace(to_json=lambda: "{}")

        def start(self) -> None:
            raise RuntimeError("host start failed")

        def close(self) -> None:
            events.append("close:host")
            raise KeyboardInterrupt("cleanup interrupted")

    monkeypatch.setattr(manager_module, "Stage1MacServiceHost", Host)

    def load(lifecycle: Stage1Lifecycle) -> ReconstructedStage1Owners:
        _register(lifecycle, events, "owner-resource")
        return _owners()

    def build(lifecycle: Stage1Lifecycle, *_args: object) -> object:
        _register(lifecycle, events, "source-resource")
        return object()

    manager = Stage1MacServiceManager(
        config=_manager_config(tmp_path),
        managed_load_owners=load,
        managed_build_sources=build,
    )
    with pytest.raises(BaseExceptionGroup, match="startup and rollback") as captured:
        manager.start()
    assert isinstance(captured.value.exceptions[0], RuntimeError)
    assert isinstance(captured.value.exceptions[1], BaseExceptionGroup)
    close_events = [event for event in events if event.startswith("close:")]
    assert close_events[0] == "close:host"
    assert close_events == [
        "close:host",
        "close:source-resource",
        "close:server",
        "close:server",
        "close:owner-resource",
    ]
    assert close_events.count("close:host") == 1


def test_managed_bootstrap_serializes_concurrent_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    _install_fake_runtime(monkeypatch, events)
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def load(lifecycle: Stage1Lifecycle) -> ReconstructedStage1Owners:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(2)
        return _owners()

    manager = Stage1MacServiceManager(
        config=_manager_config(tmp_path),
        managed_load_owners=load,
        managed_build_sources=lambda *_args: object(),
    )
    failures: list[BaseException] = []

    def start() -> None:
        try:
            manager.start()
        except BaseException as exc:
            failures.append(exc)

    first = threading.Thread(target=start)
    second = threading.Thread(target=start)
    first.start()
    assert entered.wait(1)
    second.start()
    release.set()
    first.join(2)
    second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert failures == []
    assert calls == 1
    manager.close()


def test_close_waits_for_managed_bootstrap_then_rolls_it_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    _install_fake_runtime(monkeypatch, events)
    entered = threading.Event()
    release = threading.Event()

    def load(lifecycle: Stage1Lifecycle) -> ReconstructedStage1Owners:
        _register(lifecycle, events, "loader-resource")
        entered.set()
        assert release.wait(2)
        return _owners()

    manager = Stage1MacServiceManager(
        config=_manager_config(tmp_path),
        managed_load_owners=load,
        managed_build_sources=lambda *_args: object(),
    )
    startup_errors: list[BaseException] = []
    start_thread = threading.Thread(target=lambda: _capture(manager.start, startup_errors))
    start_thread.start()
    assert entered.wait(1)
    close_returned = threading.Event()

    def close() -> None:
        manager.close()
        close_returned.set()

    close_thread = threading.Thread(target=close)
    close_thread.start()
    assert not close_returned.wait(0.05)
    release.set()
    start_thread.join(2)
    close_thread.join(2)
    assert close_returned.is_set()
    assert len(startup_errors) == 1
    assert "closed during startup" in str(startup_errors[0])
    assert events.count("close:loader-resource") == 1


def _capture(call: Callable[[], object], errors: list[BaseException]) -> None:
    try:
        call()
    except BaseException as exc:
        errors.append(exc)
