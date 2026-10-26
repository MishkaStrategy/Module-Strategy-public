from __future__ import annotations

import argparse
import hashlib
import json
import platform
import resource
import time
from pathlib import Path
from typing import Any

from benchmark import make_view

from modular_strategy_protection_target import ProtectionTargetModule, __version__


def update_digest(previous: str, plan_id: str) -> str:
    return hashlib.sha256(bytes.fromhex(previous) + plan_id.encode("utf-8")).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--state-in", type=Path)
    parser.add_argument("--state-out", type=Path, required=True)
    parser.add_argument("--result-out", type=Path, required=True)
    parser.add_argument("--digest-in", default="0" * 64)
    parser.add_argument("--max-state", type=int, default=4096)
    args = parser.parse_args()

    module = ProtectionTargetModule(
        max_processed_plans=args.max_state,
        max_diagnostic_results=0,
    )
    restore_ms = 0.0
    if args.state_in and args.state_in.exists():
        state = json.loads(args.state_in.read_text(encoding="utf-8"))
        started = time.perf_counter()
        module.restore_state(state)
        restore_ms = (time.perf_counter() - started) * 1000

    digest = args.digest_in
    started = time.perf_counter()
    for index in range(args.start, args.start + args.count):
        decision = module.on_bar(make_view(index))
        if len(decision.features) != 1:
            raise RuntimeError(f"unexpected decision at {index}: {decision}")
        digest = update_digest(digest, str(decision.features[0].value["protection_plan_id"]))
    elapsed = time.perf_counter() - started
    state = module.snapshot_state()
    encoded = json.dumps(state, sort_keys=True, separators=(",", ":"))
    args.state_out.write_text(encoded + "\n", encoding="utf-8")
    result: dict[str, Any] = {
        "module_version": __version__,
        "python": platform.python_version(),
        "start": args.start,
        "events": args.count,
        "seconds": elapsed,
        "events_per_second": args.count / elapsed,
        "restore_ms": restore_ms,
        "checkpoint_bytes": len(encoded.encode("utf-8")),
        "processed_state": len(state["processed"]),
        "state_digest": state["state_digest"],
        "output_digest": digest,
        "max_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
    }
    args.result_out.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
