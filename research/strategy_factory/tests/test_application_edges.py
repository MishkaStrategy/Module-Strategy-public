from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from test_application import Assembly

from strategy_factory.application import StrategyFactoryApplication
from strategy_factory.contracts import FactoryStage, FactoryStatus
from strategy_factory.dataset import DatasetAnalyzer, DatasetValidationPolicy
from strategy_factory.errors import CompatibilityError, ContractError


class MissingDatasetAnalyzer:
    def stage_handlers(self, run):
        del run
        return {}


class MissingStageHandlers:
    def dataset_analyzer(self, request):
        del request
        return _analyzer()


class InvalidAnalyzerAssembly:
    def dataset_analyzer(self, request):
        del request
        return object()

    def stage_handlers(self, run):
        del run
        return {}


class InvalidHandlersAssembly:
    def __init__(self, handlers) -> None:
        self.handlers = handlers

    def dataset_analyzer(self, request):
        del request
        return _analyzer()

    def stage_handlers(self, run):
        del run
        return self.handlers


def _analyzer() -> DatasetAnalyzer:
    return DatasetAnalyzer(
        DatasetValidationPolicy(
            interval_seconds=300,
            minimum_rows=10,
            minimum_history_days=Decimal("0"),
        )
    )


def _start(application, factory_request):
    return application.start(
        request=factory_request,
        commit_sha="a" * 40,
        module_versions={"z": "2.0", "a": "1.0"},
    )


def test_application_constructor_requires_complete_runtime_assembly(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="dataset_analyzer"):
        StrategyFactoryApplication(state_root=tmp_path, assembly=MissingDatasetAnalyzer())
    with pytest.raises(TypeError, match="stage_handlers"):
        StrategyFactoryApplication(state_root=tmp_path, assembly=MissingStageHandlers())


def test_application_start_rejects_wrong_request_type(tmp_path: Path) -> None:
    application = StrategyFactoryApplication(
        state_root=tmp_path,
        assembly=Assembly(()),
    )
    with pytest.raises(TypeError, match="StrategyFactoryRequest"):
        application.start(  # type: ignore[arg-type]
            request=object(),
            commit_sha="a" * 40,
            module_versions={},
        )


def test_application_rejects_invalid_analyzer(factory_request, tmp_path: Path) -> None:
    application = StrategyFactoryApplication(
        state_root=tmp_path,
        assembly=InvalidAnalyzerAssembly(),
    )
    with pytest.raises(ContractError, match="invalid DatasetAnalyzer"):
        _start(application, factory_request)


def test_application_rejects_non_mapping_handlers(factory_request, tmp_path: Path) -> None:
    application = StrategyFactoryApplication(
        state_root=tmp_path,
        assembly=InvalidHandlersAssembly([]),
    )
    with pytest.raises(ContractError, match="must return a mapping"):
        _start(application, factory_request)


def test_application_rejects_non_stage_handler_key(factory_request, tmp_path: Path) -> None:
    application = StrategyFactoryApplication(
        state_root=tmp_path,
        assembly=InvalidHandlersAssembly({"data_gate": object()}),
    )
    with pytest.raises(ContractError, match="handler key must be FactoryStage"):
        _start(application, factory_request)


def test_application_rejects_invalid_stage_handler(factory_request, tmp_path: Path) -> None:
    application = StrategyFactoryApplication(
        state_root=tmp_path,
        assembly=InvalidHandlersAssembly({FactoryStage.REGIME_DISCOVERY: object()}),
    )
    with pytest.raises(ContractError, match="invalid stage handler"):
        _start(application, factory_request)


def test_application_can_leave_blocked_run_unchanged(
    factory_request,
    tmp_path: Path,
) -> None:
    application = StrategyFactoryApplication(
        state_root=tmp_path,
        assembly=Assembly((FactoryStage.REGIME_DISCOVERY,)),
    )
    blocked = _start(application, factory_request)
    assert blocked.checkpoint.status is FactoryStatus.BLOCKED
    assert not blocked.completed

    unchanged = application.continue_run(
        run_id=blocked.run_id,
        resume_blocked=False,
    )
    assert unchanged.checkpoint == blocked.checkpoint
    assert unchanged.steps == ()


@pytest.mark.parametrize(
    "values,error",
    [
        ([], TypeError),
        ({"": "1.0"}, CompatibilityError),
        ({"module": ""}, CompatibilityError),
        ({1: "1.0"}, CompatibilityError),
        ({"module": 1}, CompatibilityError),
    ],
)
def test_module_versions_reject_malformed_values(values, error) -> None:
    with pytest.raises(error):
        StrategyFactoryApplication._module_versions(values)  # type: ignore[arg-type]


def test_module_versions_are_sorted_and_preserved() -> None:
    assert StrategyFactoryApplication._module_versions({"z": "2.0", "a": "1.0"}) == {
        "a": "1.0",
        "z": "2.0",
    }
