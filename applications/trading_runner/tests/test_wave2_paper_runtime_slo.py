from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from wave2b_profile import run_profile, write_profile


def _assert_semantics(result: dict[str, object]) -> None:
    semantic = result["semantic_zero_tolerance"]
    assert isinstance(semantic, dict)
    assert semantic == {
        "acknowledged_loss": 0,
        "capacity_side_effects": 0,
        "duplicate_fills": 0,
        "duplicate_positions": 0,
        "live_execution": 0,
        "ordering_violations": 0,
        "unauthorized_admission": 0,
    }

    admissions = result["router_paper_admission"]
    assert isinstance(admissions, list)
    for item in admissions:
        assert item["accepted"] == item["count"]
        assert item["rejected"] == 0
        assert item["duplicate_order_side_effects"] == 0
        assert item["duplicate_same_result"] is True
        assert item["reconciliation_blocked"] is False
        assert len(item["semantic_digest"]) == 64

    lifecycles = result["paper_fill_lifecycle"]
    assert isinstance(lifecycles, list)
    for item in lifecycles:
        assert item["completed_lifecycles"] == item["count"]
        assert item["duplicate_event_mutations"] == 0
        assert item["duplicate_fill_quantity"] == "0"
        assert len(item["semantic_digest"]) == 64

    factual = result["runner_factual_routing"]
    assert isinstance(factual, list)
    for item in factual:
        assert item["consumer_order_calls"] == item["count"]
        assert item["duplicate_suppressed"] is True
        assert item["duplicate_consumer_calls"] == 0
        assert item["terminal_events"] == item["count"]
        assert len(item["semantic_digest"]) == 64

    recovery = result["runner_factual_recovery"]
    assert recovery == {
        "acknowledged_loss": 0,
        "consumer_order_calls": 1,
        "duplicate_after_recovery": True,
        "duplicate_fills": 0,
        "duplicate_positions": 0,
        "ordering_violations": 0,
        "recovery_ms": recovery["recovery_ms"],
        "workload": "runner_factual_recovery",
    }

    capacity = result["upstream_capacity"]
    assert capacity["duplicate_suppressed_at_capacity"] is True
    assert capacity["pending_slot_released_exactly_once"] is True
    assert capacity["paper_admission_calls_after_capacity_failure"] == 0
    assert capacity["pending_error"] == {
        "current": 1,
        "limit": 1,
        "resource": "pending_signals",
    }
    assert capacity["total_error"] == {
        "current": 2,
        "limit": 2,
        "resource": "total_signals",
    }


def test_wave2b_smoke_profile_is_deterministic_and_fail_closed() -> None:
    first = run_profile("smoke")
    second = run_profile("smoke")
    _assert_semantics(first)
    _assert_semantics(second)

    first_admission = first["router_paper_admission"]
    second_admission = second["router_paper_admission"]
    assert [item["semantic_digest"] for item in first_admission] == [
        item["semantic_digest"] for item in second_admission
    ]
    first_lifecycle = first["paper_fill_lifecycle"]
    second_lifecycle = second["paper_fill_lifecycle"]
    assert [item["semantic_digest"] for item in first_lifecycle] == [
        item["semantic_digest"] for item in second_lifecycle
    ]
    first_factual = first["runner_factual_routing"]
    second_factual = second["runner_factual_routing"]
    assert [item["semantic_digest"] for item in first_factual] == [
        item["semantic_digest"] for item in second_factual
    ]


def test_wave2b_profile_writes_closed_json_evidence(tmp_path: Path) -> None:
    output = tmp_path / "wave2b.json"
    result = write_profile("smoke", output)
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded == result
    assert len(loaded["evidence_digest"]) == 64
    _assert_semantics(loaded)


def test_wave2b_ci_profile_writes_report_artifact() -> None:
    report_dir = os.environ.get("REPORT_DIR")
    if report_dir is None:
        return
    full_marker = Path(__file__).with_name("WAVE2B_FULL_PROFILE")
    profile = "full" if full_marker.exists() and sys.version_info[:2] == (3, 12) else "smoke"
    output = Path(report_dir) / f"wave2-paper-runtime-{profile}.json"
    result = write_profile(profile, output)
    _assert_semantics(result)
    assert output.is_file()
