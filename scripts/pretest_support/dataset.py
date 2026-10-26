from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from .models import DataFacts, PretestError, Reporter, STEP, iso, number, sha256, timestamp


def read_summary(
    reporter: Reporter, dataset: Path, symbol: str
) -> tuple[dict[str, str], dict[str, object]]:
    direct = dataset / "download_summary.csv"
    matches = [direct] if direct.is_file() else sorted(dataset.rglob("download_summary.csv"))
    if len(matches) != 1:
        raise PretestError(f"expected one download_summary.csv, found {len(matches)}")
    with matches[0].open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or ())
        required = {
            "symbol",
            "timeframe",
            "status",
            "rows",
            "first_open_time",
            "last_open_time",
            "gap_events",
            "estimated_missing_5m_rows",
        }
        if missing := sorted(required - fields):
            raise PretestError(f"summary missing columns: {missing}")
        rows = list(reader)
    if bad := [row for row in rows if row["status"] != "ok"]:
        raise PretestError(f"summary contains {len(bad)} non-ok rows")
    base = [row for row in rows if row["symbol"] == symbol and row["timeframe"] == "5m"]
    if len(base) != 1:
        raise PretestError(f"expected one {symbol} 5m summary row")
    row = base[0]
    if row["gap_events"] not in {"0", "0.0"}:
        raise PretestError(f"summary reports gaps: {row['gap_events']}")
    if row["estimated_missing_5m_rows"] not in {"0", "0.0"}:
        raise PretestError("summary reports missing 5m rows")
    timeframes = {item["timeframe"] for item in rows if item["symbol"] == symbol}
    if missing := sorted({"5m", "15m", "1h", "4h"} - timeframes):
        raise PretestError(f"summary missing timeframes: {missing}")
    if "1d" not in timeframes:
        reporter.warn("1d", "absent; generated configs request only 15m, 1h, and 4h")
    reporter.passed("summary", f"{len(rows)} ok rows and zero reported 5m gaps")
    return row, {
        "path": str(matches[0].resolve()),
        "rows": len(rows),
        "timeframes": sorted(timeframes),
        "has_sha256": "sha256" in fields or "file_sha256" in fields,
    }


def locate_data_file(dataset: Path, symbol: str, explicit: Path | None) -> Path:
    if explicit is not None:
        path = explicit.expanduser().resolve()
        if not path.is_file():
            raise PretestError(f"data file not found: {path}")
        return path
    ranked: list[tuple[int, Path]] = []
    for path in dataset.rglob("*"):
        if not path.is_file() or path.name == "download_summary.csv":
            continue
        if path.suffix.lower() not in {".csv", ".parquet"}:
            continue
        relative = str(path.relative_to(dataset)).upper()
        normalized = "".join(char for char in relative if char.isalnum())
        if symbol.upper() not in normalized or "5M" not in normalized:
            continue
        score = int(path.suffix.lower() == ".parquet")
        score += 5 * int(path.parent.name.upper() == symbol.upper())
        score += 10 * int(normalized.startswith(symbol.upper() + "5M"))
        ranked.append((score, path.resolve()))
    if not ranked:
        raise PretestError(f"cannot find {symbol} 5m CSV/Parquet; use --data-file")
    ranked.sort(key=lambda item: (-item[0], str(item[1])))
    top = [path for score, path in ranked if score == ranked[0][0]]
    if len(top) != 1:
        raise PretestError("multiple matching files; use --data-file")
    return top[0]


def column_mapping(names: Sequence[str]) -> tuple[str, dict[str, str]]:
    available = {name.strip().lower(): name for name in names}

    def pick(label: str, choices: Sequence[str]) -> str:
        for choice in choices:
            if choice in available:
                return available[choice]
        raise PretestError(f"missing {label} column")

    return pick("timestamp", ("open_time", "timestamp", "time", "open_timestamp", "datetime")), {
        "open": pick("open", ("open",)),
        "high": pick("high", ("high",)),
        "low": pick("low", ("low",)),
        "close": pick("close", ("close",)),
        "volume": pick("volume", ("volume", "base_volume")),
    }


def validate_rows(
    rows: Iterator[Mapping[str, object]],
    timestamp_column: str,
    columns: Mapping[str, str],
) -> tuple[int, datetime, datetime]:
    count = 0
    first: datetime | None = None
    previous: datetime | None = None
    for row in rows:
        current = timestamp(row[timestamp_column])
        open_price = number(row[columns["open"]], "open")
        high = number(row[columns["high"]], "high")
        low = number(row[columns["low"]], "low")
        close = number(row[columns["close"]], "close")
        volume = number(row[columns["volume"]], "volume")
        if min(open_price, high, low, close) <= 0 or volume < 0:
            raise PretestError(f"invalid OHLCV at {iso(current)}")
        if high < max(open_price, low, close) or low > min(open_price, high, close):
            raise PretestError(f"invalid OHLC geometry at {iso(current)}")
        if previous is not None:
            if current <= previous:
                raise PretestError(f"duplicate/out-of-order timestamp: {iso(current)}")
            if current - previous != STEP:
                raise PretestError(f"5m gap after {iso(previous)}")
        first = first or current
        previous = current
        count += 1
    if count == 0 or first is None or previous is None:
        raise PretestError("empty data file")
    return count, first, previous


def inspect_data(
    reporter: Reporter, path: Path, summary: Mapping[str, str], quick: bool
) -> DataFacts:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise PretestError("CSV has no header")
            timestamp_column, columns = column_mapping(reader.fieldnames)
            rows = (
                (row for index, row in enumerate(reader) if index < 1000) if quick else iter(reader)
            )
            count, first, last = validate_rows(rows, timestamp_column, columns)
        format_name = "csv"
    elif suffix == ".parquet":
        try:
            import pyarrow.parquet as pq  # type: ignore[import-untyped]
        except ImportError as exc:
            raise PretestError("Parquet requires pyarrow / '.[backtest]'") from exc
        parquet = pq.ParquetFile(path)
        timestamp_column, columns = column_mapping(parquet.schema.names)
        selected = [timestamp_column, *columns.values()]

        def parquet_rows() -> Iterator[Mapping[str, object]]:
            seen = 0
            for batch in parquet.iter_batches(columns=selected, batch_size=65_536):
                table = batch.to_pydict()
                for index in range(batch.num_rows):
                    if quick and seen >= 1000:
                        return
                    seen += 1
                    yield {name: values[index] for name, values in table.items()}

        count, first, last = validate_rows(parquet_rows(), timestamp_column, columns)
        format_name = "parquet"
    else:
        raise PretestError(f"unsupported data format: {suffix}")

    if quick:
        reporter.warn("data scan", f"quick mode checked {count} leading rows")
    else:
        if count != int(summary["rows"]):
            raise PretestError(f"row count {count} differs from summary {summary['rows']}")
        if first != timestamp(summary["first_open_time"]):
            raise PretestError("first timestamp differs from summary")
        if last != timestamp(summary["last_open_time"]):
            raise PretestError("last timestamp differs from summary")
        reporter.passed("data scan", f"{count} finite, ordered, gap-free 5m rows")
    digest = sha256(path)
    reporter.passed("data hash", digest)
    return DataFacts(
        path=str(path),
        format=format_name,
        sha256=digest,
        rows=count,
        first=iso(first),
        last=iso(last),
        gaps=0,
        timestamp_column=timestamp_column,
        columns=dict(columns),
    )
