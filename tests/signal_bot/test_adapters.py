from __future__ import annotations

import io
import json
import traceback
import urllib.error
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from modular_core import Side
from signal_bot.adapters import (
    BinanceFuturesMarketData,
    TelegramBotClient,
    TelegramNotifier,
)
from signal_bot.domain import EntryType, InstrumentRules, TradeSignal


class Response(io.BytesIO):
    def __enter__(self) -> Response:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def exchange_info() -> bytes:
    return json.dumps(
        {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "contractType": "PERPETUAL",
                    "quoteAsset": "USDT",
                    "filters": [
                        {
                            "filterType": "PRICE_FILTER",
                            "tickSize": "0.10",
                            "minPrice": "0.10",
                        },
                        {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
            ]
        }
    ).encode()


def test_exchange_info_rules_and_stale_config_detection() -> None:
    adapter = BinanceFuturesMarketData()
    with patch("urllib.request.urlopen", return_value=Response(exchange_info())):
        rules = adapter.get_instrument_rules("BTCUSDT")
    assert rules.tick_size == Decimal("0.10")
    assert rules.step_size == Decimal("0.001")
    with patch("urllib.request.urlopen", return_value=Response(exchange_info())):
        adapter.verify_instrument(rules)
    stale = InstrumentRules(
        "BTCUSDT",
        "binance_usdt_m_perpetual",
        Decimal("0.01"),
        Decimal("0.001"),
        Decimal("0.10"),
        Decimal("5"),
    )
    with patch("urllib.request.urlopen", return_value=Response(exchange_info())):
        with pytest.raises(ValueError, match="tick_size"):
            adapter.verify_instrument(stale)


def telegram_signal() -> TradeSignal:
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)
    return TradeSignal(
        "sig_test",
        "strategy",
        "1.0.0",
        "BTCUSDT",
        "binance_usdt_m_perpetual",
        Side.BUY,
        now,
        "5m",
        EntryType.LIMIT,
        Decimal("100"),
        Decimal("99"),
        (Decimal("102"),),
        None,
        (Decimal("2"),),
        ("confirmed",),
        "reason",
        None,
        "setup",
        {},
    )


def test_binance_closed_klines_and_market_guard() -> None:
    now = datetime(2026, 7, 22, 12, 10, tzinfo=timezone.utc)
    rows = [
        [
            int(datetime(2026, 7, 22, 12, i, tzinfo=timezone.utc).timestamp() * 1000),
            "100",
            "101",
            "99",
            "100",
            "10",
            int(datetime(2026, 7, 22, 12, i + 5, tzinfo=timezone.utc).timestamp() * 1000) - 1,
        ]
        for i in (0, 5)
    ]
    adapter = BinanceFuturesMarketData()
    rules = InstrumentRules("BTCUSDT", "binance_usdt_m_perpetual", Decimal("0.1"))
    with patch("urllib.request.urlopen", return_value=Response(json.dumps(rows).encode())):
        batch = adapter.get_closed_candles(rules, "5m", 2, as_of=now)
    assert len(batch.candles) == 2
    wrong = InstrumentRules("BTCUSDT", "spot", Decimal("0.1"))
    with pytest.raises(ValueError, match="incompatible market"):
        adapter.get_closed_candles(wrong, "5m", 2, as_of=now)


def test_telegram_text_photo_updates_and_errors(tmp_path: Path) -> None:
    ok = json.dumps({"ok": True, "result": {"message_id": 7}}).encode()
    notifier = TelegramNotifier("secret", "42")
    with patch("urllib.request.urlopen", return_value=Response(ok)):
        assert notifier.send(telegram_signal(), "message", None) == "7"
    chart = tmp_path / "chart.png"
    chart.write_bytes(b"png")
    with patch("urllib.request.urlopen", return_value=Response(ok)):
        assert notifier.send(telegram_signal(), "message", chart) == "7"

    updates = json.dumps({"ok": True, "result": [{"update_id": 1}]}).encode()
    client = TelegramBotClient("secret")
    with patch("urllib.request.urlopen", return_value=Response(updates)):
        assert client.get_updates(offset=1)[0]["update_id"] == 1
    with patch("urllib.request.urlopen", return_value=Response(ok)):
        assert client.send_text("42", "safe") == "7"
    failed = json.dumps({"ok": False, "description": "rate limited"}).encode()
    with patch("urllib.request.urlopen", return_value=Response(failed)):
        with pytest.raises(RuntimeError, match="rejected"):
            notifier.send(telegram_signal(), "message", None)
    with pytest.raises(ValueError, match="4096"):
        notifier.send(telegram_signal(), "x" * 4097, None)
    with pytest.raises(ValueError, match="1024"):
        notifier.send(telegram_signal(), "x" * 1025, chart)


@pytest.mark.parametrize("adapter", ["notifier", "control"])
@pytest.mark.parametrize("error_kind", ["http", "url"])
def test_telegram_transport_exceptions_redact_credentials(adapter: str, error_kind: str) -> None:
    token = "TOPSECRET"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    if error_kind == "http":
        error = urllib.error.HTTPError(url, 429, "rate limited", None, None)
    else:
        error = urllib.error.URLError(f"failed to reach {url}")

    transport = TelegramNotifier(token, "42") if adapter == "notifier" else TelegramBotClient(token)
    with patch("urllib.request.urlopen", side_effect=error):
        with pytest.raises(type(error)) as raised:
            if adapter == "notifier":
                transport.send(telegram_signal(), "message", None)
            else:
                transport.send_text("42", "message")

    exception_text = "\n".join(
        (str(raised.value), repr(raised.value), "".join(traceback.format_exception(raised.value)))
    )
    assert token not in exception_text
    assert url not in exception_text
    if error_kind == "http":
        assert raised.value.code == 429  # type: ignore[attr-defined]
