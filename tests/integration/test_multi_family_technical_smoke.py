from __future__ import annotations

from pathlib import Path

from scripts.multi_family_technical_smoke import build_evidence


def test_multi_family_technical_smoke_reaches_terminal_handoff_without_authority(
    tmp_path: Path,
) -> None:
    evidence = build_evidence(
        root=tmp_path / "state",
        commit_sha="1" * 40,
    )

    assert evidence["contract"] == "multi-family-technical-smoke-v1"
    assert evidence["family_count"] == 3
    assert evidence["holdout_accessed"] is False
    assert evidence["promotion_attempted"] is False
    assert evidence["governed_search_authorized"] is False

    families = evidence["families"]
    assert [item["family"] for item in families] == [
        "breakout_return",
        "liquidity_sweep",
        "fvg_rt",
    ]
    for family in families:
        assert family["experiment_status"] == "completed"
        assert family["run_status_counts"] == {"succeeded": 1}
        assert len(family["run_ids"]) == 1
        assert len(family["result_artifact_sha256"]) == 64
        assert family["allocated_smoke_capacity"] == 1
        planning = family["strategy_factory_planning"]
        assert planning["architecture_id"].startswith("arch-")
        assert len(planning["architecture_fingerprint"]) == 64
        assert planning["parameter_space_id"].startswith("space-")
        assert len(planning["parameter_space_fingerprint"]) == 64
        assert planning["allocated_smoke_capacity"] == 1
        assert family["trusted_runtime"]["spec_id"] == family["family"]
        assert len(family["research_specification_fingerprint"]) == 64
        assert family["holdout_accessed"] is False
        assert family["final_test_grants"] == 0
        assert family["promotion_attempted"] is False
        assert family["execution_path"] == "strategy_factory_structural_screening_handler"
        assert family["factory_checkpoint_stage"] == "coarse_search"
        assert family["restart_verified"] is True
        assert len(family["structural_semantic_sha256"]) == 64

        signal = family["signal_case"]
        assert signal["legitimate_no_signal"] is False
        assert signal["counts"] == {
            "source_features": 1,
            "canonical_setups": 1,
            "entry_plans": 1,
            "protection_plans": 1,
            "risk_requests": 1,
            "risk_decisions": 1,
            "executable_plans": 1,
        }
        assert len(signal["executable_plan_digest"]) == 64

        no_signal = family["no_signal_case"]
        assert no_signal["legitimate_no_signal"] is True
        assert set(no_signal["counts"].values()) == {0}
