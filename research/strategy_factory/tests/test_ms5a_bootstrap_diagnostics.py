from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import strategy_factory.ms5a_profile_executor as executor
from strategy_factory.ms5a_profile_executor import (
    _ChildPhaseFailure,
    _collect_bootstrap_diagnostics,
    _DiagnosticLedger,
    _profile_owner_resolution,
    _read_bounded_regular_file,
    _trace_frames,
    launch_bounded_child,
)


def test_scratch_ledger_survives_child_timeout_as_partial_evidence(tmp_path: Path) -> None:
    source = tmp_path / "accepted-source"
    source.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    project_root = Path(__file__).parents[3]
    import_roots = [
        project_root / "src",
        *[
            path
            for root in ("applications", "modules", "plugins", "research")
            for path in (project_root / root).glob("*/src")
        ],
    ]
    command = (
        sys.executable,
        "-c",
        "from pathlib import Path; "
        "from strategy_factory.ms5a_profile_executor import _DiagnosticLedger; "
        f"ledger = _DiagnosticLedger(Path({str(scratch)!r})); "
        "ledger.record('native_imports_started'); "
        "import time; time.sleep(30)",
    )

    status, _, _, _ = launch_bounded_child(
        command,
        cwd=tmp_path,
        environment={"PYTHONPATH": os.pathsep.join(map(str, import_roots))},
        timeout_seconds=1,
        rss_reader=lambda _pid: 0,
    )

    diagnostics = _collect_bootstrap_diagnostics(scratch, source)
    assert status == "hard_timeout"
    assert diagnostics["evidence_kind"] == "partial_diagnostic_evidence"
    assert diagnostics["last_recorded_phase"] == "native_imports_started"
    assert len(diagnostics["phase_events"]) == 1
    assert diagnostics["phase_events"][0]["phase"] == "native_imports_started"
    assert diagnostics["phase_events"][0]["elapsed_seconds"] >= 0
    assert "failure_phase" not in diagnostics
    assert "child_status" not in diagnostics
    assert "cases" not in diagnostics


def test_owner_profile_is_saved_when_synthetic_callback_raises(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    ledger = _DiagnosticLedger(scratch)

    def owner_call() -> None:
        sum(range(100))
        raise sqlite3.OperationalError("synthetic unavailable")

    with pytest.raises(_ChildPhaseFailure) as raised:
        _profile_owner_resolution(owner_call, scratch / "owner-resolution.prof", ledger)
    ledger.close()

    assert raised.value.phase == "owner_resolution"
    assert isinstance(raised.value.__cause__, sqlite3.OperationalError)
    assert (scratch / "owner-resolution.prof").is_file()
    diagnostics = _collect_bootstrap_diagnostics(scratch, tmp_path)
    assert diagnostics["owner_profile_sha256"] is not None
    assert [item["phase"] for item in diagnostics["phase_events"]] == [
        "owner_resolution_started",
        "owner_call_returned",
    ]


def test_trace_sanitizer_returns_only_accepted_source_relative_frames(tmp_path: Path) -> None:
    source = tmp_path / "accepted"
    source_file = source / "research" / "owner.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("pass\n", encoding="utf-8")
    private = tmp_path / "private" / "secret.py"
    private.parent.mkdir()
    private.write_text("pass\n", encoding="utf-8")
    trace = (
        f'  File "{source_file}", line 1 in resolve_owner\n'
        f'  File "{private}", line 9 in private_function\n'
        '  File "/private/user/tokenized/path.py", line 3 in helper\n'
    ).encode()

    frames = _trace_frames(trace, source)

    assert frames == [{"source_file": "research/owner.py", "line": 1, "function": "resolve_owner"}]
    assert "private" not in str(frames)
    assert "tokenized" not in str(frames)
    assert _trace_frames(b"\xff", source) == []
    many_frames = "".join(
        f'  File "{source_file}", line 1 in resolve_owner\n' for _ in range(65)
    ).encode()
    assert len(_trace_frames(many_frames, source)) == 64


def test_diagnostic_collection_rejects_symlink_and_oversize_files(tmp_path: Path) -> None:
    source = tmp_path / "accepted"
    source.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text('  File "/private/path", line 1 in hidden\n', encoding="utf-8")
    (scratch / "bootstrap-trace.txt").symlink_to(outside)
    (scratch / "bootstrap-ledger.jsonl").write_bytes(b"x" * 20_000)
    (scratch / "owner-resolution.prof").symlink_to(outside)

    diagnostics = _collect_bootstrap_diagnostics(scratch, source)

    assert diagnostics["last_recorded_phase"] is None
    assert diagnostics["phase_events"] == []
    assert diagnostics["trace_sha256"] is None
    assert diagnostics["trace_frames"] == []
    assert diagnostics["owner_profile_sha256"] is None


def test_phase_ledger_distinguishes_three_plan_builds_and_nine_callbacks(
    tmp_path: Path,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    ledger = _DiagnosticLedger(scratch)
    entries: list[tuple[str, str | None, int | None]] = [
        ("child_started", None, None),
        ("runtime_checkout_verified", None, None),
        ("native_imports_started", None, None),
        ("selection_completed", None, None),
        ("runtime_attestation_completed", None, None),
        ("native_imports_completed", None, None),
        ("owner_resolution_started", None, None),
    ]
    for phase, counter, value in (
        ("kline_resolver", "partitions_processed", 1),
        ("funding_resolver", "partitions_processed", 1),
        ("mark_price_resolver", "partitions_processed", 1),
        ("kline_overlap_count", "rows_processed", 20),
        ("registered_file_size", "files_processed", 1),
        ("funding_mark_merge", "events_built", 2),
    ):
        entries.extend(
            [
                (f"owner_{phase}_started", None, None),
                (f"owner_{phase}_returned", counter, value),
            ]
        )
    entries.append(("owner_call_returned", None, None))
    entries.extend([("plan_build_started", None, None), ("plan_build_returned", None, None)] * 3)
    entries.extend([("case_started", None, None), ("case_operation_returned", None, None)] * 9)
    for phase, counter_name, counter_value in entries:
        ledger.record(phase, counter_name, counter_value)
    ledger.close()

    diagnostics = _collect_bootstrap_diagnostics(scratch, tmp_path)

    assert (scratch / "bootstrap-ledger.jsonl").stat().st_size <= (
        executor.BOOTSTRAP_LEDGER_MAX_BYTES
    )
    assert len(entries) == 44
    assert executor.BOOTSTRAP_LEDGER_MAX_EVENTS >= len(entries)
    assert [event["phase"] for event in diagnostics["phase_events"]] == [
        entry[0] for entry in entries
    ]
    assert diagnostics["phase_events"][8]["counter_name"] == "partitions_processed"
    assert diagnostics["phase_events"][8]["counter_value"] == 1
    assert diagnostics["last_recorded_phase"] == "case_operation_returned"


def test_ledger_refuses_to_overwrite_existing_partial_evidence(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    ledger = _DiagnosticLedger(scratch)
    ledger.record("native_imports_started")
    ledger.close()
    original = (scratch / "bootstrap-ledger.jsonl").read_bytes()

    with pytest.raises(executor.CompatibilityError):
        _DiagnosticLedger(scratch)

    assert (scratch / "bootstrap-ledger.jsonl").read_bytes() == original


def test_ledger_rejects_unknown_or_over_limit_events_and_disables_on_write_error(
    tmp_path: Path,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    ledger = _DiagnosticLedger(scratch)
    ledger.record("unknown_phase")
    for _ in range(executor.BOOTSTRAP_LEDGER_MAX_EVENTS):
        ledger.record("child_started")
    ledger.record("case_started")
    ledger.close()
    assert len((scratch / "bootstrap-ledger.jsonl").read_bytes().splitlines()) == (
        executor.BOOTSTRAP_LEDGER_MAX_EVENTS
    )

    class FailedStream:
        def write(self, _payload: bytes) -> int:
            raise OSError("synthetic disk full")

        def flush(self) -> None:
            return None

        def fileno(self) -> int:
            return 1

        def close(self) -> None:
            return None

    failed_scratch = tmp_path / "failed-scratch"
    failed_scratch.mkdir()
    second = _DiagnosticLedger(failed_scratch)
    second.path.unlink()
    second._stream.close()
    second._stream = FailedStream()  # type: ignore[assignment]
    second.record("child_started")
    assert second._disabled
    assert second._events == 0
    second.close()


@pytest.mark.parametrize(
    "invalid_event",
    [
        {
            "phase": "owner_kline_resolver_returned",
            "counter_name": "rows_processed",
            "counter_value": 1,
        },
        {
            "phase": "owner_kline_resolver_returned",
            "counter_name": "partitions_processed",
            "counter_value": True,
        },
        {
            "phase": "owner_kline_resolver_returned",
            "counter_name": "partitions_processed",
            "counter_value": 2**63,
        },
        {
            "phase": "owner_funding_resolver_returned",
            "counter_name": "partitions_processed",
            "counter_value": 1,
            "path": "/private/fixture",
        },
    ],
)
def test_diagnostic_parser_rejects_invalid_owner_counters_as_partial(
    tmp_path: Path, invalid_event: dict[str, object]
) -> None:
    source = tmp_path / "accepted"
    source.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    valid = b'{"elapsed_seconds":0.25,"phase":"native_imports_started"}\n'
    malformed = json.dumps({"elapsed_seconds": 0.5, **invalid_event}, separators=(",", ":")).encode(
        "ascii"
    )
    (scratch / "bootstrap-ledger.jsonl").write_bytes(valid + malformed + b"\n")

    diagnostics = _collect_bootstrap_diagnostics(scratch, source)

    assert diagnostics["evidence_kind"] == "partial_diagnostic_evidence"
    assert diagnostics["phase_events"] == [
        {"phase": "native_imports_started", "elapsed_seconds": 0.25}
    ]
    assert diagnostics["last_recorded_phase"] == "native_imports_started"


def test_ledger_byte_cap_and_trace_startup_failures_are_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(executor, "BOOTSTRAP_LEDGER_MAX_BYTES", 1)
    ledger = _DiagnosticLedger(scratch)
    ledger.record("child_started")
    ledger.close()
    assert (scratch / "bootstrap-ledger.jsonl").read_bytes() == b""

    (scratch / "bootstrap-trace.txt").write_bytes(b"existing")
    assert executor._start_bootstrap_trace(scratch) is None
    (scratch / "bootstrap-trace.txt").unlink()
    outside = tmp_path / "outside.trace"
    outside.write_bytes(b"private")
    (scratch / "bootstrap-trace.txt").symlink_to(outside)
    assert executor._start_bootstrap_trace(scratch) is None
    (scratch / "bootstrap-trace.txt").unlink()
    monkeypatch.setattr(
        executor.faulthandler,
        "dump_traceback_later",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("timer unavailable")),
    )
    assert executor._start_bootstrap_trace(scratch) is None
    (scratch / "bootstrap-trace.txt").unlink()

    actual_open = os.open
    actual_close = os.close
    opened_descriptors: list[int] = []

    def tracked_open(path: Path, flags: int, mode: int = 0o777) -> int:
        descriptor = actual_open(path, flags, mode)
        opened_descriptors.append(descriptor)
        return descriptor

    def fail_fdopen(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic fdopen failure")

    def close_then_raise(descriptor: int) -> None:
        actual_close(descriptor)
        raise OSError("synthetic descriptor close failure")

    monkeypatch.setattr(executor.os, "open", tracked_open)
    monkeypatch.setattr(executor.os, "fdopen", fail_fdopen)
    monkeypatch.setattr(executor.os, "close", close_then_raise)
    assert executor._start_bootstrap_trace(scratch) is None
    assert len(opened_descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(opened_descriptors[0])


def test_ledger_keeps_valid_prefix_and_rejects_incomplete_or_malformed_tail(
    tmp_path: Path,
) -> None:
    source = tmp_path / "accepted"
    source.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    valid = b'{"elapsed_seconds":0.25,"phase":"native_imports_started"}\n'
    (scratch / "bootstrap-ledger.jsonl").write_bytes(valid + b"{incomplete")

    diagnostics = _collect_bootstrap_diagnostics(scratch, source)

    assert diagnostics["phase_events"] == [
        {"phase": "native_imports_started", "elapsed_seconds": 0.25}
    ]
    assert diagnostics["last_recorded_phase"] == "native_imports_started"
    assert _read_bounded_regular_file(scratch / "bootstrap-ledger.jsonl", 4) is None
    (scratch / "bootstrap-ledger.jsonl").write_bytes(valid + b"{malformed}\n")
    assert _collect_bootstrap_diagnostics(scratch, source)["phase_events"] == [
        {"phase": "native_imports_started", "elapsed_seconds": 0.25}
    ]
    (scratch / "bootstrap-ledger.jsonl").write_bytes(
        valid + b'{"elapsed_seconds":0.5,"phase":[]}\n'
    )
    assert _collect_bootstrap_diagnostics(scratch, source)["phase_events"] == [
        {"phase": "native_imports_started", "elapsed_seconds": 0.25}
    ]


def test_diagnostic_reads_are_bounded_and_require_regular_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    large = tmp_path / "large.bin"
    large.write_bytes(b"x" * 20_000)
    exact = tmp_path / "exact.bin"
    exact.write_bytes(b"1234")
    requested: list[int] = []
    returned = 0
    actual_read = os.read

    def tracked_read(descriptor: int, maximum: int) -> bytes:
        nonlocal returned
        requested.append(maximum)
        data = actual_read(descriptor, maximum)
        returned += len(data)
        return data

    monkeypatch.setattr(executor.os, "read", tracked_read)

    assert _read_bounded_regular_file(exact, 4) == b"1234"
    assert executor._hash_bounded_regular_file(exact, 4) == hashlib.sha256(b"1234").hexdigest()
    assert _read_bounded_regular_file(large, 4) is None
    assert executor._hash_bounded_regular_file(large, 4) is None
    assert _read_bounded_regular_file(tmp_path, 100) is None
    assert _read_bounded_regular_file(large, -1) is None
    fifo = tmp_path / "diagnostic.fifo"
    os.mkfifo(fifo)
    assert _read_bounded_regular_file(fifo, 4) is None
    assert requested and max(requested) <= 5
    assert returned <= 18

    actual_close = os.close

    def close_then_raise(descriptor: int) -> None:
        actual_close(descriptor)
        raise OSError("synthetic descriptor close error")

    monkeypatch.setattr(executor.os, "close", close_then_raise)
    assert _read_bounded_regular_file(exact, 4) == b"1234"
    assert executor._hash_bounded_regular_file(exact, 4) == hashlib.sha256(b"1234").hexdigest()


def test_diagnostic_cleanup_helpers_suppress_io_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    ledger = _DiagnosticLedger(scratch)
    real_stream = ledger._stream

    class FailedClose:
        def close(self) -> None:
            raise OSError("synthetic close failure")

    ledger._stream = FailedClose()  # type: ignore[assignment]
    ledger.close()
    real_stream.close()
    assert ledger._disabled

    class CloseOnly:
        closed = False

        def close(self) -> None:
            self.closed = True
            raise OSError("synthetic trace close failure")

    trace_stream = CloseOnly()
    _stop = executor._stop_bootstrap_trace
    monkeypatch.setattr(
        executor.faulthandler,
        "cancel_dump_traceback_later",
        lambda: (_ for _ in ()).throw(RuntimeError()),
    )
    _stop(trace_stream)  # type: ignore[arg-type]
    assert trace_stream.closed


def test_hard_killed_child_preserves_real_timed_trace_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "accepted-source"
    source.mkdir()
    script = source / "synthetic_hang.py"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    ready = tmp_path / "trace-ready"
    script.write_text(
        "import time\n"
        "from pathlib import Path\n"
        "import strategy_factory.ms5a_profile_executor as executor\n"
        "executor.BOOTSTRAP_TRACE_SECONDS = 0.2\n"
        f"stream = executor._start_bootstrap_trace(Path({str(scratch)!r}))\n"
        f"Path({str(ready)!r}).write_text('ready', encoding='ascii')\n"
        "def wait_for_kill():\n"
        "    time.sleep(30)\n"
        "wait_for_kill()\n",
        encoding="utf-8",
    )
    project_root = Path(__file__).parents[3]
    import_roots = [
        project_root / "src",
        *[
            path
            for root in ("applications", "modules", "plugins", "research")
            for path in (project_root / root).glob("*/src")
        ],
    ]
    process = subprocess.Popen(
        (sys.executable, str(script)),
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(map(str, import_roots))},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 10
    try:
        while not ready.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("synthetic trace child exited before timer setup")
            time.sleep(0.02)
        assert ready.is_file(), "synthetic trace child did not finish startup in time"
        trace_path = scratch / "bootstrap-trace.txt"
        trace_deadline = time.monotonic() + 5
        trace_bytes = b""
        trace_frames: list[dict[str, str | int]] = []
        while time.monotonic() < trace_deadline:
            if process.poll() is not None:
                pytest.fail("synthetic trace child exited before the timed snapshot")
            if trace_path.exists():
                trace_bytes = trace_path.read_bytes()
                trace_frames = _trace_frames(trace_bytes, source)
                functions = {frame["function"] for frame in trace_frames}
                if (
                    trace_bytes.endswith(b"\n")
                    and {"wait_for_kill"} <= functions
                    and {frame["source_file"] for frame in trace_frames} == {"synthetic_hang.py"}
                ):
                    break
            time.sleep(0.02)
        assert trace_path.exists() and trace_bytes.endswith(b"\n")
        assert {frame["function"] for frame in trace_frames} >= {"wait_for_kill"}
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    diagnostics = _collect_bootstrap_diagnostics(scratch, source)

    assert diagnostics["trace_sha256"] is not None
    assert {frame["source_file"] for frame in diagnostics["trace_frames"]} == {"synthetic_hang.py"}
    assert {frame["function"] for frame in diagnostics["trace_frames"]} >= {"wait_for_kill"}


def test_cleanup_failures_do_not_mask_primary_setup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    class BrokenLedger:
        def record(self, _phase: str) -> None:
            return None

        def close(self) -> None:
            raise OSError("synthetic close failure")

    broken = BrokenLedger()
    monkeypatch.setattr(executor, "_DiagnosticLedger", lambda _root: broken)
    monkeypatch.setattr(executor, "_start_bootstrap_trace", lambda _root: object())
    monkeypatch.setattr(
        executor, "_stop_bootstrap_trace", lambda _stream: (_ for _ in ()).throw(RuntimeError())
    )
    monkeypatch.setattr(executor, "_verify_runtime_checkout", lambda path: path)
    monkeypatch.setattr(executor, "_verify_controller_checkout", lambda *_args, **_kwargs: None)

    def fail_child(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise _ChildPhaseFailure("owner_resolution", sqlite3.OperationalError("private detail"))

    monkeypatch.setattr(executor, "_child_run", fail_child)
    result = executor._child_main(
        (
            "--child",
            "--runtime-root",
            str(tmp_path),
            "--market-data-root",
            str(tmp_path),
            "--runtime-source",
            str(tmp_path),
            "--scratch-root",
            str(scratch),
            "--bindings-json",
            json.dumps({"controller_commit_sha": "d" * 40}),
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert payload["status"] == "setup_failure"
    assert payload["setup_failure"]["phase"] == "owner_resolution"
    assert payload["setup_failure"]["error_class"] == "OperationalError"
    assert "private detail" not in str(payload)
