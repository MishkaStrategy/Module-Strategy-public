from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

from market_data_ingestion.integrity import verify_registered_dataset
from market_data_ingestion.registry import SQLiteDatasetRegistry

EXPECTED_CORPUS_HASH = "6d32c075f5adf3623679edd49c20ee8ee8758d3ea0e75b7f06613593e065c44e"
EXPECTED_DATASET_ID = "binance_usdt-m-perpetual_btcusdt_5m"
EXPECTED_ROWS = 12_968_018
EXPECTED_PARTITIONS = 3_865
SMOKE_ROWS = 576
MIN_FREE_BYTES = 8 * 1024**3
REQUIRED_SYMBOLS = {
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "SOLUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "TRXUSDT",
    "LINKUSDT",
    "AVAXUSDT",
}
FORBIDDEN_EVIDENCE_KEYS = {
    "path",
    "manifest_uri",
    "storage_uri",
    "registry_uri",
    "evidence_path",
    "source_uri",
}


def fail(message: str) -> NoReturn:
    raise SystemExit(message)


def run_json(command: list[str], *, cwd: Path | None = None) -> dict[str, object]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"command returned invalid JSON: {' '.join(command)}: {completed.stdout[-500:]}"
        ) from exc
    if not isinstance(payload, dict):
        fail(f"command returned a non-object JSON payload: {' '.join(command)}")
    return payload


def ensure_safe_directory(path: Path) -> Path:
    requested = path.expanduser()
    if requested.is_symlink():
        fail(f"unsafe symbolic-link directory: {requested}")
    requested.mkdir(parents=True, exist_ok=True)
    if requested.is_symlink() or not requested.is_dir():
        fail(f"unsafe or missing directory: {requested}")
    return requested.resolve(strict=True)


def discover_corpus(home: Path) -> Path:
    patterns = (
        home / "Library/CloudStorage/GoogleDrive-*/My Drive/binance_usdtm_futures_history",
        home / "Library/CloudStorage/GoogleDrive-*/Мой диск/binance_usdtm_futures_history",
        home / "Google Drive/My Drive/binance_usdtm_futures_history",
        home / "Google Диск/Мой диск/binance_usdtm_futures_history",
    )
    allowed_roots = tuple(
        path.resolve()
        for path in (
            home / "Library/CloudStorage",
            home / "Google Drive",
            home / "Google Диск",
        )
        if path.exists() and not path.is_symlink()
    )
    candidates: set[Path] = set()
    for pattern in patterns:
        for value in glob.glob(str(pattern)):
            candidate = Path(value)
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            resolved = candidate.resolve(strict=True)
            if not any(root == resolved or root in resolved.parents for root in allowed_roots):
                fail("discovered corpus root escaped approved local locations")
            candidates.add(resolved)
    if len(candidates) != 1:
        fail(f"expected exactly one approved baseline corpus root, found {len(candidates)}")
    root = next(iter(candidates))
    observed = {
        item.name
        for item in root.iterdir()
        if item.is_dir() and not item.is_symlink() and item.name != "XAUUSDT"
    }
    if observed != REQUIRED_SYMBOLS:
        fail(
            "baseline symbol folders mismatch; "
            f"missing={sorted(REQUIRED_SYMBOLS - observed)}, "
            f"extra={sorted(observed - REQUIRED_SYMBOLS)}"
        )
    return root


def reject_private_evidence(value: object, *, private_markers: tuple[str, ...]) -> None:
    if isinstance(value, dict):
        overlap = FORBIDDEN_EVIDENCE_KEYS.intersection(value)
        if overlap:
            fail(f"registration evidence leaks forbidden keys: {sorted(overlap)}")
        for nested in value.values():
            reject_private_evidence(nested, private_markers=private_markers)
    elif isinstance(value, list):
        for nested in value:
            reject_private_evidence(nested, private_markers=private_markers)
    elif isinstance(value, str) and any(marker and marker in value for marker in private_markers):
        fail("acceptance evidence leaks a private local path")


def verify_registration(
    *,
    evidence_path: Path,
    storage_root: Path,
    home: Path,
) -> tuple[dict[str, object], str]:
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("status") != "PASS":
        fail("persistent registration evidence is not PASS")
    exact = {
        "corpus_hash": EXPECTED_CORPUS_HASH,
        "dataset_count": 10,
        "raw_artifact_count": 50,
        "partition_count": EXPECTED_PARTITIONS,
        "total_rows": EXPECTED_ROWS,
        "raw_market_data_uploaded": False,
    }
    for key, expected in exact.items():
        if payload.get(key) != expected:
            fail(
                f"persistent evidence {key} mismatch: expected {expected!r}, "
                f"got {payload.get(key)!r}"
            )
    reused = payload.get("reused_dataset_count")
    if not isinstance(reused, int) or isinstance(reused, bool) or not 0 <= reused <= 10:
        fail("persistent registration reused count is invalid")
    datasets = payload.get("datasets")
    if not isinstance(datasets, list) or len(datasets) != 10:
        fail("persistent registration must expose ten sanitized datasets")

    registry = SQLiteDatasetRegistry(storage_root / "registry.sqlite3")
    observed_ids: set[str] = set()
    btc_hash = ""
    for item in datasets:
        if not isinstance(item, dict):
            fail("registration dataset evidence must be an object")
        dataset_id = item.get("dataset_id")
        dataset_hash = item.get("dataset_hash")
        if not isinstance(dataset_id, str) or not dataset_id:
            fail("registration dataset identity is invalid")
        if not isinstance(dataset_hash, str) or len(dataset_hash) != 64:
            fail("registration dataset hash is invalid")
        if item.get("version") != 1:
            fail("accepted baseline dataset version must remain 1")
        if item.get("validation_status") != "VALID":
            fail("persistent dataset is not VALID")
        if item.get("timeframes") != ["5m", "10m", "15m", "1h", "4h"]:
            fail("persistent dataset timeframe matrix mismatch")
        if item.get("raw_artifact_count") != 5:
            fail("persistent dataset raw-lineage count mismatch")
        descriptor = registry.inspect(dataset_id, 1)
        if descriptor is None:
            fail(f"registered descriptor missing: {dataset_id}")
        integrity = verify_registered_dataset(storage_root, descriptor, deep=True)
        if integrity.get("status") != "ok":
            fail(f"persistent dataset integrity failed: {dataset_id}: {integrity.get('issues')}")
        if dataset_id == EXPECTED_DATASET_ID:
            btc_hash = dataset_hash
        observed_ids.add(dataset_id)
    if len(observed_ids) != 10 or EXPECTED_DATASET_ID not in observed_ids:
        fail("persistent dataset identities are incomplete")
    if not btc_hash:
        fail("BTCUSDT registered dataset hash is missing")
    reject_private_evidence(
        payload,
        private_markers=(str(storage_root), str(home)),
    )
    return payload, btc_hash


def smoke_config(*, storage_root: Path, smoke_root: Path) -> Path:
    modules_path = smoke_root / "modules.yaml"
    modules_path.write_text("modules: []\n", encoding="utf-8")
    output_root = smoke_root / "results"
    output_root.mkdir(parents=True, exist_ok=True)
    config_path = smoke_root / "backtest.yaml"
    config_path.write_text(
        f"""data:
  storage_root: {storage_root}
  dataset_id: {EXPECTED_DATASET_ID}
  dataset_version: 1
  symbol: BTCUSDT
  market_type: binance-usdt-m-perpetual
  base_timeframe: 5m
  gap_policy: reject
  start: 2024-01-01T00:00:00Z
  end: 2024-01-03T00:00:00Z
modules: {modules_path}
initial_capital: 10000
requested_timeframes: []
seed: 0
end_of_test_policy: force_close
checkpoint:
  mode: disabled
fees:
  maker_rate: 0.0002
  taker_rate: 0.00055
execution:
  slippage_bps: 2.5
  stop_slippage_bps: 2.5
  intrabar_priority: stop
  limit_fill_policy: at_limit
  allow_exit_on_entry_bar: false
  allow_reentry_on_exit_bar: false
  target_liquidity: maker
  stop_limit_same_bar_policy: defer
risk:
  default_risk_fraction: 0.005
  max_risk_fraction: 0.02
  max_notional_multiple: 3.0
instrument:
  base_asset: BTC
  quote_asset: USDT
  market_type: linear_futures
  tick_size: 0.1
  quantity_step: 0.001
  min_quantity: 0.001
  min_notional: 5
  price_precision: 1
  quantity_precision: 3
output:
  directory: {output_root}
""",
        encoding="utf-8",
    )
    return config_path


def verify_smoke(
    *,
    validation: dict[str, object],
    run: dict[str, object],
    smoke_root: Path,
    expected_sha: str,
    expected_hash: str,
) -> dict[str, object]:
    expected_source = f"dataset://{EXPECTED_DATASET_ID}/v1"
    exact = {
        "path": expected_source,
        "dataset_id": EXPECTED_DATASET_ID,
        "dataset_version": 1,
        "dataset_hash": expected_hash,
        "sha256": expected_hash,
        "rows": SMOKE_ROWS,
        "gaps": 0,
        "duplicates": 0,
        "first_timestamp": "2024-01-01T00:00:00+00:00",
        "last_timestamp": "2024-01-02T23:55:00+00:00",
    }
    for key, expected in exact.items():
        if validation.get(key) != expected:
            fail(
                f"smoke validation {key} mismatch: expected {expected!r}, "
                f"got {validation.get(key)!r}"
            )
    partition_count = validation.get("partition_count")
    if not isinstance(partition_count, int) or partition_count < 1:
        fail("smoke validation partition count is invalid")
    if run.get("bars_processed") != SMOKE_ROWS:
        fail("smoke backtest processed-bar count mismatch")
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        fail("smoke run ID is invalid")
    output = Path(str(run.get("output_directory"))).resolve(strict=True)
    if smoke_root not in output.parents:
        fail("smoke result escaped temporary output root")
    result_validation = json.loads((output / "data-validation.json").read_text(encoding="utf-8"))
    if result_validation != validation:
        fail("published result validation differs from command validation")
    if not (output / "artifact-manifest.json").is_file():
        fail("smoke result artifact manifest is missing")
    reject_private_evidence(
        validation,
        private_markers=(str(Path.home()), str(smoke_root), str(output)),
    )
    return {
        "status": "PASS",
        "expected_sha": expected_sha,
        "dataset_id": EXPECTED_DATASET_ID,
        "dataset_version": 1,
        "dataset_hash": expected_hash,
        "source_uri": expected_source,
        "partition_count": partition_count,
        "rows": SMOKE_ROWS,
        "first_timestamp": validation["first_timestamp"],
        "last_timestamp": validation["last_timestamp"],
        "gaps": 0,
        "duplicates": 0,
        "run_id": run_id,
        "bars_processed": SMOKE_ROWS,
        "restart_verified": run.get("restart_verified"),
        "raw_market_data_uploaded": False,
        "local_storage_path_uploaded": False,
    }


def enforce_acceptance_inventory(acceptance_root: Path) -> None:
    files = sorted(path for path in acceptance_root.rglob("*") if path.is_file())
    if [path.name for path in files] != ["registration.json", "smoke.json"]:
        fail(f"closed evidence inventory mismatch: {[path.name for path in files]}")
    if any(path.is_symlink() for path in acceptance_root.rglob("*")):
        fail("acceptance evidence must not contain symbolic links")
    if sum(path.stat().st_size for path in files) > 64 * 1024:
        fail("acceptance evidence exceeds the 64 KiB closed boundary")


def main() -> None:
    workspace = Path(os.environ["GITHUB_WORKSPACE"]).resolve(strict=True)
    runner_temp = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    expected_sha = os.environ["EXPECTED_SHA"]
    if len(expected_sha) != 40 or any(
        character not in "0123456789abcdef" for character in expected_sha
    ):
        fail("EXPECTED_SHA must be a lowercase 40-character commit SHA")

    home = Path.home().resolve(strict=True)
    corpus_root = discover_corpus(home)
    module_data_root = ensure_safe_directory(home / "ModuleStrategyData")
    storage_root = ensure_safe_directory(module_data_root / "market_data")
    evidence_root = ensure_safe_directory(module_data_root / "evidence")
    private_root = ensure_safe_directory(module_data_root / "private")
    if shutil.disk_usage(storage_root).free < MIN_FREE_BYTES:
        fail("persistent registration requires at least 8 GiB free")

    drive_index = (
        workspace
        / "modules/market_data_ingestion/configs/market_data/binance_usdtm_baseline_drive_index.json"
    )
    private_spec = private_root / "binance-usdtm-baseline-spec.json"
    private_report = private_root / "binance-usdtm-baseline-inventory.json"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "market_data_ingestion.drive_corpus",
            "--corpus-root",
            str(corpus_root),
            "--drive-index",
            str(drive_index),
            "--output",
            str(private_spec),
            "--report",
            str(private_report),
        ],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    private_spec.chmod(0o600)
    private_report.chmod(0o600)

    persistent_evidence = evidence_root / "binance-usdtm-registration.json"
    run_json(
        [
            "market-data",
            "register-external-corpus",
            "--spec",
            str(private_spec),
            "--storage-root",
            str(storage_root),
            "--output",
            str(persistent_evidence),
        ],
        cwd=workspace,
    )
    persistent_evidence.chmod(0o600)

    acceptance_root = runner_temp / "persistent-acceptance"
    if acceptance_root.exists():
        shutil.rmtree(acceptance_root)
    acceptance_root.mkdir(parents=True)
    registration_copy = acceptance_root / "registration.json"
    shutil.copyfile(persistent_evidence, registration_copy)
    registration, btc_hash = verify_registration(
        evidence_path=registration_copy,
        storage_root=storage_root,
        home=home,
    )
    registration_copy.write_text(
        json.dumps(registration, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    smoke_root = runner_temp / "registered-btcusdt-smoke"
    if smoke_root.exists():
        shutil.rmtree(smoke_root)
    smoke_root.mkdir(parents=True)
    try:
        config_path = smoke_config(storage_root=storage_root, smoke_root=smoke_root)
        validation = run_json(
            ["modular-backtest", "validate-data", "--config", str(config_path)],
            cwd=workspace,
        )
        run = run_json(
            ["modular-backtest", "run", "--config", str(config_path)],
            cwd=workspace,
        )
        smoke = verify_smoke(
            validation=validation,
            run=run,
            smoke_root=smoke_root,
            expected_sha=expected_sha,
            expected_hash=btc_hash,
        )
        (acceptance_root / "smoke.json").write_text(
            json.dumps(smoke, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        enforce_acceptance_inventory(acceptance_root)
    finally:
        if smoke_root.exists():
            shutil.rmtree(smoke_root)

    print(
        json.dumps(
            {
                "status": "PASS",
                "dataset_count": 10,
                "dataset_id": EXPECTED_DATASET_ID,
                "dataset_version": 1,
                "smoke_rows": SMOKE_ROWS,
                "evidence_files": ["registration.json", "smoke.json"],
                "raw_market_data_uploaded": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
