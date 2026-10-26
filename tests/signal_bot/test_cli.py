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


def test_chat_id_parser_is_fail_closed() -> None:
    assert cli._chat_ids("42,-100") == frozenset({"42", "-100"})
    with pytest.raises(RuntimeError):
        cli._chat_ids("")
    with pytest.raises(RuntimeError):
        cli._chat_ids("42,not-a-chat")


def test_cli_once_uses_registry_and_chart_flag(tmp_path: Path) -> None:
    config = config_file(tmp_path / "config.yaml")
    fake_state = MagicMock()
    fake_registry = MagicMock()
    fake_registry.create.return_value = Strategy()
    fake_market = MagicMock()
    fake_orchestrator = MagicMock()
    with (
        patch.object(sys, "argv", ["signal-bot", "--config", str(config), "--once"]),
        patch.dict(
            "os.environ",
            {
                "TELEGRAM_BOT_TOKEN": "test-token",
                "TELEGRAM_CHAT_ID": "42",
                "TELEGRAM_ALLOWED_CHAT_IDS": "42,43",
                "TELEGRAM_ADMIN_CHAT_IDS": "42",
            },
            clear=False,
        ),
        patch.object(cli, "SQLiteStateStore", return_value=fake_state),
        patch.object(cli, "BinanceFuturesMarketData", return_value=fake_market),
        patch.object(cli, "TelegramBotClient"),
        patch.object(cli, "TelegramNotifier"),
        patch.object(cli, "MatplotlibChartRenderer"),
        patch.object(cli, "SignalEngine"),
        patch.object(cli, "StrategyRegistry", return_value=fake_registry),
        patch.object(cli, "SignalOrchestrator", return_value=fake_orchestrator) as factory,
    ):
        cli.main()
    fake_registry.discover.assert_called_once()
    fake_registry.create.assert_called_once_with("safe_strategy", "1.0.0", {})
    binding = factory.call_args.args[1][0]
    assert binding.chart_enabled is False
    fake_orchestrator.run_cycle.assert_called_once()
    fake_state.close.assert_called_once()


def test_cli_disabled_exits_before_secrets_and_runtime_construction(tmp_path: Path, caplog) -> None:
    config = config_file(tmp_path / "disabled.yaml")
    config.write_text("enabled: false\n" + config.read_text(encoding="utf-8"), encoding="utf-8")
    caplog.set_level("INFO", logger=cli.__name__)
    with (
        patch.object(sys, "argv", ["signal-bot", "--config", str(config)]),
        patch.dict("os.environ", {}, clear=True),
        patch.object(cli, "SQLiteStateStore") as state_store,
        patch.object(cli, "BinanceFuturesMarketData") as market_data,
        patch.object(cli, "TelegramBotClient") as bot_client,
        patch.object(cli, "TelegramNotifier") as telegram_notifier,
        patch.object(cli, "MatplotlibChartRenderer") as renderer,
        patch.object(cli, "SignalEngine") as engine,
        patch.object(cli, "StrategyRegistry") as registry,
        patch.object(cli, "SignalOrchestrator") as orchestrator,
    ):
        cli.main()

    state_store.assert_not_called()
    market_data.assert_not_called()
    bot_client.assert_not_called()
    telegram_notifier.assert_not_called()
    renderer.assert_not_called()
    engine.assert_not_called()
    registry.assert_not_called()
    orchestrator.assert_not_called()
    assert "signal_bot_disabled result=clean_exit" in caplog.text


def test_cli_closes_state_when_runtime_dependency_construction_fails(tmp_path: Path) -> None:
    config = config_file(tmp_path / "config.yaml")
    fake_state = MagicMock()
    with (
        patch.object(sys, "argv", ["signal-bot", "--config", str(config)]),
        patch.dict(
            "os.environ",
            {
                "TELEGRAM_BOT_TOKEN": "x",
                "TELEGRAM_CHAT_ID": "42",
                "TELEGRAM_ALLOWED_CHAT_IDS": "42",
                "TELEGRAM_ADMIN_CHAT_IDS": "42",
            },
            clear=False,
        ),
        patch.object(cli, "SQLiteStateStore", return_value=fake_state),
        patch.object(
            cli,
            "BinanceFuturesMarketData",
            side_effect=RuntimeError("market-data setup failed"),
        ),
        pytest.raises(RuntimeError, match="market-data setup failed"),
    ):
        cli.main()

    fake_state.close.assert_called_once()


def test_cli_rejects_admin_outside_allowlist(tmp_path: Path) -> None:
    config = config_file(tmp_path / "config.yaml")
    with (
        patch.object(sys, "argv", ["signal-bot", "--config", str(config), "--once"]),
        patch.dict(
            "os.environ",
            {
                "TELEGRAM_BOT_TOKEN": "x",
                "TELEGRAM_CHAT_ID": "42",
                "TELEGRAM_ALLOWED_CHAT_IDS": "42",
                "TELEGRAM_ADMIN_CHAT_IDS": "99",
            },
            clear=False,
        ),
        pytest.raises(RuntimeError, match="subset"),
    ):
        cli.main()


def test_cli_daemon_starts_and_stops_control_and_health(tmp_path: Path) -> None:
    config = config_file(tmp_path / "config.yaml")
    fake_state = MagicMock()
    fake_registry = MagicMock()
    fake_registry.create.return_value = Strategy()
    fake_orchestrator = MagicMock()
    fake_thread = MagicMock()
    fake_health = MagicMock()
    with (
        patch.object(sys, "argv", ["signal-bot", "--config", str(config)]),
        patch.dict(
            "os.environ",
            {
                "TELEGRAM_BOT_TOKEN": "x",
                "TELEGRAM_CHAT_ID": "42",
                "TELEGRAM_ALLOWED_CHAT_IDS": "42",
                "TELEGRAM_ADMIN_CHAT_IDS": "42",
                "SIGNAL_BOT_HEALTH_TOKEN": "h" * 64,
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
        patch.object(cli, "TelegramControlLoop") as control_factory,
        patch.object(cli.threading, "Thread", return_value=fake_thread),
        patch.object(cli, "HealthServer", return_value=fake_health) as health_factory,
    ):
        cli.main()
    fake_thread.start.assert_called_once()
    fake_orchestrator.run_forever.assert_called_once()
    control_factory.return_value.stop.assert_called_once()
    fake_thread.join.assert_called_once_with(timeout=5)
    fake_health.start.assert_called_once()
    fake_health.close.assert_called_once()
    health_factory.assert_called_once_with(
        fake_orchestrator.health,
        host="127.0.0.1",
        port=8080,
        auth_token="h" * 64,
    )
    fake_state.close.assert_called_once()


def test_cli_rejects_remote_health_bind_before_starting_threads(tmp_path: Path) -> None:
    config = config_file(tmp_path / "config.yaml")
    fake_state = MagicMock()
    fake_registry = MagicMock()
    fake_registry.create.return_value = Strategy()
    with (
        patch.object(
            sys,
            "argv",
            ["signal-bot", "--config", str(config), "--health-host", "0.0.0.0"],
        ),
        patch.dict(
            "os.environ",
            {
                "TELEGRAM_BOT_TOKEN": "x",
                "TELEGRAM_CHAT_ID": "42",
                "TELEGRAM_ALLOWED_CHAT_IDS": "42",
                "TELEGRAM_ADMIN_CHAT_IDS": "42",
                "SIGNAL_BOT_HEALTH_TOKEN": "",
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
        patch.object(cli, "SignalOrchestrator", return_value=MagicMock()),
        patch.object(cli.threading, "Thread") as thread_factory,
        pytest.raises(ValueError, match="require SIGNAL_BOT_HEALTH_TOKEN"),
    ):
        cli.main()

    thread_factory.assert_not_called()
    fake_state.close.assert_called_once()
