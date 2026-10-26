from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from modular_research_runner.effective_config import (
    EFFECTIVE_CONFIG_CONTRACT,
    ENGINE_CONTRACT_EXTENDED_REGISTERED_V1,
    PreparedBacktestConfiguration,
    render_effective_config_document,
)
from test_backtest_config import _SOURCE_SHA, _factory, _job, _policy, _write_runtime_module

import strategy_factory.backtest_config as bc
from strategy_factory.backtest_config import (
    BacktestEnginePolicy,
    OfficialBacktestConfigFactory,
    TrustedRuntimeModule,
)
from strategy_factory.contracts import MarketType
from strategy_factory.errors import CompatibilityError


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"runtime_component_id": ""}, "canonical"),
        ({"runtime_component_version": " 1 "}, "canonical"),
        ({"import_path": "notimportable"}, "importable"),
        ({"source_commit_sha": "A" * 40}, "lowercase hex"),
        ({"source_commit_sha": "a" * 41}, "lowercase hex"),
    ],
)
def test_trusted_runtime_module_rejects_invalid(changes, message) -> None:
    values = dict(
        runtime_component_id="noop",
        runtime_component_version="1.0.0",
        import_path="pkg:Noop",
        source_commit_sha=_SOURCE_SHA,
    )
    values.update(changes)
    with pytest.raises(CompatibilityError, match=message):
        TrustedRuntimeModule(**values)


def test_trusted_runtime_module_key() -> None:
    value = TrustedRuntimeModule("noop", "1.0.0", "pkg:Noop", _SOURCE_SHA)
    assert value.key == ("noop", "1.0.0")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("execution", [], "must be a mapping"),
        ("fees", {}, "fee evidence"),
        ("instrument", {}, "instrument evidence"),
        ("gap_policy", "skip", "gap_policy"),
        ("timestamp_column", "", "timestamp_column"),
        ("columns", {"open": "o"}, "exactly"),
        (
            "columns",
            {"open": "", "high": "h", "low": "l", "close": "c", "volume": "v"},
            "non-blank",
        ),
        (
            "columns",
            {"open": "open_time", "high": "h", "low": "l", "close": "c", "volume": "v"},
            "distinct",
        ),
        ("checkpoint_mode", "bad", "checkpoint_mode"),
        ("restart_at_bar", True, "restart_at_bar"),
        ("restart_at_bar", -1, "restart_at_bar"),
        ("end_of_test_policy", "bad", "end_of_test_policy"),
    ],
)
def test_backtest_policy_rejects_invalid(field, value, message) -> None:
    base = _policy()
    kwargs = dict(
        execution=base.execution,
        fees=base.fees,
        instrument=base.instrument,
        risk=base.risk,
        gap_policy=base.gap_policy,
        timestamp_column=base.timestamp_column,
        columns=base.columns,
        checkpoint_mode=base.checkpoint_mode,
        restart_at_bar=base.restart_at_bar,
        end_of_test_policy=base.end_of_test_policy,
    )
    kwargs[field] = value
    with pytest.raises(CompatibilityError, match=message):
        BacktestEnginePolicy(**kwargs)


def test_backtest_policy_restart_requires_replay_and_core_validation() -> None:
    base = _policy()
    with pytest.raises(CompatibilityError, match="requires replay"):
        BacktestEnginePolicy(
            execution=base.execution,
            fees=base.fees,
            instrument=base.instrument,
            restart_at_bar=1,
        )
    with pytest.raises(CompatibilityError, match="assumptions are invalid"):
        BacktestEnginePolicy(
            execution={**dict(base.execution), "slippage_bps": -1},
            fees=base.fees,
            instrument=base.instrument,
        )
    replay = BacktestEnginePolicy(
        execution=base.execution,
        fees=base.fees,
        instrument=base.instrument,
        checkpoint_mode="replay",
        restart_at_bar=0,
        end_of_test_policy="leave_open",
    )
    assert replay.restart_at_bar == 0


def _trusted(import_path: str = "pkg:Noop") -> TrustedRuntimeModule:
    return TrustedRuntimeModule("noop", "1.0.0", import_path, _SOURCE_SHA)


@pytest.mark.parametrize(
    ("field", "value", "exc", "message"),
    [
        ("request", object(), TypeError, "request"),
        ("policy", object(), TypeError, "policy"),
        ("trusted_modules", "bad", TypeError, "sequence"),
        ("trusted_modules", (), CompatibilityError, "contain"),
        ("trusted_modules", (object(),), CompatibilityError, "contain"),
        ("preflight_modules", 1, TypeError, "preflight_modules"),
    ],
)
def test_factory_init_rejects_invalid(
    factory_request, tmp_path, field, value, exc, message
) -> None:
    kwargs = dict(
        request=factory_request,
        policy=_policy(),
        trusted_modules=(_trusted(),),
        allowed_output_root=tmp_path / "out",
        preflight_modules=True,
    )
    kwargs[field] = value
    with pytest.raises(exc, match=message):
        OfficialBacktestConfigFactory(**kwargs)


def test_factory_init_rejects_duplicate_and_unsafe_output(factory_request, tmp_path) -> None:
    trusted = _trusted()
    with pytest.raises(CompatibilityError, match="unique"):
        OfficialBacktestConfigFactory(
            request=factory_request,
            policy=_policy(),
            trusted_modules=(trusted, trusted),
            allowed_output_root=tmp_path / "out",
        )
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(CompatibilityError, match="symlink"):
        OfficialBacktestConfigFactory(
            request=factory_request,
            policy=_policy(),
            trusted_modules=(trusted,),
            allowed_output_root=link,
        )
    file_root = tmp_path / "file"
    file_root.write_text("x", encoding="utf-8")
    with pytest.raises((CompatibilityError, FileExistsError, OSError)):
        OfficialBacktestConfigFactory(
            request=factory_request,
            policy=_policy(),
            trusted_modules=(trusted,),
            allowed_output_root=file_root,
        )


def _prepared(factory_request, tmp_path, monkeypatch):
    import_path = _write_runtime_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    output_root = tmp_path / "jobs"
    return _factory(factory_request, output_root, import_path), output_root, import_path


def test_factory_call_rejects_job_parameter_and_timeframe_risk(
    factory_request, tmp_path, monkeypatch
) -> None:
    factory, output_root, import_path = _prepared(factory_request, tmp_path, monkeypatch)
    job = _job(factory_request, output_root, import_path)
    with pytest.raises(CompatibilityError, match="parameters must be a mapping"):
        factory(
            SimpleNamespace(
                parameters=[],
                output_directory=job.output_directory,
                period=job.period,
                seed=job.seed,
            )
        )
    request = replace(
        factory_request,
        allowed_timeframes=("5m", "30m"),
        dataset=replace(factory_request.dataset, available_timeframes=("5m", "30m")),
    )
    bad = OfficialBacktestConfigFactory(
        request=request,
        policy=_policy(),
        trusted_modules=(_trusted(import_path),),
        allowed_output_root=output_root,
        preflight_modules=False,
    )
    with pytest.raises(CompatibilityError, match="does not support"):
        bad(_job(request, output_root, import_path))
    base = _policy()
    risk_policy = BacktestEnginePolicy(
        execution=base.execution,
        fees=base.fees,
        instrument=base.instrument,
        risk={"default_risk_fraction": 0.01},
    )
    bad = OfficialBacktestConfigFactory(
        request=factory_request,
        policy=risk_policy,
        trusted_modules=(_trusted(import_path),),
        allowed_output_root=output_root,
        preflight_modules=False,
    )
    with pytest.raises(CompatibilityError, match="risk default differs"):
        bad(job)


def test_effective_config_is_opt_in_and_uses_constructed_typed_values(
    factory_request, tmp_path, monkeypatch
) -> None:
    factory, output_root, import_path = _prepared(factory_request, tmp_path, monkeypatch)
    job = replace(
        _job(factory_request, output_root, import_path),
        metadata={"configuration_sha256": "d" * 64},
    )
    default_config = factory(job)
    assert not isinstance(default_config, PreparedBacktestConfiguration)

    opted_in = OfficialBacktestConfigFactory(
        request=factory_request,
        policy=_policy(),
        trusted_modules=(_trusted(import_path),),
        allowed_output_root=output_root,
        preflight_modules=False,
        effective_config_contract=EFFECTIVE_CONFIG_CONTRACT,
        execution_performance_mode="canonical_audit",
    )
    prepared = opted_in(job)
    assert isinstance(prepared, PreparedBacktestConfiguration)
    assert (
        prepared.effective_configuration["module_identity"]["configuration"]["modules"][0][
            "core_manifest_name"
        ]
        == "noop"
    )
    assert (
        prepared.effective_configuration["engine_contract_profile"]
        == ENGINE_CONTRACT_EXTENDED_REGISTERED_V1
    )
    assert prepared.config is not None
    document = render_effective_config_document(prepared, engine_run_id="synthetic-engine-run")
    encoded_document = document
    assert job.output_directory.as_posix().encode() not in encoded_document
    assert str(prepared.config.data.path).encode() not in encoded_document
    assert str(prepared.config.modules).encode() not in encoded_document
    assert prepared.effective_configuration["data_source"]["path_sha256"]
    assert prepared.effective_configuration["data_source"]["storage_root_sha256"] is None
    assert prepared.effective_configuration["app"]["output_directory_sha256"]
    assert prepared.effective_configuration["module_identity"]["configuration_path_sha256"]
    assert (
        prepared.effective_configuration["engine"]["initial_cash"]
        == prepared.config.core_engine_config().initial_cash
    )
    assert prepared.effective_configuration["instrument"] == {
        field: getattr(prepared.config.instrument_spec(), field)
        for field in prepared.effective_configuration["instrument"]
    }
    second_root = tmp_path / "other-output"
    second_factory = OfficialBacktestConfigFactory(
        request=factory_request,
        policy=_policy(),
        trusted_modules=(_trusted(import_path),),
        allowed_output_root=second_root,
        preflight_modules=False,
        effective_config_contract=EFFECTIVE_CONFIG_CONTRACT,
        execution_performance_mode="canonical_audit",
    )
    second_job = replace(
        job,
        output_directory=(second_root / "jobs" / job.run_id).resolve(),
    )
    second = second_factory(second_job)
    assert isinstance(second, PreparedBacktestConfiguration)
    assert (
        second.effective_configuration["module_identity"]["configuration_path_sha256"]
        != prepared.effective_configuration["module_identity"]["configuration_path_sha256"]
    )
    assert (
        second.effective_configuration["app"]["output_directory_sha256"]
        != prepared.effective_configuration["app"]["output_directory_sha256"]
    )
    alternate_source = tmp_path / "same-content-alternate-path.csv"
    alternate_source.write_bytes(prepared.config.data.path.read_bytes())
    alternate_request = replace(
        factory_request,
        dataset=replace(factory_request.dataset, uri=str(alternate_source)),
    )
    alternate_factory = OfficialBacktestConfigFactory(
        request=alternate_request,
        policy=_policy(),
        trusted_modules=(_trusted(import_path),),
        allowed_output_root=second_root,
        preflight_modules=False,
        effective_config_contract=EFFECTIVE_CONFIG_CONTRACT,
        execution_performance_mode="canonical_audit",
    )
    alternate = alternate_factory(second_job)
    assert isinstance(alternate, PreparedBacktestConfiguration)
    assert (
        alternate.effective_configuration["data_source"]["path_sha256"]
        != prepared.effective_configuration["data_source"]["path_sha256"]
    )


def test_effective_config_binds_loaded_modules_before_core_dependency_reordering(
    factory_request, tmp_path, monkeypatch
) -> None:
    from modular_core import ModuleRegistry

    module_path = tmp_path / "dependency_runtime.py"
    module_path.write_text(
        """from types import MappingProxyType
from modular_core import ModuleDecision, ModuleManifest, StrategyModule

class Consumer(StrategyModule):
    name = "consumer-core"
    parameters = MappingProxyType({})
    manifest = ModuleManifest(name=name, required_features=("test.ready",))
    def on_bar(self, view):
        return ModuleDecision()

class Producer(StrategyModule):
    name = "producer-core"
    parameters = MappingProxyType({})
    manifest = ModuleManifest(name=name, produced_features=("test.ready",))
    def on_bar(self, view):
        return ModuleDecision()
""",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    import_paths = ("dependency_runtime:Consumer", "dependency_runtime:Producer")
    factory = OfficialBacktestConfigFactory(
        request=factory_request,
        policy=_policy(),
        trusted_modules=tuple(
            TrustedRuntimeModule(component, "0.1.0", import_path, _SOURCE_SHA)
            for component, import_path in zip(("consumer", "producer"), import_paths, strict=True)
        ),
        allowed_output_root=tmp_path / "jobs",
        preflight_modules=False,
        effective_config_contract=EFFECTIVE_CONFIG_CONTRACT,
        execution_performance_mode="canonical_audit",
    )
    job = replace(
        _job(factory_request, tmp_path / "jobs", import_paths[0]),
        metadata={"configuration_sha256": "d" * 64},
    )
    runtime_modules = tuple(
        {
            "runtime_component_id": component,
            "runtime_component_version": "0.1.0",
            "import_path": import_path,
            "fixed_parameters": {},
            "parameter_schema": {},
            "api_version": "1.0",
            "state_schema_version": "1",
            "source_commit_sha": _SOURCE_SHA,
        }
        for component, import_path in zip(("consumer", "producer"), import_paths, strict=True)
    )
    parameters = dict(job.parameters)
    parameters["runtime_modules"] = runtime_modules
    prepared = factory(replace(job, parameters=parameters))
    assert isinstance(prepared, PreparedBacktestConfiguration)

    loaded = ModuleRegistry().load_config(prepared.config.modules)
    plan = ModuleRegistry().validate_and_plan(
        loaded, base_timeframe=factory_request.dataset.base_timeframe
    )
    assert [manifest.name for manifest in plan.manifests] == ["producer-core", "consumer-core"]
    assert [
        item["core_manifest_name"]
        for item in prepared.effective_configuration["module_identity"]["configuration"]["modules"]
    ] == ["consumer-core", "producer-core"]


@pytest.mark.parametrize(
    ("contract", "mode", "message"),
    [
        ("unknown-contract", "canonical_audit", "contract is unsupported"),
        (EFFECTIVE_CONFIG_CONTRACT, "unknown-mode", "execution_performance_mode is unsupported"),
        (EFFECTIVE_CONFIG_CONTRACT, None, "requires an explicit performance mode"),
    ],
)
def test_effective_config_requires_supported_explicit_contract_and_mode(
    factory_request, tmp_path, contract, mode, message
) -> None:
    with pytest.raises(CompatibilityError, match=message):
        OfficialBacktestConfigFactory(
            request=factory_request,
            policy=_policy(),
            trusted_modules=(_trusted("package:Module"),),
            allowed_output_root=tmp_path / "output",
            preflight_modules=False,
            effective_config_contract=contract,
            execution_performance_mode=mode,
        )


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        ({}, "job configuration hash is invalid"),
        ({"configuration_sha256": "bad"}, "job configuration hash is invalid"),
    ],
)
def test_effective_config_requires_valid_runner_job_context(
    factory_request, tmp_path, monkeypatch, metadata, message
) -> None:
    _, output_root, import_path = _prepared(factory_request, tmp_path, monkeypatch)
    job = replace(
        _job(factory_request, output_root, import_path),
        metadata=metadata,
    )
    opted_in = OfficialBacktestConfigFactory(
        request=factory_request,
        policy=_policy(),
        trusted_modules=(_trusted(import_path),),
        allowed_output_root=output_root,
        preflight_modules=False,
        effective_config_contract=EFFECTIVE_CONFIG_CONTRACT,
        execution_performance_mode="canonical_audit",
    )
    with pytest.raises(CompatibilityError, match=message):
        opted_in(job)


@pytest.mark.parametrize(
    ("job_change", "rendered_modules", "message"),
    [
        ({"parameters": {}}, None, "runtime module blueprint is missing"),
        (None, [], "rendered module configuration differs"),
    ],
)
def test_effective_config_rejects_missing_or_inconsistent_runner_blueprint(
    factory_request, tmp_path, monkeypatch, job_change, rendered_modules, message
) -> None:
    factory, output_root, import_path = _prepared(factory_request, tmp_path, monkeypatch)
    job = replace(
        _job(factory_request, output_root, import_path),
        metadata={"configuration_sha256": "d" * 64},
    )
    config = factory(job)
    opted_in = OfficialBacktestConfigFactory(
        request=factory_request,
        policy=_policy(),
        trusted_modules=(_trusted(import_path),),
        allowed_output_root=output_root,
        preflight_modules=False,
        effective_config_contract=EFFECTIVE_CONFIG_CONTRACT,
        execution_performance_mode="canonical_audit",
    )
    changed_job = replace(job, **job_change) if job_change is not None else job
    payload = (
        {"modules": rendered_modules}
        if rendered_modules is not None
        else {"modules": json.loads(config.modules.read_text(encoding="utf-8"))["modules"]}
    )
    with pytest.raises(CompatibilityError, match=message):
        opted_in._prepared_effective_configuration(
            changed_job,
            config,
            modules_payload=payload,
            modules_path=config.modules,
        )


def test_effective_config_rejects_untyped_values_and_naive_timestamps() -> None:
    with pytest.raises(CompatibilityError, match="typed dataclass"):
        bc._typed_dataclass_mapping(object(), "engine")
    with pytest.raises(CompatibilityError, match="timezone-aware"):
        bc._effective_timestamp(datetime(2026, 1, 1), "data.start")
    with pytest.raises(CompatibilityError, match="path identity is malformed"):
        bc._path_identity_sha256(object())


def test_job_output_and_period_validation(factory_request, tmp_path, monkeypatch) -> None:
    factory, output_root, import_path = _prepared(factory_request, tmp_path, monkeypatch)
    job = _job(factory_request, output_root, import_path)
    for raw, message in [
        ("x", "must be Path"),
        (Path("relative"), "absolute"),
        ((tmp_path / "escape").resolve(), "escapes"),
    ]:
        with pytest.raises(CompatibilityError, match=message):
            factory._job_output_directory(SimpleNamespace(output_directory=raw))
    no_period = replace(job, period=None)
    assert factory._job_period(no_period) == (
        factory_request.dataset.period.start,
        factory_request.dataset.period.end,
    )
    with pytest.raises(CompatibilityError, match="timezone-aware"):
        factory._job_period(
            SimpleNamespace(
                period=SimpleNamespace(
                    start=datetime(2026, 1, 1), end=factory_request.dataset.period.end
                )
            )
        )
    with pytest.raises(CompatibilityError, match="escapes dataset"):
        factory._job_period(
            SimpleNamespace(
                period=SimpleNamespace(
                    start=factory_request.dataset.period.start,
                    end=factory_request.dataset.period.start,
                )
            )
        )


def _blueprint(import_path):
    return {
        "runtime_component_id": "noop",
        "runtime_component_version": "1.0.0",
        "import_path": import_path,
        "fixed_parameters": {"period": 2},
        "parameter_schema": {"period": {"type": "integer"}},
        "api_version": "1.0",
        "state_schema_version": "1",
        "source_commit_sha": _SOURCE_SHA,
    }


@pytest.mark.parametrize(
    ("parameters", "message"),
    [
        ({}, "no runtime_modules"),
        ({"runtime_modules": (1,)}, "must be a mapping"),
        ({"runtime_modules": ({"unknown": 1},)}, "unknown fields"),
    ],
)
def test_module_blueprint_rejects_shape(
    factory_request, tmp_path, monkeypatch, parameters, message
) -> None:
    factory, _, _ = _prepared(factory_request, tmp_path, monkeypatch)
    with pytest.raises(CompatibilityError, match=message):
        factory._module_configuration(parameters)


def test_module_blueprint_rejects_identity_fixed_and_parameter_errors(
    factory_request, tmp_path, monkeypatch
) -> None:
    factory, _, import_path = _prepared(factory_request, tmp_path, monkeypatch)
    base = _blueprint(import_path)
    cases = [
        ({"runtime_modules": ({**base, "runtime_component_id": "other"},)}, "not allowlisted"),
        ({"runtime_modules": (base, base)}, "duplicate component"),
        (
            {"runtime_modules": ({**base, "fixed_parameters": {"missing": 1}},)},
            "absent from schema",
        ),
        ({"runtime_modules": ({**base, "fixed_parameters": []},)}, "must be a mapping"),
        ({"runtime_modules": ({**base, "parameter_schema": {1: {}}},)}, "keys must be strings"),
        ({"runtime_modules": (base,), 1: 2}, "names must be strings"),
        ({"runtime_modules": (base,), "unqualified": 1}, "unqualified"),
        ({"runtime_modules": (base,), "other.x": 1}, "untrusted component"),
    ]
    for parameters, message in cases:
        with pytest.raises(CompatibilityError, match=message):
            factory._module_configuration(parameters)


def test_write_module_configuration_unsafe_and_conflicting(
    factory_request, tmp_path, monkeypatch
) -> None:
    factory, output_root, _ = _prepared(factory_request, tmp_path, monkeypatch)
    output = output_root / "job"
    output.mkdir(parents=True)
    target = output / "real"
    target.mkdir()
    boundary = output / ".strategy-factory"
    boundary.symlink_to(target, target_is_directory=True)
    with pytest.raises(CompatibilityError, match="boundary"):
        factory._write_modules_configuration(output, {"modules": ()})
    boundary.unlink()
    boundary.mkdir()
    path = boundary / "modules.yaml"
    path.write_text("different", encoding="utf-8")
    with pytest.raises(CompatibilityError, match="differs"):
        factory._write_modules_configuration(output, {"modules": ()})
    path.unlink()
    path.mkdir()
    with pytest.raises(CompatibilityError, match="unsafe"):
        factory._write_modules_configuration(output, {"modules": ()})


def test_backtest_helpers_reject_invalid(factory_request, tmp_path) -> None:
    with pytest.raises(CompatibilityError, match="USDT-M"):
        bc._backtest_market_type(
            replace(
                factory_request,
                market_type=MarketType.SPOT,
                dataset=replace(factory_request.dataset, market_type=MarketType.SPOT),
            )
        )
    with pytest.raises(CompatibilityError, match="local dataset"):
        bc._local_dataset_path("https://example.com/data.csv")
    with pytest.raises(CompatibilityError, match="authority"):
        bc._local_dataset_path("file://remote/tmp/data.csv")
    with pytest.raises(CompatibilityError, match="regular file"):
        bc._local_dataset_path(str(tmp_path / "missing.csv"))
    with pytest.raises(CompatibilityError, match="seed"):
        bc._job_seed(SimpleNamespace(seed=True))
    with pytest.raises(CompatibilityError, match="timezone-aware"):
        bc._utc_datetime(datetime(2026, 1, 1), "dt")
    with pytest.raises(CompatibilityError, match="canonical"):
        bc._text(" x ", "x")
    with pytest.raises(CompatibilityError, match="mapping"):
        bc._mapping([], "x")
    with pytest.raises(CompatibilityError, match="keys"):
        bc._mapping({1: "x"}, "x")


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (float("inf"), "non-finite float"),
        (Decimal("NaN"), "non-finite Decimal"),
        (object(), "unsupported value"),
    ],
)
def test_json_primitive_rejects_invalid(value, message) -> None:
    with pytest.raises(CompatibilityError, match=message):
        bc._json_primitive(value)


def test_json_primitive_converts_nested_values() -> None:
    assert bc._json_primitive({"x": (Decimal("1.20"), [1, None]), "f": 1.5}) == {
        "x": ["1.20", [1, None]],
        "f": 1.5,
    }


def test_reject_symlink_chain_and_hash(factory_request, tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    real = tmp_path / "real"
    real.mkdir()
    link = root / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(CompatibilityError, match="symlink"):
        bc._reject_symlink_chain(link / "job", stop=root)
    with pytest.raises(CompatibilityError, match="does not descend"):
        bc._reject_symlink_chain(Path("/"), stop=root)
    with pytest.raises(CompatibilityError, match="hash changed"):
        bc._verify_regular_file_hash(Path(factory_request.dataset.uri), "0" * 64)
