from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from strategy_factory import ms5a_budget_probe as probe
from strategy_factory.canonical import canonical_json_bytes, canonical_sha256, strict_json_loads
from strategy_factory.errors import CompatibilityError


def test_diagnostic_runtime_pins_owner_diagnostics_candidate_and_digest() -> None:
    owner_path = "research/strategy_factory/src/strategy_factory/multi_strategy_owner.py"

    assert probe.SOURCE_SHA == "408d09c05435292d6995060a5d2fd0d4feb0f16e"
    assert probe.FROZEN_LINEAGE_SHA == probe.SOURCE_SHA
    assert probe.DIAGNOSTIC_RUNTIME_SHA == "24501b6d7259c2b8d9eacf1ba2ddbebb94a07c55"
    assert probe.DIAGNOSTIC_RUNTIME_FILE_SHA256[owner_path] == (
        "83002d75b2a96d2b6e9ef9e0c70d365d65d72e315ea9c472637bec4c8a4a3eed"
    )


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def _artifact(root: Path, artifact_type: str, payload: Any) -> str:
    payload_hash = canonical_sha256(payload)
    artifact_id = f"{artifact_type}-{payload_hash}"
    _write_json(
        root / "artifacts" / artifact_type / f"{payload_hash}.json",
        {
            "artifact_type": artifact_type,
            "artifact_id": artifact_id,
            "payload_hash": payload_hash,
            "payload": payload,
        },
    )
    return artifact_id


@pytest.fixture
def frozen_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, factory_request: Any) -> Any:
    source_sha = "a" * 40
    run_id = (
        f"sf-run-{canonical_sha256((factory_request.request_id, factory_request.fingerprint))[:24]}"
    )
    monkeypatch.setattr(probe, "SOURCE_SHA", source_sha)
    monkeypatch.setattr(probe, "_RUN_ID", run_id)

    root = tmp_path / "runtime"
    request_payload = strict_json_loads(canonical_json_bytes(factory_request))
    _write_json(root / "frozen-request.json", request_payload)
    _write_json(
        root / "frozen-request-meta.json",
        {
            "source_commit_sha": source_sha,
            "request_fingerprint": factory_request.fingerprint,
            "runtime_surface_fingerprint": probe.EXPECTED_RUNTIME_SURFACE_FINGERPRINT,
        },
    )
    plans = {
        "plans": [
            {
                "strategy_family": family,
                "factory_plan": {
                    "plan_id": f"plan-{index}",
                    "resource_limits": {"native_experiment_id": f"experiment-{index}"},
                    "research_specification": {"research_id": f"research-{index}"},
                },
            }
            for index, family in enumerate(probe.FAMILIES, start=1)
        ]
    }
    completed = {
        "request": _artifact(
            root / "execution" / "factory" / run_id, "factory_request", request_payload
        ),
        "experiment_planning": _artifact(
            root / "execution" / "factory" / run_id, "experiment_plans", plans
        ),
    }
    state = {
        "run_id": run_id,
        "commit_sha": source_sha,
        "request_fingerprint": factory_request.fingerprint,
        "dataset_fingerprint": factory_request.dataset.fingerprint,
        "completed_artifacts": completed,
        "submitted_batches": {"research-plan-1": {}, "research-plan-2": {}},
    }
    _write_json(
        root / "execution" / "factory" / run_id / "checkpoints" / f"{run_id}.json",
        {"payload": state, "payload_hash": canonical_sha256(state)},
    )
    return SimpleNamespace(
        root=root,
        run_id=run_id,
        request=factory_request,
        plans=plans,
        state=state,
        completed=completed,
    )


def test_select_frozen_cases_accepts_exact_synthetic_lineage_and_only_reads(
    tmp_path, frozen_runtime
):
    before = {
        path.relative_to(frozen_runtime.root): path.read_bytes()
        for path in frozen_runtime.root.rglob("*")
        if path.is_file()
    }

    bindings, cases = probe.select_frozen_cases(frozen_runtime.root)

    assert tuple(case.family for case in cases) == probe.FAMILIES
    assert tuple(case.submitted for case in cases) == (True, True, False)
    assert tuple(case.plan_hash for case in cases) == tuple(
        canonical_sha256(plan["factory_plan"]) for plan in frozen_runtime.plans["plans"]
    )
    assert bindings["request_fingerprint"] == frozen_runtime.request.fingerprint
    assert bindings["dataset_fingerprint"] == frozen_runtime.request.dataset.fingerprint
    assert bindings["runtime_surface_fingerprint"] == probe.EXPECTED_RUNTIME_SURFACE_FINGERPRINT
    after = {
        path.relative_to(frozen_runtime.root): path.read_bytes()
        for path in frozen_runtime.root.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not (frozen_runtime.root / "market-data").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_commit_sha", "b" * 40),
        ("request_fingerprint", "f" * 64),
        ("runtime_surface_fingerprint", "f" * 64),
    ],
)
def test_frozen_metadata_binding_mismatch_fails_closed(frozen_runtime, field, value):
    metadata_path = frozen_runtime.root / "frozen-request-meta.json"
    metadata = json.loads(metadata_path.read_text())
    metadata[field] = value
    _write_json(metadata_path, metadata)

    with pytest.raises(CompatibilityError):
        probe.select_frozen_cases(frozen_runtime.root)


def test_checkpoint_dataset_mismatch_fails_closed_even_with_valid_checkpoint_hash(frozen_runtime):
    checkpoint_path = (
        frozen_runtime.root
        / "execution"
        / "factory"
        / frozen_runtime.run_id
        / "checkpoints"
        / f"{frozen_runtime.run_id}.json"
    )
    checkpoint = json.loads(checkpoint_path.read_text())
    checkpoint["payload"]["dataset_fingerprint"] = "f" * 64
    checkpoint["payload_hash"] = canonical_sha256(checkpoint["payload"])
    _write_json(checkpoint_path, checkpoint)

    with pytest.raises(CompatibilityError, match="checkpoint binding"):
        probe.select_frozen_cases(frozen_runtime.root)


def test_referenced_artifact_payload_tamper_fails_closed(frozen_runtime):
    artifact_id = frozen_runtime.completed["experiment_planning"]
    artifact_type, digest = artifact_id.rsplit("-", 1)
    path = (
        frozen_runtime.root
        / "execution"
        / "factory"
        / frozen_runtime.run_id
        / "artifacts"
        / artifact_type
        / f"{digest}.json"
    )
    envelope = json.loads(path.read_text())
    envelope["payload"]["plans"].pop()
    _write_json(path, envelope)

    with pytest.raises(CompatibilityError, match="artifact hash validation"):
        probe.select_frozen_cases(frozen_runtime.root)


def test_missing_frozen_evidence_fails_closed(tmp_path):
    with pytest.raises(CompatibilityError, match="runtime root"):
        probe.select_frozen_cases(tmp_path / "missing")


def test_project_native_plan_uses_initial_optimization_and_reports_no_runtime_claim(
    monkeypatch: pytest.MonkeyPatch,
):
    roots: list[Path] = []
    parameters = {
        "factory_run_id": probe._RUN_ID,
        "modules": (
            "breakout_return@1.0.0",
            "setup_adapter.breakout_return_v1@0.1.0",
            "entry_model@0.2.0",
            "protection_target.protection@1.1.1",
            "protection_target.target@1.1.1",
            "risk_request_composer@0.1.0",
            "risk_sizing@1.2.0",
        ),
        "breakout_return.lookback_bars": 12,
    }

    class FakeBuilder:
        def __init__(self, root: Path):
            roots.append(root)

        def build(self, *, experiment_id: str, specification: Any) -> Any:
            assert experiment_id == "synthetic-experiment"
            assert specification == "synthetic-specification"
            job = SimpleNamespace(
                stage_role="optimization_000", period=object(), parameters=parameters
            )
            return SimpleNamespace(jobs=(job,))

    monkeypatch.setitem(
        sys.modules,
        "modular_research_runner",
        SimpleNamespace(ResearchPlanBuilder=FakeBuilder),
    )
    monkeypatch.setattr(probe, "_native_specification", lambda _value: "synthetic-specification")
    case = probe.FrozenCase(
        "breakout_return", "a" * 64, {"research_id": "frozen"}, "synthetic-experiment", True
    )

    report = probe.project_native_plans((case,))[0]

    assert roots == [Path("/ms5a-plan-only")]
    assert report["native_stage_role"] == "optimization_000"
    assert report["static_branch_history_minimum"] == {"base": 12}
    assert report["manifest_history_bars_by_timeframe"] == {}
    assert all(item["runtime_readiness"] == "unverified" for item in report["prefixes"])


def test_project_native_plan_rejects_wrong_lineage_and_empty_plan(monkeypatch):
    class FakeBuilder:
        def __init__(self, _root: Path):
            pass

        def build(self, *, experiment_id: str, specification: Any) -> Any:
            if experiment_id == "empty":
                return SimpleNamespace(jobs=())
            job = SimpleNamespace(
                stage_role="optimization_000",
                period=object(),
                parameters={"factory_run_id": "wrong"},
            )
            return SimpleNamespace(jobs=(job,))

    monkeypatch.setitem(
        sys.modules,
        "modular_research_runner",
        SimpleNamespace(ResearchPlanBuilder=FakeBuilder),
    )
    monkeypatch.setattr(probe, "_native_specification", lambda _value: object())
    case = probe.FrozenCase("breakout_return", "a" * 64, {}, "empty", True)
    with pytest.raises(CompatibilityError, match="no SEARCH jobs"):
        probe.project_native_plans((case,))

    case = probe.FrozenCase("breakout_return", "a" * 64, {}, "wrong-lineage", True)
    with pytest.raises(CompatibilityError, match="lineage"):
        probe.project_native_plans((case,))


def test_plan_report_shape_and_read_only_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    bindings = {
        "source_commit_sha": "a" * 40,
        "request_fingerprint": "b" * 64,
        "dataset_fingerprint": "c" * 64,
        "dataset_sha256": "d" * 64,
        "runtime_surface_fingerprint": probe.EXPECTED_RUNTIME_SURFACE_FINGERPRINT,
    }
    cases = tuple(
        probe.FrozenCase(family, f"{i:064x}", {"research_id": family}, f"exp-{i}", False)
        for i, family in enumerate(probe.FAMILIES, start=1)
    )
    monkeypatch.setattr(probe, "select_frozen_cases", lambda _root: (bindings, cases))
    monkeypatch.setattr(
        probe,
        "project_native_plans",
        lambda _cases: tuple({"family": case.family} for case in cases),
    )

    report = probe.plan_report(tmp_path)

    assert report["authority_required"] is True
    assert report["execution_implemented"] is False
    assert report["candidate_metrics_included"] is False
    assert tuple(item["family"] for item in report["cases"]) == probe.FAMILIES
    assert report["authority_manifest_template"]["coverage_claim"] == "incomplete_diagnostic_only"
    assert report["authority_manifest_template"]["frozen_lineage_source_commit_sha"] == (
        "408d09c05435292d6995060a5d2fd0d4feb0f16e"
    )
    assert report["authority_manifest_template"]["runtime_source_commit_sha"] == (
        "24501b6d7259c2b8d9eacf1ba2ddbebb94a07c55"
    )
    assert (
        report["authority_manifest_template"]["runtime_file_sha256"][
            "research/strategy_factory/src/strategy_factory/multi_strategy_owner.py"
        ]
        == "83002d75b2a96d2b6e9ef9e0c70d365d65d72e315ea9c472637bec4c8a4a3eed"
    )


def test_cli_returns_zero_for_plan_and_nonzero_with_sanitized_error(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(probe, "plan_report", lambda _root: {"execution_implemented": False})
    assert probe.main(["--runtime-root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {"execution_implemented": False}

    def fail(_root):
        raise CompatibilityError("private detail must not escape")

    monkeypatch.setattr(probe, "plan_report", fail)
    assert probe.main(["--runtime-root", str(tmp_path)]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "blocked",
        "reason_code": "CompatibilityError",
    }
