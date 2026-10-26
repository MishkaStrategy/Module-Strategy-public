from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from modular_core import Candle, MarketView, PositionSide, PositionSnapshot

from modular_strategy_position_exit_management import PositionExitManagementModule

START = datetime(2026, 1, 1, tzinfo=UTC)


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return _plain({field.name: getattr(value, field.name) for field in fields(value)})
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in sorted(value.items())}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    return value


def _view(index: int, close: float, quantity: float, stop: float) -> MarketView:
    candle = Candle(
        START + timedelta(minutes=5 * index),
        close,
        close + 1,
        close - 1,
        close,
        1,
        "BTCUSDT",
        "5m",
    )
    position = PositionSnapshot(
        id="replay-position",
        side=PositionSide.LONG,
        quantity=quantity,
        entry_price=100,
        entry_time=START,
        stop_loss=stop,
        take_profit=140,
        tag="replay",
        unrealized_pnl=0,
        setup_id="replay-setup",
    )
    return MarketView(
        base_bars=(candle,),
        timeframe_bars={},
        index=index,
        cash=10_000,
        equity=10_000,
        position=position,
        pending_orders=(),
    )


def run_evidence() -> dict[str, object]:
    config = {
        "partial_exits": [{"trigger_r": 1, "fraction": "0.5"}],
        "breakeven_trigger_r": 1,
        "trailing_mode": "r_multiple",
        "trailing_activation_r": 2,
        "trailing_distance_r": 1,
        "minimum_stop_move_bps": 0,
        "market_buffer_bps": 0,
        "emit_status_feature": True,
    }
    views = (
        _view(0, 100, 4, 95),
        _view(1, 105, 4, 95),
        _view(2, 110, 2, 100),
        _view(3, 115, 2, 105),
    )
    continuous = PositionExitManagementModule(**config)
    continuous_decisions = [continuous.on_bar(view) for view in views]

    restarted = PositionExitManagementModule(**config)
    restarted_decisions = [restarted.on_bar(view) for view in views[:2]]
    checkpoint = json.loads(json.dumps(restarted.snapshot_state()))
    replacement = PositionExitManagementModule(**config)
    replacement.restore_state(checkpoint)
    restarted_decisions.extend(replacement.on_bar(view) for view in views[2:])

    continuous_payload = _plain((continuous_decisions, continuous.snapshot_state()))
    restarted_payload = _plain((restarted_decisions, replacement.snapshot_state()))
    encoded_continuous = json.dumps(
        continuous_payload, sort_keys=True, separators=(",", ":"), default=str
    )
    encoded_restarted = json.dumps(
        restarted_payload, sort_keys=True, separators=(",", ":"), default=str
    )
    continuous_digest = hashlib.sha256(encoded_continuous.encode()).hexdigest()
    restarted_digest = hashlib.sha256(encoded_restarted.encode()).hexdigest()
    if continuous_digest != restarted_digest:
        raise RuntimeError("continuous and restarted replay diverged")
    return {
        "events": len(views),
        "checkpoint_after_event": 2,
        "continuous_digest": continuous_digest,
        "restarted_digest": restarted_digest,
        "checkpoint_bytes": len(json.dumps(checkpoint, sort_keys=True).encode()),
        "deterministic": True,
    }


def main() -> None:
    print(json.dumps(run_evidence(), sort_keys=True))


if __name__ == "__main__":
    main()
