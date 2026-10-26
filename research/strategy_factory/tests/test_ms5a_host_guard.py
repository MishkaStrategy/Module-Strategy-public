from __future__ import annotations

import importlib.util
import json
import signal
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from strategy_factory import ms5a_host_guard as guard


def _load_owner_search_script() -> ModuleType:
    repository_root = Path(__file__).resolve().parents[3]
    path = repository_root / "scripts" / "ms5a_owner_search.py"
    spec = importlib.util.spec_from_file_location("ms5a_owner_search_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _completed(
    args: tuple[str, ...],
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=args,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_non_macos_host_does_not_run_macos_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        guard,
        "_probe",
        lambda *args, **kwargs: pytest.fail("macOS probe must not run"),
    )

    payload = guard.assert_governed_host_maintenance_clear(system_name="Linux")

    assert payload == {
        "schema_version": 1,
        "platform": "Linux",
        "macos_update_restart_risk": False,
        "scheduled_restart_risk": False,
        "read_only": True,
    }


def test_macos_host_passes_when_no_restart_risk_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []
    responses = iter(
        (
            _completed(
                ("/usr/sbin/softwareupdate", "--list"),
                stdout="Software Update Tool\nNo new software available.\n",
            ),
            _completed(
                ("/usr/bin/pmset", "-g", "sched"),
                stdout="Scheduled power events:\n [0] wake at 09/30/2026 08:00:00\n",
            ),
        )
    )

    monkeypatch.setattr(
        guard.shutil,
        "which",
        lambda command: {
            "softwareupdate": "/usr/sbin/softwareupdate",
            "pmset": "/usr/bin/pmset",
        }[command],
    )

    def fake_probe(command, *, timeout_seconds):
        del timeout_seconds
        commands.append(tuple(command))
        return next(responses)

    monkeypatch.setattr(guard, "_probe", fake_probe)

    payload = guard.assert_governed_host_maintenance_clear(system_name="Darwin")

    assert commands == [
        ("/usr/sbin/softwareupdate", "--list"),
        ("/usr/bin/pmset", "-g", "sched"),
    ]
    assert payload["platform"] == "Darwin"
    assert payload["macos_update_restart_risk"] is False
    assert payload["scheduled_restart_risk"] is False
    assert payload["read_only"] is True


def test_macos_host_fails_closed_for_available_restart_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        guard.shutil,
        "which",
        lambda command: {
            "softwareupdate": "/usr/sbin/softwareupdate",
            "pmset": "/usr/bin/pmset",
        }[command],
    )
    monkeypatch.setattr(
        guard,
        "_probe",
        lambda command, *, timeout_seconds: _completed(
            tuple(command),
            stdout=(
                "Software Update Tool\n"
                "* Label: macOS 27.0.1\n"
                "    Title: macOS, Version: 27.0.1, Recommended: YES, Action: restart,\n"
            ),
        ),
    )

    with pytest.raises(
        guard.HostMaintenanceRisk,
        match="available software update requiring restart",
    ):
        guard.assert_governed_host_maintenance_clear(system_name="Darwin")


def test_macos_host_fails_closed_for_scheduled_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        (
            _completed(
                ("/usr/sbin/softwareupdate", "--list"),
                stdout="Software Update Tool\nNo new software available.\n",
            ),
            _completed(
                ("/usr/bin/pmset", "-g", "sched"),
                stdout=(
                    "Scheduled power events:\n"
                    " [0] shutdown at 09/29/2026 23:00:00 by 'com.apple.alarm.user-visible'\n"
                ),
            ),
        )
    )
    monkeypatch.setattr(
        guard.shutil,
        "which",
        lambda command: {
            "softwareupdate": "/usr/sbin/softwareupdate",
            "pmset": "/usr/bin/pmset",
        }[command],
    )
    monkeypatch.setattr(
        guard,
        "_probe",
        lambda command, *, timeout_seconds: next(responses),
    )

    with pytest.raises(
        guard.HostMaintenanceRisk,
        match="scheduled shutdown or restart",
    ):
        guard.assert_governed_host_maintenance_clear(system_name="Darwin")


@pytest.mark.parametrize("missing", ["softwareupdate", "pmset"])
def test_macos_host_fails_closed_when_required_read_only_probe_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    monkeypatch.setattr(
        guard.shutil,
        "which",
        lambda command: None if command == missing else f"/usr/bin/{command}",
    )

    with pytest.raises(guard.HostMaintenanceRisk, match=f"{missing} is unavailable"):
        guard.assert_governed_host_maintenance_clear(system_name="Darwin")


def test_probe_fails_closed_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        guard.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])
        ),
    )

    with pytest.raises(guard.HostMaintenanceRisk, match="probe failed"):
        guard._probe(("/usr/sbin/softwareupdate", "--list"), timeout_seconds=1)


def test_probe_fails_closed_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        guard.subprocess,
        "run",
        lambda *args, **kwargs: _completed(
            tuple(args[0]),
            stderr="probe failed",
            returncode=1,
        ),
    )

    with pytest.raises(guard.HostMaintenanceRisk, match="returned nonzero"):
        guard._probe(("/usr/sbin/softwareupdate", "--list"), timeout_seconds=1)


@pytest.mark.parametrize(
    ("interruption_signal", "expected_exit_code"),
    ((signal.SIGINT, 130), (signal.SIGTERM, 143)),
)
def test_interruption_summary_is_fail_closed_and_persisted(
    tmp_path: Path,
    interruption_signal: signal.Signals,
    expected_exit_code: int,
) -> None:
    output = tmp_path / "runner" / "summary.json"
    runtime_root = tmp_path / "runtime"

    guard.write_interruption_summary(
        output,
        expected_head="a" * 40,
        authority_id="authority-one",
        request_fingerprint="b" * 64,
        signum=int(interruption_signal),
        runtime_root=runtime_root,
    )

    runner_payload = json.loads(output.read_text(encoding="utf-8"))
    runtime_payload = json.loads(
        (runtime_root / "external-interruption.json").read_text(encoding="utf-8")
    )

    assert runner_payload == runtime_payload
    assert runner_payload["phase"] == "search"
    assert runner_payload["phase_outcome"] == "technical_interrupted"
    assert runner_payload["status"] == "failed"
    assert runner_payload["runtime_state_verified"] is False
    assert runner_payload["walk_forward_executed"] is False
    assert runner_payload["holdout_opened"] is False
    assert runner_payload["final_test_grants"] == 0
    assert runner_payload["live_trading_authorized"] is False
    assert runner_payload["interruption"] == {
        "signal": int(interruption_signal),
        "signal_name": interruption_signal.name,
        "exit_code": expected_exit_code,
    }


def test_search_failure_summary_is_bounded_public_safe_and_persisted(
    tmp_path: Path,
) -> None:
    output = tmp_path / "runner" / "summary.json"
    runtime_root = tmp_path / "runtime"
    long_failure = type("SensitiveFailure" + ("X" * 512), (RuntimeError,), {})(
        "secret-token=/Users/private/runner/state"
    )

    guard.write_search_failure_summary(
        output,
        expected_head="a" * 40,
        authority_id="authority-one",
        request_fingerprint="b" * 64,
        exc=long_failure,
        runtime_root=runtime_root,
    )

    raw = output.read_text(encoding="utf-8")
    payload = json.loads(raw)
    runtime_payload = json.loads(
        (runtime_root / "ordinary-failure.json").read_text(encoding="utf-8")
    )

    assert payload == runtime_payload
    assert len(raw.encode("utf-8")) <= guard._FAILURE_SUMMARY_MAX_BYTES
    assert payload["phase"] == "search"
    assert payload["phase_outcome"] == "technical_failure"
    assert payload["status"] == "failed"
    assert payload["terminal_kind"] == "ordinary_exception"
    assert payload["runtime_state_verified"] is False
    assert payload["walk_forward_executed"] is False
    assert payload["holdout_opened"] is False
    assert payload["final_test_grants"] == 0
    assert payload["live_trading_authorized"] is False
    assert payload["failure"]["message_policy"] == "omitted_for_publication_safety"
    assert len(payload["failure"]["exception_type"]) <= 128
    assert "secret-token" not in raw
    assert "/Users/private" not in raw
    assert "finalists" not in payload
    assert "candidates" not in payload


def test_owner_search_re_raises_original_failure_after_writing_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_owner_search_script()
    output = tmp_path / "summary.json"
    runtime_root = tmp_path / "runtime"

    class OriginalSearchFailure(RuntimeError):
        pass

    monkeypatch.setattr(
        module,
        "assert_governed_host_maintenance_clear",
        lambda: {"read_only": True},
    )
    monkeypatch.setattr(module, "MS5ASearchAuthority", lambda **kwargs: object())

    def fail_search(**kwargs):
        del kwargs
        raise OriginalSearchFailure("private /Users/runner/search state")

    monkeypatch.setattr(module, "execute_authorized_ms5a_search", fail_search)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ms5a_owner_search.py",
            "--expected-head",
            "a" * 40,
            "--market-data-root",
            str(tmp_path / "market-data"),
            "--runtime-root",
            str(runtime_root),
            "--authority-id",
            "authority-one",
            "--request-fingerprint",
            "b" * 64,
            "--output",
            str(output),
        ],
    )

    with pytest.raises(OriginalSearchFailure):
        module.main()

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["failure"]["exception_type"] == "OriginalSearchFailure"
    assert "private /Users/runner/search state" not in output.read_text(encoding="utf-8")
    assert (runtime_root / "ordinary-failure.json").is_file()


def test_owner_search_preserves_original_failure_when_evidence_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_owner_search_script()

    class OriginalSearchFailure(RuntimeError):
        pass

    monkeypatch.setattr(
        module,
        "assert_governed_host_maintenance_clear",
        lambda: {"read_only": True},
    )
    monkeypatch.setattr(module, "MS5ASearchAuthority", lambda **kwargs: object())

    def fail_search(**kwargs):
        del kwargs
        raise OriginalSearchFailure("original failure")

    def fail_evidence(*args, **kwargs):
        del args, kwargs
        raise OSError("evidence write failed")

    monkeypatch.setattr(module, "execute_authorized_ms5a_search", fail_search)
    monkeypatch.setattr(module, "write_search_failure_summary", fail_evidence)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ms5a_owner_search.py",
            "--expected-head",
            "a" * 40,
            "--market-data-root",
            str(tmp_path / "market-data"),
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--authority-id",
            "authority-one",
            "--request-fingerprint",
            "b" * 64,
            "--output",
            str(tmp_path / "summary.json"),
        ],
    )

    with pytest.raises(OriginalSearchFailure):
        module.main()


def test_interruption_exit_code_rejects_unhandled_signal() -> None:
    with pytest.raises(ValueError, match="unsupported interruption signal"):
        guard.interruption_exit_code(signal.SIGHUP)


def test_ms5_owner_search_wires_guard_before_execution_and_handles_signals() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    script = (repository_root / "scripts" / "ms5a_owner_search.py").read_text(encoding="utf-8")

    assert script.index("assert_governed_host_maintenance_clear()") < script.index(
        "execute_authorized_ms5a_search("
    )
    assert "signal.SIGTERM" in script
    assert "signal.SIGINT" in script
    assert "write_interruption_summary(" in script
    assert "write_search_failure_summary(" in script
    assert "except Exception as exc:" in script
    assert "except Exception as evidence_exc:" in script
    assert "runtime_root=args.runtime_root" in script
    assert "return 75" in script
