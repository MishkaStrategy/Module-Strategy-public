from __future__ import annotations

import ast
from pathlib import Path

import pytest
from modular_core import MarketView

from market_regime import MarketRegimeModule, SkipReason

from conftest import candle, feed, view_for


@pytest.mark.slow
def test_round_21_checkpoint_size_plateaus_under_longer_streams(small_kwargs, start) -> None:
    module = MarketRegimeModule(**small_kwargs)
    sizes: list[int] = []
    for index in range(10000):
        module.on_bar(view_for(candle(start, index, 100 + index * 0.001)))
        if index + 1 in {1000, 5000, 10000}:
            import json

            sizes.append(
                len(
                    json.dumps(
                        module.snapshot_state(), sort_keys=True, separators=(",", ":")
                    ).encode()
                )
            )
    assert max(sizes) - min(sizes) < 256
    assert max(sizes) < 16384


def test_round_22_benchmark_evidence_does_not_depend_on_assertions() -> None:
    benchmark = Path(__file__).parents[1] / "benchmarks" / "benchmark.py"
    tree = ast.parse(benchmark.read_text(encoding="utf-8"), filename=str(benchmark))
    offenders = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Assert)]
    assert offenders == []


def test_round_23_checked_in_standard_benchmark_is_deterministic() -> None:
    import json

    evidence = Path(__file__).parents[1] / "artifacts" / "resumption_round23_standard.json"
    report = json.loads(evidence.read_text(encoding="utf-8"))
    assert report["events"] == 50000
    assert report["runs"] == 2
    assert report["deterministic_outputs"] is True
    assert report["deterministic_states"] is True
    assert report["maximum_checkpoint_bytes"] < 16384
    assert report["median_throughput"] > 1000


def test_round_24_extended_restart_matches_continuous_evidence() -> None:
    import json

    root = Path(__file__).parents[1] / "artifacts"
    continuous = json.loads(
        (root / "resumption_round24_extended_continuous.json").read_text(encoding="utf-8")
    )
    restarted = json.loads(
        (root / "resumption_round24_extended_restart.json").read_text(encoding="utf-8")
    )
    assert continuous["events"] == restarted["events"] == 250000
    assert continuous["samples"][0]["output_digest"] == restarted["samples"][0]["output_digest"]
    assert continuous["samples"][0]["state_digest"] == restarted["samples"][0]["state_digest"]
    assert continuous["samples"][0]["feature_count"] == restarted["samples"][0]["feature_count"]
    assert continuous["samples"][0]["publications"] == restarted["samples"][0]["publications"]
    assert (
        max(continuous["maximum_checkpoint_bytes"], restarted["maximum_checkpoint_bytes"]) < 16384
    )


def test_round_25_every_skip_reason_is_typed_in_diagnostics(small_kwargs, start) -> None:
    observed: set[str] = set()
    empty = MarketRegimeModule(**small_kwargs).on_bar(
        MarketView(
            base_bars=(),
            timeframe_bars={},
            index=0,
            cash=1,
            equity=1,
            position=None,
            pending_orders=(),
        )
    )
    observed.add(empty.diagnostics["reason"])
    no_target_module = MarketRegimeModule(**small_kwargs)
    no_target = no_target_module.on_bar(view_for(candle(start, 0, 100, timeframe="5m", minutes=5)))
    observed.add(no_target.diagnostics["reason"])
    not_ready_module = MarketRegimeModule(**small_kwargs)
    not_ready = not_ready_module.on_bar(view_for(candle(start, 0, 100)))
    observed.add(not_ready.diagnostics["reason"])
    gap_module = MarketRegimeModule(**small_kwargs)
    gap = gap_module.on_bar(view_for(candle(start, 0, 100), data_gap=True))
    observed.add(gap.diagnostics["reason"])
    timeframe_gap_module = MarketRegimeModule(**small_kwargs)
    timeframe_gap_module.on_bar(view_for(candle(start, 0, 100)))
    timeframe_gap = timeframe_gap_module.on_bar(view_for(candle(start, 2, 102)))
    observed.add(timeframe_gap.diagnostics["reason"])
    duplicate_module = MarketRegimeModule(**small_kwargs)
    duplicate_bar = candle(start, 0, 100)
    duplicate_module.on_bar(view_for(duplicate_bar))
    duplicate = duplicate_module.on_bar(view_for(duplicate_bar))
    observed.add(duplicate.diagnostics["reason"])
    stale_module = MarketRegimeModule(**small_kwargs)
    target = candle(start, 0, 100)
    stale_module.on_bar(view_for(candle(start, 11, 100, timeframe="5m", minutes=5), target=target))
    stale = stale_module.on_bar(
        view_for(candle(start, 12, 100, timeframe="5m", minutes=5), target=target)
    )
    observed.add(stale.diagnostics["reason"])
    incomplete_module = MarketRegimeModule(**small_kwargs)
    incomplete = incomplete_module.on_bar(
        view_for(candle(start, 0, 100, timeframe="5m", minutes=5), target=candle(start, 0, 100))
    )
    observed.add(incomplete.diagnostics["reason"])
    assert observed == {reason.value for reason in SkipReason}


def test_round_26_health_counters_are_monotonic_across_reset_and_restart(
    small_kwargs, start
) -> None:
    module = MarketRegimeModule(**small_kwargs)

    def health() -> tuple[int, int, int, int]:
        state = module.snapshot_state()
        return (
            state["data_gap_resets"],
            state["timeframe_gap_resets"],
            state["duplicate_events"],
            state["publications"],
        )

    samples = [health()]
    bars = [candle(start, index, 100 + index) for index in range(20)]
    feed(module, bars)
    samples.append(health())
    module.on_bar(view_for(bars[-1]))
    samples.append(health())
    module.on_bar(view_for(candle(start, 21, 121), data_gap=True))
    samples.append(health())
    restored = MarketRegimeModule(**small_kwargs)
    restored.restore_state(module.snapshot_state())
    module = restored
    samples.append(health())
    module.on_bar(view_for(candle(start, 23, 123)))
    module.on_bar(view_for(candle(start, 25, 125)))
    samples.append(health())
    for previous, current in zip(samples, samples[1:], strict=False):
        assert all((after >= before for before, after in zip(previous, current, strict=True)))


def test_round_27_reproducible_sbom_and_security_evidence_are_packaged() -> None:
    import json

    artifacts = Path(__file__).parents[1] / "artifacts"
    sbom = json.loads((artifacts / "resumption_round27_sbom.json").read_text(encoding="utf-8"))
    component = sbom["metadata"]["component"]
    assert component["type"] == "library"
    assert component["name"] == "modular-strategy-market-regime"
    assert component["version"] == "1.2.0"
    dependencies = {(item["name"], item["version"]) for item in sbom["components"]}
    assert ("modular-strategy-core", "0.4.0") in dependencies
    assert ("PyYAML", "6.0.3") in dependencies
    scan = json.loads(
        (artifacts / "resumption_round27_detect_secrets.json").read_text(encoding="utf-8")
    )
    findings = [item for items in scan["results"].values() for item in items]
    assert len(findings) == 1
    assert findings[0]["filename"].endswith("tests/fixture_v1_real_checkpoint.json")
    assert findings[0]["type"] == "Hex High Entropy String"
    assert findings[0]["is_verified"] is False


def test_round_28_repository_workflows_follow_runner_rules() -> None:
    workflows = Path(__file__).parents[3] / ".github" / "workflows"
    fast = workflows / "market-regime-ci.yml"
    heavy = workflows / "market-regime-heavy.yml"
    if not fast.exists() or not heavy.exists():
        pytest.skip("repository workflows are intentionally absent from the source distribution")
    fast_text = fast.read_text(encoding="utf-8")
    heavy_text = heavy.read_text(encoding="utf-8")
    assert fast_text.count("runs-on: [self-hosted, fast]") == 1
    assert heavy_text.count("runs-on: [self-hosted, fast]") == 3
    for physical_label in ("Linux", "X64", "macOS", "ARM64", "backtest"):
        assert physical_label not in fast_text
        assert physical_label not in heavy_text
    assert "workflow_dispatch:" not in fast_text
    assert "push:" in fast_text
    assert "pull_request:" in fast_text
    assert 'pytest modules/market_regime/tests \\\n            -m "not slow"' in fast_text
    assert "workflow_dispatch:" in heavy_text
    assert "push:" not in heavy_text
    assert "pull_request:" not in heavy_text
    assert "pytest modules/market_regime/tests -m slow" in heavy_text
    assert "timeout-minutes:" in fast_text
    assert "timeout-minutes:" in heavy_text
    assert "retention-days: 14" in fast_text
    assert "retention-days: 14" in heavy_text


def test_round_29_clean_installed_lifecycle_matches_restart_evidence() -> None:
    import json

    evidence = (
        Path(__file__).parents[1] / "artifacts" / "resumption_round29_installed_lifecycle.json"
    )
    report = json.loads(evidence.read_text(encoding="utf-8"))
    assert report["distribution_version"] == report["module_version"] == "1.2.0"
    assert report["entry_point"] == "pass"
    assert report["core_registry"] == "pass"
    assert report["causality_reason"] == "market_regime.incomplete_bar"
    assert report["events"] == 5000
    assert report["restart_at"] == 2500
    assert report["continuous"] == report["restarted"]
    assert report["continuous"]["checkpoint_bytes"] < 16384
    assert report["continuous"]["publications"] > 0
    assert report["continuous"]["feature_count"] == 3 * report["continuous"]["publications"]


def test_round_30_release_evidence_is_complete_and_self_consistent() -> None:
    import hashlib
    import json

    artifacts = Path(__file__).parents[1] / "artifacts"
    report = json.loads((artifacts / "resumption_release_evidence.json").read_text())
    assert report["component"] == "market_regime"
    assert report["component_version"] == "1.2.0"
    assert report["core_version"] == "0.4.0"
    assert report["error_discovery_defects_fixed"] == 7
    assert report["completeness_rounds"] == 3
    assert report["hardening_rounds"] == 30
    assert report["intermediate_audits"] == 15
    assert report["coverage_percent"] >= report["coverage_gate_percent"]
    assert report["benchmark"]["output_digest"] == report["restart"]["output_digest"]
    assert report["benchmark"]["state_digest"] == report["restart"]["state_digest"]
    assert report["memory"]["peak_traced_bytes"] < 1000000
    assert report["benchmark"]["maximum_checkpoint_bytes"] < 16384
    assert report["quality_gates"]["pip_audit"] == "not_run_dns_unavailable"
    for name, expected in report["evidence_sha256"].items():
        actual = hashlib.sha256((artifacts / name).read_bytes()).hexdigest()
        assert actual == expected
