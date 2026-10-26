"""Download and attest a fixed BTCUSDT USD-M Futures kline dataset."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BASE_URL = "https://fapi.binance.com"
ENDPOINT = "/fapi/v1/klines"
SYMBOL = "BTCUSDT"
INTERVAL = "5m"
INTERVAL_MS = 300_000
LIMIT = 1_000
START_MS = 1_568_048_100_000  # 2019-09-08T17:55:00Z
EXPECTED_START = "2019-09-08T17:55:00+00:00"
EXPECTED_END = "2026-07-19T06:55:00+00:00"
EXPECTED_ROWS = 721_597
DRIVE_RAW_SHA256 = "2b8a20ef09e1c292abbbee4a6963a1cedb7904cfc4340c9de72d0d2cd0b87ed1"
_TRANSIENT_HTTP_CODES = frozenset({408, 429, 500, 502, 503, 504})
_FETCH_RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0, 8.0)


@dataclass(frozen=True, slots=True)
class Kline:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time_ms: int
    quote_volume: float
    trades: int
    taker_buy_base: float
    taker_buy_quote: float


def _epoch_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return int(parsed.astimezone(UTC).timestamp() * 1_000)


def discover_drive_parquet(home: Path | None = None) -> Path:
    root = (home or Path.home()).resolve()
    patterns = (
        "Library/CloudStorage/GoogleDrive-*/My Drive/binance_usdtm_futures_history/"
        "BTCUSDT/BTCUSDT_5m.parquet",
        "Library/CloudStorage/GoogleDrive-*/Мой диск/binance_usdtm_futures_history/"
        "BTCUSDT/BTCUSDT_5m.parquet",
        "Google Drive/My Drive/binance_usdtm_futures_history/BTCUSDT/BTCUSDT_5m.parquet",
        "Google Диск/Мой диск/binance_usdtm_futures_history/BTCUSDT/BTCUSDT_5m.parquet",
    )
    candidates: set[Path] = set()
    for pattern in patterns:
        for candidate in root.glob(pattern):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            candidates.add(candidate.resolve(strict=True))
    if len(candidates) != 1:
        raise RuntimeError(f"expected exactly one BTCUSDT 5m corpus file, found {len(candidates)}")
    return next(iter(candidates))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_kline(raw: object) -> Kline:
    if not isinstance(raw, list) or len(raw) < 11:
        raise ValueError("kline row must contain at least eleven fields")
    row = Kline(
        open_time_ms=int(raw[0]),
        open=float(raw[1]),
        high=float(raw[2]),
        low=float(raw[3]),
        close=float(raw[4]),
        volume=float(raw[5]),
        close_time_ms=int(raw[6]),
        quote_volume=float(raw[7]),
        trades=int(raw[8]),
        taker_buy_base=float(raw[9]),
        taker_buy_quote=float(raw[10]),
    )
    numeric = (
        row.open,
        row.high,
        row.low,
        row.close,
        row.volume,
        row.quote_volume,
        row.taker_buy_base,
        row.taker_buy_quote,
    )
    if not all(value == value and abs(value) != float("inf") for value in numeric):
        raise ValueError("kline contains non-finite numeric values")
    if (
        min(row.open, row.high, row.low, row.close) <= 0
        or min(
            row.volume,
            row.quote_volume,
            row.taker_buy_base,
            row.taker_buy_quote,
            row.trades,
        )
        < 0
    ):
        raise ValueError("kline contains invalid negative or zero values")
    if row.high < max(row.open, row.low, row.close) or row.low > min(row.open, row.high, row.close):
        raise ValueError("kline OHLC invariant failed")
    if row.close_time_ms != row.open_time_ms + INTERVAL_MS - 1:
        raise ValueError("kline close time mismatch")
    if row.taker_buy_base > row.volume or row.taker_buy_quote > row.quote_volume:
        raise ValueError("kline taker-buy volume exceeds total volume")
    return row


def request_url(start_ms: int, end_ms: int) -> str:
    query = urllib.parse.urlencode(
        {
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": LIMIT,
        }
    )
    return f"{BASE_URL}{ENDPOINT}?{query}"


def _default_fetch(url: str) -> object:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "module-strategy-canonical-v2-dataset/1.0",
        },
    )
    attempts = len(_FETCH_RETRY_DELAYS_SECONDS) + 1
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                if response.status != 200:
                    raise RuntimeError(f"Binance endpoint returned HTTP {response.status}")
                return json.load(response)
        except urllib.error.HTTPError as exc:
            body = exc.read(500).decode("utf-8", errors="replace")
            if exc.code not in _TRANSIENT_HTTP_CODES or attempt == attempts - 1:
                raise RuntimeError(f"Binance endpoint HTTP {exc.code}: {body}") from exc
        except (
            urllib.error.URLError,
            http.client.IncompleteRead,
            http.client.RemoteDisconnected,
            ConnectionError,
            TimeoutError,
        ) as exc:
            if attempt == attempts - 1:
                raise RuntimeError(
                    f"Binance endpoint unavailable after {attempts} attempts: {type(exc).__name__}"
                ) from exc
        time.sleep(_FETCH_RETRY_DELAYS_SECONDS[attempt])
    raise AssertionError("unreachable Binance fetch retry state")


def download_klines(
    *,
    start_ms: int,
    end_ms: int,
    fetch: Callable[[str], object] = _default_fetch,
    throttle_seconds: float = 0.27,
) -> tuple[list[Kline], int]:
    if start_ms > end_ms or start_ms % INTERVAL_MS or end_ms % INTERVAL_MS:
        raise ValueError("download boundaries must be ordered 5m open timestamps")
    rows: list[Kline] = []
    cursor = start_ms
    requests = 0
    while cursor <= end_ms:
        payload = fetch(request_url(cursor, end_ms))
        requests += 1
        if not isinstance(payload, list):
            raise RuntimeError("Binance endpoint returned a non-array payload")
        if not payload:
            raise RuntimeError(f"Binance endpoint ended before requested boundary at {cursor}")
        batch = [parse_kline(raw) for raw in payload]
        if batch[0].open_time_ms != cursor:
            raise RuntimeError("Binance endpoint pagination start mismatch")
        for row in batch:
            if row.open_time_ms > end_ms:
                raise RuntimeError("Binance endpoint returned a row after requested end")
            expected = start_ms + len(rows) * INTERVAL_MS
            if row.open_time_ms != expected:
                raise RuntimeError("Binance endpoint data contains a gap or duplicate")
            rows.append(row)
        next_cursor = batch[-1].open_time_ms + INTERVAL_MS
        if next_cursor <= cursor:
            raise RuntimeError("Binance endpoint pagination did not advance")
        cursor = next_cursor
        if cursor <= end_ms and throttle_seconds:
            time.sleep(throttle_seconds)
    return rows, requests


def content_sha256(rows: list[Kline]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            struct.pack(
                ">q5dqdq2d",
                row.open_time_ms,
                row.open,
                row.high,
                row.low,
                row.close,
                row.volume,
                row.close_time_ms,
                row.quote_volume,
                row.trades,
                row.taker_buy_base,
                row.taker_buy_quote,
            )
        )
    return digest.hexdigest()


def write_parquet(rows: list[Kline], path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "open_time": pa.array(
                [row.open_time_ms for row in rows], type=pa.timestamp("ms", tz="UTC")
            ),
            "open": pa.array([row.open for row in rows], type=pa.float64()),
            "high": pa.array([row.high for row in rows], type=pa.float64()),
            "low": pa.array([row.low for row in rows], type=pa.float64()),
            "close": pa.array([row.close for row in rows], type=pa.float64()),
            "volume": pa.array([row.volume for row in rows], type=pa.float64()),
            "close_time": pa.array(
                [row.close_time_ms for row in rows], type=pa.timestamp("ms", tz="UTC")
            ),
            "quote_volume": pa.array([row.quote_volume for row in rows], type=pa.float64()),
            "trades": pa.array([row.trades for row in rows], type=pa.int64()),
            "taker_buy_base": pa.array([row.taker_buy_base for row in rows], type=pa.float64()),
            "taker_buy_quote": pa.array([row.taker_buy_quote for row in rows], type=pa.float64()),
        }
    )
    pq.write_table(
        table,
        path,
        compression="zstd",
        data_page_version="2.0",
        row_group_size=100_000,
        use_dictionary=False,
        version="2.6",
        write_statistics=True,
    )


def compare_drive_corpus(official_path: Path, drive_path: Path) -> dict[str, int]:
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    if sha256_file(drive_path) != DRIVE_RAW_SHA256:
        raise RuntimeError("attested Drive corpus SHA-256 mismatch")
    columns = [
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
    ]
    official = pq.read_table(official_path, columns=columns)
    drive = pq.read_table(drive_path, columns=columns)
    if official.num_rows != drive.num_rows or official.num_columns != drive.num_columns:
        raise RuntimeError("official and Drive corpus dimensions differ")
    mismatch_counts: dict[str, int] = {}
    for column in columns:
        mismatches = pc.sum(pc.not_equal(official.column(column), drive.column(column))).as_py()
        mismatch_counts[column] = int(mismatches or 0)
    if any(mismatch_counts.values()):
        raise RuntimeError(f"official and Drive corpus content differs: {mismatch_counts}")
    return mismatch_counts


def build_provenance(
    *,
    rows: list[Kline],
    request_count: int,
    parquet_path: Path,
    drive_path: Path | None,
    generated_from_commit: str,
    drive_mismatch_counts: dict[str, int] | None,
) -> dict[str, Any]:
    drive_compared = drive_path is not None
    return {
        "content_sha256": content_sha256(rows),
        "drive_comparison_status": "MATCH" if drive_compared else "NOT_REQUESTED",
        "drive_content_parity": True if drive_compared else None,
        "drive_mismatch_counts": drive_mismatch_counts,
        "drive_observed_raw_sha256": sha256_file(drive_path) if drive_path else None,
        "retained_drive_raw_sha256": DRIVE_RAW_SHA256,
        "endpoint": {
            "base_url": BASE_URL,
            "method": "GET",
            "path": ENDPOINT,
            "security": "NONE",
        },
        "generated_from_commit": generated_from_commit,
        "interval": INTERVAL,
        "last_open_time": datetime.fromtimestamp(rows[-1].open_time_ms / 1_000, UTC).isoformat(),
        "parquet_sha256": sha256_file(parquet_path),
        "request_count": request_count,
        "row_count": len(rows),
        "schema_version": 2,
        "source": "OFFICIAL_BINANCE_USDTM_REST_API",
        "symbol": SYMBOL,
        "first_open_time": datetime.fromtimestamp(rows[0].open_time_ms / 1_000, UTC).isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--drive-input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--generated-from-commit", required=True)
    arguments = parser.parse_args()

    start_ms = _epoch_ms(EXPECTED_START)
    end_ms = _epoch_ms(EXPECTED_END)
    rows, request_count = download_klines(start_ms=start_ms, end_ms=end_ms)
    if len(rows) != EXPECTED_ROWS:
        raise RuntimeError(f"official dataset row count mismatch: {len(rows)}")
    output = arguments.output.resolve()
    drive = arguments.drive_input.resolve(strict=True) if arguments.drive_input else None
    write_parquet(rows, output)
    drive_mismatch_counts = compare_drive_corpus(output, drive) if drive else None
    provenance = build_provenance(
        rows=rows,
        request_count=request_count,
        parquet_path=output,
        drive_path=drive,
        generated_from_commit=arguments.generated_from_commit,
        drive_mismatch_counts=drive_mismatch_counts,
    )
    provenance_path = arguments.provenance.resolve()
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    provenance_path.write_text(
        json.dumps(provenance, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(provenance, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
