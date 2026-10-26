"""Strategy-independent, closed-candle signal delivery service."""

from .domain import (
    CandleBatch,
    EntryType,
    InstrumentRules,
    SignalStatus,
    StrategyContext,
    StrategyDecision,
    TradeSignal,
)
from .engine import SignalEngine
from .operator_control import (
    SignalBotControlError,
    SignalBotControlErrorCode,
    SignalBotControlLedger,
    SignalBotControlStatus,
    SignalBotOperatorControl,
    TelegramTestNotificationSender,
    TestNotificationRequest,
    TestNotificationResult,
    TrustedSignalBotOperator,
)
from .operator_http import SignalBotControlHttpServer
from .orchestrator import InstrumentBinding, SignalOrchestrator
from .state import (
    DEFAULT_MAX_PENDING_SIGNALS,
    DEFAULT_MAX_TOTAL_SIGNALS,
    SQLiteStateStore,
    StateCapacityError,
    StateTransitionError,
)

__all__ = [
    "CandleBatch",
    "DEFAULT_MAX_PENDING_SIGNALS",
    "DEFAULT_MAX_TOTAL_SIGNALS",
    "EntryType",
    "InstrumentRules",
    "InstrumentBinding",
    "SQLiteStateStore",
    "SignalBotControlError",
    "SignalBotControlErrorCode",
    "SignalBotControlHttpServer",
    "SignalBotControlLedger",
    "SignalBotControlStatus",
    "SignalBotOperatorControl",
    "SignalEngine",
    "SignalOrchestrator",
    "SignalStatus",
    "StateCapacityError",
    "StateTransitionError",
    "StrategyContext",
    "StrategyDecision",
    "TelegramTestNotificationSender",
    "TestNotificationRequest",
    "TestNotificationResult",
    "TradeSignal",
    "TrustedSignalBotOperator",
]
