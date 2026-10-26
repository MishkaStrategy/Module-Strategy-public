from __future__ import annotations

# mypy: disable-error-code=import-untyped

import csv
import hashlib
import math
from collections.abc import Iterable, Iterator, Mapping
from itertools import islice
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from modular_core.data import timeframe_to_seconds
from modular_core.models import Candle

from .config import DataSourceConfig
from .registered_data import RegisteredDataError, open_registered_candles


class DataValidationError(ValueError):
    """Raised when market data violates deterministic backtest invariants."""


@dataclass(frozen=True, slots=True)
class ValidationSummary:
    path: str
    sha256: str
    symbol: str
    market_type: str
    base_timeframe: str
    rows: int
    first_timestamp: str
    last_timestamp: str
    gaps: int
    duplicates: int
    requested_start: str | None
    requested_end: str | None
    dataset_id: str | None = None
    dataset_version: int | None = None
    dataset_hash: str | None = None
    partition_count: int | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_timestamp(raw: object) -> datetime:
    if isinstance(raw, bool):
        raise DataValidationError("timestamp must not be boolean")
    if isinstance(raw, datetime):
        value = raw
    elif isinstance(raw, int | float) or (isinstance(raw, str) and raw.strip().isdigit()):
        if isinstance(raw, float) and not raw.is_integer():
            raise DataValidationError("numeric timestamp must be an integer epoch value")
        numeric = int(raw)
        seconds = numeric / 1000 if numeric > 10_000_000_000 else numeric
        try:
            value = datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise DataValidationError(f"timestamp is outside the supported range: {raw!r}") from exc
    elif isinstance(raw, str):
        text = raw.strip().replace("Z", "+00:00")
        try:
            value = datetime.fromisoformat(text)
        except ValueError as exc:
            raise DataValidationError(f"invalid ISO-8601 timestamp: {raw!r}") from exc
    else:
        raise DataValidationError(f"unsupported timestamp value: {raw!r}")
    if value.tzinfo is None:
        raise DataValidationError("timestamps must be timezone-aware UTC values")
    return value.astimezone(UTC)


def _number(raw: object, name: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, str | bytes | int | float):
        raise DataValidationError(f"{name} is not numeric: {raw!r}")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise DataValidationError(f"{name} is not numeric: {raw!r}") from exc
    if not math.isfinite(value):
        raise DataValidationError(f"{name} must be finite")
    return value


def _row_to_candle(row: Mapping[str, object], config: DataSourceConfig) -> Candle:
    columns = config.columns
    required = {
        config.timestamp_column,
        columns["open"],
        columns["high"],
        columns["low"],
        columns["close"],
        columns["volume"],
    }
    missing = sorted(required - set(row))
    if missing:
        raise DataValidationError(f"missing columns: {missing}")
    return Candle(
        timestamp=_parse_timestamp(row[config.timestamp_column]),
        open=_number(row[columns["open"]], "open"),
        high=_number(row[columns["high"]], "high"),
        low=_number(row[columns["low"]], "low"),
        close=_number(row[columns["close"]], "close"),
        volume=_number(row[columns["volume"]], "volume"),
        symbol=config.symbol,
        timeframe=config.base_timeframe,
    )


def _stream_csv(config: DataSourceConfig) -> Iterator[Candle]:
    path = config.path
    if path is None:
        raise DataValidationError("file data source path is missing")
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise DataValidationError("CSV has no header")
        for row in reader:
            yield _row_to_candle(row, config)


def _stream_parquet(config: DataSourceConfig) -> Iterator[Candle]:
    path = config.path
    if path is None:
        raise DataValidationError("file data source path is missing")
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise DataValidationError(
            "cannot read Parquet market data without the optional dependency: "
            "pip install '.[backtest]'"
        ) from exc
    try:
        parquet = pq.ParquetFile(path)
        selected = [
            config.timestamp_column,
            config.columns["open"],
            config.columns["high"],
            config.columns["low"],
            config.columns["close"],
            config.columns["volume"],
        ]
        for batch in parquet.iter_batches(columns=selected, batch_size=65_536):
            table = batch.to_pydict()
            for index in range(batch.num_rows):
                row = {name: values[index] for name, values in table.items()}
                yield _row_to_candle(row, config)
    except DataValidationError:
        raise
    except Exception as exc:
        raise DataValidationError(f"cannot read Parquet market data: {path}") from exc


def _stream_parquet_from(config: DataSourceConfig, start_index: int) -> Iterator[Candle]:
    path = config.path
    if path is None:
        raise DataValidationError("file data source path is missing")
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise DataValidationError(
            "cannot resume Parquet market data without the optional dependency: "
            "pip install '.[backtest]'"
        ) from exc
    try:
        parquet = pq.ParquetFile(path)
        selected = [
            config.timestamp_column,
            config.columns["open"],
            config.columns["high"],
            config.columns["low"],
            config.columns["close"],
            config.columns["volume"],
        ]
        remaining = start_index
        for row_group_index in range(parquet.num_row_groups):
            row_group_rows = parquet.metadata.row_group(row_group_index).num_rows
            if remaining >= row_group_rows:
                remaining -= row_group_rows
                continue
            for batch in parquet.iter_batches(
                batch_size=65_536,
                row_groups=[row_group_index],
                columns=selected,
            ):
                if remaining >= batch.num_rows:
                    remaining -= batch.num_rows
                    continue
                if remaining:
                    batch = batch.slice(remaining)
                    remaining = 0
                columns = batch.to_pydict()
                for index in range(batch.num_rows):
                    row = {name: values[index] for name, values in columns.items()}
                    yield _row_to_candle(row, config)
        if remaining:
            raise DataValidationError("resume index exceeds Parquet row count")
    except DataValidationError:
        raise
    except Exception as exc:
        raise DataValidationError(f"cannot resume Parquet market data: {path}") from exc


def stream_candles_from(
    config: DataSourceConfig,
    start_index: int,
    *,
    start_timestamp: datetime | None = None,
) -> Iterator[Candle]:
    """Yield a deterministic suffix without creating Candle objects for the prefix."""

    if not isinstance(start_index, int) or isinstance(start_index, bool):
        raise TypeError("start_index must be int")
    if start_index < 0:
        raise ValueError("start_index cannot be negative")
    if start_timestamp is not None:
        if start_timestamp.tzinfo is None or start_timestamp.utcoffset() is None:
            raise ValueError("start_timestamp must be timezone-aware")
        start_timestamp = start_timestamp.astimezone(UTC)
    if start_index == 0:
        yield from stream_candles(config)
        return
    if config.is_registered:
        if start_timestamp is None:
            try:
                source = open_registered_candles(config)
            except RegisteredDataError as exc:
                raise DataValidationError(str(exc)) from exc
            yield from islice(source.candles, start_index, None)
            return
        resumed = replace(config, start=start_timestamp)
        try:
            source = open_registered_candles(resumed)
        except RegisteredDataError as exc:
            raise DataValidationError(str(exc)) from exc
        yield from source.candles
        return
    path = config.path
    if path is None or not path.is_file():
        raise DataValidationError(f"data file does not exist: {path}")
    if config.format == "parquet" and config.start is None and config.end is None:
        yield from _stream_parquet_from(config, start_index)
        return
    if start_timestamp is not None:
        resumed = replace(config, start=start_timestamp)
        yield from stream_candles(resumed)
        return
    if config.format == "parquet":
        yield from islice(_stream_parquet(config), start_index, None)
    else:
        yield from islice(_stream_csv(config), start_index, None)


def stream_candles(config: DataSourceConfig) -> Iterator[Candle]:
    if config.is_registered:
        try:
            registered_source = open_registered_candles(config)
            yield from registered_source.candles
        except RegisteredDataError as exc:
            raise DataValidationError(str(exc)) from exc
        return
    path = config.path
    if path is None or not path.is_file():
        raise DataValidationError(f"data file does not exist: {path}")
    file_source = _stream_csv(config) if config.format == "csv" else _stream_parquet(config)
    for candle in file_source:
        if config.start is not None and candle.timestamp < config.start:
            continue
        if config.end is not None and candle.timestamp >= config.end:
            continue
        yield candle


def _expected_interval(timeframe: str) -> timedelta:
    seconds = timeframe_to_seconds(timeframe)
    if seconds is None:
        raise DataValidationError(f"unsupported base timeframe: {timeframe!r}")
    return timedelta(seconds=seconds)


def _validated_stream(
    candles: Iterable[Candle],
    gap_policy: str,
    *,
    timeframe: str = "5m",
) -> tuple[list[Candle], int, int]:
    result: list[Candle] = []
    previous: datetime | None = None
    expected_interval = _expected_interval(timeframe)
    gaps = 0
    duplicates = 0
    for candle in candles:
        if previous is not None:
            if candle.timestamp == previous:
                duplicates += 1
                raise DataValidationError(f"duplicate timestamp: {candle.timestamp.isoformat()}")
            if candle.timestamp < previous:
                raise DataValidationError("timestamps must be strictly increasing")
            if candle.timestamp - previous != expected_interval:
                gaps += 1
                if gap_policy == "reject":
                    raise DataValidationError(
                        f"{timeframe} gap between {previous.isoformat()} "
                        f"and {candle.timestamp.isoformat()}"
                    )
        previous = candle.timestamp
        result.append(candle)
    if not result:
        raise DataValidationError("data set is empty")
    return result, gaps, duplicates


def load_validated_candles(config: DataSourceConfig) -> tuple[list[Candle], ValidationSummary]:
    if config.is_registered:
        try:
            source = open_registered_candles(config)
            candles, gaps, duplicates = _validated_stream(
                source.candles,
                config.gap_policy,
                timeframe=config.base_timeframe,
            )
        except RegisteredDataError as exc:
            raise DataValidationError(str(exc)) from exc
        summary = ValidationSummary(
            path=source.source_uri,
            sha256=source.dataset_hash,
            symbol=config.symbol,
            market_type=config.market_type,
            base_timeframe=config.base_timeframe,
            rows=len(candles),
            first_timestamp=candles[0].timestamp.isoformat(),
            last_timestamp=candles[-1].timestamp.isoformat(),
            gaps=gaps,
            duplicates=duplicates,
            requested_start=config.start.isoformat() if config.start is not None else None,
            requested_end=config.end.isoformat() if config.end is not None else None,
            dataset_id=source.dataset_id,
            dataset_version=source.dataset_version,
            dataset_hash=source.dataset_hash,
            partition_count=source.partition_count,
        )
        return candles, summary

    path = config.path
    if path is None:
        raise DataValidationError("file data source path is missing")
    source_sha256 = file_sha256(path)
    candles, gaps, duplicates = _validated_stream(
        stream_candles(config),
        config.gap_policy,
        timeframe=config.base_timeframe,
    )
    if file_sha256(path) != source_sha256:
        raise DataValidationError("market data file changed during validation")
    summary = ValidationSummary(
        path=str(path),
        sha256=source_sha256,
        symbol=config.symbol,
        market_type=config.market_type,
        base_timeframe=config.base_timeframe,
        rows=len(candles),
        first_timestamp=candles[0].timestamp.isoformat(),
        last_timestamp=candles[-1].timestamp.isoformat(),
        gaps=gaps,
        duplicates=duplicates,
        requested_start=config.start.isoformat() if config.start is not None else None,
        requested_end=config.end.isoformat() if config.end is not None else None,
    )
    return candles, summary


def validate_data(config: DataSourceConfig) -> ValidationSummary:
    _, summary = load_validated_candles(config)
    return summary
