from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from strategy_factory.canonical import canonical_sha256, strict_json_loads
from strategy_factory.contracts import (
    DatasetDescriptor,
    FactoryMode,
    FactoryStage,
    MarketType,
    ResourceBudget,
    StrategyFactoryRequest,
)
from strategy_factory.errors import ContractError


def test_request_fingerprint_is_deterministic(factory_request: StrategyFactoryRequest) -> None:
    assert factory_request.fingerprint == factory_request.fingerprint
    assert len(factory_request.fingerprint) == 64


def test_budget_stage_enum_and_string_hash_identically(budget: ResourceBudget) -> None:
    clone = ResourceBudget(
        maximum_experiments=budget.maximum_experiments,
        maximum_architectures=budget.maximum_architectures,
        maximum_finalists=budget.maximum_finalists,
        maximum_duration_seconds=budget.maximum_duration_seconds,
        maximum_memory_bytes=budget.maximum_memory_bytes,
        maximum_cpu_workers=budget.maximum_cpu_workers,
        maximum_parallel_tasks=budget.maximum_parallel_tasks,
        stage_experiment_limits={
            key: value for key, value in budget.stage_experiment_limits.items()
        },
    )
    assert canonical_sha256(budget) == canonical_sha256(clone)
    assert FactoryStage.COARSE_SEARCH.value in clone.stage_experiment_limits


def test_dataset_rejects_non_utc_timezone(descriptor: DatasetDescriptor) -> None:
    with pytest.raises(ContractError, match="timezone"):
        replace(descriptor, timezone="Europe/Amsterdam")


def test_request_rejects_spot_futures_mismatch(factory_request: StrategyFactoryRequest) -> None:
    with pytest.raises(ContractError, match="market_type"):
        replace(factory_request, market_type=MarketType.SPOT)


def test_adapt_requires_source(factory_request: StrategyFactoryRequest) -> None:
    with pytest.raises(ContractError, match="source_strategy_id"):
        replace(factory_request, mode=FactoryMode.ADAPT)


def test_validate_requires_holdout(factory_request: StrategyFactoryRequest) -> None:
    with pytest.raises(ContractError, match="explicit_holdout"):
        replace(factory_request, mode=FactoryMode.VALIDATE, baseline_strategy_id="base")


def test_git_sha_accepts_sha1_checkpoint(factory_request: StrategyFactoryRequest) -> None:
    from strategy_factory.contracts import FactoryStatus, StrategyFactoryCheckpoint

    checkpoint = StrategyFactoryCheckpoint(
        run_id="sf-run-test",
        request_fingerprint=factory_request.fingerprint,
        status=FactoryStatus.CREATED,
        stage=FactoryStage.DATA_GATE,
        stage_sequence=0,
        dataset_fingerprint=factory_request.dataset.fingerprint,
        commit_sha="a" * 40,
        manifest_id="manifest-test",
        completed_artifacts={},
        submitted_batches={},
        processed_event_ids=frozenset(),
        budget_state={},
        decision_chain_head=None,
        updated_at=datetime.now(UTC),
    )
    assert checkpoint.commit_sha == "a" * 40


def test_strict_json_rejects_duplicate_keys() -> None:
    with pytest.raises(ContractError, match="duplicate"):
        strict_json_loads('{"a":1,"a":2}')


def test_strict_json_rejects_nan() -> None:
    with pytest.raises(ContractError, match="non-finite"):
        strict_json_loads('{"a":NaN}')


def test_nonfinite_decimal_rejected(factory_request: StrategyFactoryRequest) -> None:
    with pytest.raises(ContractError, match="finite"):
        replace(factory_request, initial_capital=Decimal("NaN"))
