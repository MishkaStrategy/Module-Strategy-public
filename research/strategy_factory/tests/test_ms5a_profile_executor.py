from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from strategy_factory.errors import CompatibilityError
from strategy_factory.ms5a_budget_probe import (
    DIAGNOSTIC_REPAIR_ID,
    DIAGNOSTIC_RUNTIME_FILE_SHA256,
    DIAGNOSTIC_RUNTIME_SHA,
    FAMILIES,
    FROZEN_LINEAGE_SHA,
    PROTOCOL_ID,
    PROTOCOL_SCHEMA_VERSION,
    FrozenCase,
    authority_manifest,
)
from strategy_factory.ms5a_profile_executor import (
    _child_command,
    _consume_claim,
    _native_deadline_receipt,
    _prefix_job,
    _prepare_scratch,
    _sanitized_child_receipt,
    _tree_sha256,
    launch_bounded_child,
    require_hard_memory_cap,
    require_outer_watchdog,
)


def _stopped_child_receipt(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": PROTOCOL_SCHEMA_VERSION,
        "probe": PROTOCOL_ID,
        "status": "stopped",
        "reason_code": "CASE_STOPPED",
        "frozen_lineage_source_commit_sha": FROZEN_LINEAGE_SHA,
        "runtime_source_commit_sha": DIAGNOSTIC_RUNTIME_SHA,
        "runtime_repair_id": DIAGNOSTIC_REPAIR_ID,
        "runtime_file_sha256": dict(DIAGNOSTIC_RUNTIME_FILE_SHA256),
        "controller_commit_sha": "d" * 40,
        "runtime_module_sha256": {
            "modular_backtest.runner": "d" * 64,
            "market_data_ingestion.integrity": DIAGNOSTIC_RUNTIME_FILE_SHA256[
                "modules/market_data_ingestion/src/market_data_ingestion/integrity.py"
            ],
            "market_data_ingestion.storage": DIAGNOSTIC_RUNTIME_FILE_SHA256[
                "modules/market_data_ingestion/src/market_data_ingestion/storage.py"
            ],
        },
        "cases": [case],
        "candidate_metrics_included": False,
    }


def test_one_shot_claim_is_exclusive_and_survives_reuse_attempt(tmp_path: Path) -> None:
    claim_root = tmp_path / "claims"
    claim_root.mkdir()

    claim = _consume_claim(claim_root, "synthetic-grant", "a" * 64)

    assert claim.name == "consumed.json"
    assert json.loads(claim.read_text())["manifest_sha256"] == "a" * 64
    with pytest.raises(FileExistsError):
        _consume_claim(claim_root, "synthetic-grant", "a" * 64)
    assert claim.is_file()


def test_partial_claim_write_failure_still_consumes_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claim_root = tmp_path / "claims"
    claim_root.mkdir()

    def fail_open(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("synthetic descriptor wrapper failure")

    monkeypatch.setattr("strategy_factory.ms5a_profile_executor.os.fdopen", fail_open)
    with pytest.raises(OSError, match="synthetic"):
        _consume_claim(claim_root, "synthetic-grant", "a" * 64)

    assert (claim_root / "consumed.json").exists()
    with pytest.raises(FileExistsError):
        _consume_claim(claim_root, "synthetic-grant", "a" * 64)


def test_hard_memory_limit_fails_closed_on_non_linux_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("strategy_factory.ms5a_profile_executor.sys.platform", "darwin")

    with pytest.raises(CompatibilityError, match="Linux cgroup-v2"):
        require_hard_memory_cap()


def test_outer_watchdog_requires_pid1_kill_timeout_wrapping_execution_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc_root = tmp_path / "proc"
    pid1 = proc_root / "1"
    pid1.mkdir(parents=True)
    timeout = tmp_path / "usr" / "bin" / "timeout"
    timeout.parent.mkdir(parents=True)
    timeout.touch()
    (pid1 / "exe").symlink_to(timeout)
    argv = [
        str(timeout),
        "--signal=KILL",
        "1200s",
        "/usr/local/bin/python",
        "-B",
        "-m",
        "strategy_factory.ms5a_profile_executor",
        "--runtime-root",
        "/runtime",
        "--runtime-source",
        "/source",
        "--market-data-root",
        "/market",
        "--authority-manifest",
        "/grant/authority.json",
        "--claim-root",
        "/grant",
        "--scratch-root",
        "/scratch",
        "--report",
        "/scratch/report.json",
    ]
    (pid1 / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")
    monkeypatch.setattr("strategy_factory.ms5a_profile_executor.sys.platform", "linux")

    assert require_outer_watchdog(proc_root=proc_root) == {
        "pid1_executable": "timeout",
        "outer_timeout_seconds": 1200,
        "outer_signal": "KILL",
    }

    argv[1] = "--signal=TERM"
    (pid1 / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")
    with pytest.raises(CompatibilityError, match="watchdog command"):
        require_outer_watchdog(proc_root=proc_root)


def test_native_deadline_boundary_preserves_completed_case_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_report = {
        "family": "breakout_return",
        "bars": 1_000,
        "stage_role": "optimization_000",
        "status": "completed",
        "reason_code": None,
        "duration_seconds": 12.5,
        "input_sha256": "a" * 64,
        "result_tree_sha256": "b" * 64,
        "profile_sha256": "c" * 64,
    }
    module_hashes = {
        "modular_backtest": "d" * 64,
        "market_data_ingestion.integrity": DIAGNOSTIC_RUNTIME_FILE_SHA256[
            "modules/market_data_ingestion/src/market_data_ingestion/integrity.py"
        ],
        "market_data_ingestion.storage": DIAGNOSTIC_RUNTIME_FILE_SHA256[
            "modules/market_data_ingestion/src/market_data_ingestion/storage.py"
        ],
    }
    monkeypatch.setattr("strategy_factory.ms5a_profile_executor.time.monotonic", lambda: 105.0)

    payload = _native_deadline_receipt(
        deadline=105.0,
        reports=[case_report],
        runtime_module_hashes=module_hashes,
        expected_case_count=9,
        bindings={
            "frozen_lineage_source_commit_sha": FROZEN_LINEAGE_SHA,
            "runtime_source_commit_sha": DIAGNOSTIC_RUNTIME_SHA,
            "runtime_repair_id": DIAGNOSTIC_REPAIR_ID,
            "runtime_file_sha256": dict(DIAGNOSTIC_RUNTIME_FILE_SHA256),
            "controller_commit_sha": "d" * 40,
        },
    )

    assert payload is not None
    assert payload["status"] == "stopped"
    assert payload["reason_code"] == "NATIVE_DEADLINE"
    sanitized = _sanitized_child_receipt(json.dumps(payload))
    assert sanitized["cases"] == [case_report]
    assert sanitized["runtime_module_sha256"] == module_hashes


def test_authority_is_bound_to_frozen_case_hashes_and_exact_limits() -> None:
    from strategy_factory.ms5a_budget_probe import validate_authority

    cases = tuple(
        FrozenCase(family, f"{index:064x}", {"research_id": family}, f"exp-{index}", False)
        for index, family in enumerate(FAMILIES, start=1)
    )
    bindings = {
        "source_commit_sha": "a" * 40,
        "request_fingerprint": "b" * 64,
        "dataset_fingerprint": "c" * 64,
        "dataset_sha256": "d" * 64,
        "runtime_surface_fingerprint": "e" * 64,
    }
    manifest = authority_manifest(bindings, cases, "synthetic-grant")
    validate_authority(manifest, bindings, cases)
    manifest["cases"][0]["plan_sha256"] = "f" * 64
    with pytest.raises(CompatibilityError):
        validate_authority(manifest, bindings, cases)


def test_prefix_job_preserves_frozen_parameters_seed_and_initial_search_role(
    tmp_path: Path,
) -> None:
    from modular_research_runner.engine import BacktestJob
    from modular_research_runner.models import TimeRange

    start = datetime(2024, 1, 1, tzinfo=UTC)
    period = TimeRange(start, start + timedelta(seconds=300 * 4_000))
    original = BacktestJob(
        run_id="frozen-job",
        experiment_id="frozen-experiment",
        stage_id="optimization_000",
        split_id="search-train",
        dataset_id="registered-ms5",
        scenario_id="base",
        seed=312,
        parameters={"family": "synthetic", "factory_run_id": "frozen-run"},
        output_directory=tmp_path / "unused",
        stage_role="optimization_000",
        period=period,
    )

    bounded = _prefix_job(original, 1_000, tmp_path / "scratch-case")

    assert bounded.stage_role == original.stage_role == "optimization_000"
    assert bounded.seed == original.seed
    assert dict(bounded.parameters) == dict(original.parameters)
    assert bounded.period.start == start
    assert bounded.period.end == start + timedelta(seconds=300 * 1_000)
    assert bounded.output_directory == tmp_path / "scratch-case"


def test_prefix_job_fails_closed_when_window_escapes_frozen_train_period(
    tmp_path: Path,
) -> None:
    from modular_research_runner.engine import BacktestJob
    from modular_research_runner.models import TimeRange

    start = datetime(2024, 1, 1, tzinfo=UTC)
    job = BacktestJob(
        run_id="frozen-job",
        experiment_id="frozen-experiment",
        stage_id="optimization_000",
        split_id="search-train",
        dataset_id="registered-ms5",
        scenario_id="base",
        seed=312,
        parameters={},
        output_directory=tmp_path,
        stage_role="optimization_000",
        period=TimeRange(start, start + timedelta(hours=1)),
    )
    with pytest.raises(CompatibilityError, match="escapes"):
        _prefix_job(job, 1_000, tmp_path / "bounded")


def test_scratch_and_result_hash_helpers_reject_symlink_escape(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "escape").symlink_to(target, target_is_directory=True)

    with pytest.raises(CompatibilityError, match="empty"):
        _prepare_scratch(scratch)
    with pytest.raises(CompatibilityError, match="symlink"):
        _tree_sha256(scratch)


def test_child_receipt_only_accepts_sanitized_ordered_prefix_evidence() -> None:
    case = {
        "family": "breakout_return",
        "bars": 1_000,
        "stage_role": "optimization_000",
        "status": "completed",
        "reason_code": None,
        "duration_seconds": 0.5,
        "input_sha256": "a" * 64,
        "result_tree_sha256": "b" * 64,
        "profile_sha256": "c" * 64,
    }
    receipt = _stopped_child_receipt(case)

    sanitized = _sanitized_child_receipt(json.dumps(receipt))
    assert sanitized["cases"] == [case]
    assert "trades" not in str(sanitized).lower()

    case["trade_count"] = 17
    with pytest.raises(CompatibilityError, match="unexpected fields"):
        _sanitized_child_receipt(json.dumps(receipt))


def test_child_receipt_rejects_skipped_or_reordered_families() -> None:
    fvg_case: dict[str, Any] = {
        "family": "fvg_rt",
        "bars": 1_000,
        "stage_role": "optimization_000",
        "status": "completed",
        "reason_code": None,
        "duration_seconds": 0.5,
        "input_sha256": "a" * 64,
        "result_tree_sha256": "b" * 64,
        "profile_sha256": "c" * 64,
    }
    receipt = _stopped_child_receipt(fvg_case)
    with pytest.raises(CompatibilityError, match="order/coverage"):
        _sanitized_child_receipt(json.dumps(receipt))


def test_parent_child_bootstrap_prioritizes_accepted_source_checkout(tmp_path: Path) -> None:
    accepted = tmp_path / "accepted"
    controller = tmp_path / "controller"
    for root in (accepted, controller):
        for directory in (
            "research/strategy_factory/src/strategy_factory",
            "research/experiment_runner/src",
            "src",
            "modules/example/src",
            "plugins/fvg/src",
        ):
            (root / directory).mkdir(parents=True)

    command = _child_command(accepted, controller, ("--synthetic",))
    bootstrap = command[3]

    assert command[1] == "-B"
    assert bootstrap.index(str(accepted / "research/strategy_factory/src")) < bootstrap.index(
        str(controller / "research/strategy_factory/src")
    )
    assert str(accepted / "plugins/fvg/src") in bootstrap
    assert "strategy_factory.__path__.append" in bootstrap


def test_watchdog_terminates_child_when_synthetic_rss_exceeds_limit(tmp_path: Path) -> None:
    status, return_code, reason, stdout = launch_bounded_child(
        (sys.executable, "-c", "import time; time.sleep(30)"),
        cwd=tmp_path,
        environment={},
        timeout_seconds=10,
        memory_limit_bytes=1,
        rss_reader=lambda _pid: 2,
    )

    assert status == "memory_limit"
    assert return_code is not None and return_code != 0
    assert reason == "CHILD_RSS_LIMIT"
    assert stdout == ""


def test_watchdog_hard_timeout_and_synthetic_success(tmp_path: Path) -> None:
    timed_out = launch_bounded_child(
        (sys.executable, "-c", "import time; time.sleep(30)"),
        cwd=tmp_path,
        environment={},
        timeout_seconds=0,
        rss_reader=lambda _pid: 0,
    )
    completed = launch_bounded_child(
        (sys.executable, "-c", "print('{\"safe\":true}')"),
        cwd=tmp_path,
        environment={},
        timeout_seconds=3,
        rss_reader=lambda _pid: 0,
    )

    assert timed_out[0] == "hard_timeout"
    assert timed_out[2] == "CHILD_WALL_TIMEOUT"
    assert completed[0] == "completed"
    assert '"safe":true' in completed[3]
