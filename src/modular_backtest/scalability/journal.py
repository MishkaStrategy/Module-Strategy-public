from __future__ import annotations

import csv
import json
import platform
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from modular_core.audit import Journal, to_primitive
from modular_core.events import EventEnvelope
from modular_core.models import EquityPoint

from ..data import file_sha256
from .buffers import BoundedRecordBuffer
from .config import (
    ARTIFACT_SCHEMA_VERSION,
    ObservabilityLimitExceeded,
    PerformanceConfig,
    RuntimeCounters,
)
from .equity import CompactEquitySeries, EquitySampler
from .events import StreamingEventStore
from .memory import peak_rss_bytes, rss_bytes


_COMPRESSED_ARTIFACT_SCHEMA_VERSION = "3"


def _canonical_skip_reason(reason: str) -> tuple[str, str, str]:
    reason_code, separator, reason_detail = reason.partition(":")
    normalized_detail = reason_detail if separator else ""
    if reason_code == "module_not_ready" and normalized_detail:
        requirements: list[str] = []
        seen: set[str] = set()
        for item in normalized_detail.split(","):
            requirement = item.split(":", 1)[0]
            if requirement and requirement not in seen:
                seen.add(requirement)
                requirements.append(requirement)
        normalized_detail = ",".join(requirements)
    normalized_reason = f"{reason_code}:{normalized_detail}" if normalized_detail else reason_code
    return reason_code, normalized_detail, normalized_reason


@dataclass(slots=True)
class _ActiveSkipRange:
    module: str
    reason: str
    reason_code: str
    reason_detail: str
    symbol: str | None
    first_timestamp: datetime
    last_timestamp: datetime
    first_bar_index: int
    last_bar_index: int
    count: int = 1

    def to_dict(self) -> dict[str, object]:
        return {
            "module": self.module,
            "reason": self.reason,
            "reason_code": self.reason_code,
            "reason_detail": self.reason_detail,
            "symbol": self.symbol,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "first_bar_index": self.first_bar_index,
            "last_bar_index": self.last_bar_index,
            "count": self.count,
        }


class ScalableJournal(Journal):
    """Journal with compact equity, skip ranges, and a disk-backed event stream."""

    def __init__(
        self,
        *,
        run_id: str,
        event_store: StreamingEventStore,
        performance: PerformanceConfig,
    ) -> None:
        super().__init__(run_id=run_id, event_store=event_store)
        if performance.mode == "research_fast":
            event_store.enable_compression("gzip")
        self.performance = performance
        record_window = performance.retained_event_window
        self.setups = cast(Any, BoundedRecordBuffer[dict[str, Any]](record_window))
        self.features = cast(Any, BoundedRecordBuffer[dict[str, Any]](record_window))
        self.signals = cast(Any, BoundedRecordBuffer[dict[str, Any]](record_window))
        self.equity = cast(Any, CompactEquitySeries())
        self.retained_equity = CompactEquitySeries()
        self._equity_sampler = EquitySampler(
            cast(str, performance.equity_sampling), performance.equity_every_n_bars
        )
        self._active_skip_ranges: dict[str, _ActiveSkipRange] = {}
        self._current_bar_index = -1
        self._last_trade_count = 0
        self._skip_range_count = 0
        self._dropped_diagnostics = 0
        self._retained_base_candles = 0
        self._peak_retained_base_candles = 0
        self._retained_timeframe_candles: dict[str, int] = {}
        self._peak_retained_timeframe_candles: dict[str, int] = {}

    @property
    def streaming_store(self) -> StreamingEventStore:
        return cast(StreamingEventStore, self.event_store)

    @property
    def artifact_schema_version(self) -> str:
        if self.streaming_store.compression == "gzip":
            return _COMPRESSED_ARTIFACT_SCHEMA_VERSION
        return ARTIFACT_SCHEMA_VERSION

    @property
    def event_count(self) -> int:
        return self.streaming_store.count

    @property
    def skip_range_count(self) -> int:
        return self._skip_range_count + len(self._active_skip_ranges)

    @property
    def events(self) -> tuple[EventEnvelope, ...]:
        return self.streaming_store.read()

    def set_bar_context(self, index: int) -> None:
        self._current_bar_index = index

    def observe_retention(
        self,
        *,
        base_candles: int,
        timeframe_candles: Mapping[str, int],
    ) -> None:
        self._retained_base_candles = base_candles
        self._peak_retained_base_candles = max(self._peak_retained_base_candles, base_candles)
        self._retained_timeframe_candles = dict(timeframe_candles)
        for timeframe, count in timeframe_candles.items():
            self._peak_retained_timeframe_candles[timeframe] = max(
                self._peak_retained_timeframe_candles.get(timeframe, 0), count
            )

    @property
    def retention_metrics(self) -> dict[str, object]:
        return {
            "retained_events": self.streaming_store.retained_count,
            "current_base_candles": self._retained_base_candles,
            "peak_base_candles": self._peak_retained_base_candles,
            "current_timeframe_candles": dict(self._retained_timeframe_candles),
            "peak_timeframe_candles": dict(self._peak_retained_timeframe_candles),
            "journal_index_entries": len(self._last_event_by_entity),
        }

    def _remember_entity_event(self, entity_key: str | None, event_id: str) -> None:
        if entity_key is None:
            return
        if self.performance.mode == "research_fast" and entity_key.startswith("feature:"):
            return
        self._last_event_by_entity[entity_key] = event_id

    def emit_event(
        self,
        *,
        timestamp: datetime,
        event_type: str,
        source: str,
        payload: dict[str, Any],
        correlation_id: str | None = None,
        causation_id: str | None = None,
        symbol: str | None = None,
        entity_key: str | None = None,
    ) -> EventEnvelope:
        if (
            self.performance.max_events is not None
            and self.event_count >= self.performance.max_events
        ):
            raise ObservabilityLimitExceeded(f"max_events exceeded: {self.performance.max_events}")
        next_sequence = self._event_sequence + 1
        event = EventEnvelope(
            event_id=f"evt-{next_sequence:010d}",
            run_id=self.run_id,
            sequence=next_sequence,
            timestamp=timestamp,
            event_type=event_type,
            source=source,
            payload=payload,
            correlation_id=correlation_id,
            causation_id=causation_id,
            symbol=symbol,
            entity_key=entity_key,
        )
        self.event_store.append(event)
        self._event_sequence = next_sequence
        self._remember_entity_event(entity_key, event.event_id)
        return event

    def _next_event(
        self,
        *,
        offset: int,
        timestamp: datetime,
        event_type: str,
        source: str,
        payload: dict[str, Any],
        correlation_id: str | None = None,
        causation_id: str | None = None,
        symbol: str | None = None,
        entity_key: str | None = None,
    ) -> EventEnvelope:
        sequence = self._event_sequence + offset
        return EventEnvelope(
            event_id=f"evt-{sequence:010d}",
            run_id=self.run_id,
            sequence=sequence,
            timestamp=timestamp,
            event_type=event_type,
            source=source,
            payload=payload,
            correlation_id=correlation_id,
            causation_id=causation_id,
            symbol=symbol,
            entity_key=entity_key,
        )

    def _commit_event_batch(self, events: tuple[EventEnvelope, ...]) -> None:
        if not events:
            return
        if (
            self.performance.max_events is not None
            and self.event_count + len(events) > self.performance.max_events
        ):
            raise ObservabilityLimitExceeded(f"max_events exceeded: {self.performance.max_events}")
        self.event_store.append_many(events)
        self._event_sequence = events[-1].sequence
        for event in events:
            self._remember_entity_event(event.entity_key, event.event_id)

    def log_skip(
        self,
        timestamp: datetime,
        module: str,
        reason: str,
        *,
        symbol: str | None = None,
    ) -> None:
        reason_code, normalized_detail, normalized_reason = _canonical_skip_reason(reason)
        current = self._active_skip_ranges.get(module)
        if (
            current is not None
            and current.reason == normalized_reason
            and current.symbol == symbol
            and self._current_bar_index == current.last_bar_index + 1
        ):
            current.last_timestamp = timestamp
            current.last_bar_index = self._current_bar_index
            current.count += 1
            return
        self.close_skip_range(module, timestamp)
        self._active_skip_ranges[module] = _ActiveSkipRange(
            module=module,
            reason=normalized_reason,
            reason_code=reason_code,
            reason_detail=normalized_detail,
            symbol=symbol,
            first_timestamp=timestamp,
            last_timestamp=timestamp,
            first_bar_index=self._current_bar_index,
            last_bar_index=self._current_bar_index,
        )

    def close_skip_range(self, module: str, timestamp: datetime | None = None) -> None:
        del timestamp
        active = self._active_skip_ranges.pop(module, None)
        if active is None:
            return
        if (
            self.performance.max_skip_ranges is not None
            and self._skip_range_count >= self.performance.max_skip_ranges
        ):
            raise ObservabilityLimitExceeded(
                f"max_skip_ranges exceeded: {self.performance.max_skip_ranges}"
            )
        row = active.to_dict()
        event = self.emit_event(
            timestamp=active.last_timestamp,
            event_type="module.skip_range",
            source=active.module,
            payload={"skip_range": to_primitive(row)},
            symbol=active.symbol,
        )
        row["event_id"] = event.event_id
        self.skips.append(row)
        self._skip_range_count += 1

    def close_all_skip_ranges(self, timestamp: datetime | None = None) -> None:
        for module in tuple(self._active_skip_ranges):
            self.close_skip_range(module, timestamp)

    def log_diagnostics(
        self,
        timestamp: datetime,
        module: str,
        values: dict[str, object],
    ) -> None:
        if not values:
            return
        if self.performance.mode == "research_fast":
            self._dropped_diagnostics += 1
            return
        if (
            self.performance.max_diagnostics is not None
            and len(self.diagnostics) >= self.performance.max_diagnostics
        ):
            raise ObservabilityLimitExceeded(
                f"max_diagnostics exceeded: {self.performance.max_diagnostics}"
            )
        super().log_diagnostics(timestamp, module, values)

    def record_equity(self, point: EquityPoint, *, symbol: str | None = None) -> None:
        del symbol
        if (
            self.performance.max_equity_points is not None
            and len(self.equity) >= self.performance.max_equity_points
        ):
            raise ObservabilityLimitExceeded(
                f"max_equity_points exceeded: {self.performance.max_equity_points}"
            )
        cast(CompactEquitySeries, self.equity).append(point)
        trade_changed = len(self.trades) != self._last_trade_count
        if self._equity_sampler.accept(
            point,
            bar_index=self._current_bar_index,
            trade_count_changed=trade_changed,
        ):
            self.retained_equity.append(point)
        self._last_trade_count = len(self.trades)

    def finalize_observability(self) -> None:
        self.close_all_skip_ranges()
        if self.equity:
            final = self.equity[-1]
            if self.retained_equity and self.retained_equity[-1].timestamp == final.timestamp:
                self.retained_equity[-1] = final
            elif not self.retained_equity or self.retained_equity[-1] != final:
                self.retained_equity.append(final)
        self.streaming_store.flush()

    def counters(self, bars_processed: int) -> RuntimeCounters:
        return RuntimeCounters(
            bars_processed=bars_processed,
            events=self.event_count,
            skip_ranges=self.skip_range_count,
            equity_points=len(self.equity),
            retained_equity_points=len(self.retained_equity),
            diagnostics=len(self.diagnostics),
            trades=len(self.trades),
            rss_bytes=rss_bytes(),
            peak_rss_bytes=peak_rss_bytes(),
            retained_events=self.streaming_store.retained_count,
            retained_base_candles=self._retained_base_candles,
            retained_timeframe_candles=sum(self._retained_timeframe_candles.values()),
            journal_index_entries=len(self._last_event_by_entity),
        )

    def export_directory(self, path: str | Path) -> Path:
        self.finalize_observability()
        output = Path(path)
        output.mkdir(parents=True, exist_ok=True)
        store = self.streaming_store
        event_path = store.artifact_name
        schema_version = self.artifact_schema_version
        payload = {
            "schema_version": schema_version,
            "event_stream": {
                "path": event_path,
                "encoding": store.encoding,
                "count": self.event_count,
                "bytes": store.bytes_written,
                "logical_bytes": store.logical_bytes_written,
                "semantic_sha256": store.semantic_sha256,
                "counts_by_type": store.counts_by_type,
                "retained_window": len(self.events),
            },
            "retained_record_window": self.performance.retained_event_window,
            "setups": list(self.setups),
            "setup_records": self.setup_records,
            "features": list(self.features),
            "signals": list(self.signals),
            "skip_ranges": self.skips,
            "diagnostics": self.diagnostics,
            "orders": self.orders,
            "fills": self.fills,
            "trades": self.trades,
            "equity": {
                "exact_points": len(self.equity),
                "retained_points": len(self.retained_equity),
                "sampling_policy": self.performance.equity_sampling,
                "path": "equity.csv",
            },
            "errors": self.errors,
            "dropped_diagnostics": self._dropped_diagnostics,
            "retention": self.retention_metrics,
        }
        (output / "journal.json").write_text(
            json.dumps(
                to_primitive(payload),
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ),
            encoding="utf-8",
        )
        store.finalize_to(output / event_path)
        self._write_rows(output / "trades.csv", (to_primitive(x) for x in self.trades))
        self._write_rows(
            output / "equity.csv",
            (to_primitive(x) for x in self.retained_equity),
        )
        environment = {
            "python": sys.version,
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "artifact_schema_version": schema_version,
            "performance_mode": self.performance.mode,
        }
        (output / "environment.json").write_text(
            json.dumps(environment, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        checksums: list[str] = []
        for artifact in sorted(
            item for item in output.iterdir() if item.is_file() and item.name != "SHA256SUMS.txt"
        ):
            digest = file_sha256(artifact)
            checksums.append(f"{digest}  {artifact.name}")
        (output / "SHA256SUMS.txt").write_text(
            "\n".join(checksums) + "\n",
            encoding="utf-8",
        )
        return output

    @staticmethod
    def _write_rows(path: Path, rows: Iterable[Mapping[str, object]]) -> None:
        iterator = iter(rows)
        try:
            first = dict(next(iterator))
        except StopIteration:
            path.write_text("", encoding="utf-8")
            return
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(first.keys()))
            writer.writeheader()
            writer.writerow(first)
            for row in iterator:
                writer.writerow(dict(row))
