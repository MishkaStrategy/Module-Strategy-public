#!/usr/bin/env python3
"""Deep-verify and freeze the exact MS5A owner request without executing SEARCH."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from strategy_factory.multi_strategy_owner import MS5A_REQUEST_CREATED_AT
from strategy_factory.multi_strategy_search import _git_provenance, freeze_ms5a_request


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--market-data-root", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    _git_provenance(args.expected_head)
    frozen = freeze_ms5a_request(
        market_data_root=args.market_data_root,
        runtime_root=args.runtime_root,
        source_commit_sha=args.expected_head,
        created_at=MS5A_REQUEST_CREATED_AT,
    )
    payload = {
        **dict(frozen.summary),
        "contract": "ms5a-owner-search-preflight-v2",
        "request_created_at": frozen.request.created_at.isoformat(),
        "research_executed": False,
        "search_authorized": False,
        "walk_forward_executed": False,
        "holdout_opened": False,
        "final_test_grants": 0,
        "paper_authorized": False,
        "live_authorized": False,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
