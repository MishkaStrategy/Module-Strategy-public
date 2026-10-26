from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from time import perf_counter

from strategy_modules.risk_sizing import RiskSizingModule

from risk_sizing_benchmark import make_view


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--events", type=int, required=True)
    parser.add_argument("--state-in", type=Path)
    parser.add_argument("--state-out", type=Path, required=True)
    args = parser.parse_args()

    module = RiskSizingModule()
    if args.state_in is not None:
        module.restore_state(json.loads(args.state_in.read_text(encoding="utf-8")))

    started = perf_counter()
    for index in range(args.start, args.start + args.events):
        result = module.on_bar(make_view(index))
        if len(result.features) != 1 or not result.features[0].value["approved"]:
            raise RuntimeError(f"unexpected decision at event {index}")
    elapsed = perf_counter() - started

    state = module.snapshot_state()
    args.state_out.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    report = {
        "start": args.start,
        "events": args.events,
        "seconds": elapsed,
        "throughput_per_second": args.events / elapsed,
        "received_total": state["counters"]["received"],
        "approved_total": state["counters"]["approved"],
        "cached_requests": len(state["request_order"]),
        "checkpoint_bytes": args.state_out.stat().st_size,
        "checkpoint_sha256": hashlib.sha256(args.state_out.read_bytes()).hexdigest(),
    }
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
