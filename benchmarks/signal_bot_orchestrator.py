from __future__ import annotations

import argparse
import json
import time
import tracemalloc
from decimal import Decimal

from signal_bot.domain import InstrumentRules
from signal_bot.orchestrator import InstrumentBinding, SignalOrchestrator


class Strategy:
    strategy_id = "benchmark"
    version = "1.0.0"


class Engine:
    def retry_pending(self, instrument: InstrumentRules):
        return ()

    def process(
        self,
        instrument: InstrumentRules,
        strategy: Strategy,
        *,
        chart_enabled: bool = True,
    ):
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairs", type=int, default=50)
    parser.add_argument("--cycles", type=int, default=1000)
    args = parser.parse_args()
    bindings = tuple(
        InstrumentBinding(
            InstrumentRules(f"PAIR{i}USDT", "binance_usdt_m_perpetual", Decimal("0.0001")),
            Strategy(),  # type: ignore[arg-type]
        )
        for i in range(args.pairs)
    )
    orchestrator = SignalOrchestrator(Engine(), bindings)  # type: ignore[arg-type]
    tracemalloc.start()
    started = time.perf_counter()
    for _ in range(args.cycles):
        orchestrator.run_cycle()
    elapsed = time.perf_counter() - started
    current, peak = tracemalloc.get_traced_memory()
    print(
        json.dumps(
            {
                "pairs": args.pairs,
                "cycles": args.cycles,
                "seconds": elapsed,
                "pair_cycles_per_second": args.pairs * args.cycles / elapsed,
                "memory_current_bytes": current,
                "memory_peak_bytes": peak,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
