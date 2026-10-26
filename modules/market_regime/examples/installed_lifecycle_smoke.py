"""Clean-install lifecycle, restart, causality, registry, and entry-point smoke."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from importlib.metadata import entry_points, version
from pathlib import Path
from typing import Any

from modular_core import Candle, MarketView, ModuleRegistry
from market_regime import MODULE_VERSION, MarketRegimeModule


def candle(index: int) -> Candle:
    close = 10_000.0 + index * 0.25 + (index % 17) * 0.03
    return Candle(
        timestamp=datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(hours=index),
        open=close - 0.1,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=100.0,
        symbol="BTCUSDT",
        timeframe="1h",
    )


def view(bar: Candle) -> MarketView:
    return MarketView(
        base_bars=(bar,),
        timeframe_bars={"1h": (bar,)},
        index=0,
        cash=10_000.0,
        equity=10_000.0,
        position=None,
        pending_orders=(),
    )


def canonical_state(module: MarketRegimeModule) -> bytes:
    return json.dumps(module.snapshot_state(), sort_keys=True, separators=(",", ":")).encode()


def run(events: int, restart_at: int | None) -> dict[str, Any]:
    module = MarketRegimeModule(publish_diagnostics=False)
    module.on_start()
    output = hashlib.sha256()
    publications = 0
    feature_count = 0
    for index in range(events):
        decision = module.on_bar(view(candle(index)))
        if decision.features:
            publications += 1
            feature_count += len(decision.features)
            for feature in decision.features:
                output.update((feature.feature_id or "").encode())
                output.update(repr(feature.value).encode())
        if restart_at is not None and index + 1 == restart_at:
            checkpoint = module.snapshot_state()
            restored = MarketRegimeModule(publish_diagnostics=True)
            restored.restore_state(checkpoint)
            module = restored
    module.on_finish()
    state = canonical_state(module)
    return {
        "output_digest": output.hexdigest(),
        "state_digest": hashlib.sha256(state).hexdigest(),
        "checkpoint_bytes": len(state),
        "publications": publications,
        "feature_count": feature_count,
    }


def verify_causality() -> str:
    module = MarketRegimeModule()
    target = candle(1)
    base = Candle(
        timestamp=datetime(2020, 1, 1, tzinfo=timezone.utc),
        open=10_000.0,
        high=10_001.0,
        low=9_999.0,
        close=10_000.0,
        volume=100.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )
    decision = module.on_bar(
        MarketView(
            base_bars=(base,),
            timeframe_bars={"1h": (target,)},
            index=0,
            cash=10_000.0,
            equity=10_000.0,
            position=None,
            pending_orders=(),
        )
    )
    reason = decision.diagnostics.get("reason")
    if reason != "market_regime.incomplete_bar":
        raise RuntimeError(f"future target was not rejected: {reason!r}")
    return str(reason)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=5_000)
    parser.add_argument("--restart-at", type=int, default=2_500)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.events < 2 or not 1 <= args.restart_at < args.events:
        parser.error("restart-at must be within the event stream")

    points = {item.name: item for item in entry_points(group="modular_strategy.modules")}
    if points.get("market_regime") is None:
        raise RuntimeError("market_regime entry point is missing")
    if points["market_regime"].load() is not MarketRegimeModule:
        raise RuntimeError("market_regime entry point resolves to the wrong class")

    loaded = ModuleRegistry().load_import_path(
        "market_regime:MarketRegimeModule", {"publish_diagnostics": False}
    )
    if not isinstance(loaded, MarketRegimeModule):
        raise RuntimeError("Core registry did not load MarketRegimeModule")

    continuous = run(args.events, None)
    restarted = run(args.events, args.restart_at)
    if continuous != restarted:
        raise RuntimeError("restart changed the output or final state")

    payload = {
        "distribution_version": version("modular-strategy-market-regime"),
        "module_version": MODULE_VERSION,
        "core_registry": "pass",
        "entry_point": "pass",
        "causality_reason": verify_causality(),
        "events": args.events,
        "restart_at": args.restart_at,
        "continuous": continuous,
        "restarted": restarted,
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    print(rendered, end="")
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
