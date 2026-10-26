from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from modular_system_acceptance import (
    STAGE1_OWNER_CONTRACTS_V1,
    AcceptanceConfigurationMaterial,
    ProductionSystemAcceptanceProcess,
)
from scripts.system_acceptance_stage1_deployment import Stage1DeploymentComposition
from scripts.system_acceptance_stage1_host import (
    Stage1MacServiceConfig,
    Stage1MacServiceHost,
    build_stage1_prestart_proof,
)

_CANDIDATE_SHA = "1" * 40
_FULL_RC_SHA = "2" * 40


def _material() -> AcceptanceConfigurationMaterial:
    return AcceptanceConfigurationMaterial(
        full_rc_sha=_FULL_RC_SHA,
        dataset_id="dataset-btcusdt-5m",
        dataset_digest="3" * 64,
        dataset_version=7,
        dataset_timeframe="5m",
        strategy_id="strategy-stage1",
        strategy_package_hash="4" * 64,
        backtest_config_digest="5" * 64,
        backtest_performance_digest="6" * 64,
        valuation_policy="last",
        funding_identity_digest=None,
        mark_identity_digest=None,
        trading_runner_config_digest="7" * 64,
        trading_runner_runtime_id="paper-runtime-stage1",
        trading_runner_portfolio_id="paper-portfolio-stage1",
        research_experiment_id="research-stage1",
        research_specification_fingerprint="8" * 64,
        owner_contracts=STAGE1_OWNER_CONTRACTS_V1,
    )


def _composition() -> Stage1DeploymentComposition:
    return Stage1DeploymentComposition(
        material=_material(),
        bindings=cast(Any, object()),
        strategy_package=cast(Any, object()),
        verify_strategy_package=cast(Any, lambda package: package),
        backtest_execution_identity=cast(Any, lambda: None),
        runtime_configuration_identity=cast(Any, lambda: None),
        soak=cast(Any, object()),
    )


class _FakeProcess:
    def __init__(self, *, host: str = "127.0.0.1") -> None:
        self.running = False
        self.address = (host, 8781)
        self.closed = False

    def start(self) -> None:
        self.running = True

    def close(self) -> None:
        self.running = False
        self.closed = True


def _config(tmp_path: Path) -> Stage1MacServiceConfig:
    return Stage1MacServiceConfig(
        candidate_sha=_CANDIDATE_SHA,
        state_path=tmp_path / "private" / "acceptance.sqlite3",
        reader_token="reader-" + "r" * 24,
        operator_token="operator-" + "o" * 24,
        port=0,
    )


def test_prestart_proof_is_sanitized_and_stage1_only(tmp_path: Path) -> None:
    config = _config(tmp_path)
    proof = build_stage1_prestart_proof(_composition(), candidate_sha=config.candidate_sha)

    payload = json.loads(proof.to_json())

    assert payload["status"] == "READY_FOR_LOCAL_START"
    assert payload["candidate_sha"] == _CANDIDATE_SHA
    assert payload["full_rc_sha"] == _FULL_RC_SHA
    assert payload["config_digest"] == _material().config_digest
    assert payload["trading_runner"]["execution_mode"] == "paper"
    assert payload["policy"] == {"allow_soak_24h": False, "allow_soak_72h": False}
    rendered = proof.to_json()
    assert str(config.state_path) not in rendered
    assert config.reader_token not in rendered
    assert config.operator_token not in rendered


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"candidate_sha": "not-a-sha"}, "candidate_sha"),
        ({"config_digest": "f" * 63}, "config_digest"),
        ({"dataset_id": "unsafe dataset"}, "dataset_id"),
        ({"dataset_version": 0}, "dataset_version"),
        ({"execution_mode": "live"}, "execution_mode"),
        ({"allow_soak_24h": True}, "soak"),
        ({"allow_soak_72h": True}, "soak"),
        ({"schema_version": 2}, "schema_version"),
    ],
)
def test_prestart_proof_rejects_direct_policy_or_identity_drift(
    changes: dict[str, object],
    message: str,
) -> None:
    proof = build_stage1_prestart_proof(_composition(), candidate_sha=_CANDIDATE_SHA)

    with pytest.raises((TypeError, ValueError), match=message):
        replace(proof, **changes)  # type: ignore[arg-type]


def test_private_config_repr_hides_state_and_tokens(tmp_path: Path) -> None:
    config = _config(tmp_path)

    rendered = repr(config)

    assert _CANDIDATE_SHA in rendered
    assert str(config.state_path) not in rendered
    assert config.reader_token not in rendered
    assert config.operator_token not in rendered


def test_host_starts_exact_process_and_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _composition()
    config = _config(tmp_path)
    process = _FakeProcess()
    captured: dict[str, object] = {}

    def build_process(
        self: Stage1DeploymentComposition,
        **kwargs: object,
    ) -> ProductionSystemAcceptanceProcess:
        assert self is composition
        captured.update(kwargs)
        return cast(ProductionSystemAcceptanceProcess, process)

    monkeypatch.setattr(Stage1DeploymentComposition, "build_process", build_process)
    host = Stage1MacServiceHost(composition=composition, config=config)

    host.start()
    host.start()

    assert host.running
    assert host.address == ("127.0.0.1", 8781)
    assert captured["state_path"] == config.state_path
    assert captured["reader_token"] == config.reader_token
    assert captured["operator_token"] == config.operator_token
    assert captured["port"] == 0
    host.close()
    assert process.closed
    assert not host.running


def test_host_rejects_stopped_existing_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _composition()
    process = _FakeProcess()

    def build_process(
        self: Stage1DeploymentComposition,
        **kwargs: object,
    ) -> ProductionSystemAcceptanceProcess:
        del self, kwargs
        return cast(ProductionSystemAcceptanceProcess, process)

    monkeypatch.setattr(Stage1DeploymentComposition, "build_process", build_process)
    host = Stage1MacServiceHost(composition=composition, config=_config(tmp_path))
    host.start()
    process.running = False

    with pytest.raises(RuntimeError, match="stopped unexpectedly"):
        host.start()

    host.close()
    assert process.closed


def test_host_rejects_non_loopback_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _composition()
    process = _FakeProcess(host="0.0.0.0")

    def build_process(
        self: Stage1DeploymentComposition,
        **kwargs: object,
    ) -> ProductionSystemAcceptanceProcess:
        del self, kwargs
        return cast(ProductionSystemAcceptanceProcess, process)

    monkeypatch.setattr(Stage1DeploymentComposition, "build_process", build_process)
    host = Stage1MacServiceHost(composition=composition, config=_config(tmp_path))

    with pytest.raises(RuntimeError, match="escaped loopback"):
        host.start()

    assert process.closed
    assert not host.running


def test_service_manager_stop_event_owns_host_lifetime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _composition()
    process = _FakeProcess()

    def build_process(
        self: Stage1DeploymentComposition,
        **kwargs: object,
    ) -> ProductionSystemAcceptanceProcess:
        del self, kwargs
        return cast(ProductionSystemAcceptanceProcess, process)

    monkeypatch.setattr(Stage1DeploymentComposition, "build_process", build_process)
    host = Stage1MacServiceHost(composition=composition, config=_config(tmp_path))
    stop_event = threading.Event()
    stop_event.set()

    host.run(stop_event)

    assert process.closed
    assert not host.running


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"candidate_sha": "not-a-sha"}, "candidate_sha"),
        ({"state_path": Path("relative.sqlite3")}, "absolute"),
        (
            {"reader_token": "same-token-value-0000", "operator_token": "same-token-value-0000"},
            "distinct",
        ),
        ({"port": -1}, "port"),
        ({"host_wait_seconds": 0.01}, "host_wait_seconds"),
    ],
)
def test_service_config_rejects_invalid_values(
    tmp_path: Path,
    changes: dict[str, object],
    message: str,
) -> None:
    values: dict[str, object] = {
        "candidate_sha": _CANDIDATE_SHA,
        "state_path": tmp_path / "acceptance.sqlite3",
        "reader_token": "reader-" + "r" * 24,
        "operator_token": "operator-" + "o" * 24,
    }
    values.update(changes)

    with pytest.raises((TypeError, ValueError), match=message):
        Stage1MacServiceConfig(**values)  # type: ignore[arg-type]
