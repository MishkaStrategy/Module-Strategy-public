#!/usr/bin/env python3
"""Restartable deterministic stress runner for Liquidity Sweep.

The runner persists both module state and PRNG state so long workloads can be
split across independent processes without changing the generated candle stream.
"""

from __future__ import annotations

import argparse
import ast
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import random
import time
from typing import Any

from modular_core import Candle, MarketView
from strategy_modules.liquidity_sweep import LiquiditySweepModule, MODULE_VERSION

START = datetime(2020, 1, 1, tzinfo=timezone.utc)
SEED = 20260721


def _encode_random_state(state: object) -> str:
    return repr(state)


def _decode_random_state(value: str) -> tuple[object, ...]:
    parsed = ast.literal_eval(value)
    if not isinstance(parsed, tuple):
        raise ValueError("invalid persisted random state")
    return parsed


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.events < 1:
        raise SystemExit("--events must be positive")

    module = LiquiditySweepModule(max_active_levels=128)
    randomizer = random.Random(SEED)  # nosec B311 - deterministic benchmark data
    price = 100.0
    next_index = 0
    prior_seconds = 0.0
    prior_events = 0
    decision_digest = hashlib.sha256(b"liquidity-sweep-stress-v1").hexdigest()

    if args.input is not None:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        if payload.get("module_version") != MODULE_VERSION:
            raise SystemExit("input module version does not match current module")
        module.restore_state(payload["module_state"])
        module.on_start()
        randomizer.setstate(_decode_random_state(payload["random_state"]))
        price = float(payload["price"])
        next_index = int(payload["next_index"])
        prior_seconds = float(payload["total_seconds"])
        prior_events = int(payload["total_events"])
        decision_digest = str(payload["decision_digest"])
    else:
        module.on_start()

    started = time.perf_counter()
    for offset in range(args.events):
        index = next_index + offset
        price = max(1e-8, price + randomizer.uniform(-0.5, 0.5))
        open_price = max(1e-8, price + randomizer.uniform(-0.15, 0.15))
        close_price = max(1e-8, price + randomizer.uniform(-0.15, 0.15))
        high = max(open_price, close_price) + randomizer.uniform(0.01, 0.7)
        low = max(1e-9, min(open_price, close_price) - randomizer.uniform(0.01, 0.7))
        candle = Candle(
            START + timedelta(minutes=5 * index),
            open_price,
            high,
            low,
            close_price,
            1.0,
            "BTCUSDT",
            "5m",
        )
        decision = module.on_bar(MarketView((candle,), {}, index, 10_000, 10_000, None, ()))
        decision_identity = {
            "events": [
                item.feature_id for item in decision.features if item.key == "liquidity_sweep.event"
            ],
            "deletes": [item.feature_id for item in decision.feature_deletes],
            "setups": [item.setup_id for item in decision.setups],
            "signals": [item.reason for item in decision.signals],
            "skips": list(decision.skips),
        }
        encoded_identity = json.dumps(decision_identity, sort_keys=True, separators=(",", ":"))
        decision_digest = hashlib.sha256(
            f"{decision_digest}:{encoded_identity}".encode("utf-8")
        ).hexdigest()

    elapsed = time.perf_counter() - started
    state = module.snapshot_state()
    total_events = prior_events + args.events
    total_seconds = prior_seconds + elapsed
    result: dict[str, Any] = {
        "module_version": MODULE_VERSION,
        "seed": SEED,
        "chunk_events": args.events,
        "chunk_seconds": elapsed,
        "chunk_throughput_events_per_second": args.events / elapsed,
        "total_events": total_events,
        "total_seconds": total_seconds,
        "total_throughput_events_per_second": total_events / total_seconds,
        "next_index": next_index + args.events,
        "price": price,
        "random_state": _encode_random_state(randomizer.getstate()),
        "module_state": state,
        "module_state_sha256": _canonical_digest(state),
        "decision_digest": decision_digest,
        "checkpoint_bytes": len(
            json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ),
        "active_levels": len(state["levels"]),
        "buffered_bars": len(state["bars"]),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                key: value
                for key, value in result.items()
                if key not in {"random_state", "module_state"}
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
