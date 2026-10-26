from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .enums import RuntimeState
from .errors import CriticalQueueOverflow, MarketDataRejected
from .market_data import (
    BoundedCriticalQueue,
    MarketDataPolicy,
    MarketEventEnvelope,
    MarketEventStatus,
    MarketEventValidator,
    MarketValidationResult,
)
from .pipeline import CoreMarketPipeline, PipelineBatch
from .runtime import TradingRunner

_INGESTION_STATES = frozenset(
    {
        RuntimeState.RUNNING,
        RuntimeState.DEGRADED,
        RuntimeState.KILL_SWITCH_ACTIVE,
    }
)


@dataclass(frozen=True, slots=True)
class MarketProcessResult:
    validation: MarketValidationResult
    batch: PipelineBatch | None
    entry_allowed: bool


class TradingRuntimeService:
    """Lifecycle-bound market ingestion facade around the paper-only TradingRunner."""

    def __init__(
        self,
        *,
        runner: TradingRunner,
        policy: MarketDataPolicy,
        validator: MarketEventValidator,
        pipeline: CoreMarketPipeline,
    ) -> None:
        if not isinstance(runner, TradingRunner):
            raise TypeError("runner must be TradingRunner")
        if not isinstance(policy, MarketDataPolicy):
            raise TypeError("policy must be MarketDataPolicy")
        if not isinstance(validator, MarketEventValidator):
            raise TypeError("validator must be MarketEventValidator")
        if not isinstance(pipeline, CoreMarketPipeline):
            raise TypeError("pipeline must be CoreMarketPipeline")
        expected = MarketDataPolicy.from_runner_config(runner.config)
        if policy != expected:
            raise ValueError("market policy must exactly match the immutable runner configuration")
        if validator.policy != policy:
            raise ValueError("validator policy must match runner policy")
        self.runner = runner
        self.policy = policy
        self.validator = validator
        self.pipeline = pipeline
        self.queue: BoundedCriticalQueue[MarketEventEnvelope] = BoundedCriticalQueue(
            runner.config.max_queue_size
        )

    def enqueue_market_event(self, event: MarketEventEnvelope) -> None:
        if self.runner.state not in _INGESTION_STATES:
            raise MarketDataRejected(
                f"market ingestion is forbidden in {self.runner.state.value}",
                reason_code="runtime_not_running",
            )
        try:
            self.queue.put(event)
        except CriticalQueueOverflow:
            self._degrade()
            self._audit(
                "market.queue_overflow",
                {
                    "depth": self.queue.depth,
                    "capacity": self.queue.capacity,
                    "event_id": event.event_id,
                },
            )
            raise
        self._audit(
            "market.enqueued",
            {
                "event_id": event.event_id,
                "queue_depth": self.queue.depth,
                "queue_high_water_mark": self.queue.high_water_mark,
            },
        )

    def process_next_market_event(self) -> MarketProcessResult:
        if self.runner.state not in _INGESTION_STATES:
            raise MarketDataRejected(
                f"market processing is forbidden in {self.runner.state.value}",
                reason_code="runtime_not_ready",
            )
        event = self.queue.get()
        try:
            validation = self.validator.inspect(event, now=self.runner.clock.now())
        except MarketDataRejected as exc:
            self._degrade()
            self._audit(
                "market.rejected",
                {"event_id": event.event_id, "reason_code": exc.reason_code},
            )
            raise
        if validation.status is MarketEventStatus.DUPLICATE:
            self._audit("market.duplicate", {"event_id": event.event_id})
            return MarketProcessResult(
                validation=validation,
                batch=None,
                entry_allowed=self._entry_allowed(),
            )
        if validation.data_gap:
            self._degrade()
            self._audit(
                "market.gap",
                {
                    "event_id": event.event_id,
                    "sequence_gap": validation.sequence_gap,
                    "time_gap": validation.time_gap,
                },
            )
        try:
            batch = self.pipeline.process(event, data_gap=validation.data_gap)
        except Exception as exc:
            self._degrade()
            self._audit(
                "market.pipeline_failed",
                {
                    "event_id": event.event_id,
                    "error_type": type(exc).__name__,
                },
            )
            raise
        self.validator.commit(event)
        self._audit(
            "market.processed",
            {
                "event_id": event.event_id,
                "module_count": len(batch.module_names),
                "data_gap": batch.data_gap,
                "features_invalidated": batch.features_invalidated,
            },
        )
        return MarketProcessResult(
            validation=validation,
            batch=batch,
            entry_allowed=self._entry_allowed() and not validation.data_gap,
        )

    def process_market_queue(self, *, limit: int | None = None) -> tuple[MarketProcessResult, ...]:
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise TypeError("limit must be int or None")
            if limit < 1:
                raise ValueError("limit must be positive")
        results: list[MarketProcessResult] = []
        while self.queue.depth and (limit is None or len(results) < limit):
            results.append(self.process_next_market_event())
        return tuple(results)

    def _entry_allowed(self) -> bool:
        return self.runner.state is RuntimeState.RUNNING and not self.runner.kill_switch.active

    def _degrade(self) -> None:
        if self.runner.state is RuntimeState.RUNNING:
            self.runner.lifecycle.transition(RuntimeState.DEGRADED)

    def _audit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        self.runner.journal.append(
            timestamp=self.runner.clock.now(),
            event_type=event_type,
            aggregate_id=self.runner.config.runtime_id,
            payload=payload,
        )
