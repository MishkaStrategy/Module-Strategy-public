from __future__ import annotations

import argparse
import logging
import os
import threading

from .adapters import BinanceFuturesMarketData, TelegramBotClient, TelegramNotifier
from .charts import MatplotlibChartRenderer
from .config import load_config
from .control import (
    PreferenceAwareNotifier,
    TelegramLifecycleNotifier,
    TelegramCommandService,
    TelegramControlLoop,
)
from .engine import SignalEngine
from .health import HealthServer
from .operator_control import (
    SignalBotControlLedger,
    SignalBotOperatorControl,
    TelegramTestNotificationSender,
)
from .operator_http import SignalBotControlHttpServer
from .orchestrator import InstrumentBinding, SignalOrchestrator
from .registry import StrategyRegistry
from .state import SQLiteStateStore

LOGGER = logging.getLogger(__name__)


def _chat_ids(value: str) -> frozenset[str]:
    ids = frozenset(item.strip() for item in value.split(",") if item.strip())
    if not ids or any(not item.lstrip("-").isdigit() for item in ids):
        raise RuntimeError("Telegram chat ID allowlists must contain canonical numeric IDs")
    return ids


def _optional_secret(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one closed-candle signal-bot cycle")
    parser.add_argument(
        "--config",
        default=os.environ.get("SIGNAL_BOT_CONFIG", "configs/signal_bot.example.yaml"),
    )
    parser.add_argument("--once", action="store_true", help="Run one polling cycle and exit")
    parser.add_argument("--poll-seconds", type=float, default=15.0)
    parser.add_argument("--health-host", default="127.0.0.1")
    parser.add_argument("--health-port", type=int, default=8080)
    parser.add_argument(
        "--control-host",
        default=os.environ.get("SIGNAL_BOT_CONTROL_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--control-port",
        type=int,
        default=int(os.environ.get("SIGNAL_BOT_CONTROL_PORT", "8081")),
    )
    args = parser.parse_args()
    config = load_config(args.config)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s component=%(name)s operation=%(message)s",
    )
    if not config.enabled:
        LOGGER.info("signal_bot_disabled result=clean_exit")
        return
    token, chat_id = (
        os.environ.get("TELEGRAM_BOT_TOKEN"),
        os.environ.get("TELEGRAM_CHAT_ID"),
    )
    if not token or not chat_id:
        parser.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
    control_reader_token = _optional_secret(os.environ.get("SIGNAL_BOT_CONTROL_READER_TOKEN"))
    control_operator_token = _optional_secret(os.environ.get("SIGNAL_BOT_CONTROL_OPERATOR_TOKEN"))
    health_token = _optional_secret(os.environ.get("SIGNAL_BOT_HEALTH_TOKEN"))
    if (control_reader_token is None) != (control_operator_token is None):
        parser.error(
            "SIGNAL_BOT_CONTROL_READER_TOKEN and SIGNAL_BOT_CONTROL_OPERATOR_TOKEN "
            "must be configured together"
        )
    allowed_ids = _chat_ids(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", ""))
    admin_ids = _chat_ids(os.environ.get("TELEGRAM_ADMIN_CHAT_IDS", ""))
    if chat_id not in allowed_ids:
        raise RuntimeError("TELEGRAM_CHAT_ID must be in TELEGRAM_ALLOWED_CHAT_IDS")
    if not admin_ids <= allowed_ids:
        raise RuntimeError("Telegram admin IDs must be a subset of allowed IDs")
    state = SQLiteStateStore(config.database)
    operator_ledger: SignalBotControlLedger | None = None
    operator_server: SignalBotControlHttpServer | None = None
    health_server: HealthServer | None = None
    try:
        market_data = BinanceFuturesMarketData()
        bot_client = TelegramBotClient(token)
        notifier = PreferenceAwareNotifier(TelegramNotifier(token, chat_id), state, chat_id)
        engine = SignalEngine(
            market_data=market_data,
            state=state,
            renderer=MatplotlibChartRenderer(theme=config.chart_theme),
            notifier=notifier,
            lifecycle_notifier=TelegramLifecycleNotifier(bot_client, state, chat_id),
        )
        registry = StrategyRegistry()
        registry.discover()
        bindings = []
        for item in config.instruments:
            if not item.enabled:
                continue
            try:
                market_data.verify_instrument(item.rules)
                strategy = registry.create(
                    item.strategy.strategy_id,
                    item.strategy.version,
                    item.strategy.parameters,
                )
                bindings.append(InstrumentBinding(item.rules, strategy, item.chart_enabled))
            except Exception:
                LOGGER.exception(
                    "pair_load_failed symbol=%s strategy_id=%s result=failed",
                    item.rules.symbol,
                    item.strategy.strategy_id,
                )
        if not bindings:
            raise RuntimeError("no enabled and valid strategy bindings")
        orchestrator = SignalOrchestrator(engine, bindings, poll_seconds=args.poll_seconds)
        if args.once:
            orchestrator.run_cycle()
        else:
            health_server = HealthServer(
                orchestrator.health,
                host=args.health_host,
                port=args.health_port,
                auth_token=health_token,
            )
            if control_reader_token is not None and control_operator_token is not None:
                operator_ledger = SignalBotControlLedger(config.database)
                owner_control = SignalBotOperatorControl(
                    ledger=operator_ledger,
                    health=orchestrator.health,
                    notifications_enabled=lambda: state.notifications_enabled(chat_id),
                    sender=TelegramTestNotificationSender(bot_client, chat_id),
                    configured=True,
                )
                operator_server = SignalBotControlHttpServer(
                    owner_control,
                    reader_token=control_reader_token,
                    operator_token=control_operator_token,
                    host=args.control_host,
                    port=args.control_port,
                )
                operator_server.start()
            control = TelegramControlLoop(
                bot_client,
                TelegramCommandService(orchestrator, state, admin_ids, allowed_ids, chat_id),
            )
            control_thread = threading.Thread(
                target=control.run_forever, name="telegram-control", daemon=True
            )
            try:
                control_thread.start()
                health_server.start()
                orchestrator.run_forever()
            finally:
                control.stop()
                if control_thread.ident is not None:
                    control_thread.join(timeout=5)
                health_server.close()
                health_server = None
    finally:
        if health_server is not None:
            health_server.close()
        if operator_server is not None:
            operator_server.close()
        if operator_ledger is not None:
            operator_ledger.close()
        state.close()


if __name__ == "__main__":
    main()
