"""Fail-closed host maintenance checks for governed MS5 long-running execution."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_PROBE_TIMEOUT_SECONDS = 180.0
_RESTART_ACTION_RE = re.compile(r"\bAction:\s*restart\b", re.IGNORECASE)
_SCHEDULED_RESTART_RE = re.compile(r"\b(?:shutdown|restart)\b", re.IGNORECASE)
_INTERRUPTION_SIGNALS = (signal.SIGTERM, signal.SIGINT)
_RUNTIME_INTERRUPTION_FILENAME = "external-interruption.json"
_RUNTIME_FAILURE_FILENAME = "ordinary-failure.json"
_FAILURE_SUMMARY_MAX_BYTES = 4096
_EXCEPTION_TYPE_MAX_CHARS = 128
_EXCEPTION_TYPE_SANITIZER_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class HostMaintenanceRisk(RuntimeError):
    """Raised when a governed long run cannot establish a safe host state."""


def _probe(
    command: Sequence[str],
    *,
    timeout_seconds: float = _PROBE_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    env = dict(os.environ)
    env.update({"LC_ALL": "C", "LANG": "C"})
    try:
        completed = subprocess.run(
            tuple(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HostMaintenanceRisk(f"host maintenance probe failed: {command[0]}") from exc
    if completed.returncode != 0:
        raise HostMaintenanceRisk(f"host maintenance probe returned nonzero: {command[0]}")
    return completed


def _restart_required_update_present(output: str) -> bool:
    return bool(_RESTART_ACTION_RE.search(output))


def _scheduled_restart_present(output: str) -> bool:
    return bool(_SCHEDULED_RESTART_RE.search(output))


def assert_governed_host_maintenance_clear(
    *,
    system_name: str | None = None,
    probe_timeout_seconds: float = _PROBE_TIMEOUT_SECONDS,
) -> Mapping[str, Any]:
    """Read host maintenance state and fail closed before governed execution.

    Non-macOS runners do not use the macOS Software Update reboot gate. On
    macOS, the check is intentionally read-only: it lists available updates and
    scheduled power events, and never changes Software Update or power settings.
    """

    selected_system = system_name or platform.system()
    if selected_system != "Darwin":
        return {
            "schema_version": 1,
            "platform": selected_system,
            "macos_update_restart_risk": False,
            "scheduled_restart_risk": False,
            "read_only": True,
        }

    softwareupdate = shutil.which("softwareupdate")
    pmset = shutil.which("pmset")
    if softwareupdate is None:
        raise HostMaintenanceRisk("softwareupdate is unavailable on macOS")
    if pmset is None:
        raise HostMaintenanceRisk("pmset is unavailable on macOS")

    update_probe = _probe(
        (softwareupdate, "--list"),
        timeout_seconds=probe_timeout_seconds,
    )
    restart_required = _restart_required_update_present(
        f"{update_probe.stdout}\n{update_probe.stderr}"
    )
    if restart_required:
        raise HostMaintenanceRisk("macOS reports an available software update requiring restart")

    schedule_probe = _probe(
        (pmset, "-g", "sched"),
        timeout_seconds=min(probe_timeout_seconds, 30.0),
    )
    scheduled_restart = _scheduled_restart_present(
        f"{schedule_probe.stdout}\n{schedule_probe.stderr}"
    )
    if scheduled_restart:
        raise HostMaintenanceRisk("macOS reports a scheduled shutdown or restart")

    return {
        "schema_version": 1,
        "platform": selected_system,
        "macos_update_restart_risk": False,
        "scheduled_restart_risk": False,
        "read_only": True,
    }


def interruption_exit_code(signum: int) -> int:
    selected = signal.Signals(signum)
    if selected not in _INTERRUPTION_SIGNALS:
        raise ValueError(f"unsupported interruption signal: {signum}")
    return 128 + int(selected)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    try:
        temporary.write_text(serialized, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _bounded_exception_type(exc: BaseException) -> str:
    raw = type(exc).__name__
    sanitized = _EXCEPTION_TYPE_SANITIZER_RE.sub("_", raw).strip("_.-")
    if not sanitized:
        sanitized = "Exception"
    return sanitized[:_EXCEPTION_TYPE_MAX_CHARS]


def _assert_failure_summary_bound(payload: Mapping[str, Any]) -> None:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    if len(serialized.encode("utf-8")) > _FAILURE_SUMMARY_MAX_BYTES:
        raise ValueError("MS5A failure summary exceeds fixed publication bound")


def write_search_failure_summary(
    output: str | Path,
    *,
    expected_head: str,
    authority_id: str,
    request_fingerprint: str,
    exc: BaseException,
    runtime_root: str | Path | None = None,
) -> None:
    """Write bounded public-safe evidence for an ordinary SEARCH exception.

    Raw exception messages are intentionally omitted because they can contain
    runner-private paths, credentials, or unbounded application state.
    """

    payload = {
        "schema_version": 1,
        "phase": "search",
        "phase_outcome": "technical_failure",
        "status": "failed",
        "terminal_kind": "ordinary_exception",
        "commit_sha": expected_head,
        "authority_id": authority_id,
        "request_fingerprint": request_fingerprint,
        "runtime_state_verified": False,
        "walk_forward_executed": False,
        "holdout_opened": False,
        "final_test_grants": 0,
        "live_trading_authorized": False,
        "failure": {
            "exception_type": _bounded_exception_type(exc),
            "message_policy": "omitted_for_publication_safety",
        },
    }
    _assert_failure_summary_bound(payload)

    _atomic_write_json(Path(output), payload)
    if runtime_root is not None:
        _atomic_write_json(
            Path(runtime_root) / _RUNTIME_FAILURE_FILENAME,
            payload,
        )


def write_interruption_summary(
    output: str | Path,
    *,
    expected_head: str,
    authority_id: str,
    request_fingerprint: str,
    signum: int,
    runtime_root: str | Path | None = None,
) -> None:
    """Write bounded fail-closed evidence for a catchable external interruption."""

    selected = signal.Signals(signum)
    if selected not in _INTERRUPTION_SIGNALS:
        raise ValueError(f"unsupported interruption signal: {signum}")

    payload = {
        "schema_version": 1,
        "phase": "search",
        "phase_outcome": "technical_interrupted",
        "status": "failed",
        "commit_sha": expected_head,
        "authority_id": authority_id,
        "request_fingerprint": request_fingerprint,
        "runtime_state_verified": False,
        "walk_forward_executed": False,
        "holdout_opened": False,
        "final_test_grants": 0,
        "live_trading_authorized": False,
        "interruption": {
            "signal": int(selected),
            "signal_name": selected.name,
            "exit_code": interruption_exit_code(int(selected)),
        },
    }

    if runtime_root is not None:
        _atomic_write_json(
            Path(runtime_root) / _RUNTIME_INTERRUPTION_FILENAME,
            payload,
        )
    _atomic_write_json(Path(output), payload)
