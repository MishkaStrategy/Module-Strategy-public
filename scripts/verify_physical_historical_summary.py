from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


class HistoricalSummaryError(ValueError):
    pass


def _require_bool(payload: dict[str, Any], key: str, expected: bool) -> None:
    value = payload.get(key)
    if value is not expected:
        raise HistoricalSummaryError(f"{key} must be {expected!r}, got {value!r}")


def validate_summary(path: Path, *, expected_head: str, expected_base: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise HistoricalSummaryError("historical PASS summary is missing or symlinked")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HistoricalSummaryError(
            "historical PASS summary is unreadable or invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise HistoricalSummaryError("historical PASS summary must be a JSON object")
    if payload.get("schema_version") != 1:
        raise HistoricalSummaryError("historical PASS summary schema_version must be 1")
    if payload.get("status") != "PASS":
        raise HistoricalSummaryError("historical PASS summary status must be PASS")
    if payload.get("candidate_sha") != expected_head:
        raise HistoricalSummaryError("historical PASS summary candidate SHA mismatch")
    if payload.get("full_rc_sha") != expected_base:
        raise HistoricalSummaryError("historical PASS summary full-RC SHA mismatch")
    if payload.get("host_os") != "Darwin" or payload.get("host_arch") != "arm64":
        raise HistoricalSummaryError("historical PASS summary physical host identity mismatch")
    _require_bool(payload, "canonical_prepare", True)
    _require_bool(payload, "canonical_smoke_compare_restart_replay", True)
    _require_bool(payload, "canonical_full_historical", True)
    _require_bool(payload, "paper_only", True)
    _require_bool(payload, "live_trading_authorized", False)
    _require_bool(payload, "final_24h_soak_complete", False)
    _require_bool(payload, "raw_market_data_uploaded", False)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the physical historical PASS sentinel.")
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--expected-base", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        validate_summary(
            args.summary,
            expected_head=args.expected_head,
            expected_base=args.expected_base,
        )
    except HistoricalSummaryError as exc:
        print(f"historical PASS sentinel validation failed: {exc}")
        return 1
    print("historical PASS sentinel validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
