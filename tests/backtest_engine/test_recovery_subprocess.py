from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _write_fixture(root: Path, output: Path) -> Path:
    (root / "bars.csv").write_text(
        "open_time,open,high,low,close,volume\n"
        + "\n".join(
            f"2024-01-01T00:{index * 5:02d}:00Z,{100 + index},{102 + index},"
            f"{99 + index},{101 + index},{10 + index}"
            for index in range(8)
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "process_fixture.py").write_text(
        """from __future__ import annotations

from collections.abc import Mapping

from modular_core.manifest import ModuleManifest
from modular_core.module_api import MarketView, ModuleDecision, StrategyModule


class ProcessFixtureModule(StrategyModule):
    name = "process_fixture"
    manifest = ModuleManifest(
        name=name,
        version="0.1.0",
        api_version="1.0",
        skip_until_ready=False,
        state_schema_version="1",
    )

    def __init__(self) -> None:
        self.counter = 0

    def on_bar(self, view: MarketView) -> ModuleDecision:
        self.counter += 1
        return ModuleDecision()

    def snapshot_state(self) -> Mapping[str, object]:
        return {"counter": self.counter}

    def restore_state(self, state: Mapping[str, object]) -> None:
        self.counter = int(state["counter"])
""",
        encoding="utf-8",
    )
    (root / "modules.yaml").write_text(
        "modules:\n  - class: process_fixture:ProcessFixtureModule\n",
        encoding="utf-8",
    )
    config = root / f"config-{output.name}.yaml"
    config.write_text(
        f"""data:
  path: bars.csv
  format: csv
  symbol: BTCUSDT
modules: modules.yaml
initial_capital: 10000
requested_timeframes: [15m]
seed: 17
checkpoint:
  mode: replay
  restart_at_bar: 3
output:
  directory: {output.name}
""",
        encoding="utf-8",
    )
    return config


def _run_cli(
    config: Path,
    *,
    hash_seed: int,
) -> tuple[dict[str, object], dict[str, object]]:
    repository = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    pythonpath = [str(config.parent), str(repository / "src")]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    env["PYTHONHASHSEED"] = str(hash_seed)
    completed = subprocess.run(
        [sys.executable, "-m", "modular_backtest", "run", "--config", str(config)],
        cwd=repository,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    response = json.loads(completed.stdout)
    result_directory = Path(str(response["output_directory"]))
    evidence = json.loads((result_directory / "restart-evidence.json").read_text(encoding="utf-8"))
    return response, evidence


def test_replay_restart_is_stable_across_independent_python_processes(
    tmp_path: Path,
) -> None:
    first_config = _write_fixture(tmp_path, tmp_path / "process-one")
    second_config = _write_fixture(tmp_path, tmp_path / "process-two")

    first_response, first_evidence = _run_cli(first_config, hash_seed=1)
    second_response, second_evidence = _run_cli(second_config, hash_seed=9173)

    assert first_response["restart_verified"] is True
    assert second_response["restart_verified"] is True
    assert first_response["run_id"] == second_response["run_id"]
    assert first_response["output_directory"] != second_response["output_directory"]

    assert first_evidence["continuous_sha256"] is None
    assert second_evidence["continuous_sha256"] is None
    assert first_evidence["restarted_sha256"] == second_evidence["restarted_sha256"]
    assert first_evidence["simulation_passes"] == 1
    assert second_evidence["simulation_passes"] == 1
    assert first_evidence["recovery_mode"] == "runtime_snapshot_v1"

    first_checkpoint = first_evidence["checkpoint"]
    second_checkpoint = second_evidence["checkpoint"]
    assert isinstance(first_checkpoint, dict)
    assert isinstance(second_checkpoint, dict)
    assert first_checkpoint["bars_processed"] == 4
    assert first_checkpoint["runtime_sha256"] == second_checkpoint["runtime_sha256"]
    assert first_checkpoint["event_sha256"] == second_checkpoint["event_sha256"]
    assert first_checkpoint["equity_sha256"] == second_checkpoint["equity_sha256"]
