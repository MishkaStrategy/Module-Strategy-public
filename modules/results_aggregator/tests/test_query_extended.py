from __future__ import annotations

from dataclasses import replace

import pytest

from conftest import make_package
from modular_results_registry.errors import IntegrityError
from modular_results_registry.models import CandidateRole, ConfigurationRecord, RoleDecision
from modular_results_registry.query import Page, RegistryQueryAPI, _drop_none
from modular_results_registry.storage import SQLiteRegistry


def _registry_with_roles(path):
    package = make_package()
    baseline_config = ConfigurationRecord.create(
        strategy_id="strategy-001",
        schema_version="1",
        canonical_configuration={"threshold": 2},
        stage="research",
        execution_assumptions={"mode": "conservative"},
        risk_settings={"risk_per_trade": "0.5%"},
        cost_settings={"maker": "0.02%"},
        funding_settings={"scenario": "base"},
        creation_source="TEST",
    )
    baseline_role = RoleDecision(
        experiment_id=package.experiments[0].experiment_id,
        configuration_id=baseline_config.configuration_id,
        role=CandidateRole.BASELINE,
        reason="baseline",
    )
    package = replace(
        package,
        configurations=package.configurations + (baseline_config,),
        roles=package.roles + (baseline_role,),
    )
    registry = SQLiteRegistry(path)
    registry.register_package(package)
    return registry, package, baseline_config


def test_query_api_typed_getters_filters_and_relationships(tmp_path) -> None:
    registry, package, baseline = _registry_with_roles(tmp_path / "registry.sqlite")
    try:
        api = RegistryQueryAPI(registry, timeout_seconds=1)
        artifact = package.artifacts[0]
        dataset = package.datasets[0]
        configuration = package.configurations[0]
        experiment = package.experiments[0]
        run = package.runs[0]
        definition = package.metric_definitions[0]
        evidence = package.evidence[0]

        assert api.artifact(artifact.artifact_id) == artifact
        assert api.dataset(dataset.dataset_id) == dataset
        assert api.configuration(configuration.configuration_id) == configuration
        assert api.experiment(experiment.experiment_id) == experiment
        assert api.run(run.run_id) == run
        assert api.metric_definition(definition.definition_id) == definition
        assert api.evidence(evidence.evidence_id) == evidence
        assert api.run("missing") is None

        experiments = api.experiments(
            strategy_id=experiment.strategy_id,
            research_id=experiment.research_id,
            status=experiment.status.value,
        )
        assert experiments.items == (experiment,)
        assert experiments.has_more is False
        assert api.runs(
            experiment_id=experiment.experiment_id,
            configuration_id=run.configuration_id,
            status=run.status.value,
        ).items == (run,)
        assert api.metric_definitions(
            metric_name=definition.metric_name,
            definition_version=definition.definition_version,
            owner_module=definition.owner_module,
        ).items == (definition,)
        assert (
            api.metrics_for_run(
                run.run_id,
                metric_name=package.metrics[0].metric_name,
                definition_id=package.metrics[0].definition_id,
            ).items
            == package.metrics
        )
        assert api.evidence_for_experiment(
            experiment.experiment_id,
            evidence_type=evidence.evidence_type,
            status=evidence.status.value,
        ).items == (evidence,)

        role_page = api.roles(experiment.experiment_id, limit=1)
        assert len(role_page.items) == 1
        assert role_page.has_more is True
        assert role_page.next_offset == 1
        second = api.roles(experiment.experiment_id, limit=1, offset=1)
        assert len(second.items) == 1
        assert second.has_more is False

        assert api.baselines(experiment.experiment_id) == (baseline,)
        assert api.candidates(experiment.experiment_id) == (configuration,)
        assert api.artifacts_for_run(run.run_id) == (artifact,)
        assert api.datasets_for_run(run.run_id) == (dataset,)
        assert api.artifacts_for_run("missing") == ()
        assert api.datasets_for_run("missing") == ()
    finally:
        registry.close()


def test_query_api_validation(tmp_path) -> None:
    path = tmp_path / "registry.sqlite"
    with SQLiteRegistry(path) as registry:
        with pytest.raises(TypeError):
            RegistryQueryAPI(object())
        for value in (0, -1, True, float("inf"), "1"):
            with pytest.raises(ValueError):
                RegistryQueryAPI(registry, timeout_seconds=value)
        api = RegistryQueryAPI(registry)
        for kwargs in (
            {"limit": 0},
            {"limit": 10_001},
            {"offset": -1},
        ):
            with pytest.raises(ValueError, match="pagination"):
                api.experiments(**kwargs)
        with pytest.raises(TypeError, match="CandidateRole"):
            api.roles("experiment-001", role="BASELINE")
    assert _drop_none({"a": "x", "b": None}) == {"a": "x"}


def test_page_has_more_property() -> None:
    assert Page(items=(1,), offset=0, limit=1, next_offset=1).has_more is True
    assert Page(items=(), offset=0, limit=1, next_offset=None).has_more is False


def test_relationship_integrity_failures_are_loud(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, package, _ = _registry_with_roles(tmp_path / "registry.sqlite")
    try:
        api = RegistryQueryAPI(registry)
        monkeypatch.setattr(api, "artifact", lambda _: None)
        with pytest.raises(IntegrityError, match="missing artifact"):
            api.artifacts_for_run(package.runs[0].run_id)
        monkeypatch.undo()
        monkeypatch.setattr(api, "dataset", lambda _: None)
        with pytest.raises(IntegrityError, match="missing dataset"):
            api.datasets_for_run(package.runs[0].run_id)
        monkeypatch.undo()
        monkeypatch.setattr(api, "configuration", lambda _: None)
        with pytest.raises(IntegrityError, match="missing configuration"):
            api.baselines(package.experiments[0].experiment_id)
    finally:
        registry.close()


def test_role_pagination_must_advance(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    with SQLiteRegistry(tmp_path / "registry.sqlite") as registry:
        api = RegistryQueryAPI(registry)
        monkeypatch.setattr(
            api,
            "roles",
            lambda *args, **kwargs: Page(items=(), offset=0, limit=1, next_offset=0),
        )
        with pytest.raises(IntegrityError, match="did not advance"):
            api._all_roles("experiment-001")
