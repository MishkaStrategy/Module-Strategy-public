from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from strategy_factory import multi_strategy_search as search
from strategy_factory.contracts import (
    DatasetDescriptor,
    FactoryMode,
    FactoryStage,
    FactoryStatus,
    MarketType,
    ResourceBudget,
    StrategyFactoryRequest,
    TimeRange,
)
from strategy_factory.errors import CompatibilityError
from strategy_factory.registry import StrategyModuleCatalog

_HEAD = "a" * 40
_REQUEST_SHA = "b" * 64
_CREATED = datetime(2026, 9, 23, 16, 0, tzinfo=UTC)
_FAMILIES = frozenset({"breakout_return", "liquidity_sweep", "fvg_rt"})


def _request(*, created_at: datetime = _CREATED) -> StrategyFactoryRequest:
    dataset = DatasetDescriptor(
        dataset_id="binance_usdt-m-perpetual_btcusdt_5m",
        source="market_data_registry",
        symbol="BTCUSDT",
        exchange="binance_usdtm",
        market_type=MarketType.LINEAR_FUTURES,
        base_timeframe="5m",
        available_timeframes=("5m", "15m", "1h", "4h", "1d"),
        period=TimeRange(_CREATED - timedelta(days=100), _CREATED),
        timezone="UTC",
        file_format="parquet",
        uri="market-data-registry:binance_usdt-m-perpetual_btcusdt_5m:v1",
        sha256=_REQUEST_SHA,
        row_count=100_000,
        received_at=_CREATED - timedelta(days=1),
        funding_uri="market-data-registry:funding:v4",
        funding_sha256="c" * 64,
    )
    return StrategyFactoryRequest(
        request_id="ms5a-multi-strategy-search-v1",
        mode=FactoryMode.CREATE,
        symbol="BTCUSDT",
        exchange="binance_usdtm",
        market_type=MarketType.LINEAR_FUTURES,
        dataset=dataset,
        allowed_timeframes=dataset.available_timeframes,
        initial_capital=Decimal("10000"),
        research_risk_fraction=Decimal("0.005"),
        maximum_drawdown_fraction=Decimal("0.15"),
        minimum_trade_count=30,
        objectives={"profit_factor": "maximize", "max_drawdown": "minimize"},
        budget=ResourceBudget(
            maximum_experiments=132,
            maximum_architectures=12,
            maximum_finalists=3,
            maximum_duration_seconds=86_400,
            maximum_memory_bytes=8 * 1024 * 1024 * 1024,
            maximum_cpu_workers=1,
            maximum_parallel_tasks=1,
            stage_experiment_limits={
                FactoryStage.STRUCTURAL_SCREENING: 96,
                FactoryStage.COARSE_SEARCH: 24,
                FactoryStage.LOCAL_REFINEMENT: 12,
            },
        ),
        random_seed=312,
        allowed_strategy_families=_FAMILIES,
        created_at=created_at,
    )


def _surface() -> SimpleNamespace:
    return SimpleNamespace(
        source_commit_sha=_HEAD,
        allowed_strategy_families=_FAMILIES,
        runtime_closure=SimpleNamespace(fingerprint="d" * 64),
        trusted_runtime_modules=(),
    )


def test_authority_is_bound_to_exact_action_source_and_request() -> None:
    authority = search.MS5ASearchAuthority(
        authority_id="gov-search-001",
        source_commit_sha=_HEAD,
        request_fingerprint=_REQUEST_SHA,
    )

    assert authority.action == "MS5A_SEARCH"
    assert len(authority.fingerprint) == 64

    with pytest.raises(CompatibilityError, match="action"):
        search.MS5ASearchAuthority(
            authority_id="gov-search-001",
            source_commit_sha=_HEAD,
            request_fingerprint=_REQUEST_SHA,
            action="HOLDOUT",
        )


def test_freeze_is_replay_safe_and_does_not_refingerprint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request = _request()
    seen_created_at: list[datetime] = []

    def fake_owner(root: str | Path, *, created_at: datetime) -> SimpleNamespace:
        del root
        seen_created_at.append(created_at)
        assert created_at == request.created_at
        return SimpleNamespace(request=request)

    monkeypatch.setattr(search, "build_ms5a_owner_state", fake_owner)
    monkeypatch.setattr(search, "build_ms5a_search_surface", lambda **kwargs: _surface())

    first = search.freeze_ms5a_request(
        market_data_root=tmp_path,
        runtime_root=tmp_path / "runtime",
        source_commit_sha=_HEAD,
        created_at=request.created_at,
    )
    second = search.freeze_ms5a_request(
        market_data_root=tmp_path,
        runtime_root=tmp_path / "runtime",
        source_commit_sha=_HEAD,
        created_at=request.created_at + timedelta(days=1),
    )

    assert first.request.fingerprint == second.request.fingerprint
    assert first.summary["walk_forward_authorized"] is False
    assert first.summary["holdout_authorized"] is False
    assert seen_created_at == [request.created_at, request.created_at]

    loaded, owner, surface = search._load_frozen_request(
        market_data_root=tmp_path,
        runtime_root=tmp_path / "runtime",
        source_commit_sha=_HEAD,
    )
    assert loaded.request == request
    assert owner.request == request
    assert surface.source_commit_sha == _HEAD


def test_freeze_rejects_surface_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request = _request()
    monkeypatch.setattr(
        search,
        "build_ms5a_owner_state",
        lambda root, *, created_at: SimpleNamespace(request=request),
    )
    monkeypatch.setattr(search, "build_ms5a_search_surface", lambda **kwargs: _surface())
    search.freeze_ms5a_request(
        market_data_root=tmp_path,
        runtime_root=tmp_path / "runtime",
        source_commit_sha=_HEAD,
        created_at=request.created_at,
    )

    drifted = _surface()
    drifted.runtime_closure = SimpleNamespace(fingerprint="e" * 64)
    monkeypatch.setattr(search, "build_ms5a_search_surface", lambda **kwargs: drifted)

    with pytest.raises(CompatibilityError, match="runtime surface changed"):
        search.freeze_ms5a_request(
            market_data_root=tmp_path,
            runtime_root=tmp_path / "runtime",
            source_commit_sha=_HEAD,
            created_at=request.created_at,
        )


def test_authority_receipt_allows_only_same_exact_lineage(tmp_path: Path) -> None:
    root = search._safe_private_root(tmp_path / "runtime")
    first = search.MS5ASearchAuthority(
        authority_id="gov-search-001",
        source_commit_sha=_HEAD,
        request_fingerprint=_REQUEST_SHA,
    )
    search._consume_authority(root, first, run_id="sf-run-one")
    search._consume_authority(root, first, run_id="sf-run-one")

    second = search.MS5ASearchAuthority(
        authority_id="gov-search-002",
        source_commit_sha=_HEAD,
        request_fingerprint=_REQUEST_SHA,
    )
    with pytest.raises(CompatibilityError, match="already consumed"):
        search._consume_authority(root, second, run_id="sf-run-two")


def test_execute_refuses_search_without_frozen_request(tmp_path: Path) -> None:
    authority = search.MS5ASearchAuthority(
        authority_id="gov-search-001",
        source_commit_sha=_HEAD,
        request_fingerprint=_REQUEST_SHA,
    )
    with pytest.raises(CompatibilityError, match="previously frozen request"):
        search.execute_authorized_ms5a_search(
            authority=authority,
            expected_head=_HEAD,
            market_data_root=tmp_path,
            runtime_root=tmp_path / "runtime",
        )


class _FakeRun:
    def __init__(self, stage: FactoryStage) -> None:
        self.stage = stage

    def initialize(self) -> SimpleNamespace:
        return SimpleNamespace(stage=self.stage, status=FactoryStatus.RUNNING)


class _FakeCoordinator:
    def __init__(self, run: _FakeRun, stages: tuple[FactoryStage, ...]) -> None:
        self._run = run
        self._stages = iter(stages)

    def step(self) -> SimpleNamespace:
        prior = self._run.stage
        self._run.stage = next(self._stages)
        return SimpleNamespace(stage=prior, action="completed")


def test_search_advancer_stops_before_walk_forward() -> None:
    run = _FakeRun(FactoryStage.REGIME_DISCOVERY)
    coordinator = _FakeCoordinator(
        run,
        (
            FactoryStage.HYPOTHESIS_GENERATION,
            FactoryStage.ARCHITECTURE_GENERATION,
            FactoryStage.EXPERIMENT_PLANNING,
            FactoryStage.STRUCTURAL_SCREENING,
            FactoryStage.COARSE_SEARCH,
            FactoryStage.LOCAL_REFINEMENT,
            FactoryStage.WALK_FORWARD,
        ),
    )

    search._advance_to_walk_forward(run, coordinator)

    assert run.stage is FactoryStage.WALK_FORWARD


def test_search_advancer_rejects_downstream_stage() -> None:
    run = _FakeRun(FactoryStage.ROBUSTNESS)
    coordinator = _FakeCoordinator(run, ())

    with pytest.raises(CompatibilityError, match="hard pre-WALK_FORWARD boundary"):
        search._advance_to_walk_forward(run, coordinator)


def test_runtime_paths_are_private_and_module_versions_are_deterministic(tmp_path: Path) -> None:
    root = search._safe_private_root(tmp_path / "runtime")
    paths = search._runtime_paths(root)

    assert paths.factory_state_root.is_dir()
    assert paths.research_artifact_root.is_dir()
    assert paths.research_output_root.is_dir()
    assert paths.results_package_root.is_dir()
    assert paths.research_state_path.parent.is_dir()
    assert paths.results_registry_path.parent.is_dir()

    surface = SimpleNamespace(
        trusted_runtime_modules=(
            SimpleNamespace(
                runtime_component_id="zeta",
                runtime_component_version="2.0",
            ),
            SimpleNamespace(
                runtime_component_id="alpha",
                runtime_component_version="1.0",
            ),
        )
    )
    assert search._module_versions(surface) == {"alpha": "1.0", "zeta": "2.0"}

    conflict = SimpleNamespace(
        trusted_runtime_modules=(
            SimpleNamespace(
                runtime_component_id="alpha",
                runtime_component_version="1.0",
            ),
            SimpleNamespace(
                runtime_component_id="alpha",
                runtime_component_version="2.0",
            ),
        )
    )
    with pytest.raises(CompatibilityError, match="version conflict"):
        search._module_versions(conflict)


def test_git_provenance_requires_exact_clean_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    outputs = iter((str(tmp_path), _HEAD, "", "feature/ms5a"))

    def fake_run(*args, **kwargs):
        del args, kwargs
        return SimpleNamespace(stdout=next(outputs) + "\n")

    monkeypatch.setattr(search.subprocess, "run", fake_run)

    provenance, environment = search._git_provenance(_HEAD)

    assert provenance.commit_sha == _HEAD
    assert provenance.clean is True
    assert provenance.branch == "feature/ms5a"
    assert environment is None


def test_search_summary_freezes_cross_family_finalist_evidence() -> None:
    request = _request()
    authority = search.MS5ASearchAuthority(
        authority_id="gov-search-summary",
        source_commit_sha=_HEAD,
        request_fingerprint=request.fingerprint,
    )
    fingerprint = "e" * 64
    completed_artifacts = {}
    artifacts = {}
    evidence_stages = (
        FactoryStage.ARCHITECTURE_GENERATION,
        FactoryStage.EXPERIMENT_PLANNING,
        FactoryStage.STRUCTURAL_SCREENING,
        FactoryStage.COARSE_SEARCH,
        FactoryStage.LOCAL_REFINEMENT,
    )
    for stage in evidence_stages:
        artifact_id = f"artifact-{stage.value}"
        completed_artifacts[stage.value] = artifact_id
        artifacts[artifact_id] = {
            "family_allocations": (
                {"family_id": "breakout_return"},
                {"family_id": "fvg_rt"},
                {"family_id": "liquidity_sweep"},
            ),
            "unused_stage_capacity": 0,
            "experiments": (
                {
                    "experiment_id": f"exp-{stage.value}",
                    "factory_plan_id": f"plan-{stage.value}",
                    "status": "completed",
                    "run_ids": (f"run-{stage.value}",),
                    "strategy_family": "breakout_return",
                },
            ),
        }

    artifacts[completed_artifacts[FactoryStage.LOCAL_REFINEMENT.value]].update(
        {
            "finalist_selection_policy": "global-pareto-finalists-v1",
            "finalist_selection_fingerprint": fingerprint,
            "finalist_count": 1,
            "finalists": (
                {
                    "candidate": {
                        "candidate_id": "candidate-breakout",
                        "configuration_hash": "f" * 64,
                    },
                    "source_experiment_id": "exp-breakout",
                    "source_factory_plan_id": "plan-breakout",
                    "strategy_family": "breakout_return",
                    "selection_reasons": ("GLOBAL_PARETO",),
                },
            ),
        }
    )

    checkpoint = SimpleNamespace(
        stage=FactoryStage.WALK_FORWARD,
        status=FactoryStatus.RUNNING,
        completed_artifacts=completed_artifacts,
    )

    class Run:
        run_id = "sf-run-summary"
        commit_sha = _HEAD

        def __init__(self):
            self.request = request
            self.checkpoint = checkpoint

        def initialize(self):
            return self.checkpoint

        def load_artifact(self, artifact_id):
            return artifacts[artifact_id]

    summary = search._search_summary(Run(), authority)

    assert summary["phase"] == "search"
    assert summary["phase_outcome"] == "finalists_frozen"
    assert summary["next_stage"] == FactoryStage.WALK_FORWARD.value
    assert summary["finalist_selection_fingerprint"] == fingerprint
    assert summary["finalists"][0]["candidate_id"] == "candidate-breakout"
    assert summary["holdout_opened"] is False
    assert summary["walk_forward_executed"] is False
    assert summary["live_trading_authorized"] is False


def test_search_composition_wires_only_pre_walk_forward_handlers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request = _request()
    run = SimpleNamespace(request=request)
    owner = SimpleNamespace(request=request)
    surface = SimpleNamespace(
        allowed_strategy_families=request.allowed_strategy_families,
        catalog=StrategyModuleCatalog(),
        runtime_closure=SimpleNamespace(feature_contracts=frozenset()),
    )
    closed = []

    monkeypatch.setattr(
        search,
        "build_ms5a_research_runtime",
        lambda **kwargs: (object(), object(), object(), object(), None),
    )
    monkeypatch.setattr(
        search,
        "ResearchRunnerExecutionAdapter",
        lambda **kwargs: SimpleNamespace(),
    )

    class Aggregator:
        def __init__(self, **kwargs):
            del kwargs

        def close(self):
            closed.append(True)

    monkeypatch.setattr(search, "PublishingResultsAggregator", Aggregator)
    monkeypatch.setattr(
        search,
        "ResultsAggregatorCandidateSelector",
        lambda **kwargs: SimpleNamespace(),
    )

    class Coordinator:
        def __init__(self, *, run, dataset_analyzer, handlers):
            self.run = run
            self.dataset_analyzer = dataset_analyzer
            self.handlers = handlers

    monkeypatch.setattr(search, "FactoryCoordinator", Coordinator)

    paths = search.RuntimePaths(
        factory_state_root=tmp_path / "factory",
        research_state_path=tmp_path / "research.sqlite3",
        research_artifact_root=tmp_path / "research-artifacts",
        research_output_root=tmp_path / "research-output",
        results_package_root=tmp_path / "packages",
        results_registry_path=tmp_path / "registry.sqlite3",
    )
    composition = search.MS5ASearchComposition(
        run=run,
        owner=owner,
        surface=surface,
        paths=paths,
        market_data_root=tmp_path,
        git_provenance=object(),
        environment_manifest=None,
    )

    assert set(composition.coordinator.handlers) == {
        FactoryStage.REGIME_DISCOVERY,
        FactoryStage.HYPOTHESIS_GENERATION,
        FactoryStage.ARCHITECTURE_GENERATION,
        FactoryStage.EXPERIMENT_PLANNING,
        FactoryStage.STRUCTURAL_SCREENING,
        FactoryStage.COARSE_SEARCH,
        FactoryStage.LOCAL_REFINEMENT,
    }
    assert FactoryStage.WALK_FORWARD not in composition.coordinator.handlers
    composition.close()
    assert closed == [True]


@pytest.mark.parametrize(
    ("authority_id", "source_sha", "request_sha", "match"),
    (
        (" ", _HEAD, _REQUEST_SHA, "authority_id"),
        ("gov-search", "A" * 40, _REQUEST_SHA, "source_commit_sha"),
        ("gov-search", _HEAD, "z" * 64, "request_fingerprint"),
    ),
)
def test_authority_rejects_noncanonical_identity(
    authority_id: str,
    source_sha: str,
    request_sha: str,
    match: str,
) -> None:
    with pytest.raises(CompatibilityError, match=match):
        search.MS5ASearchAuthority(
            authority_id=authority_id,
            source_commit_sha=source_sha,
            request_fingerprint=request_sha,
        )


def test_search_contract_helpers_and_private_paths_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(CompatibilityError, match="lowercase hexadecimal"):
        search._sha("g" * 40, "sha")
    with pytest.raises(CompatibilityError, match="mapping"):
        search._mapping((), "payload")
    with pytest.raises(CompatibilityError, match="sequence"):
        search._sequence("not-a-sequence", "payload")
    with pytest.raises(CompatibilityError, match="canonical non-blank"):
        search._text(" padded ", "text")

    root_target = tmp_path / "real-root"
    root_target.mkdir()
    root_link = tmp_path / "root-link"
    root_link.symlink_to(root_target, target_is_directory=True)
    with pytest.raises(CompatibilityError, match="root must not be a symlink"):
        search._safe_private_root(root_link)

    root = search._safe_private_root(tmp_path / "safe-root")
    child_target = tmp_path / "child-target"
    child_target.mkdir()
    (root / "execution").symlink_to(child_target, target_is_directory=True)
    with pytest.raises(CompatibilityError, match="child must not be a symlink"):
        search._safe_private_child(root, "execution")


def test_freeze_rejects_naive_time_and_family_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(CompatibilityError, match="timezone-aware"):
        search.freeze_ms5a_request(
            market_data_root=tmp_path,
            runtime_root=tmp_path / "naive",
            source_commit_sha=_HEAD,
            created_at=datetime(2026, 1, 1),
        )

    from dataclasses import replace

    request = replace(_request(), allowed_strategy_families=frozenset({"breakout_return"}))
    monkeypatch.setattr(
        search,
        "build_ms5a_owner_state",
        lambda root, *, created_at: SimpleNamespace(request=request),
    )
    monkeypatch.setattr(search, "build_ms5a_search_surface", lambda **kwargs: _surface())
    with pytest.raises(CompatibilityError, match="family sets differ"):
        search.freeze_ms5a_request(
            market_data_root=tmp_path,
            runtime_root=tmp_path / "family-drift",
            source_commit_sha=_HEAD,
            created_at=request.created_at,
        )


def test_frozen_request_artifacts_reject_incomplete_and_metadata_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import json

    request = _request()
    monkeypatch.setattr(
        search,
        "build_ms5a_owner_state",
        lambda root, *, created_at: SimpleNamespace(request=request),
    )
    monkeypatch.setattr(search, "build_ms5a_search_surface", lambda **kwargs: _surface())

    incomplete_root = tmp_path / "incomplete"
    search.freeze_ms5a_request(
        market_data_root=tmp_path,
        runtime_root=incomplete_root,
        source_commit_sha=_HEAD,
        created_at=request.created_at,
    )
    (incomplete_root / "frozen-request-meta.json").unlink()
    with pytest.raises(CompatibilityError, match="incomplete"):
        search.freeze_ms5a_request(
            market_data_root=tmp_path,
            runtime_root=incomplete_root,
            source_commit_sha=_HEAD,
            created_at=request.created_at,
        )

    drift_root = tmp_path / "drift"
    search.freeze_ms5a_request(
        market_data_root=tmp_path,
        runtime_root=drift_root,
        source_commit_sha=_HEAD,
        created_at=request.created_at,
    )
    meta_path = drift_root / "frozen-request-meta.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    metadata["source_commit_sha"] = "f" * 40
    meta_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(CompatibilityError, match="another exact source"):
        search.freeze_ms5a_request(
            market_data_root=tmp_path,
            runtime_root=drift_root,
            source_commit_sha=_HEAD,
            created_at=request.created_at,
        )

    fingerprint_root = tmp_path / "fingerprint"
    search.freeze_ms5a_request(
        market_data_root=tmp_path,
        runtime_root=fingerprint_root,
        source_commit_sha=_HEAD,
        created_at=request.created_at,
    )
    meta_path = fingerprint_root / "frozen-request-meta.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    metadata["request_fingerprint"] = "0" * 64
    meta_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(CompatibilityError, match="fingerprint metadata drifted"):
        search.freeze_ms5a_request(
            market_data_root=tmp_path,
            runtime_root=fingerprint_root,
            source_commit_sha=_HEAD,
            created_at=request.created_at,
        )


def test_authority_receipt_rejects_unsafe_and_unreadable_state(tmp_path: Path) -> None:
    authority = search.MS5ASearchAuthority(
        authority_id="gov-search-001",
        source_commit_sha=_HEAD,
        request_fingerprint=_REQUEST_SHA,
    )

    root = search._safe_private_root(tmp_path / "unsafe")
    target = tmp_path / "receipt-target"
    target.write_text("{}", encoding="utf-8")
    (root / "search-authority.json").symlink_to(target)
    with pytest.raises(CompatibilityError, match="receipt is unsafe"):
        search._consume_authority(root, authority, run_id="run")

    root = search._safe_private_root(tmp_path / "unreadable")
    (root / "search-authority.json").write_text("{", encoding="utf-8")
    with pytest.raises(CompatibilityError, match="receipt is unreadable"):
        search._consume_authority(root, authority, run_id="run")


def test_git_provenance_rejects_head_and_dirty_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def run_with(outputs):
        values = iter(outputs)

        def fake_run(*args, **kwargs):
            del args, kwargs
            return SimpleNamespace(stdout=next(values) + "\n")

        monkeypatch.setattr(search.subprocess, "run", fake_run)

    run_with((str(tmp_path), "b" * 40, "", "feature/ms5a"))
    with pytest.raises(CompatibilityError, match="HEAD differs"):
        search._git_provenance(_HEAD)

    run_with((str(tmp_path), _HEAD, " M file.py", "feature/ms5a"))
    with pytest.raises(CompatibilityError, match="must be clean"):
        search._git_provenance(_HEAD)


def test_search_advancer_rejects_terminal_bad_action_and_step_limit() -> None:
    class TerminalRun:
        def initialize(self):
            return SimpleNamespace(
                stage=FactoryStage.REGIME_DISCOVERY,
                status=FactoryStatus.FAILED,
            )

    with pytest.raises(CompatibilityError, match="stopped before WALK_FORWARD"):
        search._advance_to_walk_forward(TerminalRun(), SimpleNamespace())

    run = _FakeRun(FactoryStage.REGIME_DISCOVERY)

    class BadCoordinator:
        def step(self):
            return SimpleNamespace(stage=FactoryStage.REGIME_DISCOVERY, action="blocked")

    with pytest.raises(CompatibilityError, match="stage did not complete"):
        search._advance_to_walk_forward(run, BadCoordinator())

    run = _FakeRun(FactoryStage.REGIME_DISCOVERY)

    class LoopCoordinator:
        def step(self):
            return SimpleNamespace(stage=FactoryStage.REGIME_DISCOVERY, action="completed")

    with pytest.raises(CompatibilityError, match="step limit"):
        search._advance_to_walk_forward(run, LoopCoordinator(), maximum_steps=1)


def test_execute_authorized_search_wires_exact_frozen_lineage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request = _request()
    frozen = search.MS5AFrozenRequest(request, _HEAD, "d" * 64)
    owner = SimpleNamespace(request=request)
    surface = _surface()
    factory_root = tmp_path / "factory"
    paths = SimpleNamespace(factory_state_root=factory_root)
    events: list[str] = []

    monkeypatch.setattr(
        search,
        "_load_frozen_request",
        lambda **kwargs: (frozen, owner, surface),
    )
    monkeypatch.setattr(search, "_runtime_paths", lambda root: paths)
    monkeypatch.setattr(search, "_git_provenance", lambda head: (object(), None))
    monkeypatch.setattr(search, "_module_versions", lambda value: {"fixture": "1.0"})

    class Run:
        run_id = "sf-run-execute"

        def __init__(self, *, request, commit_sha, state_root, module_versions, now):
            self.request = request
            self.commit_sha = commit_sha
            self.state_root = state_root
            self.module_versions = module_versions
            self.now = now
            (Path(state_root) / self.run_id).mkdir(parents=True, exist_ok=True)

        @classmethod
        def restore_existing(cls, **kwargs):
            del kwargs
            raise AssertionError("fresh SEARCH must not restore without a persisted checkpoint")

    monkeypatch.setattr(search, "StrategyFactoryRun", Run)

    class Composition:
        def __init__(self, **kwargs):
            self.coordinator = object()
            events.append("composition")

        def close(self):
            events.append("closed")

    monkeypatch.setattr(search, "MS5ASearchComposition", Composition)
    monkeypatch.setattr(
        search,
        "_advance_to_walk_forward",
        lambda run, coordinator: events.append("advanced"),
    )
    monkeypatch.setattr(
        search,
        "_search_summary",
        lambda run, authority: {"run_id": run.run_id, "authority_id": authority.authority_id},
    )

    authority = search.MS5ASearchAuthority(
        authority_id="gov-search-execute",
        source_commit_sha=_HEAD,
        request_fingerprint=request.fingerprint,
    )
    result = search.execute_authorized_ms5a_search(
        authority=authority,
        expected_head=_HEAD,
        market_data_root=tmp_path,
        runtime_root=tmp_path / "runtime",
    )

    assert result == {"run_id": "sf-run-execute", "authority_id": "gov-search-execute"}
    assert events == ["composition", "advanced", "closed"]
    assert (tmp_path / "runtime" / "search-authority.json").is_file()


def test_execute_authorized_search_rejects_authority_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request = _request()
    authority = search.MS5ASearchAuthority(
        authority_id="gov-search-execute",
        source_commit_sha=_HEAD,
        request_fingerprint="f" * 64,
    )
    with pytest.raises(CompatibilityError, match="source SHA differs"):
        search.execute_authorized_ms5a_search(
            authority=authority,
            expected_head="e" * 40,
            market_data_root=tmp_path,
            runtime_root=tmp_path / "runtime-a",
        )

    monkeypatch.setattr(
        search,
        "_load_frozen_request",
        lambda **kwargs: (
            search.MS5AFrozenRequest(request, _HEAD, "d" * 64),
            SimpleNamespace(request=request),
            _surface(),
        ),
    )
    with pytest.raises(CompatibilityError, match="request fingerprint differs"):
        search.execute_authorized_ms5a_search(
            authority=authority,
            expected_head=_HEAD,
            market_data_root=tmp_path,
            runtime_root=tmp_path / "runtime-b",
        )


def test_search_composition_rejects_owner_and_family_drift(tmp_path: Path) -> None:
    request = _request()
    run = SimpleNamespace(request=request)
    surface = SimpleNamespace(allowed_strategy_families=request.allowed_strategy_families)

    with pytest.raises(CompatibilityError, match="run request differs"):
        search.MS5ASearchComposition(
            run=run,
            owner=SimpleNamespace(request=object()),
            surface=surface,
            paths=SimpleNamespace(),
            market_data_root=tmp_path,
            git_provenance=object(),
            environment_manifest=None,
        )

    with pytest.raises(CompatibilityError, match="families differ"):
        search.MS5ASearchComposition(
            run=run,
            owner=SimpleNamespace(request=request),
            surface=SimpleNamespace(allowed_strategy_families=frozenset({"other"})),
            paths=SimpleNamespace(),
            market_data_root=tmp_path,
            git_provenance=object(),
            environment_manifest=None,
        )


def test_private_writer_wraps_create_and_write_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_open(*args, **kwargs):
        del args, kwargs
        raise OSError("create failed")

    monkeypatch.setattr(search.os, "open", fail_open)
    with pytest.raises(CompatibilityError, match="cannot create private MS5A artifact"):
        search._write_exclusive(tmp_path / "create.json", b"payload")

    class BrokenHandle:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            del exc_type, exc, traceback
            return False

        def write(self, payload):
            del payload
            raise OSError("write failed")

        def flush(self):
            return None

        def fileno(self):
            return 1

    monkeypatch.setattr(search.os, "open", lambda *args, **kwargs: 1)
    monkeypatch.setattr(search.os, "fdopen", lambda *args, **kwargs: BrokenHandle())
    with pytest.raises(CompatibilityError, match="cannot persist private MS5A artifact"):
        search._write_exclusive(tmp_path / "write.json", b"payload")


def test_frozen_request_rejects_symlink_unreadable_malformed_and_owner_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    request = _request()
    monkeypatch.setattr(
        search,
        "build_ms5a_owner_state",
        lambda root, *, created_at: SimpleNamespace(request=request),
    )
    monkeypatch.setattr(search, "build_ms5a_search_surface", lambda **kwargs: _surface())

    symlink_root = tmp_path / "symlink"
    search.freeze_ms5a_request(
        market_data_root=tmp_path,
        runtime_root=symlink_root,
        source_commit_sha=_HEAD,
        created_at=request.created_at,
    )
    request_path = symlink_root / "frozen-request.json"
    request_path.unlink()
    target = tmp_path / "frozen-target.json"
    target.write_text("{}", encoding="utf-8")
    request_path.symlink_to(target)
    with pytest.raises(CompatibilityError, match="must not be symlinks"):
        search.freeze_ms5a_request(
            market_data_root=tmp_path,
            runtime_root=symlink_root,
            source_commit_sha=_HEAD,
            created_at=request.created_at,
        )

    unreadable_root = tmp_path / "unreadable"
    search.freeze_ms5a_request(
        market_data_root=tmp_path,
        runtime_root=unreadable_root,
        source_commit_sha=_HEAD,
        created_at=request.created_at,
    )
    (unreadable_root / "frozen-request.json").write_text("{", encoding="utf-8")
    with pytest.raises(CompatibilityError, match="artifacts are unreadable"):
        search.freeze_ms5a_request(
            market_data_root=tmp_path,
            runtime_root=unreadable_root,
            source_commit_sha=_HEAD,
            created_at=request.created_at,
        )

    malformed_root = tmp_path / "malformed"
    search.freeze_ms5a_request(
        market_data_root=tmp_path,
        runtime_root=malformed_root,
        source_commit_sha=_HEAD,
        created_at=request.created_at,
    )
    (malformed_root / "frozen-request-meta.json").write_text("[]", encoding="utf-8")
    with pytest.raises(CompatibilityError, match="metadata is malformed"):
        search.freeze_ms5a_request(
            market_data_root=tmp_path,
            runtime_root=malformed_root,
            source_commit_sha=_HEAD,
            created_at=request.created_at,
        )

    owner_drift_root = tmp_path / "owner-drift"
    search.freeze_ms5a_request(
        market_data_root=tmp_path,
        runtime_root=owner_drift_root,
        source_commit_sha=_HEAD,
        created_at=request.created_at,
    )
    drifted = replace(request, request_id="ms5a-owner-drift")
    monkeypatch.setattr(
        search,
        "build_ms5a_owner_state",
        lambda root, *, created_at: SimpleNamespace(request=drifted),
    )
    with pytest.raises(CompatibilityError, match="owner request changed after freeze"):
        search.freeze_ms5a_request(
            market_data_root=tmp_path,
            runtime_root=owner_drift_root,
            source_commit_sha=_HEAD,
            created_at=request.created_at,
        )


def test_freeze_wraps_concurrent_artifact_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request = _request()
    monkeypatch.setattr(
        search,
        "build_ms5a_owner_state",
        lambda root, *, created_at: SimpleNamespace(request=request),
    )
    monkeypatch.setattr(search, "build_ms5a_search_surface", lambda **kwargs: _surface())
    monkeypatch.setattr(
        search,
        "_write_exclusive",
        lambda path, payload: (_ for _ in ()).throw(FileExistsError(path)),
    )

    with pytest.raises(CompatibilityError, match="concurrent MS5A request freeze"):
        search.freeze_ms5a_request(
            market_data_root=tmp_path,
            runtime_root=tmp_path / "concurrent",
            source_commit_sha=_HEAD,
            created_at=request.created_at,
        )


def test_load_frozen_request_rejects_corruption_binding_and_owner_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import json
    from dataclasses import replace

    request = _request()
    monkeypatch.setattr(
        search,
        "build_ms5a_owner_state",
        lambda root, *, created_at: SimpleNamespace(request=request),
    )
    monkeypatch.setattr(search, "build_ms5a_search_surface", lambda **kwargs: _surface())

    symlink_root = tmp_path / "load-symlink"
    search.freeze_ms5a_request(
        market_data_root=tmp_path,
        runtime_root=symlink_root,
        source_commit_sha=_HEAD,
        created_at=request.created_at,
    )
    request_path = symlink_root / "frozen-request.json"
    request_path.unlink()
    target = tmp_path / "load-target.json"
    target.write_text("{}", encoding="utf-8")
    request_path.symlink_to(target)
    with pytest.raises(CompatibilityError, match="must not be symlinks"):
        search._load_frozen_request(
            market_data_root=tmp_path,
            runtime_root=symlink_root,
            source_commit_sha=_HEAD,
        )

    unreadable_root = tmp_path / "load-unreadable"
    search.freeze_ms5a_request(
        market_data_root=tmp_path,
        runtime_root=unreadable_root,
        source_commit_sha=_HEAD,
        created_at=request.created_at,
    )
    (unreadable_root / "frozen-request.json").write_text("{", encoding="utf-8")
    with pytest.raises(CompatibilityError, match="artifacts are unreadable"):
        search._load_frozen_request(
            market_data_root=tmp_path,
            runtime_root=unreadable_root,
            source_commit_sha=_HEAD,
        )

    malformed_root = tmp_path / "load-malformed"
    search.freeze_ms5a_request(
        market_data_root=tmp_path,
        runtime_root=malformed_root,
        source_commit_sha=_HEAD,
        created_at=request.created_at,
    )
    (malformed_root / "frozen-request-meta.json").write_text("[]", encoding="utf-8")
    with pytest.raises(CompatibilityError, match="metadata is malformed"):
        search._load_frozen_request(
            market_data_root=tmp_path,
            runtime_root=malformed_root,
            source_commit_sha=_HEAD,
        )

    binding_root = tmp_path / "load-binding"
    search.freeze_ms5a_request(
        market_data_root=tmp_path,
        runtime_root=binding_root,
        source_commit_sha=_HEAD,
        created_at=request.created_at,
    )
    meta_path = binding_root / "frozen-request-meta.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    metadata["runtime_surface_fingerprint"] = "0" * 64
    meta_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(CompatibilityError, match="binding is invalid"):
        search._load_frozen_request(
            market_data_root=tmp_path,
            runtime_root=binding_root,
            source_commit_sha=_HEAD,
        )

    owner_drift_root = tmp_path / "load-owner-drift"
    search.freeze_ms5a_request(
        market_data_root=tmp_path,
        runtime_root=owner_drift_root,
        source_commit_sha=_HEAD,
        created_at=request.created_at,
    )
    drifted = replace(request, request_id="ms5a-live-owner-drift")
    monkeypatch.setattr(
        search,
        "build_ms5a_owner_state",
        lambda root, *, created_at: SimpleNamespace(request=drifted),
    )
    with pytest.raises(CompatibilityError, match="live owner state differs"):
        search._load_frozen_request(
            market_data_root=tmp_path,
            runtime_root=owner_drift_root,
            source_commit_sha=_HEAD,
        )


def test_authority_receipt_recovers_exclusive_create_race(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    authority = search.MS5ASearchAuthority(
        authority_id="gov-search-race",
        source_commit_sha=_HEAD,
        request_fingerprint=_REQUEST_SHA,
    )
    root = search._safe_private_root(tmp_path / "race")
    original = search._write_exclusive
    calls = 0

    def racing_write(path: Path, payload: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            original(path, payload)
            raise FileExistsError(path)
        original(path, payload)

    monkeypatch.setattr(search, "_write_exclusive", racing_write)
    search._consume_authority(root, authority, run_id="run-race")

    assert calls == 1
    assert (root / "search-authority.json").is_file()


def test_git_provenance_wraps_subprocess_failure_and_detached_head(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        search.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("git unavailable")),
    )
    with pytest.raises(CompatibilityError, match="checkout cannot be verified"):
        search._git_provenance(_HEAD)

    outputs = iter((str(tmp_path), _HEAD, "", ""))

    def fake_run(*args, **kwargs):
        del args, kwargs
        return SimpleNamespace(stdout=next(outputs) + "\n")

    monkeypatch.setattr(search.subprocess, "run", fake_run)
    provenance, _ = search._git_provenance(_HEAD)
    assert provenance.branch == f"detached-{_HEAD[:12]}"


def _summary_run(
    *,
    stage: FactoryStage = FactoryStage.WALK_FORWARD,
    forbidden: bool = False,
    malformed_architecture: bool = False,
    local_policy: str = "global-pareto-finalists-v1",
    local_fingerprint: str = "e" * 64,
    finalist_count: int | bool = 0,
    finalists=(),
):
    request = _request()
    stages = (
        FactoryStage.ARCHITECTURE_GENERATION,
        FactoryStage.EXPERIMENT_PLANNING,
        FactoryStage.STRUCTURAL_SCREENING,
        FactoryStage.COARSE_SEARCH,
        FactoryStage.LOCAL_REFINEMENT,
    )
    completed = {item.value: f"artifact-{item.value}" for item in stages}
    if forbidden:
        completed[FactoryStage.HOLDOUT.value] = "artifact-holdout"
    artifacts = {
        artifact_id: {"family_allocations": (), "experiments": (), "unused_stage_capacity": 0}
        for artifact_id in completed.values()
    }
    local_id = completed[FactoryStage.LOCAL_REFINEMENT.value]
    artifacts[local_id].update(
        {
            "finalist_selection_policy": local_policy,
            "finalist_selection_fingerprint": local_fingerprint,
            "finalist_count": finalist_count,
            "finalists": finalists,
        }
    )
    if malformed_architecture:
        artifacts[completed[FactoryStage.ARCHITECTURE_GENERATION.value]] = ()

    checkpoint = SimpleNamespace(
        stage=stage,
        status=FactoryStatus.RUNNING,
        completed_artifacts=completed,
    )

    class Run:
        run_id = "sf-run-summary-guards"
        commit_sha = _HEAD

        def __init__(self):
            self.request = request
            self.checkpoint = checkpoint

        def initialize(self):
            return self.checkpoint

        def load_artifact(self, artifact_id):
            return artifacts[artifact_id]

    return Run()


def test_search_summary_rejects_boundary_and_evidence_drift() -> None:
    authority = search.MS5ASearchAuthority(
        authority_id="gov-search-summary-guards",
        source_commit_sha=_HEAD,
        request_fingerprint=_request().fingerprint,
    )

    with pytest.raises(CompatibilityError, match="requires the WALK_FORWARD boundary"):
        search._search_summary(
            _summary_run(stage=FactoryStage.LOCAL_REFINEMENT),
            authority,
        )

    with pytest.raises(CompatibilityError, match="forbidden downstream artifacts"):
        search._search_summary(_summary_run(forbidden=True), authority)

    missing = _summary_run()
    missing.checkpoint.completed_artifacts.pop(FactoryStage.ARCHITECTURE_GENERATION.value)
    with pytest.raises(CompatibilityError, match="lacks architecture_generation evidence"):
        search._search_summary(missing, authority)

    with pytest.raises(CompatibilityError, match="artifact is malformed"):
        search._search_summary(_summary_run(malformed_architecture=True), authority)

    with pytest.raises(CompatibilityError, match="selection policy drifted"):
        search._search_summary(_summary_run(local_policy="wrong-policy"), authority)

    with pytest.raises(CompatibilityError, match="lowercase hexadecimal"):
        search._search_summary(_summary_run(local_fingerprint="not-a-sha"), authority)

    with pytest.raises(CompatibilityError, match="finalist count evidence is invalid"):
        search._search_summary(_summary_run(finalist_count=True), authority)


def test_search_summary_records_no_admissible_candidate() -> None:
    request = _request()
    authority = search.MS5ASearchAuthority(
        authority_id="gov-search-no-finalists",
        source_commit_sha=_HEAD,
        request_fingerprint=request.fingerprint,
    )
    summary = search._search_summary(_summary_run(), authority)

    assert summary["phase_outcome"] == "no_admissible_candidate"
    assert summary["finalists"] == ()
    assert summary["final_test_grants"] == 0


def test_execute_authorized_search_restores_existing_exact_lineage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request = _request()
    frozen = search.MS5AFrozenRequest(request, _HEAD, "d" * 64)
    owner = SimpleNamespace(request=request)
    surface = _surface()
    factory_root = tmp_path / "factory"
    checkpoint_root = factory_root / "sf-run-restore" / "checkpoints"
    checkpoint_root.mkdir(parents=True)
    (checkpoint_root / "sf-run-restore.json").write_text("fixture", encoding="utf-8")
    paths = SimpleNamespace(factory_state_root=factory_root)
    events: list[str] = []

    monkeypatch.setattr(
        search,
        "_load_frozen_request",
        lambda **kwargs: (frozen, owner, surface),
    )
    monkeypatch.setattr(search, "_runtime_paths", lambda root: paths)
    monkeypatch.setattr(search, "_git_provenance", lambda head: (object(), None))
    monkeypatch.setattr(search, "_module_versions", lambda value: {"fixture": "1.0"})

    restored = SimpleNamespace(
        request=request,
        commit_sha=_HEAD,
        run_id="sf-run-restore",
    )

    class Run:
        run_id = "sf-run-restore"

        def __init__(self, *, request, commit_sha, state_root, module_versions, now):
            self.request = request
            self.commit_sha = commit_sha
            self.state_root = state_root
            self.module_versions = module_versions
            self.now = now

        @classmethod
        def restore_existing(cls, **kwargs):
            del kwargs
            events.append("restored")
            return restored

    monkeypatch.setattr(search, "StrategyFactoryRun", Run)

    class Composition:
        def __init__(self, **kwargs):
            del kwargs
            self.coordinator = object()

        def close(self):
            events.append("closed")

    monkeypatch.setattr(search, "MS5ASearchComposition", Composition)
    monkeypatch.setattr(
        search,
        "_advance_to_walk_forward",
        lambda run, coordinator: events.append("advanced"),
    )
    monkeypatch.setattr(
        search,
        "_search_summary",
        lambda run, authority: {"run_id": run.run_id},
    )
    authority = search.MS5ASearchAuthority(
        authority_id="gov-search-restore",
        source_commit_sha=_HEAD,
        request_fingerprint=request.fingerprint,
    )

    result = search.execute_authorized_ms5a_search(
        authority=authority,
        expected_head=_HEAD,
        market_data_root=tmp_path,
        runtime_root=tmp_path / "runtime",
    )

    assert result == {"run_id": "sf-run-restore"}
    assert events == ["restored", "advanced", "closed"]


def test_execute_authorized_search_rejects_restored_lineage_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request = _request()
    frozen = search.MS5AFrozenRequest(request, _HEAD, "d" * 64)
    owner = SimpleNamespace(request=request)
    surface = _surface()
    factory_root = tmp_path / "factory-drift"
    checkpoint_root = factory_root / "sf-run-restore-drift" / "checkpoints"
    checkpoint_root.mkdir(parents=True)
    (checkpoint_root / "sf-run-restore-drift.json").write_text("fixture", encoding="utf-8")
    paths = SimpleNamespace(factory_state_root=factory_root)

    monkeypatch.setattr(
        search,
        "_load_frozen_request",
        lambda **kwargs: (frozen, owner, surface),
    )
    monkeypatch.setattr(search, "_runtime_paths", lambda root: paths)
    monkeypatch.setattr(search, "_git_provenance", lambda head: (object(), None))
    monkeypatch.setattr(search, "_module_versions", lambda value: {"fixture": "1.0"})

    class Run:
        run_id = "sf-run-restore-drift"

        def __init__(self, *, request, commit_sha, state_root, module_versions, now):
            self.request = request
            self.commit_sha = commit_sha

        @classmethod
        def restore_existing(cls, **kwargs):
            del kwargs
            return SimpleNamespace(
                request=object(),
                commit_sha=_HEAD,
                run_id="sf-run-restore-drift",
            )

    monkeypatch.setattr(search, "StrategyFactoryRun", Run)
    authority = search.MS5ASearchAuthority(
        authority_id="gov-search-restore-drift",
        source_commit_sha=_HEAD,
        request_fingerprint=request.fingerprint,
    )

    with pytest.raises(CompatibilityError, match="restored MS5A SEARCH lineage differs"):
        search.execute_authorized_ms5a_search(
            authority=authority,
            expected_head=_HEAD,
            market_data_root=tmp_path,
            runtime_root=tmp_path / "runtime-drift",
        )
