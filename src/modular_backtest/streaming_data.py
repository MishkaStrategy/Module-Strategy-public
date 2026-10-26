from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta

from modular_core.data import timeframe_to_seconds
from modular_core.models import Candle

from .config import DataSourceConfig
from .data import (
    DataValidationError,
    ValidationSummary,
    file_sha256,
    stream_candles,
    stream_candles_from,
)
from .registered_data import (
    RegisteredDataError,
    open_registered_candles,
    open_streaming_registered_candles,
)
from .scalability.manifest import CandleDataHasher

_REGISTERED_MODE_ENV = "MODULAR_BACKTEST_REGISTERED_DATASET_MODE"
_REGISTERED_BATCH_SIZE_ENV = "MODULAR_BACKTEST_REGISTERED_STREAM_BATCH_SIZE"
_DEFAULT_REGISTERED_MODE = "legacy_materialized"
_DEFAULT_STREAM_BATCH_SIZE = 8_192
_SUPPORTED_REGISTERED_MODES = frozenset({_DEFAULT_REGISTERED_MODE, "streaming"})
_FROZEN_REGISTERED_OPTIONS: ContextVar[tuple[str, int | None] | None] = ContextVar(
    "modular_backtest_frozen_registered_options",
    default=None,
)


@dataclass(frozen=True, slots=True)
class _SourceMetadata:
    path: str
    sha256: str
    dataset_id: str | None = None
    dataset_version: int | None = None
    dataset_hash: str | None = None
    partition_count: int | None = None
    source_mode: str = "file"
    stream_batch_size: int | None = None


@dataclass(frozen=True, slots=True)
class _ValidationPass:
    data_hash: str
    rows: int
    first: Candle
    last: Candle
    gaps: int
    duplicates: int


@dataclass(frozen=True, slots=True)
class RegisteredValidationSummary(ValidationSummary):
    """Validation evidence for the selected registered-dataset loader contract."""

    source_mode: str = _DEFAULT_REGISTERED_MODE
    stream_batch_size: int | None = None


@dataclass(frozen=True, slots=True)
class PreparedCandleSource:
    """Replayable validated source that never retains the complete Candle history."""

    config: DataSourceConfig
    validation: ValidationSummary
    data_hash: str
    first_candle: Candle
    last_candle: Candle
    source_mode: str = "file"
    stream_batch_size: int | None = None

    @property
    def total_bars(self) -> int:
        return self.validation.rows

    @property
    def symbol(self) -> str:
        return self.first_candle.symbol

    @property
    def base_timeframe(self) -> str:
        return self.first_candle.timeframe

    def iter_candles(self) -> Iterator[Candle]:
        if self.config.is_registered:
            metadata, candles = _open_source(
                self.config,
                registered_mode=self.source_mode,
                stream_batch_size=self.stream_batch_size,
            )
        else:
            metadata, candles = _open_source(self.config)
        if metadata.sha256 != self.validation.sha256:
            raise DataValidationError("market data identity changed after preparation")
        if (
            metadata.source_mode != self.source_mode
            or metadata.stream_batch_size != self.stream_batch_size
        ):
            raise DataValidationError("market data loading mode changed after preparation")
        hasher = CandleDataHasher()
        expected_interval = _expected_interval(self.config.base_timeframe)
        previous: datetime | None = None
        count = 0
        first: Candle | None = None
        last: Candle | None = None
        gaps = 0
        duplicates = 0
        for candle in candles:
            gaps, duplicates = _validate_next(
                candle,
                config=self.config,
                previous=previous,
                expected_interval=expected_interval,
                gaps=gaps,
                duplicates=duplicates,
            )
            previous = candle.timestamp
            hasher.update(candle)
            if first is None:
                first = candle
            last = candle
            count += 1
            yield candle
        if first is None or last is None:
            raise DataValidationError("data set is empty")
        observed_hash = hasher.hexdigest()
        if (
            count != self.total_bars
            or observed_hash != self.data_hash
            or first.timestamp != self.first_candle.timestamp
            or last.timestamp != self.last_candle.timestamp
            or gaps != self.validation.gaps
            or duplicates != self.validation.duplicates
        ):
            raise DataValidationError("market data changed between validation and simulation")
        if not self.config.is_registered:
            path = self.config.path
            if path is None or file_sha256(path) != self.validation.sha256:
                raise DataValidationError("market data file changed during simulation")

    def iter_candles_from(self, start_index: int) -> Iterator[Candle]:
        """Resume at an exact validated row boundary without re-simulating the prefix."""

        if not isinstance(start_index, int) or isinstance(start_index, bool):
            raise TypeError("start_index must be int")
        if start_index < 0 or start_index >= self.total_bars:
            raise ValueError("start_index must select a candle in the prepared source")
        if start_index == 0:
            yield from self.iter_candles()
            return

        expected_interval = _expected_interval(self.config.base_timeframe)
        expected_first_timestamp = (
            self.first_candle.timestamp + expected_interval * start_index
            if self.validation.gaps == 0
            else None
        )
        if self.config.is_registered:
            metadata, candles = _open_source(
                self.config,
                registered_mode=self.source_mode,
                stream_batch_size=self.stream_batch_size,
            )
        else:
            metadata, candles = _open_source(self.config)
        if metadata.sha256 != self.validation.sha256:
            raise DataValidationError("market data identity changed before resume")
        if (
            metadata.source_mode != self.source_mode
            or metadata.stream_batch_size != self.stream_batch_size
        ):
            raise DataValidationError("market data loading mode changed before resume")

        iterator: Iterator[Candle]
        previous: datetime | None
        if self.config.is_registered:
            iterator = iter(candles)
            previous_candle: Candle | None = None
            try:
                for _ in range(start_index):
                    previous_candle = next(iterator)
            except StopIteration as exc:
                close = getattr(iterator, "close", None)
                if callable(close):
                    close()
                raise DataValidationError(
                    "registered market data ended before the resume boundary"
                ) from exc
            if previous_candle is None:
                raise DataValidationError("registered resume boundary is invalid")
            previous = previous_candle.timestamp
        else:
            close = getattr(candles, "close", None)
            if callable(close):
                close()
            iterator = stream_candles_from(
                self.config,
                start_index,
                start_timestamp=expected_first_timestamp,
            )
            previous = (
                expected_first_timestamp - expected_interval
                if expected_first_timestamp is not None
                else None
            )

        count = 0
        last: Candle | None = None
        gaps = 0
        duplicates = 0
        try:
            for candle in iterator:
                if (
                    count == 0
                    and expected_first_timestamp is not None
                    and candle.timestamp != expected_first_timestamp
                ):
                    raise DataValidationError("market data suffix start boundary changed")
                gaps, duplicates = _validate_next(
                    candle,
                    config=self.config,
                    previous=previous,
                    expected_interval=expected_interval,
                    gaps=gaps,
                    duplicates=duplicates,
                )
                previous = candle.timestamp
                last = candle
                count += 1
                yield candle
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                close()

        expected_count = self.total_bars - start_index
        if count != expected_count or last is None:
            raise DataValidationError("market data suffix changed after checkpoint")
        if last.timestamp != self.last_candle.timestamp:
            raise DataValidationError("market data suffix end boundary changed")
        if not self.config.is_registered:
            source_path = self.config.path
            if source_path is None or file_sha256(source_path) != self.validation.sha256:
                raise DataValidationError("market data file changed during resumed simulation")


def _expected_interval(timeframe: str) -> timedelta:
    seconds = timeframe_to_seconds(timeframe)
    if seconds is None:
        raise DataValidationError(f"unsupported base timeframe: {timeframe!r}")
    return timedelta(seconds=seconds)


def _registered_options(
    config: DataSourceConfig,
    registered_mode: str | None,
    stream_batch_size: int | None,
) -> tuple[str, int | None]:
    frozen = _FROZEN_REGISTERED_OPTIONS.get()
    if frozen is not None:
        return frozen
    environment_mode = os.environ.get(_REGISTERED_MODE_ENV)
    raw_mode = registered_mode
    if raw_mode is None:
        raw_mode = environment_mode if environment_mode is not None else config.registered_mode
    if not isinstance(raw_mode, str):
        raise DataValidationError("registered dataset mode must be a string")
    mode = raw_mode.strip().lower()
    if mode not in _SUPPORTED_REGISTERED_MODES:
        supported = ", ".join(sorted(_SUPPORTED_REGISTERED_MODES))
        raise DataValidationError(f"registered dataset mode must be one of: {supported}")

    environment_batch = os.environ.get(_REGISTERED_BATCH_SIZE_ENV)
    raw_batch_size: int | str | None = stream_batch_size
    if raw_batch_size is None and environment_batch is not None:
        raw_batch_size = environment_batch
    if raw_batch_size is None and environment_mode is None and mode == config.registered_mode:
        raw_batch_size = config.stream_batch_size

    if mode == _DEFAULT_REGISTERED_MODE:
        if raw_batch_size is not None:
            raise DataValidationError(
                "registered stream batch size requires registered dataset mode streaming"
            )
        return mode, None

    if raw_batch_size is None:
        raw_batch_size = _DEFAULT_STREAM_BATCH_SIZE
    if isinstance(raw_batch_size, bool):
        raise DataValidationError("registered stream batch size must be a positive integer")
    try:
        batch_size = int(raw_batch_size)
    except (TypeError, ValueError) as exc:
        raise DataValidationError(
            "registered stream batch size must be a positive integer"
        ) from exc
    if batch_size < 1:
        raise DataValidationError("registered stream batch size must be a positive integer")
    return mode, batch_size


def resolve_registered_data_options(config: DataSourceConfig) -> tuple[str, int | None]:
    """Resolve the registered-data mode and batch exactly as the reader does."""

    if not isinstance(config, DataSourceConfig) or not config.is_registered:
        raise TypeError("registered data options require a registered DataSourceConfig")
    return _registered_options(config, registered_mode=None, stream_batch_size=None)


def current_frozen_registered_data_options() -> tuple[str, int | None] | None:
    """Return the active execution selection, if one is frozen in this context."""

    return _FROZEN_REGISTERED_OPTIONS.get()


@contextmanager
def frozen_registered_data_options(
    mode: str,
    batch_size: int | None,
) -> Iterator[None]:
    """Keep the validated reader selection fixed for one effective-config execution."""

    if mode not in _SUPPORTED_REGISTERED_MODES:
        raise DataValidationError("registered dataset mode is unsupported")
    if mode == _DEFAULT_REGISTERED_MODE:
        if batch_size is not None:
            raise DataValidationError("materialized registered data cannot have a stream batch")
    elif not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
        raise DataValidationError("registered stream batch size must be a positive integer")
    token = _FROZEN_REGISTERED_OPTIONS.set((mode, batch_size))
    try:
        yield
    finally:
        _FROZEN_REGISTERED_OPTIONS.reset(token)


def _open_source(
    config: DataSourceConfig,
    *,
    registered_mode: str | None = None,
    stream_batch_size: int | None = None,
) -> tuple[_SourceMetadata, Iterable[Candle]]:
    if config.is_registered:
        mode, batch_size = _registered_options(
            config,
            registered_mode,
            stream_batch_size,
        )
        try:
            if mode == "streaming":
                if batch_size is None:
                    raise DataValidationError("registered streaming mode requires a batch size")
                streaming_source = open_streaming_registered_candles(
                    config,
                    batch_size=batch_size,
                )
                return (
                    _SourceMetadata(
                        path=streaming_source.source_uri,
                        sha256=streaming_source.dataset_hash,
                        dataset_id=streaming_source.dataset_id,
                        dataset_version=streaming_source.dataset_version,
                        dataset_hash=streaming_source.dataset_hash,
                        partition_count=streaming_source.partition_count,
                        source_mode=mode,
                        stream_batch_size=batch_size,
                    ),
                    streaming_source.candles,
                )
            source = open_registered_candles(config)
        except RegisteredDataError as exc:
            raise DataValidationError(str(exc)) from exc
        return (
            _SourceMetadata(
                path=source.source_uri,
                sha256=source.dataset_hash,
                dataset_id=source.dataset_id,
                dataset_version=source.dataset_version,
                dataset_hash=source.dataset_hash,
                partition_count=source.partition_count,
                source_mode=mode,
            ),
            source.candles,
        )
    path = config.path
    if path is None or not path.is_file():
        raise DataValidationError(f"data file does not exist: {path}")
    source_hash = file_sha256(path)
    return _SourceMetadata(path=str(path), sha256=source_hash), stream_candles(config)


def _validate_next(
    candle: Candle,
    *,
    config: DataSourceConfig,
    previous: datetime | None,
    expected_interval: timedelta,
    gaps: int,
    duplicates: int,
) -> tuple[int, int]:
    if not isinstance(candle, Candle):
        raise DataValidationError("data source yielded a non-Candle value")
    if candle.symbol != config.symbol:
        raise DataValidationError("candle symbol differs from data configuration")
    if candle.timeframe != config.base_timeframe:
        raise DataValidationError("candle timeframe differs from data configuration")
    if previous is None:
        return gaps, duplicates
    if candle.timestamp == previous:
        duplicates += 1
        raise DataValidationError(f"duplicate timestamp: {candle.timestamp.isoformat()}")
    if candle.timestamp < previous:
        raise DataValidationError("timestamps must be strictly increasing")
    if candle.timestamp - previous != expected_interval:
        gaps += 1
        if config.gap_policy == "reject":
            raise DataValidationError(
                f"{config.base_timeframe} gap between {previous.isoformat()} "
                f"and {candle.timestamp.isoformat()}"
            )
    return gaps, duplicates


def _run_validation_pass(
    config: DataSourceConfig,
    candles: Iterable[Candle],
) -> _ValidationPass:
    expected_interval = _expected_interval(config.base_timeframe)
    previous: datetime | None = None
    first: Candle | None = None
    last: Candle | None = None
    gaps = 0
    duplicates = 0
    hasher = CandleDataHasher()
    rows = 0
    for candle in candles:
        gaps, duplicates = _validate_next(
            candle,
            config=config,
            previous=previous,
            expected_interval=expected_interval,
            gaps=gaps,
            duplicates=duplicates,
        )
        previous = candle.timestamp
        hasher.update(candle)
        if first is None:
            first = candle
        last = candle
        rows += 1
    if first is None or last is None:
        raise DataValidationError("data set is empty")
    return _ValidationPass(
        data_hash=hasher.hexdigest(),
        rows=rows,
        first=first,
        last=last,
        gaps=gaps,
        duplicates=duplicates,
    )


def _validation_summary(
    config: DataSourceConfig,
    metadata: _SourceMetadata,
    validated: _ValidationPass,
) -> ValidationSummary:
    if config.is_registered:
        return RegisteredValidationSummary(
            path=metadata.path,
            sha256=metadata.sha256,
            symbol=config.symbol,
            market_type=config.market_type,
            base_timeframe=config.base_timeframe,
            rows=validated.rows,
            first_timestamp=validated.first.timestamp.isoformat(),
            last_timestamp=validated.last.timestamp.isoformat(),
            gaps=validated.gaps,
            duplicates=validated.duplicates,
            requested_start=(config.start.isoformat() if config.start is not None else None),
            requested_end=config.end.isoformat() if config.end is not None else None,
            dataset_id=metadata.dataset_id,
            dataset_version=metadata.dataset_version,
            dataset_hash=metadata.dataset_hash,
            partition_count=metadata.partition_count,
            source_mode=metadata.source_mode,
            stream_batch_size=metadata.stream_batch_size,
        )
    return ValidationSummary(
        path=metadata.path,
        sha256=metadata.sha256,
        symbol=config.symbol,
        market_type=config.market_type,
        base_timeframe=config.base_timeframe,
        rows=validated.rows,
        first_timestamp=validated.first.timestamp.isoformat(),
        last_timestamp=validated.last.timestamp.isoformat(),
        gaps=validated.gaps,
        duplicates=validated.duplicates,
        requested_start=config.start.isoformat() if config.start is not None else None,
        requested_end=config.end.isoformat() if config.end is not None else None,
        dataset_id=metadata.dataset_id,
        dataset_version=metadata.dataset_version,
        dataset_hash=metadata.dataset_hash,
        partition_count=metadata.partition_count,
    )


def prepare_candle_source(
    config: DataSourceConfig,
    *,
    registered_mode: str | None = None,
    stream_batch_size: int | None = None,
) -> PreparedCandleSource:
    mode = "file"
    batch_size: int | None = None
    if config.is_registered:
        mode, batch_size = _registered_options(
            config,
            registered_mode,
            stream_batch_size,
        )
    elif registered_mode is not None or stream_batch_size is not None:
        raise DataValidationError(
            "registered dataset loading options require a registered data source"
        )

    if config.is_registered:
        metadata, candles = _open_source(
            config,
            registered_mode=mode,
            stream_batch_size=batch_size,
        )
    else:
        metadata, candles = _open_source(config)
    validated = _run_validation_pass(config, candles)
    if not config.is_registered:
        path = config.path
        if path is None or file_sha256(path) != metadata.sha256:
            raise DataValidationError("market data file changed during validation")
    summary = _validation_summary(config, metadata, validated)
    return PreparedCandleSource(
        config=config,
        validation=summary,
        data_hash=validated.data_hash,
        first_candle=validated.first,
        last_candle=validated.last,
        source_mode=metadata.source_mode,
        stream_batch_size=metadata.stream_batch_size,
    )
