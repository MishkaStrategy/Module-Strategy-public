#!/usr/bin/env python3
"""Compare module-derived timeframes with direct multi-year Parquet files."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import resource
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from market_data_ingestion.aggregation import aggregate_klines_stream
from market_data_ingestion.records import KlineRecord
from market_data_ingestion.version import PACKAGE_VERSION

COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_buy_base",
    "taker_buy_quote",
)
PRICE_FIELDS = ("open", "high", "low", "close")
SUM_FIELDS = ("volume", "quote_volume", "taker_buy_base", "taker_buy_quote")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if platform.system() == "Darwin" else value * 1024)


def base_records(
    path: Path,
    *,
    batch_size: int,
    symbol: str,
    exchange_id: str,
    market_type: str,
) -> Iterator[KlineRecord]:
    parquet = pq.ParquetFile(path)
    if tuple(parquet.schema_arrow.names) != COLUMNS:
        raise RuntimeError("unexpected base schema")
    for batch in parquet.iter_batches(batch_size=batch_size, columns=list(COLUMNS)):
        data = batch.to_pydict()
        for index in range(batch.num_rows):
            yield KlineRecord(
                open_time=data["open_time"][index],
                close_time=data["close_time"][index],
                open=str(data["open"][index]),
                high=str(data["high"][index]),
                low=str(data["low"][index]),
                close=str(data["close"][index]),
                volume=str(data["volume"][index]),
                quote_volume=str(data["quote_volume"][index]),
                trade_count=int(data["trades"][index]),
                taker_buy_base_volume=str(data["taker_buy_base"][index]),
                taker_buy_quote_volume=str(data["taker_buy_quote"][index]),
                symbol=symbol,
                timeframe="5m",
                exchange_id=exchange_id,
                market_type=market_type,
                source_kind="verified_external_parquet",
                is_closed=True,
            )


def direct_rows(path: Path, batch_size: int) -> Iterator[dict[str, object]]:
    parquet = pq.ParquetFile(path)
    if tuple(parquet.schema_arrow.names) != COLUMNS:
        raise RuntimeError("unexpected direct schema")
    for batch in parquet.iter_batches(batch_size=batch_size, columns=list(COLUMNS)):
        data = batch.to_pydict()
        for index in range(batch.num_rows):
            yield {name: data[name][index] for name in COLUMNS}


def compare(
    base: Path,
    direct: Path,
    *,
    timeframe: str,
    batch_size: int,
    symbol: str,
    exchange_id: str,
    market_type: str,
    price_tolerance: Decimal,
    sum_absolute_tolerance: Decimal,
    sum_relative_tolerance: Decimal,
) -> dict[str, Any]:
    started = perf_counter()
    derived = aggregate_klines_stream(
        base_records(
            base,
            batch_size=batch_size,
            symbol=symbol,
            exchange_id=exchange_id,
            market_type=market_type,
        ),
        target_timeframe=timeframe,
    )
    direct_iter = direct_rows(direct, batch_size)
    count = 0
    timestamp_mismatches = 0
    close_time_mismatches = 0
    trade_mismatches = 0
    exact_price_mismatches = {field: 0 for field in PRICE_FIELDS}
    exact_sum_mismatches = {field: 0 for field in SUM_FIELDS}
    max_abs = {field: Decimal(0) for field in (*PRICE_FIELDS, *SUM_FIELDS)}
    max_rel = {field: Decimal(0) for field in (*PRICE_FIELDS, *SUM_FIELDS)}
    first_open: datetime | None = None
    last_open: datetime | None = None
    digest = hashlib.sha256()

    for record in derived:
        try:
            direct_row = next(direct_iter)
        except StopIteration as exc:
            raise RuntimeError("direct timeframe ended before derived stream") from exc
        count += 1
        if record.open_time != direct_row["open_time"]:
            timestamp_mismatches += 1
        if record.close_time != direct_row["close_time"]:
            close_time_mismatches += 1
        if record.trade_count != int(direct_row["trades"]):
            trade_mismatches += 1
        first_open = first_open or record.open_time
        last_open = record.open_time

        values = {
            "open": record.open,
            "high": record.high,
            "low": record.low,
            "close": record.close,
            "volume": record.volume,
            "quote_volume": record.quote_volume,
            "taker_buy_base": record.taker_buy_base_volume,
            "taker_buy_quote": record.taker_buy_quote_volume,
        }
        for field, derived_text in values.items():
            direct_value = Decimal(str(direct_row[field]))
            derived_value = Decimal(derived_text)
            difference = abs(derived_value - direct_value)
            denominator = max(abs(direct_value), Decimal("1e-30"))
            relative = difference / denominator
            max_abs[field] = max(max_abs[field], difference)
            max_rel[field] = max(max_rel[field], relative)
            if difference:
                mismatch_target = (
                    exact_price_mismatches if field in PRICE_FIELDS else exact_sum_mismatches
                )
                mismatch_target[field] += 1

        digest.update(
            (
                f"{record.open_time.isoformat()}|{record.open}|{record.high}|"
                f"{record.low}|{record.close}|{record.volume}\n"
            ).encode()
        )

    try:
        extra = next(direct_iter)
    except StopIteration:
        extra = None
    if extra is not None:
        raise RuntimeError("direct timeframe contains records after derived stream")

    direct_count = pq.ParquetFile(direct).metadata.num_rows
    criteria = {
        "timestamps_exact": timestamp_mismatches == 0,
        "close_times_exact": close_time_mismatches == 0,
        "trades_exact": trade_mismatches == 0,
        "price_within_tolerance": all(max_abs[field] <= price_tolerance for field in PRICE_FIELDS),
        "sums_within_float_tolerance": all(
            max_abs[field] <= sum_absolute_tolerance or max_rel[field] <= sum_relative_tolerance
            for field in SUM_FIELDS
        ),
        "row_count_matches_metadata": count == direct_count,
    }
    return {
        "timeframe": timeframe,
        "direct_rows": direct_count,
        "compared_rows": count,
        "range": {
            "start": first_open.isoformat() if first_open else None,
            "last_open": last_open.isoformat() if last_open else None,
        },
        "mismatches": {
            "timestamps": timestamp_mismatches,
            "close_times": close_time_mismatches,
            "trades": trade_mismatches,
            "exact_price": exact_price_mismatches,
            "exact_sums": exact_sum_mismatches,
        },
        "max_absolute_error": {key: format(value, "f") for key, value in max_abs.items()},
        "max_relative_error": {key: format(value, "e") for key, value in max_rel.items()},
        "derived_digest_sha256": digest.hexdigest(),
        "elapsed_seconds": round(perf_counter() - started, 6),
        "criteria": criteria,
        "passed": all(criteria.values()),
    }


def parse_target(value: str) -> tuple[str, Path]:
    timeframe, separator, raw_path = value.partition("=")
    if not separator or not timeframe or not raw_path:
        raise argparse.ArgumentTypeError("target must use TIMEFRAME=PATH")
    return timeframe, Path(raw_path).expanduser().resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=Path)
    parser.add_argument(
        "--target",
        action="append",
        type=parse_target,
        required=True,
        help="Direct comparison file as TIMEFRAME=PATH; repeat for each target.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=8_192)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--exchange-id", default="binance")
    parser.add_argument("--market-type", default="usdt_m_perpetual")
    parser.add_argument("--price-absolute-tolerance", default="1e-12")
    parser.add_argument("--sum-absolute-tolerance", default="1e-8")
    parser.add_argument("--sum-relative-tolerance", default="1e-12")
    parser.add_argument("--expected-base-sha256")
    args = parser.parse_args()

    base = args.base.expanduser().resolve()
    if args.expected_base_sha256 is not None:
        observed = sha256_file(base)
        if observed != args.expected_base_sha256.lower():
            raise SystemExit(
                "base SHA-256 mismatch: "
                f"expected {args.expected_base_sha256.lower()}, observed {observed}"
            )

    targets = dict(args.target)
    if len(targets) != len(args.target):
        raise SystemExit("target timeframes must be unique")
    started = perf_counter()
    results = [
        compare(
            base,
            target,
            timeframe=timeframe,
            batch_size=args.batch_size,
            symbol=args.symbol,
            exchange_id=args.exchange_id,
            market_type=args.market_type,
            price_tolerance=Decimal(args.price_absolute_tolerance),
            sum_absolute_tolerance=Decimal(args.sum_absolute_tolerance),
            sum_relative_tolerance=Decimal(args.sum_relative_tolerance),
        )
        for timeframe, target in targets.items()
    ]
    payload = {
        "benchmark_kind": "real_direct_vs_derived_timeframe_crosscheck",
        "benchmark_version": 1,
        "package_version": PACKAGE_VERSION,
        "python": platform.python_version(),
        "pyarrow": pa.__version__,
        "platform": platform.platform(),
        "inputs": {
            "5m": {
                "sha256": sha256_file(base),
                "rows": pq.ParquetFile(base).metadata.num_rows,
            },
            **{
                timeframe: {
                    "sha256": sha256_file(path),
                    "rows": pq.ParquetFile(path).metadata.num_rows,
                }
                for timeframe, path in targets.items()
            },
        },
        "tolerances": {
            "price_absolute": args.price_absolute_tolerance,
            "sum_absolute": args.sum_absolute_tolerance,
            "sum_relative": args.sum_relative_tolerance,
        },
        "results": results,
        "peak_rss_bytes": peak_rss_bytes(),
        "total_seconds": round(perf_counter() - started, 6),
        "passed": all(result["passed"] for result in results),
        "completed_at": datetime.now(UTC).isoformat(),
    }
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
