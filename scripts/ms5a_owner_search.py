#!/usr/bin/env python3
"""Execute exactly one separately authorized MS5A SEARCH and stop before WALK_FORWARD."""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from types import FrameType

from strategy_factory.ms5a_host_guard import (
    HostMaintenanceRisk,
    assert_governed_host_maintenance_clear,
    interruption_exit_code,
    write_interruption_summary,
    write_search_failure_summary,
)
from strategy_factory.multi_strategy_search import (
    MS5ASearchAuthority,
    execute_authorized_ms5a_search,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--market-data-root", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--authority-id", required=True)
    parser.add_argument("--request-fingerprint", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    output = Path(args.output)

    try:
        maintenance = assert_governed_host_maintenance_clear()
    except HostMaintenanceRisk as exc:
        print(f"MS5 host maintenance guard blocked governed execution: {exc}", file=sys.stderr)
        return 75

    print(
        "MS5 host maintenance guard passed: "
        + json.dumps(maintenance, sort_keys=True, separators=(",", ":")),
        flush=True,
    )

    previous_handlers = {
        signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)
    }
    interruption_started = False

    def handle_interruption(signum: int, _frame: FrameType | None) -> None:
        nonlocal interruption_started
        if interruption_started:
            return
        interruption_started = True
        write_interruption_summary(
            output,
            expected_head=args.expected_head,
            authority_id=args.authority_id,
            request_fingerprint=args.request_fingerprint,
            signum=signum,
            runtime_root=args.runtime_root,
        )
        raise SystemExit(interruption_exit_code(signum))

    for signum in previous_handlers:
        signal.signal(signum, handle_interruption)

    try:
        authority = MS5ASearchAuthority(
            authority_id=args.authority_id,
            source_commit_sha=args.expected_head,
            request_fingerprint=args.request_fingerprint,
        )
        payload = execute_authorized_ms5a_search(
            authority=authority,
            expected_head=args.expected_head,
            market_data_root=args.market_data_root,
            runtime_root=args.runtime_root,
        )
    except Exception as exc:
        try:
            write_search_failure_summary(
                output,
                expected_head=args.expected_head,
                authority_id=args.authority_id,
                request_fingerprint=args.request_fingerprint,
                exc=exc,
                runtime_root=args.runtime_root,
            )
        except Exception as evidence_exc:
            print(
                "MS5A SEARCH failure evidence write failed: " + type(evidence_exc).__name__,
                file=sys.stderr,
            )
        raise
    finally:
        for signum, previous_handler in previous_handlers.items():
            signal.signal(signum, previous_handler)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
