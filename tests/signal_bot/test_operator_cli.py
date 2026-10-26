from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import signal_bot.cli as cli


class Strategy:
    strategy_id = "safe_strategy"
    version = "1.0.0"
    required_timeframes = ("5m",)

    def warmup_bars(self, timeframe: str) -> int:
        return 1

    def evaluate(self, context):
        raise NotImplementedError


def config_file(path: Path) -> Path:
    path.write_text(
        """
state: {database: state.db}
charts: {theme: dark}
instruments:
  BTCUSDT:
    enabled: true
    market: binance_usdt_m_perpetual
    tick_size: "0.1"
    step_size: "0.001"
    strategy: {id: safe_strategy, version: "1.0.0", parameters: {}}
    notifications: {chart_enabled: false}
""",
        encoding="utf-8",
    )
    return path


def test_cli_rejects_partial_signal_control_credentials(tmp_path: Path) -> None:
    config = config_file(tmp_path / "config.yaml")
    with (
        patch.object(sys, "argv", ["signal-bot", "--config", str(config), "--once"]),
        patch.dict(
            "os.environ",
            {
                "TELEGRAM_BOT_TOKEN": "telegram-token",
                "TELEGRAM_CHAT_ID": "42",
                "TELEGRAM_ALLOWED_CHAT_IDS": "42",
                "TELEGRAM_ADMIN_CHAT_IDS": "42",
                "SIGNAL_BOT_CONTROL_READER_TOKEN": "reader-token",
                "SIGNAL_BOT_CONTROL_OPERATOR_TOKEN": "",
            },
            clear=False,
        ),
        pytest.raises(SystemExit),
    ):
        cli.main()


def test_cli_daemon_composes_and_closes_owner_control_transport(tmp_path: Path) -> None:
    config = config_file(tmp_path / "config.yaml")
    fake_state = MagicMock()
    fake_state.notifications_enabled.return_value = True
    fake_registry = MagicMock()
    fake_registry.create.return_value = Strategy()
    fake_orchestrator = MagicMock()
    fake_thread = MagicMock()
    fake_health = MagicMock()
    fake_ledger = MagicMock()
    fake_owner_control = MagicMock()
    fake_operator_server = MagicMock()
    fake_bot_client = MagicMock()
    with (
        patch.object(sys, "argv", ["signal-bot", "--config", str(config)]),
        patch.dict(
            "os.environ",
            {
                "TELEGRAM_BOT_TOKEN": "telegram-token",
                "TELEGRAM_CHAT_ID": "42",
                "TELEGRAM_ALLOWED_CHAT_IDS": "42",
                "TELEGRAM_ADMIN_CHAT_IDS": "42",
                "SIGNAL_BOT_CONTROL_READER_TOKEN": "reader-token",
                "SIGNAL_BOT_CONTROL_OPERATOR_TOKEN": "operator-token",
                "SIGNAL_BOT_CONTROL_HOST": "127.0.0.1",
                "SIGNAL_BOT_CONTROL_PORT": "18081",
            },
            clear=False,
        ),
        patch.object(cli, "SQLiteStateStore", return_value=fake_state),
        patch.object(cli, "BinanceFuturesMarketData", return_value=MagicMock()),
        patch.object(cli, "TelegramBotClient", return_value=fake_bot_client),
        patch.object(cli, "TelegramNotifier"),
        patch.object(cli, "MatplotlibChartRenderer"),
        patch.object(cli, "SignalEngine"),
        patch.object(cli, "StrategyRegistry", return_value=fake_registry),
        patch.object(cli, "SignalOrchestrator", return_value=fake_orchestrator),
        patch.object(cli, "TelegramControlLoop"),
        patch.object(cli.threading, "Thread", return_value=fake_thread),
        patch.object(cli, "HealthServer", return_value=fake_health),
        patch.object(cli, "SignalBotControlLedger", return_value=fake_ledger) as ledger_factory,
        patch.object(
            cli, "SignalBotOperatorControl", return_value=fake_owner_control
        ) as control_factory,
        patch.object(
            cli, "SignalBotControlHttpServer", return_value=fake_operator_server
        ) as server_factory,
        patch.object(cli, "TelegramTestNotificationSender") as sender_factory,
    ):
        cli.main()

    ledger_factory.assert_called_once()
    control_kwargs = control_factory.call_args.kwargs
    assert control_kwargs["ledger"] is fake_ledger
    assert control_kwargs["health"] is fake_orchestrator.health
    assert control_kwargs["configured"] is True
    assert control_kwargs["notifications_enabled"]() is True
    fake_state.notifications_enabled.assert_called_with("42")
    sender_factory.assert_called_once_with(fake_bot_client, "42")
    server_factory.assert_called_once_with(
        fake_owner_control,
        reader_token="reader-token",
        operator_token="operator-token",
        host="127.0.0.1",
        port=18081,
    )
    fake_operator_server.start.assert_called_once()
    fake_operator_server.close.assert_called_once()
    fake_ledger.close.assert_called_once()
    fake_state.close.assert_called_once()


def test_cli_once_does_not_start_control_transport_even_when_credentials_exist(
    tmp_path: Path,
) -> None:
    config = config_file(tmp_path / "config.yaml")
    fake_state = MagicMock()
    fake_registry = MagicMock()
    fake_registry.create.return_value = Strategy()
    fake_orchestrator = MagicMock()
    with (
        patch.object(sys, "argv", ["signal-bot", "--config", str(config), "--once"]),
        patch.dict(
            "os.environ",
            {
                "TELEGRAM_BOT_TOKEN": "telegram-token",
                "TELEGRAM_CHAT_ID": "42",
                "TELEGRAM_ALLOWED_CHAT_IDS": "42",
                "TELEGRAM_ADMIN_CHAT_IDS": "42",
                "SIGNAL_BOT_CONTROL_READER_TOKEN": "reader-token",
                "SIGNAL_BOT_CONTROL_OPERATOR_TOKEN": "operator-token",
            },
            clear=False,
        ),
        patch.object(cli, "SQLiteStateStore", return_value=fake_state),
        patch.object(cli, "BinanceFuturesMarketData", return_value=MagicMock()),
        patch.object(cli, "TelegramBotClient"),
        patch.object(cli, "TelegramNotifier"),
        patch.object(cli, "MatplotlibChartRenderer"),
        patch.object(cli, "SignalEngine"),
        patch.object(cli, "StrategyRegistry", return_value=fake_registry),
        patch.object(cli, "SignalOrchestrator", return_value=fake_orchestrator),
        patch.object(cli, "SignalBotControlHttpServer") as server_factory,
    ):
        cli.main()

    fake_orchestrator.run_cycle.assert_called_once()
    server_factory.assert_not_called()
    fake_state.close.assert_called_once()
