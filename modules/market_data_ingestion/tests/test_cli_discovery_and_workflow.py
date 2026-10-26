from __future__ import annotations

import json
from pathlib import Path

from market_data_ingestion.cli import main


def _output(capsys):  # type: ignore[no-untyped-def]
    return json.loads(capsys.readouterr().out)


def test_cli_discovery_commands_are_executable(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["exchanges"]) == 0
    exchanges = _output(capsys)
    assert {item["exchange"] for item in exchanges} == {"binance", "fake"}

    assert main(["markets", "--exchange", "fake"]) == 0
    assert "usdt_m_perpetual" in _output(capsys)

    assert main(["capabilities", "--exchange", "fake"]) == 0
    capabilities = _output(capsys)
    assert capabilities["exchange"]["id"] == "fake"

    assert (
        main(
            [
                "instruments",
                "--exchange",
                "fake",
                "--market",
                "usdt_m_perpetual",
                "--symbol",
                "BTCUSDT",
            ]
        )
        == 0
    )
    instrument = _output(capsys)
    assert instrument["reference"]["symbol"] == "BTCUSDT"

    assert main(["markets", "--exchange", "missing"]) == 2
    assert _output(capsys)["code"] == "EXCHANGE_UNSUPPORTED"


def test_cli_fake_download_inspect_validate_and_status(
    tmp_path: Path,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    config = tmp_path / "fake.yaml"
    config.write_text(
        """schema_version: 1
request_id: cli-full-workflow
exchange:
  id: fake
  market_type: usdt_m_perpetual
instrument:
  symbol: BTCUSDT
range:
  mode: explicit
  start: "2026-01-01T00:00:00Z"
  end: "2026-01-01T01:00:00Z"
data:
  base_timeframe: 5m
  derived_timeframes: [15m, 1h]
  required_types: [klines]
  optional_types: []
download:
  concurrency: 2
  retries: 2
  timeout_seconds: 5
  resume: true
  max_records_in_memory: 1000
validation:
  deep: true
  repair_gaps: true
  reject_unresolved_required_gaps: true
  allow_partial_optional_data: true
storage:
  root: ./market-data
  format: csv
  compression: zstd
  partition: monthly
""",
        encoding="utf-8",
    )

    assert main(["download", "--config", str(config)]) == 0
    download = _output(capsys)
    dataset = download["dataset"]
    dataset_id = dataset["dataset_id"]
    storage_root = tmp_path / "market-data"

    assert (
        main(
            [
                "inspect",
                "--storage-root",
                str(storage_root),
                "--dataset-id",
                dataset_id,
            ]
        )
        == 0
    )
    inspected = _output(capsys)
    assert inspected["dataset_hash"] == dataset["dataset_hash"]

    assert (
        main(
            [
                "validate",
                "--storage-root",
                str(storage_root),
                "--dataset-id",
                dataset_id,
                "--deep",
            ]
        )
        == 0
    )
    assert _output(capsys)["status"] == "ok"

    assert main(["status", "--storage-root", str(storage_root)]) == 0
    status = _output(capsys)
    assert status["status"] == "ok"
    assert status["datasets"] == 1

    assert (
        main(
            [
                "inspect",
                "--storage-root",
                str(storage_root),
                "--dataset-id",
                "missing_dataset",
            ]
        )
        == 4
    )
    assert _output(capsys)["code"] == "DATASET_NOT_FOUND"
