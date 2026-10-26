#!/usr/bin/env python3
"""Reproducible Strategy Governance benchmark with no network or trading access."""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import sqlite3
import statistics
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from strategy_audit_validation.canonical import canonical_sha256
from strategy_audit_validation.governance import (
    COMPOSITION_SCHEMA_VERSION,
    ApprovalAuthority,
    ApprovalOutcome,
    AuditCheckpoint,
    GovernanceLedger,
    ImmutableEvidence,
    ModuleContract,
    load_composition,
)

SOURCE_SHA = "a" * 40
NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
SIGNING_KEY = b"wave2f-benchmark-independent-approval-key-32+"
WORKLOAD_ID = "strategy-governance-synthetic-v1"


def _contracts() -> dict[str, ModuleContract]:
    return {
        "module.signal": ModuleContract(
            module_id="module.signal",
            module_version="1.0.0",
            required_parameters=frozenset({"threshold"}),
            allowed_timeframes=frozenset({"15m"}),
            contract_complete=True,
            source_path="modules/signal.py",
            source_commit_sha=SOURCE_SHA,
        ),
        "module.execution": ModuleContract(
            module_id="module.execution",
            module_version="2.0.0",
            required_parameters=frozenset({"ttl"}),
            allowed_timeframes=frozenset({"5m"}),
            contract_complete=True,
            source_path="modules/execution.py",
            source_commit_sha=SOURCE_SHA,
        ),
    }


def _composition(index: int) -> dict[str, object]:
    return {
        "schema_version": COMPOSITION_SCHEMA_VERSION,
        "composition_name": WORKLOAD_ID,
        "composition_version": "1.0.0",
        "state": "CANDIDATE",
        "timezone": "UTC",
        "lookahead_allowed": False,
        "unfinished_htf_allowed": False,
        "created_by": "strategy-factory",
        "source_provenance": {
            "repository": "MishkaStrategy/Module-Strategy",
            "commit_sha": SOURCE_SHA,
            "paths": ["modules/signal.py", "modules/execution.py"],
        },
        "modules": [
            {
                "module_id": "module.signal",
                "module_version": "1.0.0",
                "role": "signal",
                "parameters": {"threshold": f"0.{450000 + index:06d}"},
                "timeframes": ["15m"],
                "dependencies": [],
                "contract_complete": True,
                "source_provenance": {
                    "path": "modules/signal.py",
                    "commit_sha": SOURCE_SHA,
                },
            },
            {
                "module_id": "module.execution",
                "module_version": "2.0.0",
                "role": "execution",
                "parameters": {"ttl": 36 + (index % 12)},
                "timeframes": ["5m"],
                "dependencies": ["module.signal"],
                "contract_complete": True,
                "source_provenance": {
                    "path": "modules/execution.py",
                    "commit_sha": SOURCE_SHA,
                },
            },
        ],
        "execution_order": ["module.signal", "module.execution"],
        "dependency_order": ["module.signal", "module.execution"],
    }


def _evidence(
    candidate_id: str,
    composition_hash: str,
    *,
    index: int,
    payload_bytes: int = 0,
    stale: bool = False,
) -> dict[str, object]:
    generated_at = NOW - timedelta(days=8) if stale else NOW
    return {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "composition_hash": composition_hash,
        "composition_version": "1.0.0",
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "source_commit_sha": SOURCE_SHA,
        "provenance": {
            "workload_id": WORKLOAD_ID,
            "utc_only": True,
            "unfinished_htf_allowed": False,
            "lookahead_allowed": False,
        },
        "results": {
            "index": index,
            "deterministic_digest": canonical_sha256(
                {"candidate_id": candidate_id, "index": index}
            ),
            "large_payload": "x" * payload_bytes,
        },
    }


def _rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform == "darwin":
        return value
    return value * 1024


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile + 0.5)))
    return ordered[index]


def _metrics(latencies_ms: list[float]) -> dict[str, float]:
    return {
        "count": float(len(latencies_ms)),
        "mean_ms": statistics.fmean(latencies_ms) if latencies_ms else 0.0,
        "p50_ms": _percentile(latencies_ms, 0.50),
        "p95_ms": _percentile(latencies_ms, 0.95),
        "p99_ms": _percentile(latencies_ms, 0.99),
        "max_ms": max(latencies_ms, default=0.0),
    }


def _reader_probe(path: Path, iterations: int) -> int:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        total = 0
        for _ in range(iterations):
            row = connection.execute("SELECT COUNT(*) FROM composition_candidates").fetchone()
            total += int(row[0])
        return total
    finally:
        connection.close()


def _process_one(
    *,
    ledger: GovernanceLedger,
    authority: ApprovalAuthority,
    index: int,
    workload: str,
    large_payload_bytes: int,
    warm: bool,
) -> tuple[str, bool, bool]:
    source_index = 0 if workload == "repeated" else index
    payload = _composition(source_index)
    if workload == "mostly-invalid" and index % 4 == 1:
        payload["modules"][0]["module_id"] = "module.unknown"  # type: ignore[index]
    candidate = load_composition(payload, contracts=_contracts())
    evidence = ImmutableEvidence.from_mapping(
        _evidence(
            candidate.candidate_id,
            candidate.composition_hash,
            index=source_index,
            payload_bytes=large_payload_bytes if workload == "large" else 0,
            stale=workload == "mostly-invalid" and index % 4 == 2,
        )
    )
    evidence.verify_for(candidate, now=NOW, maximum_age=timedelta(days=7))
    candidate_inserted = ledger.register_candidate(candidate)
    evidence_inserted = ledger.register_evidence(evidence)
    checkpoint = AuditCheckpoint.create(
        session_id=f"session-{source_index:08d}",
        candidate_id=candidate.candidate_id,
        evidence_digest=evidence.evidence_digest,
        completed_steps=(
            ("composition", "evidence") if not warm else ("composition", "evidence", "replay")
        ),
    )
    ledger.save_checkpoint(checkpoint)
    if workload == "mostly-invalid" and index % 4 == 3:
        actor_id = "strategy-factory"
        authority_for_call = ApprovalAuthority(
            authority_id="strategy-factory",
            signing_key=SIGNING_KEY,
        )
    else:
        actor_id = "independent-auditor"
        authority_for_call = authority
    record = authority_for_call.issue(
        candidate=candidate,
        evidence=evidence,
        outcome=ApprovalOutcome.APPROVED_FOR_PAPER,
        actor_id=actor_id,
        actor_role="independent_auditor",
        now=NOW,
    )
    approval_inserted = ledger.record_approval(record, authority=authority_for_call)
    logical = canonical_sha256(
        {
            "candidate_id": candidate.candidate_id,
            "evidence_digest": evidence.evidence_digest,
            "approval_digest": record.approval_digest,
            "candidate_inserted": candidate_inserted,
            "evidence_inserted": evidence_inserted,
            "approval_inserted": approval_inserted,
        }
    )
    return logical, candidate_inserted, evidence_inserted


def _run_phase(
    *,
    ledger: GovernanceLedger,
    authority: ApprovalAuthority,
    workload: str,
    candidates: int,
    large_payload_bytes: int,
    warm: bool,
    start_index: int = 0,
) -> tuple[list[float], list[str], Counter[str], int, int]:
    latencies: list[float] = []
    logical: list[str] = []
    failures: Counter[str] = Counter()
    candidate_inserts = 0
    evidence_inserts = 0
    for index in range(start_index, start_index + candidates):
        started = time.perf_counter_ns()
        try:
            digest, candidate_inserted, evidence_inserted = _process_one(
                ledger=ledger,
                authority=authority,
                index=index,
                workload=workload,
                large_payload_bytes=large_payload_bytes,
                warm=warm,
            )
            logical.append(digest)
            candidate_inserts += int(candidate_inserted)
            evidence_inserts += int(evidence_inserted)
        except Exception as exc:  # benchmark classifies expected fail-closed paths
            failures[type(exc).__name__] += 1
            logical.append(canonical_sha256({"index": index, "failure": type(exc).__name__}))
        latencies.append((time.perf_counter_ns() - started) / 1_000_000)
    return latencies, logical, failures, candidate_inserts, evidence_inserts


def run(args: argparse.Namespace) -> dict[str, Any]:
    database = Path(args.database)
    database.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{database}{suffix}")
        if path.exists():
            path.unlink()

    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    rss_before = _rss_bytes()
    authority = ApprovalAuthority(
        authority_id="independent-auditor",
        signing_key=SIGNING_KEY,
    )
    restart_overhead_ms = 0.0
    logical: list[str] = []
    failures: Counter[str] = Counter()
    integrity_failures: Counter[str] = Counter()
    cold_latencies: list[float] = []
    warm_latencies: list[float] = []
    inserted_candidates = 0
    inserted_evidence = 0

    if args.workload == "interrupted":
        split = max(1, args.candidates // 2)
        ledger = GovernanceLedger(database)
        try:
            first = _run_phase(
                ledger=ledger,
                authority=authority,
                workload="valid",
                candidates=split,
                large_payload_bytes=args.large_payload_bytes,
                warm=False,
            )
            cold_latencies.extend(first[0])
            logical.extend(first[1])
            failures.update(first[2])
            inserted_candidates += first[3]
            inserted_evidence += first[4]
            ledger.verify_integrity()
        finally:
            ledger.close()
        restart_started = time.perf_counter_ns()
        ledger = GovernanceLedger(database)
        restart_overhead_ms = (time.perf_counter_ns() - restart_started) / 1_000_000
        try:
            for index in range(split):
                candidate = load_composition(_composition(index), contracts=_contracts())
                evidence = ImmutableEvidence.from_mapping(
                    _evidence(candidate.candidate_id, candidate.composition_hash, index=index)
                )
                ledger.resume_checkpoint(
                    session_id=f"session-{index:08d}",
                    candidate_id=candidate.candidate_id,
                    evidence_digest=evidence.evidence_digest,
                )
            second_count = args.candidates - split
            second = _run_phase(
                ledger=ledger,
                authority=authority,
                workload="valid",
                candidates=second_count,
                large_payload_bytes=args.large_payload_bytes,
                warm=False,
                start_index=split,
            )
            cold_latencies.extend(second[0])
            logical.extend(second[1])
            failures.update(second[2])
            inserted_candidates += second[3]
            inserted_evidence += second[4]
            ledger.verify_integrity()
            artifact_bytes = ledger.artifact_bytes()
            counts = dict(ledger.counts())
        finally:
            ledger.close()
    else:
        workload = args.workload
        ledger = GovernanceLedger(database)
        try:
            cold = _run_phase(
                ledger=ledger,
                authority=authority,
                workload=workload,
                candidates=args.candidates,
                large_payload_bytes=args.large_payload_bytes,
                warm=False,
            )
            cold_latencies.extend(cold[0])
            logical.extend(cold[1])
            failures.update(cold[2])
            inserted_candidates += cold[3]
            inserted_evidence += cold[4]
            if workload not in {"mostly-invalid", "corrupted"}:
                warm = _run_phase(
                    ledger=ledger,
                    authority=authority,
                    workload=workload,
                    candidates=args.candidates,
                    large_payload_bytes=args.large_payload_bytes,
                    warm=True,
                )
                warm_latencies.extend(warm[0])
                logical.extend(warm[1])
                failures.update(warm[2])
                inserted_candidates += warm[3]
                inserted_evidence += warm[4]
            if workload == "corrupted":
                row = ledger._connection.execute(
                    "SELECT evidence_digest FROM governance_evidence LIMIT 1"
                ).fetchone()
                if row is not None:
                    ledger._connection.execute(
                        "UPDATE governance_evidence SET payload_json = ? WHERE evidence_digest = ?",
                        (b"{}", str(row[0])),
                    )
                    try:
                        ledger.verify_integrity()
                    except Exception as exc:
                        integrity_failures[type(exc).__name__] += 1
                else:
                    integrity_failures["CorruptionFixtureUnavailable"] += 1
            else:
                ledger.verify_integrity()
            artifact_bytes = ledger.artifact_bytes()
            counts = dict(ledger.counts())
        finally:
            ledger.close()

    reader_wall_ms = 0.0
    reader_operations = 0
    if args.workload == "concurrent-readers" and args.readers > 0:
        reader_started = time.perf_counter_ns()
        with ThreadPoolExecutor(max_workers=args.readers) as executor:
            results = list(
                executor.map(
                    lambda _: _reader_probe(database, args.reader_iterations),
                    range(args.readers),
                )
            )
        reader_wall_ms = (time.perf_counter_ns() - reader_started) / 1_000_000
        reader_operations = args.readers * args.reader_iterations
        logical.append(canonical_sha256({"reader_results": results}))

    wall_seconds = time.perf_counter() - wall_started
    cpu_seconds = time.process_time() - cpu_started
    rss_peak = _rss_bytes()
    successful_attempts = len(cold_latencies) + len(warm_latencies) - sum(failures.values())
    processed_attempts = len(cold_latencies) + len(warm_latencies)
    throughput = processed_attempts / wall_seconds if wall_seconds else 0.0
    deterministic_digest = canonical_sha256(
        {
            "workload": args.workload,
            "candidate_count": args.candidates,
            "logical": logical,
            "attempt_failures": dict(sorted(failures.items())),
            "integrity_failures": dict(sorted(integrity_failures.items())),
            "counts": counts,
        }
    )
    return {
        "schema_version": 1,
        "benchmark": "strategy-governance-wave2f",
        "workload": {
            "id": WORKLOAD_ID,
            "mode": args.workload,
            "candidate_count": args.candidates,
            "large_payload_bytes": args.large_payload_bytes,
            "readers": args.readers,
            "reader_iterations": args.reader_iterations,
            "network_used": False,
            "exchange_credentials_used": False,
            "live_trading_used": False,
        },
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "pid": os.getpid(),
        },
        "metrics": {
            "attempts": processed_attempts,
            "successful_attempts": successful_attempts,
            "wall_seconds": wall_seconds,
            "cpu_seconds": cpu_seconds,
            "candidates_per_second": throughput,
            "cold_latency": _metrics(cold_latencies),
            "warm_latency": _metrics(warm_latencies),
            "peak_rss_bytes": rss_peak,
            "rss_delta_bytes": max(0, rss_peak - rss_before),
            "artifact_bytes": artifact_bytes,
            "artifact_bytes_per_requested_candidate": (
                artifact_bytes / args.candidates if args.candidates else 0.0
            ),
            "restart_overhead_ms": restart_overhead_ms,
            "reader_wall_ms": reader_wall_ms,
            "reader_operations": reader_operations,
            "inserted_candidates": inserted_candidates,
            "inserted_evidence": inserted_evidence,
            "duplicate_candidate_suppression": max(
                0, processed_attempts - inserted_candidates - sum(failures.values())
            ),
            "failure_counts": dict(sorted((failures + integrity_failures).items())),
            "attempt_failure_counts": dict(sorted(failures.items())),
            "integrity_failure_counts": dict(sorted(integrity_failures.items())),
            "durable_counts": counts,
        },
        "determinism": {
            "logical_digest": deterministic_digest,
            "timings_excluded_from_digest": True,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--workload",
        choices=(
            "valid",
            "repeated",
            "mostly-invalid",
            "large",
            "interrupted",
            "corrupted",
            "concurrent-readers",
        ),
        default="valid",
    )
    parser.add_argument("--candidates", type=int, default=100)
    parser.add_argument("--large-payload-bytes", type=int, default=262_144)
    parser.add_argument("--readers", type=int, default=4)
    parser.add_argument("--reader-iterations", type=int, default=100)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.candidates < 1:
        parser.error("--candidates must be positive")
    if args.large_payload_bytes < 0:
        parser.error("--large-payload-bytes cannot be negative")
    if args.database is None:
        args.database = Path(tempfile.mkdtemp(prefix="wave2f-governance-")) / "ledger.sqlite3"
    return args


def main() -> int:
    args = parse_args()
    result = run(args)
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
