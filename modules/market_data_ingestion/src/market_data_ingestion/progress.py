from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from typing import Any

from .adapters.base import AcquiredData, ExchangeAdapter, InstrumentResolution
from .contracts import (
    DownloadChunk,
    DownloadPlan,
    ExchangeCapabilities,
    InstrumentMetadata,
    InstrumentReference,
    MarketDataRequest,
)
from .enums import DataType, RunState

_COUNTER_NAMES = frozenset(
    {
        "checkpoint_completed_chunks",
        "downloaded_chunks",
        "restored_chunks",
        "downloaded_records",
        "restored_records",
        "downloaded_bytes",
    }
)


@dataclass(frozen=True, slots=True)
class IngestionProgressSnapshot:
    phase: RunState
    determinate: bool
    planned_chunks: int = 0
    completed_chunks: int = 0
    total_chunks: int | None = None
    checkpoint_completed_chunks: int = 0
    downloaded_chunks: int = 0
    restored_chunks: int = 0
    downloaded_records: int = 0
    restored_records: int = 0
    downloaded_bytes: int = 0
    current_range_start: datetime | None = None
    current_range_end: datetime | None = None
    current_data_type: DataType | None = None
    current_timeframe: str | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        integer_fields = (
            self.planned_chunks,
            self.completed_chunks,
            self.checkpoint_completed_chunks,
            self.downloaded_chunks,
            self.restored_chunks,
            self.downloaded_records,
            self.restored_records,
            self.downloaded_bytes,
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in integer_fields
        ):
            raise ValueError("progress counters must be non-negative integers")
        if self.completed_chunks > self.planned_chunks:
            raise ValueError("completed chunks must not exceed planned chunks")
        if self.determinate:
            if self.phase is not RunState.DOWNLOADING:
                raise ValueError(
                    "determinate ingestion progress is supported only while downloading"
                )
            if self.total_chunks is None or self.total_chunks <= 0:
                raise ValueError("determinate progress requires a positive chunk total")
            if self.total_chunks != self.planned_chunks:
                raise ValueError("determinate total must equal the measured plan size")
            if self.completed_chunks > self.total_chunks:
                raise ValueError("completed chunks must not exceed total chunks")
        elif self.total_chunks is not None:
            raise ValueError("indeterminate progress must not include total_chunks")
        if self.checkpoint_completed_chunks > self.completed_chunks:
            raise ValueError("checkpoint completed chunks must not exceed completed chunks")
        if (self.current_range_start is None) != (self.current_range_end is None):
            raise ValueError("current progress range requires both start and end")
        if self.current_range_start is not None and self.current_range_end is not None:
            start = _aware_utc(self.current_range_start)
            end = _aware_utc(self.current_range_end)
            if start >= end:
                raise ValueError("current progress range start must precede end")
            object.__setattr__(self, "current_range_start", start)
            object.__setattr__(self, "current_range_end", end)
        if self.current_timeframe is not None:
            normalized = self.current_timeframe.strip().lower()
            if not normalized or len(normalized) > 16:
                raise ValueError("current timeframe must be non-empty and bounded")
            object.__setattr__(self, "current_timeframe", normalized)
        if self.message is not None:
            normalized_message = self.message.strip()
            if not normalized_message or len(normalized_message) > 500:
                raise ValueError("progress message must be non-empty and bounded")
            object.__setattr__(self, "message", normalized_message)


ProgressObserver = Callable[[IngestionProgressSnapshot], None]
_PROGRESS_OBSERVER: ContextVar[ProgressObserver | None] = ContextVar(
    "market_data_progress_observer",
    default=None,
)
_PROGRESS_OBSERVER_ACTIVE: ContextVar[bool] = ContextVar(
    "market_data_progress_observer_active",
    default=False,
)


@contextmanager
def observe_progress(observer: ProgressObserver) -> Iterator[None]:
    """Scope a non-authoritative progress sink to one ingestion execution."""

    if not callable(observer):
        raise TypeError("observer must be callable")
    token = _PROGRESS_OBSERVER.set(observer)
    try:
        yield
    finally:
        _PROGRESS_OBSERVER.reset(token)


def emit_progress(snapshot: IngestionProgressSnapshot) -> None:
    """Emit progress without allowing observability failures to change domain outcomes."""

    observer = _PROGRESS_OBSERVER.get()
    if observer is None or _PROGRESS_OBSERVER_ACTIVE.get():
        return
    active_token = _PROGRESS_OBSERVER_ACTIVE.set(True)
    try:
        with suppress(Exception):
            observer(snapshot)
    finally:
        _PROGRESS_OBSERVER_ACTIVE.reset(active_token)


class DownloadProgressTracker:
    """Convert an exact plan and committed pipeline log records to monotonic snapshots."""

    def __init__(
        self,
        *,
        checkpoint_completed_chunks: Iterable[str] = (),
        initial_counters: Mapping[str, int] | None = None,
    ) -> None:
        counters = dict(initial_counters or {})
        unknown = set(counters) - _COUNTER_NAMES - {"planned_chunks"}
        if unknown:
            raise ValueError(f"unknown progress counters: {sorted(unknown)}")
        for name, value in counters.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"progress counter must be a non-negative integer: {name}")
        self._checkpoint_ids = frozenset(str(item) for item in checkpoint_completed_chunks)
        self._lock = RLock()
        self._chunks: dict[str, DownloadChunk] = {}
        self._completed_ids: set[str] = set()
        self._observed_ids: set[str] = set()
        self._checkpoint_completed = counters.get("checkpoint_completed_chunks", 0)
        self._downloaded_chunks = counters.get("downloaded_chunks", 0)
        self._restored_chunks = counters.get("restored_chunks", 0)
        self._downloaded_records = counters.get("downloaded_records", 0)
        self._restored_records = counters.get("restored_records", 0)
        self._downloaded_bytes = counters.get("downloaded_bytes", 0)

    def set_plan(self, chunks: Iterable[DownloadChunk]) -> None:
        ordered = tuple(chunks)
        identities = [chunk.chunk_id for chunk in ordered]
        if len(identities) != len(set(identities)):
            raise ValueError("download plan chunk IDs must be unique")
        with self._lock:
            if self._chunks:
                raise ValueError("download progress plan may be set only once")
            self._chunks = {chunk.chunk_id: chunk for chunk in ordered}
            self._completed_ids = set(self._checkpoint_ids) & set(self._chunks)
            self._checkpoint_completed = max(
                self._checkpoint_completed,
                len(self._completed_ids),
            )
            snapshot = self._snapshot(
                phase=RunState.PLANNING,
                determinate=False,
                message="download plan measured",
            )
        emit_progress(snapshot)

    def begin_downloading(self) -> None:
        with self._lock:
            if not self._chunks:
                snapshot = self._snapshot(
                    phase=RunState.DOWNLOADING,
                    determinate=False,
                    message="download plan contains no historical chunks",
                )
            else:
                snapshot = self._snapshot(
                    phase=RunState.DOWNLOADING,
                    determinate=True,
                    message="historical chunk acquisition in progress",
                )
        emit_progress(snapshot)

    def record_committed(
        self,
        chunk_id: str,
        *,
        restored: bool,
        records: int,
        downloaded_bytes: int,
    ) -> None:
        if records < 0 or downloaded_bytes < 0:
            raise ValueError("committed progress counters must be non-negative")
        with self._lock:
            chunk = self._chunks.get(chunk_id)
            if chunk is None or chunk_id in self._observed_ids:
                return
            self._observed_ids.add(chunk_id)
            self._completed_ids.add(chunk_id)
            if restored:
                self._restored_chunks += 1
                self._restored_records += records
            else:
                self._downloaded_chunks += 1
                self._downloaded_records += records
                self._downloaded_bytes += downloaded_bytes
            snapshot = self._snapshot(
                phase=RunState.DOWNLOADING,
                determinate=bool(self._chunks),
                current=chunk,
                message=(
                    "historical chunk restored from immutable raw"
                    if restored
                    else "historical chunk downloaded"
                ),
            )
        emit_progress(snapshot)

    def _snapshot(
        self,
        *,
        phase: RunState,
        determinate: bool,
        message: str,
        current: DownloadChunk | None = None,
    ) -> IngestionProgressSnapshot:
        return IngestionProgressSnapshot(
            phase=phase,
            determinate=determinate,
            planned_chunks=len(self._chunks),
            completed_chunks=len(self._completed_ids),
            total_chunks=len(self._chunks) if determinate else None,
            checkpoint_completed_chunks=self._checkpoint_completed,
            downloaded_chunks=self._downloaded_chunks,
            restored_chunks=self._restored_chunks,
            downloaded_records=self._downloaded_records,
            restored_records=self._restored_records,
            downloaded_bytes=self._downloaded_bytes,
            current_range_start=current.start if current is not None else None,
            current_range_end=current.end if current is not None else None,
            current_data_type=current.data_type if current is not None else None,
            current_timeframe=current.timeframe if current is not None else None,
            message=message,
        )


class ProgressObservingAdapter(ExchangeAdapter):
    """Delegate every exchange behavior while exposing the exact download plan."""

    def __init__(self, delegate: ExchangeAdapter, tracker: DownloadProgressTracker) -> None:
        self._delegate = delegate
        self._tracker = tracker
        self.exchange_id = delegate.exchange_id
        self.adapter_version = delegate.adapter_version

    def get_capabilities(self) -> ExchangeCapabilities:
        return self._delegate.get_capabilities()

    def resolve_instrument(self, market_type: object, symbol: str) -> InstrumentMetadata:
        return self._delegate.resolve_instrument(market_type, symbol)

    def resolve_instrument_with_evidence(
        self, market_type: object, symbol: str
    ) -> InstrumentResolution:
        return self._delegate.resolve_instrument_with_evidence(market_type, symbol)

    def restore_instrument_resolution(
        self, market_type: object, symbol: str, raw_payload: bytes
    ) -> InstrumentResolution:
        return self._delegate.restore_instrument_resolution(market_type, symbol, raw_payload)

    def plan_download(
        self, request: MarketDataRequest, instrument: InstrumentReference
    ) -> DownloadPlan:
        plan = self._delegate.plan_download(request, instrument)
        self._tracker.set_plan(plan.chunks)
        return plan

    def acquire(self, chunk: DownloadChunk, instrument: InstrumentReference) -> AcquiredData:
        return self._delegate.acquire(chunk, instrument)

    def restore(
        self,
        chunk: DownloadChunk,
        instrument: InstrumentReference,
        raw_payload: bytes,
    ) -> AcquiredData:
        return self._delegate.restore(chunk, instrument, raw_payload)


class CommittedChunkProgressHandler(logging.Handler):
    """Consume only the pipeline's public committed-chunk log contract."""

    def __init__(self, tracker: DownloadProgressTracker, *, request_id: str) -> None:
        super().__init__(level=logging.INFO)
        self._tracker = tracker
        self._request_id = request_id

    def emit(self, record: logging.LogRecord) -> None:
        with suppress(Exception):
            message = record.getMessage()
            restored = message == "chunk restored from immutable raw"
            if not restored and message != "chunk downloaded":
                return
            context = getattr(record, "context", None)
            if not isinstance(context, Mapping) or context.get("request_id") != self._request_id:
                return
            chunk_id = context.get("chunk_id")
            if not isinstance(chunk_id, str) or not chunk_id:
                return
            rows_key = "rows_restored" if restored else "rows_downloaded"
            rows = _non_negative_integer(context.get(rows_key), rows_key)
            raw_bytes = _non_negative_integer(
                context.get("downloaded_bytes", 0), "downloaded_bytes"
            )
            self._tracker.record_committed(
                chunk_id,
                restored=restored,
                records=rows,
                downloaded_bytes=raw_bytes,
            )


def progress_logger(handler: logging.Handler, *, name: str) -> logging.Logger:
    logger = logging.Logger(name=name, level=logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    return logger


def _non_negative_integer(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("progress timestamps must be timezone-aware")
    return value.astimezone(UTC)
