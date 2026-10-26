from __future__ import annotations

from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from modular_core.models import Candle

if TYPE_CHECKING:
    from .config import DataSourceConfig

_DEFAULT_BATCH_SIZE = 8_192
BRIDGE_VERSION = "registered-dataset-streaming-v1"


class RegisteredDataError(ValueError):
    """Raised when an immutable registered dataset cannot be consumed safely."""


@dataclass(frozen=True, slots=True)
class RegisteredCandleSource:
    """Canonical legacy registered source with full preflight validation."""

    candles: Iterator[Candle]
    dataset_id: str
    dataset_version: int
    dataset_hash: str
    partition_count: int
    source_uri: str


@dataclass(frozen=True, slots=True)
class RegisteredSourcePosition:
    """Position of the next physical row to read from a registered dataset."""

    dataset_id: str
    dataset_version: int
    dataset_hash: str
    manifest_checksum: str
    schema_fingerprint: str
    source_version: str
    partition_index: int
    row_offset: int
    last_timestamp: datetime | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "dataset_hash": self.dataset_hash,
            "manifest_checksum": self.manifest_checksum,
            "schema_fingerprint": self.schema_fingerprint,
            "source_version": self.source_version,
            "partition_index": self.partition_index,
            "row_offset": self.row_offset,
            "last_timestamp": (
                self.last_timestamp.astimezone(UTC).isoformat()
                if self.last_timestamp is not None
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class RegisteredDatasetMetadata:
    dataset_id: str
    dataset_version: int
    dataset_hash: str
    manifest_checksum: str
    schema_fingerprint: str
    symbol: str
    timeframe: str
    row_count: int
    first_timestamp: datetime
    last_timestamp: datetime
    partition_count: int
    source_uri: str
    lineage: tuple[str, ...] = ()
    bridge_version: str = BRIDGE_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "dataset_hash": self.dataset_hash,
            "manifest_checksum": self.manifest_checksum,
            "schema_fingerprint": self.schema_fingerprint,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "row_count": self.row_count,
            "first_timestamp": self.first_timestamp.astimezone(UTC).isoformat(),
            "last_timestamp": self.last_timestamp.astimezone(UTC).isoformat(),
            "partition_count": self.partition_count,
            "source_uri": self.source_uri,
            "lineage": list(self.lineage),
            "bridge_version": self.bridge_version,
        }


@dataclass(slots=True)
class StreamingRegisteredCandleSource:
    """Replayable registered source with bounded Arrow/Python batch ownership."""

    config: DataSourceConfig
    dataset_id: str
    dataset_version: int
    dataset_hash: str
    manifest_checksum: str
    schema_fingerprint: str
    partition_count: int
    source_uri: str
    row_count: int
    first_timestamp: datetime
    last_timestamp: datetime
    symbol: str
    timeframe: str
    interval: timedelta
    batch_size: int = _DEFAULT_BATCH_SIZE
    lineage: tuple[str, ...] = ()
    bridge_version: str = BRIDGE_VERSION
    _selection: Any = field(default=None, repr=False)
    _open_count: int = field(default=0, init=False, repr=False)

    @property
    def candles(self) -> Iterator[Candle]:
        """Return a fresh bounded iterator without materializing the source."""

        return self.open()

    @property
    def source_open_count(self) -> int:
        return self._open_count

    def metadata(self) -> RegisteredDatasetMetadata:
        return RegisteredDatasetMetadata(
            dataset_id=self.dataset_id,
            dataset_version=self.dataset_version,
            dataset_hash=self.dataset_hash,
            manifest_checksum=self.manifest_checksum,
            schema_fingerprint=self.schema_fingerprint,
            symbol=self.symbol,
            timeframe=self.timeframe,
            row_count=self.row_count,
            first_timestamp=self.first_timestamp,
            last_timestamp=self.last_timestamp,
            partition_count=self.partition_count,
            source_uri=self.source_uri,
            lineage=self.lineage,
            bridge_version=self.bridge_version,
        )

    def _position(
        self,
        *,
        partition_index: int,
        row_offset: int,
        last_timestamp: datetime | None,
    ) -> RegisteredSourcePosition:
        return RegisteredSourcePosition(
            dataset_id=self.dataset_id,
            dataset_version=self.dataset_version,
            dataset_hash=self.dataset_hash,
            manifest_checksum=self.manifest_checksum,
            schema_fingerprint=self.schema_fingerprint,
            source_version=self.bridge_version,
            partition_index=partition_index,
            row_offset=row_offset,
            last_timestamp=last_timestamp,
        )

    def initial_position(self) -> RegisteredSourcePosition:
        requested_start = self.config.start
        if requested_start is None or requested_start <= self.first_timestamp:
            return self._position(partition_index=0, row_offset=0, last_timestamp=None)
        for partition_index, partition in enumerate(self._selection.partitions):
            if partition.end <= requested_start:
                continue
            delta = requested_start - partition.start
            offset = max(0, (delta + self.interval - timedelta(microseconds=1)) // self.interval)
            row_offset = min(int(offset), partition.row_count)
            if row_offset == partition.row_count:
                continue
            previous = partition.start + (row_offset - 1) * self.interval
            return self._position(
                partition_index=partition_index,
                row_offset=row_offset,
                last_timestamp=previous,
            )
        return self._position(
            partition_index=self.partition_count,
            row_offset=0,
            last_timestamp=self.last_timestamp,
        )

    def open(
        self,
        start_position: RegisteredSourcePosition | None = None,
    ) -> StreamingRegisteredCandleIterator:
        position = start_position or self.initial_position()
        self._validate_position(position)
        self._open_count += 1
        return StreamingRegisteredCandleIterator(
            self,
            position,
            resumed=start_position is not None,
        )

    def _expected_last_timestamp(
        self,
        position: RegisteredSourcePosition,
    ) -> datetime | None:
        if position.partition_index == self.partition_count:
            return self.last_timestamp
        partition = self._selection.partitions[position.partition_index]
        partition_start = cast(datetime, partition.start)
        if position.row_offset > 0:
            return partition_start + (position.row_offset - 1) * self.interval
        if position.partition_index == 0:
            return None
        return partition_start - self.interval

    def _validate_position(self, position: RegisteredSourcePosition) -> None:
        if (
            position.dataset_id != self.dataset_id
            or position.dataset_version != self.dataset_version
            or position.dataset_hash != self.dataset_hash
            or position.manifest_checksum != self.manifest_checksum
            or position.schema_fingerprint != self.schema_fingerprint
            or position.source_version != self.bridge_version
        ):
            raise RegisteredDataError("source position belongs to a different dataset identity")
        if (
            not isinstance(position.partition_index, int)
            or isinstance(position.partition_index, bool)
            or position.partition_index < 0
            or position.partition_index > self.partition_count
        ):
            raise RegisteredDataError("source position partition index is invalid")
        if position.last_timestamp is not None:
            if (
                position.last_timestamp.tzinfo is None
                or position.last_timestamp.utcoffset() is None
            ):
                raise RegisteredDataError("source position timestamp must be timezone-aware")
            if position.last_timestamp.utcoffset() != timedelta(0):
                raise RegisteredDataError("source position timestamp must be UTC")
        if position.partition_index == self.partition_count:
            if position.row_offset != 0:
                raise RegisteredDataError("end-of-source position must use row_offset zero")
        else:
            partition = self._selection.partitions[position.partition_index]
            if (
                not isinstance(position.row_offset, int)
                or isinstance(position.row_offset, bool)
                or position.row_offset < 0
                or position.row_offset >= partition.row_count
            ):
                raise RegisteredDataError("source position row offset is invalid")
        if position.last_timestamp != self._expected_last_timestamp(position):
            raise RegisteredDataError(
                "source position timestamp does not match its physical row position"
            )


class StreamingRegisteredCandleIterator(Iterator[Candle]):
    """Single-use iterator that exposes a checkpoint-safe next-row cursor."""

    def __init__(
        self,
        source: StreamingRegisteredCandleSource,
        start_position: RegisteredSourcePosition,
        *,
        resumed: bool,
    ) -> None:
        self._source = source
        self._position = start_position
        self._resumed = resumed
        self._closed = False
        self._iterator = self._iterate()

    def __iter__(self) -> StreamingRegisteredCandleIterator:
        return self

    def __next__(self) -> Candle:
        if self._closed:
            raise StopIteration
        return next(self._iterator)

    def checkpoint_position(self) -> RegisteredSourcePosition:
        return self._position

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._iterator.close()

    def _advance_position(
        self,
        *,
        partition_index: int,
        partition_row_count: int,
        physical_row: int,
        timestamp: datetime,
    ) -> None:
        next_partition = partition_index
        next_row = physical_row + 1
        if next_row == partition_row_count:
            next_partition += 1
            next_row = 0
        self._position = self._source._position(
            partition_index=next_partition,
            row_offset=next_row,
            last_timestamp=timestamp,
        )

    def _iterate(self) -> Generator[Candle, None, None]:
        if self._position.partition_index == self._source.partition_count:
            self._closed = True
            if self._resumed:
                return
            raise RegisteredDataError("registered dataset selection is empty")

        try:
            from market_data_ingestion.errors import StructuredIngestionError
            from market_data_ingestion.streaming_dataset_reader import (
                iter_registered_kline_batches,
            )
        except ImportError as exc:
            raise RegisteredDataError(
                "registered dataset support requires modular-market-data-ingestion with parquet support"
            ) from exc

        emitted = False
        previous = self._position.last_timestamp if self._resumed else None
        batches: Iterator[Any] | None = None
        try:
            batches = iter_registered_kline_batches(
                self._source._selection,
                batch_size=self._source.batch_size,
                start_partition_index=self._position.partition_index,
                start_row_offset=self._position.row_offset,
            )
            for batch in batches:
                partition = self._source._selection.partitions[batch.partition_index]
                for batch_index, record in enumerate(batch.records):
                    if record.symbol != self._source.symbol:
                        raise RegisteredDataError(
                            "registered candle symbol differs from source metadata"
                        )
                    if record.timeframe != self._source.timeframe:
                        raise RegisteredDataError(
                            "registered candle timeframe differs from source metadata"
                        )
                    if (
                        self._source.config.end is not None
                        and record.open_time >= self._source.config.end
                    ):
                        if not emitted:
                            if self._resumed:
                                return
                            raise RegisteredDataError("registered dataset selection is empty")
                        return
                    if previous is not None:
                        if record.open_time == previous:
                            raise RegisteredDataError(
                                f"duplicate timestamp: {record.open_time.isoformat()}"
                            )
                        if record.open_time < previous:
                            raise RegisteredDataError(
                                "registered candle timestamps must be strictly increasing"
                            )
                        if record.open_time - previous != self._source.interval:
                            raise RegisteredDataError(
                                f"{self._source.timeframe} gap between "
                                f"{previous.isoformat()} and {record.open_time.isoformat()}"
                            )
                    previous = record.open_time
                    physical_row = batch.row_offset + batch_index
                    self._advance_position(
                        partition_index=batch.partition_index,
                        partition_row_count=partition.row_count,
                        physical_row=physical_row,
                        timestamp=record.open_time,
                    )
                    if (
                        self._source.config.start is not None
                        and record.open_time < self._source.config.start
                    ):
                        continue
                    emitted = True
                    yield Candle(
                        timestamp=record.open_time,
                        open=float(record.open),
                        high=float(record.high),
                        low=float(record.low),
                        close=float(record.close),
                        volume=float(record.volume),
                        symbol=record.symbol,
                        timeframe=record.timeframe,
                    )
        except RegisteredDataError:
            raise
        except (StructuredIngestionError, OSError, TypeError, ValueError) as exc:
            raise RegisteredDataError(f"registered dataset validation failed: {exc}") from exc
        finally:
            close = getattr(batches, "close", None)
            if callable(close):
                close()
            self._closed = True
        if not emitted:
            raise RegisteredDataError("registered dataset selection is empty")


@dataclass(frozen=True, slots=True)
class RegisteredCandleBatch:
    """Legacy compatibility container that intentionally materializes all candles."""

    candles: tuple[Candle, ...]
    dataset_id: str
    dataset_version: int
    dataset_hash: str
    partition_count: int
    source_uri: str


def open_registered_candles(config: DataSourceConfig) -> RegisteredCandleSource:
    """Open the unchanged canonical deep-validated registered loader."""

    if config.storage_root is None or config.dataset_id is None or config.dataset_version is None:
        raise RegisteredDataError("registered data source is incomplete")
    try:
        from market_data_ingestion.dataset_reader import (
            iter_registered_klines,
            resolve_registered_kline_dataset,
        )
        from market_data_ingestion.errors import StructuredIngestionError
    except ImportError as exc:
        raise RegisteredDataError(
            "registered dataset support requires modular-market-data-ingestion with parquet support"
        ) from exc

    try:
        selection = resolve_registered_kline_dataset(
            config.storage_root,
            config.dataset_id,
            config.dataset_version,
            timeframe=config.base_timeframe,
            consumer="modular_backtest",
            deep=True,
        )
        if selection.manifest.instrument.symbol != config.symbol:
            raise RegisteredDataError(
                "registered dataset symbol differs from the backtest configuration"
            )
    except RegisteredDataError:
        raise
    except (StructuredIngestionError, OSError, ValueError) as exc:
        raise RegisteredDataError(f"registered dataset validation failed: {exc}") from exc

    def candles() -> Iterator[Candle]:
        emitted = False
        try:
            for record in iter_registered_klines(
                selection,
                start=config.start,
                end=config.end,
            ):
                emitted = True
                yield Candle(
                    timestamp=record.open_time,
                    open=float(record.open),
                    high=float(record.high),
                    low=float(record.low),
                    close=float(record.close),
                    volume=float(record.volume),
                    symbol=record.symbol,
                    timeframe=record.timeframe,
                )
        except RegisteredDataError:
            raise
        except (StructuredIngestionError, OSError, ValueError) as exc:
            raise RegisteredDataError(f"registered dataset validation failed: {exc}") from exc
        if not emitted:
            raise RegisteredDataError("registered dataset selection is empty")

    descriptor = selection.descriptor
    return RegisteredCandleSource(
        candles=candles(),
        dataset_id=descriptor.dataset_id,
        dataset_version=descriptor.version,
        dataset_hash=descriptor.dataset_hash,
        partition_count=len(selection.partitions),
        source_uri=f"dataset://{descriptor.dataset_id}/v{descriptor.version}",
    )


def open_streaming_registered_candles(
    config: DataSourceConfig,
    *,
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> StreamingRegisteredCandleSource:
    """Open the opt-in bounded reader without changing the canonical legacy path."""

    if config.storage_root is None or config.dataset_id is None or config.dataset_version is None:
        raise RegisteredDataError("registered data source is incomplete")
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size <= 0:
        raise RegisteredDataError("registered dataset batch_size must be a positive integer")
    try:
        from market_data_ingestion.errors import StructuredIngestionError
        from market_data_ingestion.streaming_dataset_reader import (
            KLINE_SCHEMA_FINGERPRINT,
            MAX_STREAM_BATCH_SIZE,
            resolve_streaming_registered_kline_dataset,
        )
        from market_data_ingestion.timeframes import timeframe_delta
    except ImportError as exc:
        raise RegisteredDataError(
            "registered dataset support requires modular-market-data-ingestion with parquet support"
        ) from exc
    if batch_size > MAX_STREAM_BATCH_SIZE:
        raise RegisteredDataError(
            f"registered dataset batch_size must not exceed {MAX_STREAM_BATCH_SIZE}"
        )

    try:
        selection = resolve_streaming_registered_kline_dataset(
            config.storage_root,
            config.dataset_id,
            config.dataset_version,
            timeframe=config.base_timeframe,
            consumer="modular_backtest",
        )
        if selection.manifest.instrument.symbol != config.symbol:
            raise RegisteredDataError(
                "registered dataset symbol differs from the backtest configuration"
            )
    except RegisteredDataError:
        raise
    except (StructuredIngestionError, OSError, TypeError, ValueError) as exc:
        raise RegisteredDataError(f"registered dataset validation failed: {exc}") from exc

    descriptor = selection.descriptor
    interval = timeframe_delta(selection.timeframe)
    source_uri = f"dataset://{descriptor.dataset_id}/v{descriptor.version}"
    last_timestamp = selection.partitions[-1].end - interval
    return StreamingRegisteredCandleSource(
        config=config,
        dataset_id=descriptor.dataset_id,
        dataset_version=descriptor.version,
        dataset_hash=descriptor.dataset_hash,
        manifest_checksum=selection.manifest_checksum,
        schema_fingerprint=KLINE_SCHEMA_FINGERPRINT,
        partition_count=len(selection.partitions),
        source_uri=source_uri,
        row_count=selection.row_count,
        first_timestamp=selection.partitions[0].start,
        last_timestamp=last_timestamp,
        symbol=selection.manifest.instrument.symbol,
        timeframe=selection.timeframe,
        interval=interval,
        batch_size=batch_size,
        lineage=selection.manifest.lineage,
        _selection=selection,
    )


def load_registered_candles(config: DataSourceConfig) -> RegisteredCandleBatch:
    source = open_registered_candles(config)
    return RegisteredCandleBatch(
        candles=tuple(source.candles),
        dataset_id=source.dataset_id,
        dataset_version=source.dataset_version,
        dataset_hash=source.dataset_hash,
        partition_count=source.partition_count,
        source_uri=source.source_uri,
    )
