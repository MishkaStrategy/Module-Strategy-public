from __future__ import annotations

import json
import mimetypes
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from modular_core.models import Candle

from .domain import CandleBatch, InstrumentRules, TradeSignal


def _https_open(request: str | urllib.request.Request, *, timeout: float) -> Any:
    url = request.full_url if isinstance(request, urllib.request.Request) else request
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("only HTTPS endpoints are allowed")
    # The only dangerous scheme is rejected immediately above.
    try:
        return urllib.request.urlopen(request, timeout=timeout)  # nosec B310
    except urllib.error.HTTPError as exc:
        if _is_telegram_url(url):
            raise urllib.error.HTTPError(
                "https://api.telegram.org/<redacted>",
                exc.code,
                "Telegram API request failed",
                exc.headers,
                exc.fp,
            ) from None
        raise
    except urllib.error.URLError:
        if _is_telegram_url(url):
            raise urllib.error.URLError("Telegram API connection failed") from None
        raise


def _is_telegram_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname == "api.telegram.org"


class BinanceFuturesMarketData:
    """REST backfill adapter for Binance USDT-M perpetual closed klines."""

    BASE_URL = "https://fapi.binance.com"

    def __init__(self, *, timeout: float = 10.0) -> None:
        self.timeout = timeout

    def get_instrument_rules(self, symbol: str) -> InstrumentRules:
        query = urllib.parse.urlencode({"symbol": symbol})
        request = urllib.request.Request(
            f"{self.BASE_URL}/fapi/v1/exchangeInfo?{query}",
            headers={"User-Agent": "modular-signal-bot/0.1"},
        )
        with _https_open(request, timeout=self.timeout) as response:
            payload = json.load(response)
        rows = payload.get("symbols", ())
        if len(rows) != 1 or rows[0].get("symbol") != symbol:
            raise ValueError(f"unknown Binance Futures symbol: {symbol}")
        row = rows[0]
        if row.get("contractType") != "PERPETUAL" or row.get("quoteAsset") != "USDT":
            raise ValueError(f"{symbol} is not a USDT-M perpetual contract")
        filters = {item["filterType"]: item for item in row.get("filters", ())}
        try:
            price_filter = filters["PRICE_FILTER"]
            lot_filter = filters["LOT_SIZE"]
            notional_filter = filters.get("MIN_NOTIONAL", {})
            return InstrumentRules(
                symbol=symbol,
                market="binance_usdt_m_perpetual",
                tick_size=Decimal(str(price_filter["tickSize"])),
                step_size=Decimal(str(lot_filter["stepSize"])),
                min_price=Decimal(str(price_filter["minPrice"])),
                min_notional=(
                    Decimal(str(notional_filter["notional"]))
                    if notional_filter.get("notional") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"incomplete exchange rules for {symbol}") from exc

    def verify_instrument(self, configured: InstrumentRules) -> None:
        actual = self.get_instrument_rules(configured.symbol)
        mismatches = [
            name
            for name in (
                "market",
                "tick_size",
                "step_size",
                "min_price",
                "min_notional",
            )
            if getattr(configured, name) is not None
            and getattr(configured, name) != getattr(actual, name)
        ]
        if mismatches:
            raise ValueError(
                f"stale exchange rules for {configured.symbol}: {', '.join(mismatches)}"
            )

    def get_closed_candles(
        self,
        instrument: InstrumentRules,
        timeframe: str,
        limit: int,
        *,
        as_of: datetime,
    ) -> CandleBatch:
        if instrument.market != "binance_usdt_m_perpetual":
            raise ValueError("Binance Futures adapter received an incompatible market")
        query = urllib.parse.urlencode(
            {
                "symbol": instrument.symbol,
                "interval": timeframe,
                "limit": min(max(limit + 1, 2), 1500),
                "endTime": int(as_of.timestamp() * 1000),
            }
        )
        request = urllib.request.Request(
            f"{self.BASE_URL}/fapi/v1/klines?{query}",
            headers={"User-Agent": "modular-signal-bot/0.1"},
        )
        with _https_open(request, timeout=self.timeout) as response:
            rows = json.load(response)
        candles = []
        observed = as_of.astimezone(timezone.utc)
        for row in rows:
            candle = Candle(
                timestamp=datetime.fromtimestamp(row[0] / 1000, timezone.utc),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                symbol=instrument.symbol,
                timeframe=timeframe,
            )
            exchange_close = datetime.fromtimestamp((row[6] + 1) / 1000, timezone.utc)
            if exchange_close <= observed:
                candles.append(candle)
        if len(candles) < limit:
            raise ValueError(f"insufficient closed candles: wanted {limit}, got {len(candles)}")
        return CandleBatch(instrument.symbol, timeframe, tuple(candles[-limit:]), observed)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, *, timeout: float = 15.0) -> None:
        if not token or not chat_id:
            raise ValueError("Telegram token and chat_id are required")
        self.url = f"https://api.telegram.org/bot{token}/sendPhoto"
        self.chat_id, self.timeout = chat_id, timeout

    def send(self, signal: TradeSignal, message: str, chart: Path | None) -> str:
        if chart is None:
            if len(message) > 4096:
                raise ValueError("Telegram text message exceeds 4096 characters")
            data = urllib.parse.urlencode({"chat_id": self.chat_id, "text": message}).encode()
            request = urllib.request.Request(
                self.url.replace("sendPhoto", "sendMessage"), data=data, method="POST"
            )
            with _https_open(request, timeout=self.timeout) as response:
                result = json.load(response)
            if not result.get("ok"):
                raise RuntimeError("Telegram rejected the notification")
            return str(result["result"]["message_id"])
        boundary = "----signalbot" + secrets.token_hex(12)
        if len(message) > 1024:
            raise ValueError("Telegram photo caption exceeds 1024 characters")
        parts: list[bytes] = []
        for name, value in (("chat_id", self.chat_id), ("caption", message)):
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            )
        mime = mimetypes.guess_type(chart.name)[0] or "image/png"
        parts.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; '
                f'filename="{chart.name}"\r\nContent-Type: {mime}\r\n\r\n'
            ).encode()
            + chart.read_bytes()
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        request = urllib.request.Request(
            self.url,
            data=b"".join(parts),
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with _https_open(request, timeout=self.timeout) as response:
            result = json.load(response)
        if not result.get("ok"):
            raise RuntimeError("Telegram rejected the notification")
        return str(result["result"]["message_id"])


class TelegramBotClient:
    """Minimal Bot API client used by the command control plane."""

    def __init__(self, token: str, *, timeout: float = 15.0) -> None:
        if not token:
            raise ValueError("Telegram token is required")
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.timeout = timeout

    def get_updates(
        self, *, offset: int | None = None, poll_timeout: int = 20
    ) -> list[dict[str, object]]:
        parameters: dict[str, object] = {
            "timeout": poll_timeout,
            "allowed_updates": json.dumps(["message"]),
        }
        if offset is not None:
            parameters["offset"] = offset
        query = urllib.parse.urlencode(parameters)
        with _https_open(
            f"{self.base_url}/getUpdates?{query}", timeout=poll_timeout + self.timeout
        ) as response:
            payload = json.load(response)
        if not payload.get("ok"):
            raise RuntimeError("Telegram getUpdates failed")
        return list(payload.get("result", ()))

    def send_text(self, chat_id: str, text: str) -> str:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        request = urllib.request.Request(f"{self.base_url}/sendMessage", data=data, method="POST")
        with _https_open(request, timeout=self.timeout) as response:
            payload = json.load(response)
        if not payload.get("ok"):
            raise RuntimeError("Telegram sendMessage failed")
        return str(payload["result"]["message_id"])
