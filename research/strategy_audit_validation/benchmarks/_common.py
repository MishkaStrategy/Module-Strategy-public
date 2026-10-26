"""Shared deterministic data and signal contracts for Variant 2 research."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

Side = Literal["long", "short"]

RAW_5M_SHA256 = "2b8a20ef09e1c292abbbee4a6963a1cedb7904cfc4340c9de72d0d2cd0b87ed1"
REGISTERED_DATASET_HASH = "49ee948c7c22dc3c552bf6a96d2fdce9c5bba97662a694ae98ab85fc521c04f5"
COMPOSITION_HASH = "51659c6ed582f2843357bd33fbff35059f9a535dd27f05a585a9e7dfa78365c9"
CANDIDATE_ID = "strategy-candidate-51659c6ed582f2843357bd33"

MAKER_FEE = 0.0002
MARKET_FEE = 0.00055
ADVERSE_SLIPPAGE = 0.00025
RISK_FRACTION = 0.005
MAX_NOTIONAL_MULTIPLE = 3.0
INITIAL_EQUITY = 10_000.0


@dataclass(frozen=True, slots=True)
class Signal:
    available_at: pd.Timestamp
    module_id: str
    module_version: str
    setup_id: str
    side: Side
    priority: int
    entry: float
    stop: float
    target_r: float
    ttl_bars: int
    max_hold_bars: int
    no_progress_bars: int
    no_progress_min_r: float
    signal_key: str


@dataclass(slots=True)
class PendingOrder:
    signal: Signal
    created_index: int
    expires_before_index: int


@dataclass(slots=True)
class Position:
    signal: Signal
    fill_index: int
    fill_time: pd.Timestamp
    quantity: float
    entry_fee: float
    initial_r: float
    target: float
    mfe_price: float


LEDGER_COLUMNS = [
    "trade_id",
    "module_id",
    "module_version",
    "setup_id",
    "side",
    "signal_time",
    "fill_time",
    "exit_time",
    "entry_price",
    "stop_price",
    "target_price",
    "exit_price",
    "quantity",
    "initial_r",
    "target_r",
    "exit_reason",
    "bars_held",
    "gross_pnl",
    "entry_fee",
    "exit_fee",
    "net_pnl",
    "equity_before",
    "equity_after",
    "return_on_equity",
    "mfe_r",
    "signal_key",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def discover_drive_parquet(home: Path | None = None) -> Path:
    root = (home or Path.home()).resolve()
    patterns = (
        "Library/CloudStorage/GoogleDrive-*/My Drive/binance_usdtm_futures_history/"
        "BTCUSDT/BTCUSDT_5m.parquet",
        "Library/CloudStorage/GoogleDrive-*/Мой диск/binance_usdtm_futures_history/"
        "BTCUSDT/BTCUSDT_5m.parquet",
        "Google Drive/My Drive/binance_usdtm_futures_history/BTCUSDT/BTCUSDT_5m.parquet",
        "Google Диск/Мой диск/binance_usdtm_futures_history/BTCUSDT/BTCUSDT_5m.parquet",
    )
    candidates: set[Path] = set()
    for pattern in patterns:
        for candidate in root.glob(pattern):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            candidates.add(candidate.resolve(strict=True))
    if len(candidates) != 1:
        raise RuntimeError(f"expected exactly one BTCUSDT 5m corpus file, found {len(candidates)}")
    return next(iter(candidates))


def load_5m(path: Path, *, expected_sha256: str) -> pd.DataFrame:
    if sha256_file(path) != expected_sha256:
        raise RuntimeError("BTCUSDT 5m raw SHA-256 mismatch")
    columns = ["open_time", "open", "high", "low", "close", "volume", "close_time"]
    frame = pd.read_parquet(path, columns=columns, engine="pyarrow")
    if list(frame.columns) != columns:
        raise RuntimeError("unexpected BTCUSDT parquet schema")
    frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True)
    frame["close_time"] = pd.to_datetime(frame["close_time"], utc=True)
    frame = frame.sort_values("open_time", kind="mergesort").reset_index(drop=True)
    if frame.empty or frame["open_time"].duplicated().any():
        raise RuntimeError("BTCUSDT 5m timestamps are empty or duplicated")
    expected_delta = pd.Timedelta(minutes=5)
    if not frame["open_time"].diff().dropna().eq(expected_delta).all():
        raise RuntimeError("BTCUSDT 5m data contains a gap")
    if (
        not (frame["close_time"] - frame["open_time"])
        .eq(pd.Timedelta(minutes=5) - pd.Timedelta(milliseconds=1))
        .all()
    ):
        raise RuntimeError("BTCUSDT 5m close-time convention mismatch")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="raise").astype("float64")
    values = frame[["open", "high", "low", "close", "volume"]].to_numpy()
    if not np.isfinite(values).all():
        raise RuntimeError("BTCUSDT 5m contains non-finite values")
    if (frame[["open", "high", "low", "close"]] <= 0).any().any():
        raise RuntimeError("BTCUSDT 5m contains non-positive prices")
    if (frame["volume"] < 0).any():
        raise RuntimeError("BTCUSDT 5m contains negative volume")
    if (frame["high"] < frame[["open", "close", "low"]].max(axis=1)).any():
        raise RuntimeError("BTCUSDT 5m high invariant failed")
    if (frame["low"] > frame[["open", "close", "high"]].min(axis=1)).any():
        raise RuntimeError("BTCUSDT 5m low invariant failed")
    return frame


def aggregate(frame: pd.DataFrame, rule: str, expected_count: int) -> pd.DataFrame:
    indexed = frame.set_index("open_time")
    grouped = indexed.resample(rule, label="left", closed="left", origin="epoch")
    result = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        count=("close", "count"),
    )
    result = result.loc[result["count"].eq(expected_count)].drop(columns="count")
    result_index = pd.DatetimeIndex(result.index)
    if result_index.tz is None:
        result_index = result_index.tz_localize("UTC")
    else:
        result_index = result_index.tz_convert("UTC")
    result.index = result_index.rename("open_time")
    return result


def true_range(frame: pd.DataFrame) -> pd.Series:
    previous = frame["close"].shift(1)
    return pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous).abs(),
            (frame["low"] - previous).abs(),
        ],
        axis=1,
    ).max(axis=1)


def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["atr14"] = true_range(result).rolling(14, min_periods=14).mean()
    result["ema50"] = result["close"].ewm(span=50, adjust=False, min_periods=50).mean()
    result["ema200"] = result["close"].ewm(span=200, adjust=False, min_periods=200).mean()
    delta = result["close"].diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    average_gain = gains.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    average_loss = losses.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = average_gain / average_loss
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.mask((average_loss == 0) & (average_gain > 0), 100.0)
    rsi = rsi.mask((average_loss == 0) & (average_gain == 0), 50.0)
    result["rsi14"] = rsi
    result["volume_sma20"] = result["volume"].rolling(20, min_periods=20).mean()
    return result


def rolling_location(
    frame: pd.DataFrame,
    window: int,
    *,
    short: bool,
) -> pd.Series:
    high = frame["high"].rolling(window, min_periods=window).max()
    low = frame["low"].rolling(window, min_periods=window).min()
    denominator = high - low
    numerator = high - frame["close"] if short else frame["close"] - low
    return (numerator / denominator).where(denominator > 0)


def completed_asof(source: pd.DataFrame, availability: pd.DatetimeIndex) -> pd.DataFrame:
    prepared = source.copy()
    prepared["available_at"] = prepared.index + (prepared.index[1] - prepared.index[0])
    prepared = prepared.set_index("available_at")
    return prepared.reindex(availability, method="ffill")


def _signal_key(
    *,
    module_id: str,
    module_version: str,
    setup_id: str,
    available_at: pd.Timestamp,
    entry: float,
    stop: float,
) -> str:
    return canonical_sha256(
        {
            "available_at": available_at.isoformat(),
            "entry": round(entry, 12),
            "module_id": module_id,
            "module_version": module_version,
            "setup_id": setup_id,
            "stop": round(stop, 12),
        }
    )


def _append_signal(
    output: list[Signal],
    *,
    available_at: pd.Timestamp,
    module_id: str,
    module_version: str,
    setup_id: str,
    side: Side,
    priority: int,
    entry: float,
    stop: float,
    max_stop_fraction: float,
    target_r: float,
    ttl_bars: int,
    max_hold_bars: int,
    no_progress_bars: int,
    no_progress_min_r: float,
) -> None:
    if not (math.isfinite(entry) and math.isfinite(stop) and entry > 0 and stop > 0):
        return
    distance = entry - stop if side == "long" else stop - entry
    if distance <= 0 or distance / entry > max_stop_fraction:
        return
    output.append(
        Signal(
            available_at=available_at,
            module_id=module_id,
            module_version=module_version,
            setup_id=setup_id,
            side=side,
            priority=priority,
            entry=entry,
            stop=stop,
            target_r=target_r,
            ttl_bars=ttl_bars,
            max_hold_bars=max_hold_bars,
            no_progress_bars=no_progress_bars,
            no_progress_min_r=no_progress_min_r,
            signal_key=_signal_key(
                module_id=module_id,
                module_version=module_version,
                setup_id=setup_id,
                available_at=available_at,
                entry=entry,
                stop=stop,
            ),
        )
    )
