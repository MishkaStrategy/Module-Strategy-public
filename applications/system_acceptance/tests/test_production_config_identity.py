from __future__ import annotations

from dataclasses import fields, replace

import pytest

from modular_system_acceptance.contracts import AcceptanceOwnerError
from modular_system_acceptance.production_config_identity import (
    CONFIG_IDENTITY_SCHEMA_VERSION,
    STAGE1_OWNER_CONTRACTS_V1,
    AcceptanceConfigurationMaterial,
    OwnerContractIdentity,
)

FULL_RC = "a" * 40
DATASET_DIGEST = "b" * 64
STRATEGY_PACKAGE_HASH = "c" * 64
BACKTEST_CONFIG_DIGEST = "d" * 64
BACKTEST_PERFORMANCE_DIGEST = "e" * 64
TRADING_RUNNER_CONFIG_DIGEST = "f" * 64
RESEARCH_FINGERPRINT = "1" * 64
FUNDING_DIGEST = "2" * 64
MARK_DIGEST = "3" * 64


def owner_contracts() -> tuple[OwnerContractIdentity, ...]:
    return STAGE1_OWNER_CONTRACTS_V1


def material(**overrides: object) -> AcceptanceConfigurationMaterial:
    values: dict[str, object] = {
        "full_rc_sha": FULL_RC,
        "dataset_id": "btc-usdtm-5m",
        "dataset_digest": DATASET_DIGEST,
        "dataset_version": 1,
        "dataset_timeframe": "5m",
        "strategy_id": "strategy-btc-v1",
        "strategy_package_hash": STRATEGY_PACKAGE_HASH,
        "backtest_config_digest": BACKTEST_CONFIG_DIGEST,
        "backtest_performance_digest": BACKTEST_PERFORMANCE_DIGEST,
        "valuation_policy": "last",
        "funding_identity_digest": FUNDING_DIGEST,
        "mark_identity_digest": None,
        "trading_runner_config_digest": TRADING_RUNNER_CONFIG_DIGEST,
        "trading_runner_runtime_id": "paper-stage1",
        "trading_runner_portfolio_id": "portfolio-stage1",
        "research_experiment_id": "experiment-stage1",
        "research_specification_fingerprint": RESEARCH_FINGERPRINT,
        "owner_contracts": owner_contracts(),
        "schema_version": CONFIG_IDENTITY_SCHEMA_VERSION,
    }
    values.update(overrides)
    return AcceptanceConfigurationMaterial(**values)  # type: ignore[arg-type]


def test_config_digest_is_deterministic_and_builds_trusted_identity() -> None:
    first = material()
    second = material()

    assert first.config_digest == second.config_digest
    assert len(first.config_digest) == 64

    identity = first.trusted_identity()
    assert identity.full_rc_sha == FULL_RC
    assert identity.dataset_id == "btc-usdtm-5m"
    assert identity.dataset_digest == DATASET_DIGEST
    assert identity.strategy_id == "strategy-btc-v1"
    assert identity.strategy_digest == STRATEGY_PACKAGE_HASH
    assert identity.config_digest == first.config_digest


@pytest.mark.parametrize(
    ("field_name", "changed_value"),
    (
        ("trading_runner_config_digest", "4" * 64),
        ("trading_runner_runtime_id", "paper-stage1-other"),
        ("trading_runner_portfolio_id", "portfolio-stage1-other"),
        ("research_experiment_id", "experiment-stage1-other"),
        ("research_specification_fingerprint", "5" * 64),
    ),
)
def test_semantic_owner_identity_change_changes_config_digest(
    field_name: str,
    changed_value: str,
) -> None:
    baseline = material()

    changed = replace(baseline, **{field_name: changed_value})

    assert changed.config_digest != baseline.config_digest


def test_full_rc_change_changes_config_digest() -> None:
    baseline = material()
    changed = replace(baseline, full_rc_sha="9" * 40)

    assert changed.config_digest != baseline.config_digest


@pytest.mark.parametrize(
    "field_name",
    tuple(
        field.name
        for field in fields(AcceptanceConfigurationMaterial)
        if field.name != "schema_version"
    ),
)
def test_required_canonical_field_omission_fails_closed(field_name: str) -> None:
    baseline = material()
    values = {field.name: getattr(baseline, field.name) for field in fields(baseline)}
    values.pop(field_name)

    with pytest.raises(TypeError, match=field_name):
        AcceptanceConfigurationMaterial(**values)  # type: ignore[arg-type]


def test_missing_required_owner_contract_fails_closed() -> None:
    incomplete = tuple(item for item in owner_contracts() if item.owner != "strategy_factory")

    with pytest.raises(AcceptanceOwnerError, match="required authoritative owner set"):
        material(owner_contracts=incomplete)


def test_owner_contract_ordering_drift_fails_closed() -> None:
    contracts = owner_contracts()

    with pytest.raises(AcceptanceOwnerError, match="canonically ordered"):
        material(owner_contracts=tuple(reversed(contracts)))


def test_duplicate_owner_contract_fails_closed() -> None:
    contracts = list(owner_contracts())
    contracts[-1] = OwnerContractIdentity(contracts[-2].owner, "other-v1")
    contracts.sort()

    with pytest.raises(AcceptanceOwnerError, match="duplicate owners"):
        material(owner_contracts=tuple(contracts))


def test_owner_contract_version_drift_fails_closed() -> None:
    contracts = list(owner_contracts())
    index = next(index for index, item in enumerate(contracts) if item.owner == "backtest")
    contracts[index] = OwnerContractIdentity("backtest", "application-v2")
    contracts.sort()

    with pytest.raises(AcceptanceOwnerError, match="versions must match Stage-1 V1"):
        material(owner_contracts=tuple(contracts))


def test_unsupported_schema_version_fails_closed() -> None:
    with pytest.raises(AcceptanceOwnerError, match="unsupported acceptance config"):
        material(schema_version="2")


def test_mark_valuation_requires_registered_mark_identity() -> None:
    with pytest.raises(AcceptanceOwnerError, match="requires a registered mark"):
        material(valuation_policy="mark", mark_identity_digest=None)

    marked = material(valuation_policy="mark", mark_identity_digest=MARK_DIGEST)
    assert marked.mark_identity_digest == MARK_DIGEST


def test_last_valuation_rejects_mark_identity() -> None:
    with pytest.raises(AcceptanceOwnerError, match="must not publish a mark"):
        material(valuation_policy="last", mark_identity_digest=MARK_DIGEST)


def test_invalid_digest_dataset_version_and_owner_ids_fail_closed() -> None:
    with pytest.raises(AcceptanceOwnerError, match="Backtest configuration digest"):
        material(backtest_config_digest="not-a-digest")

    with pytest.raises(AcceptanceOwnerError, match="positive integer"):
        material(dataset_version=0)

    with pytest.raises(AcceptanceOwnerError, match="Trading Runner runtime_id"):
        material(trading_runner_runtime_id="bad runtime id")

    with pytest.raises(AcceptanceOwnerError, match="Research experiment_id"):
        material(research_experiment_id="bad experiment id")


def test_canonical_mapping_contains_explicit_runtime_and_research_owner_ids() -> None:
    payload = material().canonical_mapping()
    trading_runner = payload["trading_runner"]
    research = payload["research"]

    assert isinstance(trading_runner, dict)
    assert trading_runner == {
        "config_digest": TRADING_RUNNER_CONFIG_DIGEST,
        "runtime_id": "paper-stage1",
        "portfolio_id": "portfolio-stage1",
        "strategy_id": "strategy-btc-v1",
        "execution_mode": "paper",
    }
    assert research == {
        "experiment_id": "experiment-stage1",
        "specification_fingerprint": RESEARCH_FINGERPRINT,
    }


def test_canonical_mapping_contains_no_deployment_private_fields() -> None:
    payload = material().canonical_mapping()
    rendered = repr(dict(payload))

    assert "state_path" not in rendered
    assert "reader_token" not in rendered
    assert "operator_token" not in rendered
    assert "storage_root" not in rendered
    assert "output_directory" not in rendered
