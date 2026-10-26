from __future__ import annotations

import cProfile
import hashlib
import io
import json
import os
import platform
import pstats
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wave2b_runtime_slo import (
    measure_factual_routing,
    measure_router_paper_admission,
)


def _semantic_digest(result: dict[str, Any]) -> str:
    stable = {
        key: value
        for key, value in result.items()
        if key
        not in {
            "admission_latency",
            "admission_per_second",
            "events_per_second",
            "restart_reconcile_ms",
            "router_generation_per_second",
            "routing_latency",
        }
    }
    payload = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _profile(
    function: Callable[[int], dict[str, Any]],
    count: int,
) -> tuple[dict[str, Any], str]:
    profiler = cProfile.Profile()
    result = profiler.runcall(function, count)
    output = io.StringIO()
    pstats.Stats(profiler, stream=output).strip_dirs().sort_stats("cumulative").print_stats(50)
    return result, output.getvalue()


def _assert_admission_semantics(result: dict[str, Any]) -> None:
    assert result["accepted"] == result["count"]
    assert result["rejected"] == 0
    assert result["duplicate_same_result"] is True
    assert result["duplicate_order_side_effects"] == 0
    assert result["reconciliation_blocked"] is False
    assert len(result["semantic_digest"]) == 64


def _assert_factual_semantics(result: dict[str, Any]) -> None:
    assert result["consumer_order_calls"] == result["count"]
    assert result["duplicate_suppressed"] is True
    assert result["duplicate_consumer_calls"] == 0
    assert result["terminal_events"] == result["count"]
    assert len(result["semantic_digest"]) == 64


def test_wave2b_repeat_and_cprofile_evidence() -> None:
    report_dir = os.environ.get("REPORT_DIR")
    marker = Path(__file__).with_name("WAVE2B_REPEAT_PROFILE")
    if report_dir is None or not marker.exists() or sys.version_info[:2] != (3, 12):
        return

    admission_repeats: list[dict[str, Any]] = []
    factual_repeats: list[dict[str, Any]] = []
    for _ in range(2):
        admission = measure_router_paper_admission(1000)
        factual = measure_factual_routing(1000)
        _assert_admission_semantics(admission)
        _assert_factual_semantics(factual)
        admission_repeats.append(admission)
        factual_repeats.append(factual)

    profiled_admission, admission_profile = _profile(
        measure_router_paper_admission,
        1000,
    )
    profiled_factual, factual_profile = _profile(measure_factual_routing, 1000)
    _assert_admission_semantics(profiled_admission)
    _assert_factual_semantics(profiled_factual)

    evidence = {
        "schema_version": 1,
        "workload": "wave2b-repeat-and-cprofile",
        "count": 1000,
        "environment": {
            "python": sys.version.split()[0],
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python_hash_seed": os.environ.get("PYTHONHASHSEED"),
        },
        "baseline_full_artifact": {
            "artifact_id": 8835882167,
            "artifact_zip_sha256": (
                "4bd8ebb80112c73ddd481eeb5695e482d7e6bee319de15ddbca7a8cc7664e45d"
            ),
            "evidence_digest": ("c8b44843d3d6f96387c12cbed7e8fe66e4b3638c17b8c158dc201f920311a0e0"),
            "json_sha256": ("135e5bfdbe762777a66201dd49c22a11bb545fc22b179de3022c2b18575895d8"),
        },
        "admission_repeats": admission_repeats,
        "factual_repeats": factual_repeats,
        "profiled_admission": profiled_admission,
        "profiled_factual": profiled_factual,
        "semantic_digests": {
            "admission": [_semantic_digest(item) for item in admission_repeats],
            "factual": [_semantic_digest(item) for item in factual_repeats],
        },
    }
    evidence["evidence_digest"] = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    target = Path(report_dir)
    target.mkdir(parents=True, exist_ok=True)
    (target / "wave2b-repeat-profile.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (target / "wave2b-admission-cprofile.txt").write_text(
        admission_profile,
        encoding="utf-8",
    )
    (target / "wave2b-factual-cprofile.txt").write_text(
        factual_profile,
        encoding="utf-8",
    )
