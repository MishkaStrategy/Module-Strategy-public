from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from strategy_factory.backtest_config import (
    BacktestEnginePolicy,
    OfficialBacktestConfigFactory,
    TrustedRuntimeModule,
)
from strategy_factory.errors import CompatibilityError

_SOURCE_SHA = "c" * 40


def _write_runtime_module(root: Path) -> str:
    package = root / "trusted_runtime.py"
    package.write_text(
        """from types import MappingProxyType
from modular_core import ModuleDecision, ModuleManifest, StrategyModule


class NoopModule(StrategyModule):
    name = "noop"

    def __init__(self, period=1):
        self.parameters = MappingProxyType({"period": period})
        self.manifest = ModuleManifest(
            name=self.name,
            version="1.0.0",
            api_version="1.0",
            parameters_schema={"period": {"type": "integer", "minimum": 1}},
            state_schema_version="1",
        )

    def on_bar(self, view):
        return ModuleDecision()
""",
        encoding="utf-8",
    )
    return "trusted_runtime:NoopModule"


def _policy() -> BacktestEnginePolicy:
    return BacktestEnginePolicy(
        execution={
            "slippage_bps": 2.5,
            "stop_slippage_bps": 3.0,
            "intrabar_priority": "stop",
        },
        fees={"maker_rate": 0.0002, "taker_rate": 0.00055},
        instrument={
            "base_asset": "ETH",
            "quote_asset": "USDT",
            "market_type": "linear_futures",
            "tick_size": 0.1,
            "quantity_step": 0.001,
            "min_quantity": 0.001,
            "min_notional": 5.0,
            "contract_size": 1.0,
        },
    )


def _job(factory_request, output_root: Path, import_path: str, **parameter_overrides):
    from modular_research_runner import BacktestJob, TimeRange

    parameters = {
        "architecture_id": "arch-backtest",
        "architecture_fingerprint": "a" * 64,
        "runtime_modules": (
            {
                "runtime_component_id": "noop",
                "runtime_component_version": "1.0.0",
                "import_path": import_path,
                "fixed_parameters": {"period": 2},
                "parameter_schema": {"period": {"type": "integer", "minimum": 1}},
                "api_version": "1.0",
                "state_schema_version": "1",
                "source_commit_sha": _SOURCE_SHA,
            },
        ),
        "factory_run_id": "sf-run-backtest",
        "factory_manifest_id": "sf-manifest-backtest",
        "factory.noop_variant": 0,
    }
    parameters.update(parameter_overrides)
    return BacktestJob(
        run_id="research-run-backtest",
        experiment_id="experiment-backtest",
        stage_id="stage-train",
        split_id="split-train",
        dataset_id=factory_request.dataset.dataset_id,
        scenario_id="base",
        seed=123,
        parameters=parameters,
        output_directory=(output_root / "research-run-backtest").resolve(),
        stage_role="train",
        period=TimeRange(
            factory_request.dataset.period.start,
            factory_request.dataset.period.end,
        ),
    )


def _factory(factory_request, output_root: Path, import_path: str):
    return OfficialBacktestConfigFactory(
        request=factory_request,
        policy=_policy(),
        trusted_modules=(
            TrustedRuntimeModule(
                runtime_component_id="noop",
                runtime_component_version="1.0.0",
                import_path=import_path,
                source_commit_sha=_SOURCE_SHA,
            ),
        ),
        allowed_output_root=output_root,
    )


def test_official_config_factory_builds_and_executes_real_backtest(
    factory_request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_path = _write_runtime_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    output_root = tmp_path / "jobs"
    factory = _factory(factory_request, output_root, import_path)
    job = _job(factory_request, output_root, import_path)

    config = factory(job)
    assert config.seed == 123
    assert config.data.path == Path(factory_request.dataset.uri).resolve()
    assert config.data.start == factory_request.dataset.period.start
    assert config.requested_timeframes == ("15m",)
    payload = json.loads(config.modules.read_text(encoding="utf-8"))
    assert payload == {
        "modules": [
            {
                "class": import_path,
                "enabled": True,
                "parameters": {"period": 2},
            }
        ]
    }
    assert factory(job).modules.read_bytes() == config.modules.read_bytes()

    from modular_research_runner import ModularBacktestEngineAdapter

    reference = ModularBacktestEngineAdapter(factory).execute(job)
    assert reference.requested_run_id == job.run_id
    assert reference.bars_processed == factory_request.dataset.row_count
    assert reference.output_directory.is_dir()
    manifests = tuple(
        (job.output_directory / "engine-results").glob(".research-runner-output-manifest.json.*")
    )
    assert len(manifests) == 1


def test_official_config_factory_applies_dynamic_parameters(
    factory_request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_path = _write_runtime_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    output_root = tmp_path / "jobs"
    factory = _factory(factory_request, output_root, import_path)
    job = _job(factory_request, output_root, import_path, **{"noop.period": 2})
    payload = json.loads(factory(job).modules.read_text(encoding="utf-8"))
    assert payload["modules"][0]["parameters"] == {"period": 2}


def _breakout_return_factory_and_job(factory_request, tmp_path: Path, value: Decimal):
    import_path = "strategy_modules.breakout_return:BreakoutReturnModule"
    output_root = tmp_path / "jobs"
    factory = OfficialBacktestConfigFactory(
        request=factory_request,
        policy=_policy(),
        trusted_modules=(
            TrustedRuntimeModule(
                runtime_component_id="breakout_return",
                runtime_component_version="1.0.0",
                import_path=import_path,
                source_commit_sha=_SOURCE_SHA,
            ),
        ),
        allowed_output_root=output_root,
    )
    runtime_modules = (
        {
            "runtime_component_id": "breakout_return",
            "runtime_component_version": "1.0.0",
            "import_path": import_path,
            "fixed_parameters": {},
            "parameter_schema": {
                "min_return_depth": {"type": "number", "minimum": 0, "maximum": 1}
            },
            "api_version": "1.0",
            "state_schema_version": "1",
            "source_commit_sha": _SOURCE_SHA,
        },
    )
    job = _job(
        factory_request,
        output_root,
        import_path,
        runtime_modules=runtime_modules,
        **{"breakout_return.min_return_depth": value},
    )
    return factory, job


def test_official_config_factory_normalizes_decimal_number_for_breakout_return(
    factory_request, tmp_path: Path
) -> None:
    from modular_core import ModuleRegistry

    factory, job = _breakout_return_factory_and_job(
        factory_request,
        tmp_path,
        Decimal("0.02"),
    )

    config = factory(job)
    payload = json.loads(config.modules.read_text(encoding="utf-8"))
    value = payload["modules"][0]["parameters"]["min_return_depth"]

    assert value == 0.02
    assert isinstance(value, float)
    modules = ModuleRegistry().load_config(config.modules)
    assert len(modules) == 1
    assert modules[0].name == "breakout_return"


@pytest.mark.parametrize(
    ("value", "message"),
    (
        (Decimal("NaN"), "must be a finite number"),
        (
            Decimal("0.12345678901234567890123456789"),
            "cannot be represented exactly as a numeric JSON primitive",
        ),
    ),
)
def test_official_config_factory_rejects_unsafe_decimal_number_conversion(
    factory_request,
    tmp_path: Path,
    value: Decimal,
    message: str,
) -> None:
    factory, job = _breakout_return_factory_and_job(factory_request, tmp_path, value)

    with pytest.raises(CompatibilityError, match=message):
        factory(job)


def test_official_config_factory_rejects_changed_fixed_parameter(
    factory_request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_path = _write_runtime_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    output_root = tmp_path / "jobs"
    factory = _factory(factory_request, output_root, import_path)
    job = _job(factory_request, output_root, import_path, **{"noop.period": 3})
    with pytest.raises(CompatibilityError, match="changes fixed value"):
        factory(job)


def test_official_config_factory_rejects_dataset_hash_drift(
    factory_request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_path = _write_runtime_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    output_root = tmp_path / "jobs"
    factory = _factory(factory_request, output_root, import_path)
    job = _job(factory_request, output_root, import_path)
    Path(factory_request.dataset.uri).write_text("corrupted", encoding="utf-8")
    with pytest.raises(CompatibilityError, match="dataset hash changed"):
        factory(job)


def test_official_config_factory_rejects_untrusted_runtime_provenance(
    factory_request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_path = _write_runtime_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    output_root = tmp_path / "jobs"
    factory = _factory(factory_request, output_root, import_path)
    job = _job(factory_request, output_root, "trusted_runtime:OtherModule")
    with pytest.raises(CompatibilityError, match="provenance mismatch"):
        factory(job)


def test_backtest_policy_requires_explicit_cost_and_instrument_evidence() -> None:
    with pytest.raises(CompatibilityError, match="execution evidence"):
        BacktestEnginePolicy(
            execution={},
            fees={"maker_rate": 0.0, "taker_rate": 0.0},
            instrument={
                "base_asset": "ETH",
                "quote_asset": "USDT",
                "tick_size": 0.1,
                "quantity_step": 0.001,
                "min_quantity": 0.001,
                "min_notional": 5.0,
                "contract_size": 1.0,
            },
        )


def test_official_config_factory_rejects_output_symlink(
    factory_request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_path = _write_runtime_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    output_root = tmp_path / "jobs"
    output_root.mkdir()
    factory = _factory(factory_request, output_root, import_path)
    job = _job(factory_request, output_root, import_path)
    real = output_root / "real"
    real.mkdir()
    job.output_directory.symlink_to(real, target_is_directory=True)
    with pytest.raises(CompatibilityError, match="symlink"):
        factory(job)


def test_official_config_factory_rejects_dataset_symlink(
    factory_request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    import_path = _write_runtime_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    original = Path(factory_request.dataset.uri)
    linked = tmp_path / "linked.csv"
    linked.symlink_to(original)
    request = replace(
        factory_request,
        dataset=replace(factory_request.dataset, uri=str(linked)),
    )
    output_root = tmp_path / "jobs"
    factory = _factory(request, output_root, import_path)
    job = _job(request, output_root, import_path)
    with pytest.raises(CompatibilityError, match="dataset path must not be a symlink"):
        factory(job)


def test_official_config_factory_rejects_unknown_runtime_parameter(
    factory_request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_path = _write_runtime_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    output_root = tmp_path / "jobs"
    factory = _factory(factory_request, output_root, import_path)
    job = _job(factory_request, output_root, import_path, **{"noop.unknown": 1})
    with pytest.raises(CompatibilityError, match="absent from registered schema"):
        factory(job)
