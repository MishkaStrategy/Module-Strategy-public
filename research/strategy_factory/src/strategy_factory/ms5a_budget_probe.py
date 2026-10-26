"""Read-only selector for a bounded MS5A profiling proposal.

This command verifies frozen Factory artifacts and reports which proposed
prefixes reach the selected production module's warm-up. It never runs a
Backtest or opens Factory/Research state for writing.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from .canonical import canonical_sha256, strict_json_loads
from .contract_codec import decode_strategy_factory_request
from .errors import CompatibilityError

SOURCE_SHA = "408d09c05435292d6995060a5d2fd0d4feb0f16e"
FROZEN_LINEAGE_SHA = SOURCE_SHA
DIAGNOSTIC_RUNTIME_SHA = "24501b6d7259c2b8d9eacf1ba2ddbebb94a07c55"
DIAGNOSTIC_REPAIR_ID = "MishkaStrategy/Module-Strategy#1407@03b3f83"
DIAGNOSTIC_RUNTIME_FILE_SHA256 = {
    "modules/market_data_ingestion/src/market_data_ingestion/integrity.py": (
        "8d4141a0a6856bba7fd235f9eca54234809f0b715e450ac8f257fd7f8351a4c5"
    ),
    "modules/market_data_ingestion/src/market_data_ingestion/storage.py": (
        "e6f97dfa1dba11400bda10610c04bcfc53c72e1ec7bb0d6a3275546e2f7d240f"
    ),
    "research/strategy_factory/src/strategy_factory/multi_strategy_owner.py": (
        "83002d75b2a96d2b6e9ef9e0c70d365d65d72e315ea9c472637bec4c8a4a3eed"
    ),
}
PROTOCOL_SCHEMA_VERSION = 2
PROTOCOL_ID = "ms5a-bounded-profile-v2-repaired-runtime"
EXPECTED_RUNTIME_SURFACE_FINGERPRINT = (
    "e84520e50af54e65dc6b42558fe04182146e088eb7ad7c66066fb72a726e55e9"
)
FAMILIES = ("breakout_return", "fvg_rt", "liquidity_sweep")
WINDOWS = (1_000, 2_000, 4_000)
BASE_SECONDS = 5 * 60
_RUN_ID = "sf-run-4a2799648bcd52ca8ad7e185"
PER_CASE_SECONDS = 120
TOTAL_SECONDS = 20 * 60
MEMORY_BYTES = 8 * 1024**3


@dataclass(frozen=True, slots=True)
class FrozenCase:
    family: str
    plan_hash: str
    specification: Mapping[str, Any]
    experiment_id: str
    submitted: bool
    specification_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "specification_sha256", canonical_sha256(self.specification))

    def public(self) -> dict[str, Any]:
        if canonical_sha256(self.specification) != self.specification_sha256:
            raise CompatibilityError("MS5A probe frozen Research specification was mutated")
        return {
            "family": self.family,
            "plan_sha256": self.plan_hash,
            "specification_sha256": self.specification_sha256,
            "submitted_in_original_lineage": self.submitted,
        }


def authority_manifest(
    bindings: Mapping[str, Any], cases: Sequence[FrozenCase], authority_id: str
) -> dict[str, Any]:
    """Describe exact limits a future separately authorized execution must bind."""

    return {
        "schema_version": PROTOCOL_SCHEMA_VERSION,
        "action": "MS5A_BOUNDED_PROFILE_REPAIRED_RUNTIME_V2",
        "authority_id": authority_id,
        **dict(bindings),
        "frozen_lineage_source_commit_sha": bindings.get(
            "frozen_lineage_source_commit_sha", FROZEN_LINEAGE_SHA
        ),
        "runtime_source_commit_sha": bindings.get(
            "runtime_source_commit_sha", DIAGNOSTIC_RUNTIME_SHA
        ),
        "runtime_repair_id": bindings.get("runtime_repair_id", DIAGNOSTIC_REPAIR_ID),
        "runtime_file_sha256": bindings.get(
            "runtime_file_sha256", dict(DIAGNOSTIC_RUNTIME_FILE_SHA256)
        ),
        "controller_commit_sha": bindings.get(
            "controller_commit_sha", "plan-only-controller-binding-required"
        ),
        "cases": [case.public() for case in cases],
        "window_bars": list(WINDOWS),
        "per_case_timeout_seconds": PER_CASE_SECONDS,
        "total_timeout_seconds": TOTAL_SECONDS,
        "memory_bytes": MEMORY_BYTES,
        "workers": 1,
        "coverage_claim": "incomplete_diagnostic_only",
    }


def validate_authority(
    value: Any, bindings: Mapping[str, Any], cases: Sequence[FrozenCase]
) -> None:
    if not isinstance(value, Mapping):
        raise CompatibilityError("MS5A probe authority manifest must be an object")
    authority_id = value.get("authority_id")
    if not isinstance(authority_id, str) or not authority_id.strip():
        raise CompatibilityError("MS5A probe authority_id is required")
    if dict(value) != authority_manifest(bindings, cases, authority_id):
        raise CompatibilityError("MS5A probe authority differs from selected cases/limits")


def _read_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise CompatibilityError("MS5A probe evidence file is missing or unsafe")
    try:
        return strict_json_loads(path.read_bytes())
    except (OSError, ValueError, TypeError) as exc:
        raise CompatibilityError("MS5A probe evidence JSON is invalid") from exc


def _verified_artifact(root: Path, artifact_id: str) -> tuple[str, Any]:
    if not isinstance(artifact_id, str) or "-" not in artifact_id:
        raise CompatibilityError("MS5A probe checkpoint artifact reference is invalid")
    artifact_type, payload_hash = artifact_id.rsplit("-", 1)
    if len(payload_hash) != 64 or any(char not in "0123456789abcdef" for char in payload_hash):
        raise CompatibilityError("MS5A probe checkpoint artifact hash is invalid")
    envelope = _read_json(root / "artifacts" / artifact_type / f"{payload_hash}.json")
    if not isinstance(envelope, Mapping):
        raise CompatibilityError("MS5A probe artifact envelope is malformed")
    payload = envelope.get("payload")
    if (
        envelope.get("artifact_type") != artifact_type
        or envelope.get("artifact_id") != artifact_id
        or envelope.get("payload_hash") != payload_hash
        or canonical_sha256(payload) != payload_hash
    ):
        raise CompatibilityError("MS5A probe artifact hash validation failed")
    return artifact_type, payload


def _select_frozen_plans(
    payload: Mapping[str, Any], submitted: Mapping[str, Any]
) -> tuple[FrozenCase, ...]:
    plans = payload.get("plans")
    if not isinstance(plans, list):
        raise CompatibilityError("MS5A probe frozen plans are malformed")
    selected: list[FrozenCase] = []
    for family in FAMILIES:
        plan = next(
            (
                item
                for item in plans
                if isinstance(item, Mapping) and item.get("strategy_family") == family
            ),
            None,
        )
        if plan is None:
            raise CompatibilityError("MS5A probe has no frozen plan for every family")
        factory_plan = plan.get("factory_plan")
        if not isinstance(factory_plan, Mapping):
            raise CompatibilityError("MS5A probe frozen Factory plan is malformed")
        specification = factory_plan.get("research_specification")
        plan_id = factory_plan.get("plan_id")
        limits = factory_plan.get("resource_limits")
        experiment_id = limits.get("native_experiment_id") if isinstance(limits, Mapping) else None
        if not isinstance(specification, Mapping) or not isinstance(plan_id, str):
            raise CompatibilityError("MS5A probe frozen Research plan identity is malformed")
        if not isinstance(experiment_id, str) or not experiment_id:
            raise CompatibilityError("MS5A probe frozen Research experiment identity is missing")
        selected.append(
            FrozenCase(
                family=family,
                plan_hash=canonical_sha256(factory_plan),
                specification=specification,
                experiment_id=experiment_id,
                submitted=f"research-{plan_id}" in submitted,
            )
        )
    if len({case.plan_hash for case in selected}) != len(FAMILIES):
        raise CompatibilityError("MS5A probe family plan selection is not unique")
    return tuple(selected)


def select_frozen_cases(runtime_root: str | Path) -> tuple[dict[str, str], tuple[FrozenCase, ...]]:
    """Verify accepted frozen request, checkpoint, and referenced artifacts."""

    root = Path(runtime_root).expanduser()
    if root.is_symlink() or not root.is_dir():
        raise CompatibilityError("MS5A probe runtime root must be an existing real directory")
    request_raw = _read_json(root / "frozen-request.json")
    metadata = _read_json(root / "frozen-request-meta.json")
    if not isinstance(metadata, Mapping):
        raise CompatibilityError("MS5A probe frozen-request metadata is malformed")
    try:
        request = decode_strategy_factory_request(request_raw)
    except Exception as exc:
        raise CompatibilityError("MS5A probe frozen request is invalid") from exc
    runtime_fingerprint = _validate_runtime_surface_fingerprint(
        metadata.get("runtime_surface_fingerprint")
    )
    if (
        metadata.get("source_commit_sha") != SOURCE_SHA
        or metadata.get("request_fingerprint") != request.fingerprint
    ):
        raise CompatibilityError("MS5A probe frozen request binding differs from accepted source")

    factory_root = root / "execution" / "factory" / _RUN_ID
    checkpoint = _read_json(factory_root / "checkpoints" / f"{_RUN_ID}.json")
    if not isinstance(checkpoint, Mapping) or not isinstance(checkpoint.get("payload"), Mapping):
        raise CompatibilityError("MS5A probe checkpoint is malformed")
    state = checkpoint["payload"]
    if (
        checkpoint.get("payload_hash") != canonical_sha256(state)
        or state.get("run_id") != _RUN_ID
        or state.get("commit_sha") != SOURCE_SHA
        or state.get("request_fingerprint") != request.fingerprint
        or state.get("dataset_fingerprint") != request.dataset.fingerprint
    ):
        raise CompatibilityError("MS5A probe checkpoint binding/hash validation failed")
    completed = state.get("completed_artifacts")
    submitted = state.get("submitted_batches")
    if not isinstance(completed, Mapping) or not isinstance(submitted, Mapping):
        raise CompatibilityError("MS5A probe checkpoint references are malformed")
    expected_run = f"sf-run-{canonical_sha256((request.request_id, request.fingerprint))[:24]}"
    if expected_run != _RUN_ID:
        raise CompatibilityError("MS5A probe request does not derive the accepted Factory run")

    loaded: dict[str, tuple[str, Any]] = {}
    for stage, artifact_id in completed.items():
        artifact_type, payload = _verified_artifact(factory_root, str(artifact_id))
        if not isinstance(payload, Mapping):
            raise CompatibilityError("MS5A probe completed artifact payload is malformed")
        loaded[str(stage)] = (artifact_type, payload)
    request_artifact = loaded.get("request")
    if request_artifact is None or request_artifact[0] != "factory_request":
        raise CompatibilityError("MS5A probe checkpoint has no frozen Factory request")
    if decode_strategy_factory_request(request_artifact[1]) != request:
        raise CompatibilityError("MS5A probe Factory request differs from frozen request")
    planning = loaded.get("experiment_planning")
    if planning is None or planning[0] != "experiment_plans":
        raise CompatibilityError("MS5A probe checkpoint has no completed experiment plan")
    cases = _select_frozen_plans(planning[1], submitted)
    bindings = {
        "source_commit_sha": SOURCE_SHA,
        "request_fingerprint": request.fingerprint,
        "dataset_fingerprint": request.dataset.fingerprint,
        "dataset_sha256": request.dataset.sha256,
        "runtime_surface_fingerprint": runtime_fingerprint,
    }
    return bindings, cases


def _validate_runtime_surface_fingerprint(value: Any) -> str:
    if value != EXPECTED_RUNTIME_SURFACE_FINGERPRINT:
        raise CompatibilityError("MS5A probe runtime surface differs from accepted lineage")
    return EXPECTED_RUNTIME_SURFACE_FINGERPRINT


def _restore_for_native_plan(value: Any) -> Any:
    """Restore canonical Decimal envelopes without changing JSON list shapes."""

    if isinstance(value, Mapping):
        if set(value) == {"$decimal"}:
            raw = value["$decimal"]
            if not isinstance(raw, str):
                raise CompatibilityError("MS5A probe canonical decimal is malformed")
            return Decimal(raw)
        return {str(key): _restore_for_native_plan(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore_for_native_plan(item) for item in value]
    return value


def _native_specification(value: Mapping[str, Any]) -> Any:
    from modular_research_runner import research_specification_from_mapping

    restored = _restore_for_native_plan(value)
    if not isinstance(restored, dict):
        raise CompatibilityError("MS5A probe frozen Research specification is malformed")
    split_policy = restored.get("split_policy")
    if isinstance(split_policy, dict):
        # Canonical frozen plans preserve optional nulls; the native loader
        # represents those optional fields by omission.
        restored["split_policy"] = {
            key: item for key, item in split_policy.items() if item is not None
        }
    try:
        return research_specification_from_mapping(restored)
    except Exception as exc:
        raise CompatibilityError("MS5A probe cannot decode frozen Research specification") from exc


def _warmup_projection(case: FrozenCase, parameters: Mapping[str, Any]) -> dict[str, Any]:
    module_ids = parameters.get("modules")
    if not isinstance(module_ids, tuple | list) or not all(
        isinstance(item, str) for item in module_ids
    ):
        raise CompatibilityError("MS5A probe native plan has no frozen module identities")
    common_modules = {
        "entry_model@0.2.0",
        "protection_target.protection@1.1.1",
        "protection_target.target@1.1.1",
        "risk_request_composer@0.1.0",
        "risk_sizing@1.2.0",
    }
    if case.family == "breakout_return":
        from strategy_modules.breakout_return.module import BreakoutReturnModule

        warmup = parameters.get("breakout_return.lookback_bars")
        if not isinstance(warmup, int) or isinstance(warmup, bool) or warmup < 1:
            raise CompatibilityError("MS5A probe breakout warm-up is unavailable")
        selected = common_modules | {
            "breakout_return@1.0.0",
            "setup_adapter.breakout_return_v1@0.1.0",
        }
        manifest = BreakoutReturnModule(lookback_bars=warmup).manifest
        branch_history = {"base": warmup}
    elif case.family == "liquidity_sweep":
        from strategy_modules.liquidity_sweep.module import LiquiditySweepModule

        left = parameters.get("liquidity_sweep.pivot_left_bars", 2)
        right = parameters.get("liquidity_sweep.pivot_right_bars", 2)
        if any(
            not isinstance(item, int) or isinstance(item, bool) or item < 1
            for item in (left, right)
        ):
            raise CompatibilityError("MS5A probe liquidity warm-up is unavailable")
        selected = common_modules | {
            "liquidity_sweep@0.1.2",
            "setup_adapter.liquidity_sweep_v1@0.1.0",
        }
        manifest = LiquiditySweepModule(pivot_left_bars=left, pivot_right_bars=right).manifest
        branch_history = {"base": left + 1 + right}
    elif case.family == "fvg_rt":
        from strategy_modules.fvg_rt.composite import FvgRtCompositeModule

        if not any("fvg_rt_composite" in item for item in module_ids):
            raise CompatibilityError("MS5A probe selected FVG plan lacks composite module")
        selected = common_modules | {
            "fvg_rt@1.0.0",
            "setup_adapter.fvg_rt_composite_v1@0.1.0",
        }
        manifest = FvgRtCompositeModule().manifest
        branch_history = {}
    else:
        raise CompatibilityError("MS5A probe family has no warm-up contract")
    if set(module_ids) != selected:
        raise CompatibilityError("MS5A probe native plan has an unsupported module selection")
    if manifest is None:
        raise CompatibilityError("MS5A probe selected module manifest is unavailable")
    return {
        "module_ids": list(module_ids),
        "manifest_history_bars_by_timeframe": dict(manifest.warmup_bars),
        "skip_until_ready": manifest.skip_until_ready,
        "static_branch_history_minimum": branch_history,
    }


def project_native_plans(cases: Sequence[FrozenCase]) -> tuple[dict[str, Any], ...]:
    """Build pure native plan objects and report prefix warm-up readiness only."""

    from modular_research_runner import ResearchPlanBuilder

    reports: list[dict[str, Any]] = []
    for case in cases:
        specification = _native_specification(case.specification)
        native_plan = ResearchPlanBuilder(Path("/ms5a-plan-only")).build(
            experiment_id=case.experiment_id, specification=specification
        )
        if not native_plan.jobs:
            raise CompatibilityError("MS5A probe frozen plan contains no SEARCH jobs")
        job = native_plan.jobs[0]
        _validate_first_search_job(job)
        if job.parameters.get("factory_run_id") != _RUN_ID:
            raise CompatibilityError("MS5A probe native job lost the accepted Factory lineage")
        if canonical_sha256(case.specification) != case.specification_sha256:
            raise CompatibilityError("MS5A probe frozen Research specification was mutated")
        warmup = _warmup_projection(case, job.parameters)
        requirements = warmup["manifest_history_bars_by_timeframe"]
        ready = _prefix_readiness(requirements, skip_until_ready=warmup["skip_until_ready"])
        reports.append(
            {
                **case.public(),
                "native_stage_role": job.stage_role,
                "manifest_history_bars_by_timeframe": requirements,
                "skip_until_ready": warmup["skip_until_ready"],
                "static_branch_history_minimum": warmup["static_branch_history_minimum"],
                "prefixes": ready,
            }
        )
    return tuple(reports)


def _validate_first_search_job(job: Any) -> None:
    if job.period is None or job.stage_role != "optimization_000":
        raise CompatibilityError("MS5A probe first frozen job is not optimization_000 SEARCH")


def _prefix_readiness(
    requirements: Mapping[str, Any], *, skip_until_ready: bool
) -> list[dict[str, Any]]:
    ready: list[dict[str, Any]] = []
    for bars in WINDOWS:
        complete = True
        for timeframe, required in requirements.items():
            if timeframe == "base":
                available = bars
            else:
                seconds = _timeframe_seconds(timeframe)
                # Optimistic upper bound: alignment can only reduce coverage.
                available = (bars * BASE_SECONDS) // seconds
            if not isinstance(required, int) or isinstance(required, bool) or available < required:
                complete = False
        ready.append(
            {
                "bars": bars,
                "manifest_history_complete": complete,
                "runtime_readiness": "unverified",
                "skip_until_ready": skip_until_ready,
            }
        )
    return ready


def _timeframe_seconds(timeframe: str) -> int:
    from re import fullmatch

    match = fullmatch(r"([1-9][0-9]*)([smhdw])", timeframe)
    if match is None:
        raise CompatibilityError("MS5A probe timeframe warm-up is unsupported")
    scale = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]
    return int(match.group(1)) * scale


def plan_report(runtime_root: str | Path) -> dict[str, Any]:
    bindings, cases = select_frozen_cases(runtime_root)
    return {
        "schema_version": PROTOCOL_SCHEMA_VERSION,
        "probe": PROTOCOL_ID,
        "bindings": bindings,
        "cases": list(project_native_plans(cases)),
        "authority_manifest_template": authority_manifest(bindings, cases, ""),
        "window_bars": list(WINDOWS),
        "base_timeframe": "5m",
        "per_case_timeout_seconds": PER_CASE_SECONDS,
        "total_timeout_seconds": TOTAL_SECONDS,
        "memory_bytes": MEMORY_BYTES,
        "workers": 1,
        "authority_required": True,
        "execution_implemented": False,
        "candidate_metrics_included": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(plan_report(args.runtime_root), sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "blocked", "reason_code": type(exc).__name__}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
