from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType

from modular_core.audit import Journal
from modular_core.config import EngineConfig
from modular_core.execution import BrokerSimulator
from modular_core.instruments import InstrumentSpec
from modular_core.models import Candle, PositionSide

from .funding import (
    FundingDataError,
    FundingEvent,
    FundingSettlement,
    FundingSettlementLedger,
    RegisteredFundingSource,
)


FUNDING_RUNTIME_MODEL_VERSION = "registered-owner-funding-runtime-v2"


class FundingSettlementError(ValueError):
    """Raised when owner-backed funding cannot be settled deterministically."""


class FundingAwareBrokerSimulator(BrokerSimulator):
    """BrokerSimulator that settles registered funding before same-time bar execution.

    The wrapper deliberately lives in the Backtest application layer. Core keeps one
    execution API and one authoritative broker state. Because the broker itself is
    part of ``ScalableRuntimeState``, the funding source, cursor and cumulative
    cashflow are serialized by the existing direct runtime snapshot path.
    """

    def __init__(
        self,
        config: EngineConfig,
        journal: Journal,
        instrument: InstrumentSpec,
        *,
        funding_source: RegisteredFundingSource,
    ) -> None:
        super().__init__(config, journal, instrument)
        if funding_source.symbol != self.instrument.symbol:
            raise FundingSettlementError("funding source symbol does not match broker instrument")
        contract_size = _decimal_from_float(
            self.instrument.contract_size,
            "instrument contract_size",
        )
        if contract_size <= 0:
            raise FundingSettlementError("instrument contract_size must be positive")
        self.funding_source = funding_source
        self.funding_ledger = FundingSettlementLedger(funding_source)
        self._funding_started = False
        self._funding_events_journaled = 0

    @property
    def funding_cursor(self) -> int:
        return self.funding_ledger.next_ordinal

    @property
    def cumulative_funding_cashflow(self) -> Decimal:
        return self.funding_ledger.cumulative_cashflow

    def funding_state(self) -> Mapping[str, object]:
        """Return sanitized deterministic runtime evidence for publication/tests."""

        return MappingProxyType(
            {
                "model_version": FUNDING_RUNTIME_MODEL_VERSION,
                "source_identity_sha256": self.funding_source.identity_sha256(),
                "source": dict(self.funding_source.identity()),
                "ledger": dict(self.funding_ledger.snapshot()),
                "events_journaled": self._funding_events_journaled,
            }
        )

    def process_bar(self, bar: Candle, index: int) -> None:
        if bar.symbol != self.funding_source.symbol:
            raise FundingSettlementError("funding source symbol differs from processed candle")
        if (
            bar.timestamp < self.funding_source.coverage_start_inclusive
            or bar.timestamp >= self.funding_source.coverage_end_exclusive
        ):
            raise FundingSettlementError(
                "funding source coverage does not include processed candle open"
            )
        self._settle_before_bar_execution(bar.timestamp)
        super().process_bar(bar, index)

    def assert_funding_complete_through(self, timestamp: datetime) -> None:
        """Fail when an owner event at/before ``timestamp`` remains unsettled."""

        event = self._next_funding_event()
        if event is not None and event.timestamp <= timestamp:
            raise FundingSettlementError(
                "funding source contains an unsettled event inside processed time range"
            )

    def _settle_before_bar_execution(self, timestamp: datetime) -> None:
        if not self._funding_started:
            self._consume_prestart_events(timestamp)
            self._funding_started = True

        event = self._next_funding_event()
        if event is None:
            return
        if event.timestamp < timestamp:
            raise FundingSettlementError(
                "funding event does not align with a processed candle-open boundary"
            )
        if event.timestamp != timestamp:
            return
        self._settle_event(event)

    def _consume_prestart_events(self, timestamp: datetime) -> None:
        """Advance irrelevant pre-run events while the account is necessarily flat.

        This allows a registered owner dataset to cover a superset of the candle
        interval without emitting journal events whose timestamps predate run.started.
        Once the first bar is processed, any later missed funding boundary is fatal.
        """

        event = self._next_funding_event()
        while event is not None and event.timestamp < timestamp:
            self._consume_flat(event)
            event = self._next_funding_event()

    def _next_funding_event(self) -> FundingEvent | None:
        cursor = self.funding_ledger.next_ordinal
        if cursor >= self.funding_source.event_count:
            return None
        return self.funding_source.events[cursor]

    def _settle_event(self, event: FundingEvent) -> None:
        position = self.portfolio.position
        position_id: str | None = None
        position_side: PositionSide | None = None
        position_quantity = Decimal("0")
        contract_size = _decimal_from_float(
            self.instrument.contract_size,
            "instrument contract_size",
        )

        if position is None:
            settlement = self._consume_flat(event)
        else:
            position_id = position.id
            position_side = position.side
            position_quantity = _decimal_from_float(position.quantity, "position quantity")
            if position_quantity <= 0:
                raise FundingSettlementError("position quantity must be positive")
            effective_quantity = position_quantity * contract_size
            try:
                applied = self.funding_ledger.apply(
                    event,
                    side=position.side.value,
                    quantity=effective_quantity,
                )
            except FundingDataError as exc:
                raise FundingSettlementError(str(exc)) from exc
            if applied is None:
                raise FundingSettlementError("funding event was unexpectedly replayed")
            settlement = applied
            projected_cash = _project_cash(
                self.portfolio.cash,
                settlement.cashflow,
            )
            projected_historical_closed_equity: float | None = None
            if self.config.risk.sizing_model == "historical_limit_cost":
                projected_historical_closed_equity = _project_cash(
                    self._historical_closed_equity,
                    settlement.cashflow,
                )
            self.portfolio.cash = projected_cash
            if projected_historical_closed_equity is not None:
                self._historical_closed_equity = projected_historical_closed_equity

        payload = settlement.to_payload()
        payload.update(
            {
                "model_version": FUNDING_RUNTIME_MODEL_VERSION,
                "source_identity_sha256": self.funding_source.identity_sha256(),
                "dataset_id": self.funding_source.dataset_id,
                "dataset_version": self.funding_source.dataset_version,
                "dataset_hash": self.funding_source.dataset_hash,
                "position_id": position_id,
                "position_side": position_side.value if position_side is not None else None,
                "position_quantity": str(position_quantity),
                "contract_size": str(contract_size),
            }
        )
        self.journal.emit_event(
            timestamp=event.timestamp,
            event_type="portfolio.funding",
            source="backtest_funding",
            payload={"funding": payload},
            symbol=event.symbol,
            entity_key=f"funding:{self.funding_source.identity_sha256()}:{event.ordinal}",
        )
        self._funding_events_journaled += 1

    def _consume_flat(self, event: FundingEvent) -> FundingSettlement:
        cursor = self.funding_ledger.next_ordinal
        if event.ordinal != cursor:
            raise FundingSettlementError("funding settlement sequence has a gap")
        if event.ordinal >= self.funding_source.event_count:
            raise FundingSettlementError("funding event ordinal exceeds immutable source")
        if self.funding_source.events[event.ordinal] != event:
            raise FundingSettlementError("funding event differs from immutable source")

        settlement = FundingSettlement(
            event_key=event.key(self.funding_source.identity_sha256()),
            ordinal=event.ordinal,
            timestamp=event.timestamp,
            symbol=event.symbol,
            side="flat",
            quantity=Decimal("0"),
            notional=Decimal("0"),
            rate=event.rate,
            cashflow=Decimal("0"),
        )
        self.funding_ledger = FundingSettlementLedger(
            self.funding_source,
            next_ordinal=cursor + 1,
            cumulative_cashflow=self.funding_ledger.cumulative_cashflow,
        )
        return settlement


def _decimal_from_float(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise FundingSettlementError(f"{name} must be a finite number")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise FundingSettlementError(f"{name} must be a finite number")
    return Decimal(str(numeric))


def _project_cash(current_cash: float, cashflow: Decimal) -> float:
    """Project exact Decimal funding cashflow into Core's finite float cash ledger."""

    current = _decimal_from_float(current_cash, "portfolio cash")
    projected = float(current + cashflow)
    if not math.isfinite(projected):
        raise FundingSettlementError("funding cash projection is not finite")
    return projected
