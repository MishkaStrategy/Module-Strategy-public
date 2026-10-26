from __future__ import annotations

from typing import Any

from ..paper import PaperRuntimeControlService
from ..service import Clock, utc_now


class EmbeddedPaperRuntimeControl(PaperRuntimeControlService):
    """Bind Control Plane to already-owned live paper runtime objects.

    Owner packages are imported lazily only when this adapter is explicitly
    constructed. The adapter never creates or starts a TradingRunner or venue.
    """

    def __init__(
        self,
        runner: Any,
        execution_coordinator: Any,
        *,
        runtime_service: Any | None = None,
        clock: Clock = utc_now,
    ) -> None:
        from modular_trading_runner.control_query import ReadOnlyTradingRuntimeQuery
        from paper_execution.control_query import ReadOnlyPaperExecutionQuery

        runtime_reader = ReadOnlyTradingRuntimeQuery(
            runner,
            runtime_service=runtime_service,
        )
        execution_reader = ReadOnlyPaperExecutionQuery(execution_coordinator)
        super().__init__(runtime_reader, execution_reader, clock=clock)
