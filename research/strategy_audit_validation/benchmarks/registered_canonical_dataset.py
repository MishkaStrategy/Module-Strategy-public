"""Materialize the fixed canonical BTCUSDT corpus through the registered Market Data owner."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._common import REGISTERED_DATASET_HASH
from .official_usdtm_dataset import (
    DRIVE_RAW_SHA256,
    ENDPOINT,
    EXPECTED_END,
    EXPECTED_ROWS,
    EXPECTED_START,
    INTERVAL,
    SYMBOL,
    Kline,
    content_sha256,
    parse_kline,
    sha256_file,
    write_parquet,
)

REGISTERED_SOURCE = "REGISTERED_CANONICAL_BINANCE_USDTM_CORPUS"
REGISTERED_DATASET_ID = "binance_usdt-m-perpetual_btcusdt_5m"
REGISTERED_DATASET_VERSION = 1
REGISTERED_SOURCE_URI = f"dataset://{REGISTERED_DATASET_ID}/v{REGISTERED_DATASET_VERSION}"
EXPECTED_PARTITIONS = 83
OFFICIAL_REST_CONTENT_SHA256 = "7caf6cedc99e77991a1b618ee77c3b31f5433342ba88122fa6a1614708fed130"
OFFICIAL_REST_PARQUET_SHA256 = "f9f8953b4a08a33a699b9ffe53fbf0a26fb32b49318f13ecee72c6d1f4319b36"
_CONSUMER = "modular_research_runner"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _epoch_ms(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeError("registered canonical timestamp must be timezone-aware")
    normalized = value.astimezone(UTC)
    if normalized.microsecond % 1_000:
        raise RuntimeError("registered canonical timestamp must be millisecond-aligned")
    delta = normalized - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000 + delta.microseconds // 1_000


def _record_to_kline(record: Any) -> Kline:
    if record.symbol != SYMBOL or record.timeframe != INTERVAL:
        raise RuntimeError("registered canonical row identity mismatch")
    if not record.is_closed:
        raise RuntimeError("registered canonical row must be closed")
    return parse_kline(
        [
            _epoch_ms(record.open_time),
            record.open,
            record.high,
            record.low,
            record.close,
            record.volume,
            _epoch_ms(record.close_time),
            record.quote_volume,
            record.trade_count,
            record.taker_buy_base_volume,
            record.taker_buy_quote_volume,
        ]
    )


def _default_reader_functions() -> tuple[Callable[..., Any], Callable[..., Iterable[Any]]]:
    from market_data_ingestion.dataset_reader import (
        iter_registered_klines,
        resolve_registered_kline_dataset,
    )

    return resolve_registered_kline_dataset, iter_registered_klines


def load_registered_rows(
    storage_root: Path,
    *,
    resolve_dataset: Callable[..., Any] | None = None,
    iter_klines: Callable[..., Iterable[Any]] | None = None,
) -> tuple[list[Kline], dict[str, object]]:
    if resolve_dataset is None or iter_klines is None:
        default_resolve, default_iter = _default_reader_functions()
        resolve_dataset = resolve_dataset or default_resolve
        iter_klines = iter_klines or default_iter

    selection = resolve_dataset(
        storage_root,
        REGISTERED_DATASET_ID,
        REGISTERED_DATASET_VERSION,
        timeframe=INTERVAL,
        consumer=_CONSUMER,
        deep=True,
    )
    descriptor = selection.descriptor
    if descriptor.dataset_id != REGISTERED_DATASET_ID:
        raise RuntimeError("registered canonical dataset id mismatch")
    if descriptor.version != REGISTERED_DATASET_VERSION:
        raise RuntimeError("registered canonical dataset version mismatch")
    if descriptor.dataset_hash != REGISTERED_DATASET_HASH:
        raise RuntimeError("registered canonical dataset hash mismatch")
    if selection.manifest.instrument.symbol != SYMBOL or selection.timeframe != INTERVAL:
        raise RuntimeError("registered canonical selection identity mismatch")
    if len(selection.partitions) != EXPECTED_PARTITIONS:
        raise RuntimeError(
            "registered canonical partition count mismatch: "
            f"expected {EXPECTED_PARTITIONS}, got {len(selection.partitions)}"
        )
    if selection.row_count != EXPECTED_ROWS:
        raise RuntimeError(
            "registered canonical row count mismatch: "
            f"expected {EXPECTED_ROWS}, got {selection.row_count}"
        )

    rows = [_record_to_kline(record) for record in iter_klines(selection)]
    if len(rows) != EXPECTED_ROWS:
        raise RuntimeError(
            "registered canonical emitted row count mismatch: "
            f"expected {EXPECTED_ROWS}, got {len(rows)}"
        )
    expected_first = datetime.fromisoformat(EXPECTED_START)
    expected_last = datetime.fromisoformat(EXPECTED_END)
    if rows[0].open_time_ms != _epoch_ms(expected_first):
        raise RuntimeError("registered canonical first timestamp mismatch")
    if rows[-1].open_time_ms != _epoch_ms(expected_last):
        raise RuntimeError("registered canonical last timestamp mismatch")
    observed_content = content_sha256(rows)
    if observed_content != OFFICIAL_REST_CONTENT_SHA256:
        raise RuntimeError(
            "registered canonical content differs from accepted official REST content: "
            f"{observed_content}"
        )
    metadata = {
        "consumer": _CONSUMER,
        "dataset_hash": descriptor.dataset_hash,
        "dataset_id": descriptor.dataset_id,
        "dataset_version": descriptor.version,
        "partition_count": len(selection.partitions),
        "row_count": selection.row_count,
        "source_uri": REGISTERED_SOURCE_URI,
        "validation_status": descriptor.validation_status.value,
    }
    return rows, metadata


def build_registered_provenance(
    *,
    rows: list[Kline],
    metadata: dict[str, object],
    parquet_path: Path,
    generated_from_commit: str,
) -> dict[str, Any]:
    observed_content = content_sha256(rows)
    if observed_content != OFFICIAL_REST_CONTENT_SHA256:
        raise RuntimeError("registered canonical provenance content mismatch")
    parquet_sha256 = sha256_file(parquet_path)
    if parquet_sha256 != OFFICIAL_REST_PARQUET_SHA256:
        raise RuntimeError(
            "registered canonical Parquet differs from accepted official REST artifact: "
            f"{parquet_sha256}"
        )
    return {
        "api_attempt_status": "HTTP_451_RESTRICTED_LOCATION",
        "content_sha256": observed_content,
        "drive_comparison_status": "NOT_REQUESTED",
        "drive_content_parity": None,
        "drive_mismatch_counts": None,
        "drive_observed_raw_sha256": None,
        "endpoint": {
            "base_url": "https://fapi.binance.com",
            "method": "GET",
            "path": ENDPOINT,
            "security": "NONE",
        },
        "first_open_time": EXPECTED_START,
        "generated_from_commit": generated_from_commit,
        "interval": INTERVAL,
        "last_open_time": EXPECTED_END,
        "official_content_parity": True,
        "official_rest_content_sha256": OFFICIAL_REST_CONTENT_SHA256,
        "parquet_sha256": parquet_sha256,
        "registered_dataset": metadata,
        "retained_drive_raw_sha256": DRIVE_RAW_SHA256,
        "row_count": len(rows),
        "schema_version": 4,
        "source": REGISTERED_SOURCE,
        "symbol": SYMBOL,
    }


def run(
    *,
    storage_root: Path,
    output: Path,
    provenance_path: Path,
    generated_from_commit: str,
    resolve_dataset: Callable[..., Any] | None = None,
    iter_klines: Callable[..., Iterable[Any]] | None = None,
) -> dict[str, Any]:
    rows, metadata = load_registered_rows(
        storage_root.resolve(strict=True),
        resolve_dataset=resolve_dataset,
        iter_klines=iter_klines,
    )
    write_parquet(rows, output)
    provenance = build_registered_provenance(
        rows=rows,
        metadata=metadata,
        parquet_path=output,
        generated_from_commit=generated_from_commit,
    )
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    provenance_path.write_text(
        json.dumps(provenance, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return provenance


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--storage-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--generated-from-commit", required=True)
    arguments = parser.parse_args()
    provenance = run(
        storage_root=arguments.storage_root,
        output=arguments.output.resolve(),
        provenance_path=arguments.provenance.resolve(),
        generated_from_commit=arguments.generated_from_commit,
    )
    print(json.dumps(provenance, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
