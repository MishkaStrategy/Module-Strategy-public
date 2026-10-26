"""Prove canonical Variant 2 parity through the registered Backtest data bridge."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn

import pandas as pd
from modular_backtest.config import ConfigurationError, DataSourceConfig
from modular_backtest.registered_data import (
    RegisteredDataError,
    RegisteredSourcePosition,
    open_streaming_registered_candles,
)

from ._common import (
    CANDIDATE_ID,
    COMPOSITION_HASH,
    REGISTERED_DATASET_HASH,
    canonical_sha256,
    load_5m,
    sha256_file,
)
from ._portfolio import run_portfolio, summarize
from ._signals import build_signals

EXPECTED_DATASET_ID = "binance_usdt-m-perpetual_btcusdt_5m"
EXPECTED_DATASET_VERSION = 1
EXPECTED_ROWS = 721_597
EXPECTED_FIRST = pd.Timestamp("2019-09-08T17:55:00+00:00")
EXPECTED_LAST = pd.Timestamp("2026-07-19T06:55:00+00:00")
EXPECTED_SEMANTIC_DIGEST = "63a2c102601cec5a613ff3749754d31955d6513c654c73cd3dd7e09bbc5cefdb"
EXPECTED_LEDGER_HASHES = {
    "combined-ledger.csv": "0adac1fffc4d3d3e31187c6ac9deb4a026e768619a9aad8abac054a112aa4212",
    "fvg_rt_final_15m-ledger.csv": (
        "3998ce979e4547417e653f803fdc089709cfbae6143d4b51a34000e07529b879"
    ),
    "frozen_1h_short-ledger.csv": (
        "61fb968c0ad73a6c3d119608d91072e3496c12d72097d87aadc541a1a688ddc0"
    ),
    "frozen_4h_short-ledger.csv": (
        "df8a67e24068db1b7591fba0177735b118a3c28d3a8846e591a2e5d8eadad249"
    ),
}
EXPECTED_METRICS = {
    "final_equity": 15_266.31855203826,
    "max_closed_equity_drawdown": 0.1164174394297347,
    "net_return": 0.526631855203826,
    "profit_factor": 1.328366621740268,
    "trade_count": 438,
    "win_rate": 0.3515981735159817,
}
EXPECTED_COMPONENT_TRADES = {
    "fvg_rt_final_15m": 333,
    "frozen_1h_short": 84,
    "frozen_4h_short": 21,
}


def _fail(message: str) -> NoReturn:
    raise RuntimeError(message)


def _load_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        _fail(f"{path.name} must contain a JSON object")
    return value


def _candle_record(candle: Any) -> dict[str, object]:
    timestamp = pd.Timestamp(candle.timestamp)
    if timestamp.tzinfo is None:
        _fail("registered candle timestamp must be timezone-aware")
    timestamp = timestamp.tz_convert("UTC")
    return {
        "open_time": timestamp,
        "open": float(candle.open),
        "high": float(candle.high),
        "low": float(candle.low),
        "close": float(candle.close),
        "volume": float(candle.volume),
        "close_time": (timestamp + pd.Timedelta(minutes=5) - pd.Timedelta(milliseconds=1)),
    }


def _frame_from_records(records: list[dict[str, object]]) -> pd.DataFrame:
    columns = [
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
    ]
    frame = pd.DataFrame.from_records(records, columns=columns)
    timestamp_dtype = "datetime64[ms, UTC]"
    frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True).astype(timestamp_dtype)
    frame["close_time"] = pd.to_datetime(frame["close_time"], utc=True).astype(timestamp_dtype)
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="raise").astype("float64")
    return frame


def _expect_fail_closed_error(name: str, operation: Callable[[], object]) -> str:
    try:
        operation()
    except (ConfigurationError, RegisteredDataError):
        return name
    _fail(f"negative registered-data contract did not fail closed: {name}")


def _registered_config(
    *,
    storage_root: Path,
    dataset_id: str,
    dataset_version: int,
    start: pd.Timestamp,
    end: pd.Timestamp,
    timeframe: str = "5m",
    symbol: str = "BTCUSDT",
    batch_size: int = 8_192,
) -> DataSourceConfig:
    return DataSourceConfig(
        symbol=symbol,
        storage_root=storage_root,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        base_timeframe=timeframe,
        gap_policy="reject",
        start=start.to_pydatetime(),
        end=end.to_pydatetime(),
        registered_mode="streaming",
        stream_batch_size=batch_size,
    )


def _read_registered_with_restart(
    *,
    storage_root: Path,
    dataset_id: str,
    dataset_version: int,
    official_frame: pd.DataFrame,
    batch_size: int,
    restart_at_row: int,
) -> tuple[pd.DataFrame, dict[str, object], RegisteredSourcePosition]:
    start = pd.Timestamp(official_frame["open_time"].iloc[0])
    end = pd.Timestamp(official_frame["open_time"].iloc[-1]) + pd.Timedelta(minutes=5)
    config = _registered_config(
        storage_root=storage_root,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        start=start,
        end=end,
        batch_size=batch_size,
    )
    source = open_streaming_registered_candles(config, batch_size=batch_size)
    metadata = source.metadata()
    if metadata.dataset_id != dataset_id or metadata.dataset_version != dataset_version:
        _fail("registered dataset identity mismatch")
    if metadata.dataset_hash != REGISTERED_DATASET_HASH:
        _fail("registered dataset hash mismatch")
    if metadata.source_uri != f"dataset://{dataset_id}/v{dataset_version}":
        _fail("registered dataset source URI mismatch")
    if metadata.symbol != "BTCUSDT" or metadata.timeframe != "5m":
        _fail("registered dataset symbol/timeframe mismatch")
    if metadata.row_count != EXPECTED_ROWS:
        _fail("registered dataset row count mismatch")
    if pd.Timestamp(metadata.first_timestamp) != EXPECTED_FIRST:
        _fail("registered dataset first timestamp mismatch")
    if pd.Timestamp(metadata.last_timestamp) != EXPECTED_LAST:
        _fail("registered dataset last timestamp mismatch")
    if len(metadata.manifest_checksum) != 64 or len(metadata.schema_fingerprint) != 64:
        _fail("registered dataset checksum/fingerprint is malformed")
    if not 0 < restart_at_row < metadata.row_count:
        _fail("restart row must be inside the registered corpus")

    records: list[dict[str, object]] = []
    first = source.open()
    try:
        for _ in range(restart_at_row):
            records.append(_candle_record(next(first)))
        checkpoint = first.checkpoint_position()
    except StopIteration as exc:
        raise RuntimeError("registered source ended before the restart checkpoint") from exc
    finally:
        first.close()

    resumed = source.open(checkpoint)
    try:
        records.extend(_candle_record(candle) for candle in resumed)
        final_position = resumed.checkpoint_position()
    finally:
        resumed.close()

    if source.source_open_count != 2:
        _fail("registered source restart open count mismatch")
    if final_position.partition_index != metadata.partition_count:
        _fail("registered source did not reach its terminal partition position")
    if final_position.row_offset != 0:
        _fail("registered terminal row offset must be zero")
    if pd.Timestamp(final_position.last_timestamp) != EXPECTED_LAST:
        _fail("registered terminal timestamp mismatch")

    negative_checks = [
        _expect_fail_closed_error(
            "missing_version",
            lambda: open_streaming_registered_candles(
                _registered_config(
                    storage_root=storage_root,
                    dataset_id=dataset_id,
                    dataset_version=dataset_version + 1,
                    start=start,
                    end=end,
                    batch_size=batch_size,
                ),
                batch_size=batch_size,
            ),
        ),
        _expect_fail_closed_error(
            "wrong_timeframe",
            lambda: open_streaming_registered_candles(
                _registered_config(
                    storage_root=storage_root,
                    dataset_id=dataset_id,
                    dataset_version=dataset_version,
                    start=start,
                    end=end,
                    timeframe="1m",
                    batch_size=batch_size,
                ),
                batch_size=batch_size,
            ),
        ),
        _expect_fail_closed_error(
            "wrong_symbol",
            lambda: open_streaming_registered_candles(
                _registered_config(
                    storage_root=storage_root,
                    dataset_id=dataset_id,
                    dataset_version=dataset_version,
                    start=start,
                    end=end,
                    symbol="ETHUSDT",
                    batch_size=batch_size,
                ),
                batch_size=batch_size,
            ),
        ),
        _expect_fail_closed_error(
            "checkpoint_dataset_hash_mismatch",
            lambda: source.open(replace(checkpoint, dataset_hash="0" * 64)),
        ),
        _expect_fail_closed_error(
            "checkpoint_manifest_checksum_mismatch",
            lambda: source.open(replace(checkpoint, manifest_checksum="0" * 64)),
        ),
        _expect_fail_closed_error(
            "checkpoint_schema_fingerprint_mismatch",
            lambda: source.open(replace(checkpoint, schema_fingerprint="0" * 64)),
        ),
        _expect_fail_closed_error(
            "checkpoint_source_version_mismatch",
            lambda: source.open(replace(checkpoint, source_version="invalid-source-version")),
        ),
    ]

    frame = _frame_from_records(records)
    metadata_report = {
        "batch_size": batch_size,
        "bridge_version": metadata.bridge_version,
        "dataset_hash": metadata.dataset_hash,
        "dataset_id": metadata.dataset_id,
        "dataset_version": metadata.dataset_version,
        "first_timestamp": pd.Timestamp(metadata.first_timestamp).isoformat(),
        "last_timestamp": pd.Timestamp(metadata.last_timestamp).isoformat(),
        "lineage_count": len(metadata.lineage),
        "lineage_digest": canonical_sha256(list(metadata.lineage)),
        "manifest_checksum": metadata.manifest_checksum,
        "negative_checks": negative_checks,
        "partition_count": metadata.partition_count,
        "restart_at_row": restart_at_row,
        "restart_open_count": source.source_open_count,
        "row_count": metadata.row_count,
        "schema_fingerprint": metadata.schema_fingerprint,
        "source_uri": metadata.source_uri,
        "symbol": metadata.symbol,
        "timeframe": metadata.timeframe,
    }
    return frame, metadata_report, final_position


def _validate_canonical_summary(summary: dict[str, object], output_dir: Path) -> None:
    if summary.get("candidate_id") != CANDIDATE_ID:
        _fail("canonical candidate identity mismatch")
    if summary.get("composition_hash") != COMPOSITION_HASH:
        _fail("canonical composition hash mismatch")
    if summary.get("semantic_digest") != EXPECTED_SEMANTIC_DIGEST:
        _fail("canonical semantic digest drift")
    if summary.get("metrics") != EXPECTED_METRICS:
        _fail("canonical metrics drift")
    ledgers = summary.get("ledgers")
    if not isinstance(ledgers, dict):
        _fail("canonical ledger summary is malformed")
    combined = ledgers.get("combined")
    components = ledgers.get("components")
    if not isinstance(combined, dict) or not isinstance(components, dict):
        _fail("canonical ledger inventory is malformed")
    if combined.get("trade_count") != 438:
        _fail("canonical combined trade count drift")
    for module_id, expected_count in EXPECTED_COMPONENT_TRADES.items():
        component = components.get(module_id)
        if not isinstance(component, dict) or component.get("trade_count") != expected_count:
            _fail(f"canonical component trade count drift: {module_id}")
    for name, expected_hash in EXPECTED_LEDGER_HASHES.items():
        path = output_dir / name
        if sha256_file(path) != expected_hash:
            _fail(f"canonical ledger hash drift: {name}")


def run(
    *,
    storage_root: Path,
    dataset_id: str,
    dataset_version: int,
    official_input: Path,
    provenance_path: Path,
    output_dir: Path,
    generated_from_commit: str,
    batch_size: int,
    restart_at_row: int,
) -> dict[str, object]:
    provenance = _load_json_object(provenance_path)
    parquet_sha = provenance.get("parquet_sha256")
    if not isinstance(parquet_sha, str) or len(parquet_sha) != 64:
        _fail("official dataset provenance lacks a valid Parquet SHA-256")
    official_frame = load_5m(official_input, expected_sha256=parquet_sha)
    if len(official_frame) != EXPECTED_ROWS:
        _fail("official dataset row count mismatch")
    if pd.Timestamp(official_frame["open_time"].iloc[0]) != EXPECTED_FIRST:
        _fail("official dataset first timestamp mismatch")
    if pd.Timestamp(official_frame["open_time"].iloc[-1]) != EXPECTED_LAST:
        _fail("official dataset last timestamp mismatch")

    registered_frame, registered, final_position = _read_registered_with_restart(
        storage_root=storage_root,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        official_frame=official_frame,
        batch_size=batch_size,
        restart_at_row=restart_at_row,
    )
    pd.testing.assert_frame_equal(
        registered_frame,
        official_frame,
        check_dtype=True,
        check_exact=True,
        check_like=False,
    )

    signals = build_signals(registered_frame)
    ledger, counts = run_portfolio(registered_frame, signals)
    summary = summarize(
        ledger,
        signal_counts=counts,
        output_dir=output_dir,
        input_path=official_input,
        row_count=len(registered_frame),
        generated_from_commit=generated_from_commit,
        dataset_provenance=provenance,
    )
    _validate_canonical_summary(summary, output_dir)

    report: dict[str, object] = {
        "canonical_result": {
            "candidate_id": CANDIDATE_ID,
            "composition_hash": COMPOSITION_HASH,
            "ledger_hashes": EXPECTED_LEDGER_HASHES,
            "metrics": EXPECTED_METRICS,
            "semantic_digest": EXPECTED_SEMANTIC_DIGEST,
            "semantic_digest_contract": "canonical-strategy-v2-result-v1",
        },
        "generated_from_commit": generated_from_commit,
        "official_dataset": {
            "content_sha256": provenance.get("content_sha256"),
            "first_timestamp": EXPECTED_FIRST.isoformat(),
            "last_timestamp": EXPECTED_LAST.isoformat(),
            "parquet_sha256": parquet_sha,
            "row_count": EXPECTED_ROWS,
            "source": provenance.get("source"),
            "symbol": "BTCUSDT",
            "timeframe": "5m",
        },
        "parity": {
            "canonical_ledgers_match_accepted_baseline": True,
            "canonical_metrics_match_accepted_baseline": True,
            "canonical_semantic_digest_match": True,
            "frame_exact": True,
            "registered_dataset_relation": "PROVEN_BY_EXACT_FRAME_AND_RESULT_PARITY",
            "restart_exact": True,
        },
        "registered_dataset": registered,
        "safety": {
            "live_trading": False,
            "local_storage_path_published": False,
            "private_credentials_used": False,
            "production_dataset_mutated": False,
            "raw_market_data_published": False,
            "research_paper_only": True,
        },
        "schema_version": 1,
        "status": "REGISTERED_DATASET_CANONICAL_VARIANT2_PARITY_PASS",
        "terminal_position": {
            "last_timestamp": pd.Timestamp(final_position.last_timestamp).isoformat(),
            "partition_index": final_position.partition_index,
            "row_offset": final_position.row_offset,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "registered-parity.json"
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--storage-root", type=Path, required=True)
    parser.add_argument("--dataset-id", default=EXPECTED_DATASET_ID)
    parser.add_argument("--dataset-version", type=int, default=EXPECTED_DATASET_VERSION)
    parser.add_argument("--official-input", type=Path, required=True)
    parser.add_argument("--dataset-provenance", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--generated-from-commit", required=True)
    parser.add_argument("--batch-size", type=int, default=8_192)
    parser.add_argument("--restart-at-row", type=int, default=300_000)
    arguments = parser.parse_args()
    report = run(
        storage_root=arguments.storage_root.expanduser().resolve(strict=True),
        dataset_id=arguments.dataset_id,
        dataset_version=arguments.dataset_version,
        official_input=arguments.official_input.resolve(strict=True),
        provenance_path=arguments.dataset_provenance.resolve(strict=True),
        output_dir=arguments.output.resolve(),
        generated_from_commit=arguments.generated_from_commit,
        batch_size=arguments.batch_size,
        restart_at_row=arguments.restart_at_row,
    )
    print(json.dumps(report, sort_keys=True, allow_nan=False))


if __name__ == "__main__":  # pragma: no cover
    main()
