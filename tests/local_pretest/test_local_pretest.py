from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "local_pretest.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("local_pretest", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_dataset(root: Path, *, gap: bool = False) -> Path:
    dataset = root / "dataset"
    dataset.mkdir()
    data_path = dataset / "BTCUSDT_5m.csv"
    start = datetime(2026, 1, 1, tzinfo=UTC)
    timestamps = [start + timedelta(minutes=5 * index) for index in range(12)]
    if gap:
        timestamps = [
            value + (timedelta(minutes=5) if index >= 5 else timedelta())
            for index, value in enumerate(timestamps)
        ]
    with data_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["open_time", "open", "high", "low", "close", "volume"],
        )
        writer.writeheader()
        for index, timestamp in enumerate(timestamps):
            price = 100 + index
            writer.writerow(
                {
                    "open_time": timestamp.isoformat(),
                    "open": price,
                    "high": price + 2,
                    "low": price - 1,
                    "close": price + 1,
                    "volume": 10,
                }
            )

    with (dataset / "download_summary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "symbol",
                "timeframe",
                "status",
                "rows",
                "first_open_time",
                "last_open_time",
                "gap_events",
                "estimated_missing_5m_rows",
            ],
        )
        writer.writeheader()
        for timeframe in ("5m", "15m", "1h", "4h"):
            writer.writerow(
                {
                    "symbol": "BTCUSDT",
                    "timeframe": timeframe,
                    "status": "ok",
                    "rows": 12 if timeframe == "5m" else 1,
                    "first_open_time": timestamps[0].isoformat(),
                    "last_open_time": timestamps[-1].isoformat(),
                    "gap_events": 0 if timeframe == "5m" else "",
                    "estimated_missing_5m_rows": 0 if timeframe == "5m" else "",
                }
            )
    return dataset


def _pretest_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "repo" / ".local-tests" / "pretest"
    (workspace / "results").mkdir(parents=True)
    return workspace


def test_full_csv_scan_and_config_generation(tmp_path: Path) -> None:
    module = _load_module()
    dataset = _write_dataset(tmp_path)
    reporter = module.Reporter()
    summary, _ = module._read_summary(reporter, dataset, "BTCUSDT")
    data_path = module._locate_data_file(dataset, "BTCUSDT", None)
    facts = module._inspect_data(reporter, data_path, summary, quick=False)
    assert facts.rows == 12
    assert facts.gaps == 0

    repo = tmp_path / "repo"
    (repo / "configs").mkdir(parents=True)
    (repo / "configs" / "integration.example.yaml").write_text("modules: []\n", encoding="utf-8")
    generated = module._write_configs(
        reporter,
        repo_dir=repo,
        workspace=repo / ".local-tests" / "pretest",
        data=facts,
        symbol="BTCUSDT",
    )
    smoke = Path(generated["smoke"]).read_text(encoding="utf-8")
    restart = Path(generated["restart_replay"]).read_text(encoding="utf-8")
    assert "requested_timeframes: [15m, 1h, 4h]" in smoke
    assert "1d" not in smoke
    assert "mode: replay" in restart
    assert generated["smoke"] != generated["smoke_repeat"]


def test_gap_is_blocking(tmp_path: Path) -> None:
    module = _load_module()
    dataset = _write_dataset(tmp_path, gap=True)
    reporter = module.Reporter()
    summary, _ = module._read_summary(reporter, dataset, "BTCUSDT")
    data_path = module._locate_data_file(dataset, "BTCUSDT", None)
    try:
        module._inspect_data(reporter, data_path, summary, quick=False)
    except module.PretestError as exc:
        assert "gap" in str(exc)
    else:
        raise AssertionError("gap must fail the full data scan")


def test_script_help() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert result.returncode == 0
    assert "--dataset-dir" in result.stdout


def test_report_is_strict_json(tmp_path: Path) -> None:
    module = _load_module()
    reporter = module.Reporter()
    reporter.warn("example", "warning")
    path = module._write_report(
        workspace=tmp_path,
        reporter=reporter,
        repository={"head": "abc"},
        disk={"free_gib": 100.0},
        summary=None,
        data=None,
        generated={},
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["failures"] == 0
    assert payload["warnings"] == 1


def test_generated_result_reset_is_allowlisted_and_idempotent(tmp_path: Path) -> None:
    module = _load_module()
    workspace = _pretest_workspace(tmp_path)
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    source_marker = dataset / "source.parquet"
    source_marker.write_text("immutable-source", encoding="utf-8")
    unrelated = workspace / "results" / "unrelated"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("keep", encoding="utf-8")

    for name in ("smoke", "smoke-repeat", "restart-replay", "full-btc"):
        run = workspace / "results" / name / "run-existing"
        run.mkdir(parents=True)
        (run / "result.json").write_text("{}", encoding="utf-8")

    removed = module._reset_generated_results(
        workspace,
        ("smoke", "smoke-repeat", "restart-replay", "full-btc"),
    )

    assert {path.name for path in removed} == {
        "smoke",
        "smoke-repeat",
        "restart-replay",
        "full-btc",
    }
    for name in ("smoke", "smoke-repeat", "restart-replay", "full-btc"):
        assert not (workspace / "results" / name).exists()
    assert (unrelated / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert source_marker.read_text(encoding="utf-8") == "immutable-source"
    assert module._reset_generated_results(workspace, ("smoke", "restart-replay")) == ()


@pytest.mark.parametrize(
    "invalid",
    ("../dataset", "/tmp/not-owned", "arbitrary", "smoke/child", ".", ""),
)
def test_generated_result_reset_rejects_non_owned_names(
    tmp_path: Path,
    invalid: str,
) -> None:
    module = _load_module()
    workspace = _pretest_workspace(tmp_path)
    existing = workspace / "results" / "smoke" / "run-existing"
    existing.mkdir(parents=True)

    with pytest.raises(module.PretestError, match="non-generated result root"):
        module._reset_generated_results(workspace, ("smoke", invalid))

    assert existing.is_dir(), "all names must validate before any deletion"


def test_generated_result_reset_rejects_wrong_workspace(tmp_path: Path) -> None:
    module = _load_module()
    workspace = tmp_path / "repo" / "pretest"
    workspace.mkdir(parents=True)

    with pytest.raises(module.PretestError, match="workspace"):
        module._reset_generated_results(workspace, ("smoke",))


def test_generated_result_reset_rejects_symlink_target(tmp_path: Path) -> None:
    module = _load_module()
    workspace = _pretest_workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    (workspace / "results" / "smoke").symlink_to(outside, target_is_directory=True)

    with pytest.raises(module.PretestError, match="must not be a symlink"):
        module._reset_generated_results(workspace, ("smoke",))

    assert marker.read_text(encoding="utf-8") == "keep"


def test_generated_result_reset_rejects_symlink_results_root(tmp_path: Path) -> None:
    module = _load_module()
    workspace = tmp_path / "repo" / ".local-tests" / "pretest"
    workspace.mkdir(parents=True)
    outside = tmp_path / "outside-results"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    (workspace / "results").symlink_to(outside, target_is_directory=True)

    with pytest.raises(module.PretestError, match="results root must not be a symlink"):
        module._reset_generated_results(workspace, ("smoke",))

    assert marker.read_text(encoding="utf-8") == "keep"


def test_generated_result_reset_rejects_file_collision(tmp_path: Path) -> None:
    module = _load_module()
    workspace = _pretest_workspace(tmp_path)
    collision = workspace / "results" / "smoke"
    collision.write_text("not-a-directory", encoding="utf-8")

    with pytest.raises(module.PretestError, match="not a directory"):
        module._reset_generated_results(workspace, ("smoke",))

    assert collision.read_text(encoding="utf-8") == "not-a-directory"
