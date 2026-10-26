from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from effective_config_fixture import prepared_effective_config
from modular_backtest.config import BacktestAppConfig
from modular_core.config import EngineConfig

from modular_research_runner.effective_config import (
    EFFECTIVE_CONFIG_CONTRACT,
    EFFECTIVE_CONFIG_FILENAME,
    EFFECTIVE_CONFIG_SCHEMA_ID,
    EFFECTIVE_CONFIG_SCHEMA_VERSION,
    ENGINE_CONTRACT_EXTENDED_REGISTERED_V1,
    ENGINE_CONTRACT_FILE_ONLY_V1,
    EffectiveConfigArtifactReference,
    PreparedBacktestConfiguration,
    canonical_json_bytes,
    official_engine_contract_profile,
    projection_sha256,
    read_sidecar_for_job,
    render_effective_config_document,
    validate_effective_config_document,
    validate_legacy_v2_effective_config_document,
    verify_actual_backtest_configuration,
)
from modular_research_runner.models import SpecificationError


def test_effective_config_is_complete_canonical_and_context_bound(tmp_path) -> None:
    prepared = prepared_effective_config(tmp_path, run_id="runner-run")
    payload = render_effective_config_document(prepared, engine_run_id="core-run")
    document = validate_effective_config_document(json.loads(payload))
    assert document["context"]["run_id"] == "runner-run"
    assert document["context"]["engine_run_id"] == "core-run"
    assert document["effective_configuration"]["engine"]["initial_cash"] == 1000.0
    assert document["effective_configuration"]["module_identity"]["components"] == ["noop"]


def test_effective_config_v2_is_not_accepted_as_v3(tmp_path) -> None:
    prepared = prepared_effective_config(tmp_path, run_id="runner-run")
    document = json.loads(render_effective_config_document(prepared, engine_run_id="core-run"))
    document["schema_version"] = "2"
    with pytest.raises(SpecificationError, match="schema version is unsupported"):
        validate_effective_config_document(document)


def test_legacy_v2_validator_accepts_old_app_shape_without_v3_normalization(tmp_path) -> None:
    prepared = prepared_effective_config(tmp_path, run_id="runner-run")
    document = json.loads(render_effective_config_document(prepared, engine_run_id="core-run"))
    document["schema_version"] = "2"
    app = document["effective_configuration"]["app"]
    app.pop("max_base_history")
    app.pop("max_timeframe_history")
    app.pop("performance")
    document["projection_sha256"] = projection_sha256(document)

    legacy = validate_legacy_v2_effective_config_document(document)

    assert legacy["schema_version"] == "2"
    assert "performance" not in legacy["effective_configuration"]["app"]
    with pytest.raises(SpecificationError, match="schema version is unsupported"):
        validate_effective_config_document(document)


def test_effective_config_rejects_mutated_values_and_incomplete_schema(tmp_path) -> None:
    prepared = prepared_effective_config(tmp_path)
    document = json.loads(render_effective_config_document(prepared, engine_run_id="core-run"))
    document["effective_configuration"]["engine"]["initial_cash"] = True
    with pytest.raises(SpecificationError, match="finite number"):
        validate_effective_config_document(document)


def test_effective_config_rejects_legacy_registered_canonical_replay(tmp_path) -> None:
    prepared = prepared_effective_config(tmp_path)
    effective = json.loads(prepared._effective_configuration_bytes)
    effective["app"]["checkpoint"] = {"mode": "replay", "restart_at_bar": 10}
    effective["app"]["max_base_history"] = 65536
    effective["app"]["max_timeframe_history"] = 4096
    performance = replace(
        prepared.performance_config,
        max_base_history=65536,
        max_timeframe_history=4096,
    )
    effective["app"]["performance"]["values"] = {
        item.name: getattr(performance, item.name) for item in fields(performance)
    }
    effective["data_source"]["registered_mode"] = "legacy_materialized"
    effective["data_source"]["stream_batch_size"] = None
    effective["engine"]["data"]["max_base_history"] = 65536

    with pytest.raises(
        SpecificationError,
        match="cannot attest canonical-audit replay for registered legacy-materialized data",
    ):
        PreparedBacktestConfiguration(
            EFFECTIVE_CONFIG_CONTRACT,
            prepared.config,
            prepared.context,
            effective,
            performance,
        )

    document = json.loads(render_effective_config_document(prepared, engine_run_id="core-run"))
    document["effective_configuration"]["instrument"].pop("tick_size")
    with pytest.raises(
        SpecificationError,
        match="fields are invalid|cannot describe registered data",
    ):
        validate_effective_config_document(document)


def test_effective_config_rejects_module_hash_and_identity_drift(tmp_path) -> None:
    prepared = prepared_effective_config(tmp_path)
    document = json.loads(render_effective_config_document(prepared, engine_run_id="core-run"))
    document["effective_configuration"]["module_identity"]["components"] = ["other"]
    with pytest.raises(SpecificationError, match="component list"):
        validate_effective_config_document(document)


@pytest.mark.parametrize("mode", [True, "unknown", None])
def test_effective_config_rejects_unknown_callback_mode(tmp_path, mode) -> None:
    prepared = prepared_effective_config(tmp_path)
    document = json.loads(render_effective_config_document(prepared, engine_run_id="core-run"))
    document["context"]["execution_performance_mode"] = mode
    with pytest.raises(SpecificationError, match="execution performance mode"):
        validate_effective_config_document(document)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("app", "seed"), True, "must be an integer"),
        (("app", "checkpoint", "restart_at_bar"), True, "must be an integer"),
        (("app", "output_directory_sha256"), "BAD", "lowercase SHA-256"),
        (("data_source", "source_kind"), "local", "unsupported"),
        (("data_source", "columns", "close"), 5, "canonical non-blank string"),
        (("engine", "one_position"), 1, "must be boolean"),
        (("instrument", "max_quantity"), True, "must be a finite number"),
        (("module_identity", "configuration_sha256"), "BAD", "module configuration SHA-256"),
        (("module_identity", "configuration", "modules"), [], "non-empty list"),
        (
            ("module_identity", "configuration", "modules", 0, "source_commit_sha"),
            "bad",
            "source_commit_sha is invalid",
        ),
        (
            ("module_identity", "configuration", "rendered", "modules"),
            [],
            "does not match identities",
        ),
        (("module_identity", "components"), ["noop", "noop"], "unique non-empty strings"),
    ],
)
def test_effective_config_rejects_resealed_field_mutations(tmp_path, path, value, message) -> None:
    prepared = prepared_effective_config(tmp_path)
    document = json.loads(render_effective_config_document(prepared, engine_run_id="core-run"))
    target: Any = document["effective_configuration"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    document["projection_sha256"] = projection_sha256(document)
    with pytest.raises(SpecificationError, match=message):
        validate_effective_config_document(document)


def test_effective_config_rejects_file_source_without_opaque_path_identity(tmp_path) -> None:
    prepared = prepared_effective_config(tmp_path)
    document = json.loads(render_effective_config_document(prepared, engine_run_id="core-run"))
    source = document["effective_configuration"]["data_source"]
    source["source_kind"] = "file"
    source["path_sha256"] = None
    document["projection_sha256"] = projection_sha256(document)
    with pytest.raises(SpecificationError, match="file data source path identity is invalid"):
        validate_effective_config_document(document)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("data_source", "timestamp_column"), " open_time "),
        (("data_source", "columns", "close"), " close "),
    ],
)
def test_effective_config_rejects_padded_physical_column_names(tmp_path, path, value) -> None:
    prepared = prepared_effective_config(tmp_path)
    document = json.loads(render_effective_config_document(prepared, engine_run_id="core-run"))
    target: Any = document["effective_configuration"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    document["projection_sha256"] = projection_sha256(document)
    with pytest.raises(SpecificationError, match="canonical non-blank string"):
        validate_effective_config_document(document)


def test_actual_typed_config_rejects_padded_physical_column_names(tmp_path) -> None:
    prepared = prepared_effective_config(tmp_path)
    padded_timestamp = replace(prepared.config.data, timestamp_column=" open_time ")
    with pytest.raises(SpecificationError, match="canonical non-blank string"):
        verify_actual_backtest_configuration(
            replace(prepared, config=replace(prepared.config, data=padded_timestamp))
        )

    columns = dict(prepared.config.data.columns)
    columns["close"] = " close "
    padded_columns = replace(prepared.config.data, columns=columns)
    with pytest.raises(SpecificationError, match="canonical non-blank string"):
        verify_actual_backtest_configuration(
            replace(prepared, config=replace(prepared.config, data=padded_columns))
        )


@pytest.mark.parametrize("field", ["symbol", "base_asset", "quote_asset", "market_type"])
def test_effective_config_rejects_padded_instrument_identity_fields(tmp_path, field) -> None:
    prepared = prepared_effective_config(tmp_path)
    document = json.loads(render_effective_config_document(prepared, engine_run_id="core-run"))
    document["effective_configuration"]["instrument"][field] = (
        f" {document['effective_configuration']['instrument'][field]} "
    )
    document["projection_sha256"] = projection_sha256(document)
    with pytest.raises(SpecificationError, match="canonical non-blank string"):
        validate_effective_config_document(document)


@pytest.mark.parametrize("field", ["base_asset", "quote_asset"])
def test_actual_typed_config_rejects_padded_instrument_identity_fields(tmp_path, field) -> None:
    prepared = prepared_effective_config(tmp_path)
    instrument = dict(prepared.config.instrument)
    instrument[field] = f" {instrument[field]} "
    with pytest.raises(SpecificationError, match="canonical non-blank string"):
        verify_actual_backtest_configuration(
            replace(prepared, config=replace(prepared.config, instrument=instrument))
        )


def test_file_source_registered_mode_is_checked_before_projection_normalizes_it(tmp_path) -> None:
    prepared = prepared_effective_config(tmp_path)
    data = prepared.config.data
    if not hasattr(data, "registered_mode"):
        assert prepared.effective_configuration["engine_contract_profile"] == (
            ENGINE_CONTRACT_FILE_ONLY_V1
        )
        assert "registered_mode" not in {item.name for item in fields(data)}
        return
    object.__setattr__(data, "path", tmp_path / "synthetic.csv")
    object.__setattr__(data, "format", "csv")
    object.__setattr__(data, "storage_root", None)
    object.__setattr__(data, "dataset_id", None)
    object.__setattr__(data, "dataset_version", None)
    object.__setattr__(data, "registered_mode", "streaming")
    object.__setattr__(data, "stream_batch_size", None)
    with pytest.raises(SpecificationError, match="registered_mode must be legacy_materialized"):
        verify_actual_backtest_configuration(prepared)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_id", "old", "schema id is unsupported"),
        ("schema_version", 1, "schema version is unsupported"),
        ("projection_sha256", "invalid", "projection SHA-256 is invalid"),
    ],
)
def test_effective_config_rejects_document_identity_drift(tmp_path, field, value, message) -> None:
    prepared = prepared_effective_config(tmp_path)
    document = json.loads(render_effective_config_document(prepared, engine_run_id="core-run"))
    document[field] = value
    with pytest.raises(SpecificationError, match=message):
        validate_effective_config_document(document)


@pytest.mark.parametrize(
    ("relative_path", "sha256", "size_bytes", "message"),
    [
        ("other.json", "a" * 64, 1, "path is not canonical"),
        (EFFECTIVE_CONFIG_FILENAME, "bad", 1, "SHA-256 is invalid"),
        (EFFECTIVE_CONFIG_FILENAME, "a" * 64, True, "size must be positive"),
    ],
)
def test_effective_config_artifact_reference_rejects_bad_identity(
    relative_path, sha256, size_bytes, message
) -> None:
    with pytest.raises(SpecificationError, match=message):
        EffectiveConfigArtifactReference(relative_path, sha256, size_bytes)


def test_effective_config_public_contract_rejects_invalid_runtime_inputs(tmp_path) -> None:
    with pytest.raises(SpecificationError, match="unsupported effective config contract"):
        PreparedBacktestConfiguration("v0", object(), {}, {})
    with pytest.raises(SpecificationError, match="configuration is missing"):
        PreparedBacktestConfiguration(EFFECTIVE_CONFIG_CONTRACT, None, {}, {})
    with pytest.raises(SpecificationError, match="cannot be canonically encoded"):
        canonical_json_bytes({"unsupported": object()})
    with pytest.raises(SpecificationError, match="cannot be canonically encoded"):
        canonical_json_bytes({"non_finite": float("nan")})
    with pytest.raises(SpecificationError, match="reference is invalid"):
        read_sidecar_for_job(tmp_path, object())  # type: ignore[arg-type]


def test_effective_config_validator_rejects_zero_capacity_and_bad_timestamp(tmp_path) -> None:
    prepared = prepared_effective_config(tmp_path)
    document = json.loads(render_effective_config_document(prepared, engine_run_id="core-run"))
    document["effective_configuration"]["engine"]["initial_cash"] = 0
    document["projection_sha256"] = projection_sha256(document)
    with pytest.raises(SpecificationError, match="must be positive"):
        validate_effective_config_document(document)

    document = json.loads(render_effective_config_document(prepared, engine_run_id="core-run"))
    document["context"]["period_start"] = "not-a-timestampZ"
    document["projection_sha256"] = projection_sha256(document)
    with pytest.raises(SpecificationError, match="canonical UTC timestamp"):
        validate_effective_config_document(document)

    document = json.loads(render_effective_config_document(prepared, engine_run_id="core-run"))
    document["context"]["repetition_index"] = -1
    document["projection_sha256"] = projection_sha256(document)
    with pytest.raises(SpecificationError, match="non-negative integer"):
        validate_effective_config_document(document)


def test_actual_config_verifier_rejects_non_official_or_unsafe_module_config(tmp_path) -> None:
    prepared = prepared_effective_config(tmp_path)
    with pytest.raises(SpecificationError, match="requires PreparedBacktestConfiguration"):
        verify_actual_backtest_configuration(object())  # type: ignore[arg-type]
    non_engine = replace(prepared, config=object())
    with pytest.raises(SpecificationError, match="official BacktestAppConfig"):
        verify_actual_backtest_configuration(non_engine)

    symlink = tmp_path / "linked-modules.json"
    symlink.symlink_to(prepared.config.modules)
    unsafe = replace(prepared, config=replace(prepared.config, modules=symlink))
    with pytest.raises(SpecificationError, match="module configuration path is unsafe"):
        verify_actual_backtest_configuration(unsafe)


def test_actual_registered_source_path_changes_bound_identity(tmp_path) -> None:
    prepared = prepared_effective_config(tmp_path)
    if hasattr(prepared.config.data, "storage_root"):
        changed_data = replace(
            prepared.config.data,
            storage_root=tmp_path / "alternate-registered-root",
        )
    else:
        changed_data = replace(
            prepared.config.data,
            path=tmp_path / "alternate-file-source.csv",
        )
    changed_config = replace(prepared.config, data=changed_data)
    changed = replace(prepared, config=changed_config)
    with pytest.raises(SpecificationError, match="differs from actual Backtest configuration"):
        verify_actual_backtest_configuration(changed)


@pytest.mark.parametrize(
    ("environment_name", "environment_value"),
    [
        ("MODULAR_BACKTEST_REGISTERED_DATASET_MODE", "legacy_materialized"),
        ("MODULAR_BACKTEST_REGISTERED_STREAM_BATCH_SIZE", "4096"),
    ],
)
def test_actual_registered_reader_environment_overrides_must_match_projection(
    tmp_path, monkeypatch, environment_name, environment_value
) -> None:
    prepared = prepared_effective_config(tmp_path)
    if prepared.effective_configuration["data_source"]["source_kind"] != "registered":
        pytest.skip("installed Backtest profile does not support registered data")
    monkeypatch.setenv(environment_name, environment_value)

    with pytest.raises(SpecificationError, match="differs from actual Backtest configuration"):
        verify_actual_backtest_configuration(prepared)


def test_effective_config_requires_a_known_matching_engine_profile(tmp_path) -> None:
    prepared = prepared_effective_config(tmp_path)
    assert prepared.effective_configuration["engine_contract_profile"] in {
        ENGINE_CONTRACT_FILE_ONLY_V1,
        ENGINE_CONTRACT_EXTENDED_REGISTERED_V1,
    }
    document = json.loads(render_effective_config_document(prepared, engine_run_id="core-run"))
    document["effective_configuration"]["engine_contract_profile"] = "unknown-profile"
    document["projection_sha256"] = projection_sha256(document)
    with pytest.raises(SpecificationError, match="engine contract profile is unsupported"):
        validate_effective_config_document(document)


def _extended_registered_profile_document(tmp_path):
    prepared = prepared_effective_config(tmp_path)
    document = json.loads(render_effective_config_document(prepared, engine_run_id="core-run"))
    effective = document["effective_configuration"]
    effective["engine_contract_profile"] = ENGINE_CONTRACT_EXTENDED_REGISTERED_V1
    source = effective["data_source"]
    source.update(
        source_kind="registered",
        path_sha256=None,
        storage_root_sha256="d" * 64,
        format=None,
        dataset_id=document["context"]["dataset_id"],
        dataset_sha256=document["context"]["dataset_sha256"],
        dataset_version=1,
        registered_mode="streaming",
        stream_batch_size=8192,
    )
    execution = effective["engine"]["execution"]
    execution.update(allow_limit_stop_on_entry_bar=False, close_request_timing="next_open")
    effective["engine"]["risk"]["sizing_model"] = "stop_distance"
    effective["engine"]["data"]["max_base_history"] = None
    document["projection_sha256"] = projection_sha256(document)
    return document


def test_extended_registered_profile_round_trips_with_exact_schema_identity(tmp_path) -> None:
    document = _extended_registered_profile_document(tmp_path)
    encoded = canonical_json_bytes(document)
    restored = json.loads(encoded)
    validated = validate_effective_config_document(restored)
    assert validated["schema_id"] == EFFECTIVE_CONFIG_SCHEMA_ID
    assert validated["schema_version"] == EFFECTIVE_CONFIG_SCHEMA_VERSION == "3"
    assert validated["effective_configuration"]["engine_contract_profile"] == (
        ENGINE_CONTRACT_EXTENDED_REGISTERED_V1
    )
    assert canonical_json_bytes(dict(validated)) == encoded


@pytest.mark.parametrize(
    "path",
    [
        ("data_source", "storage_root_sha256"),
        ("engine", "execution", "allow_limit_stop_on_entry_bar"),
        ("engine", "execution", "close_request_timing"),
        ("engine", "risk", "sizing_model"),
        ("engine", "data", "max_base_history"),
    ],
)
def test_extended_registered_profile_rejects_missing_required_fields(tmp_path, path) -> None:
    document = _extended_registered_profile_document(tmp_path)
    target: Any = document["effective_configuration"]
    for key in path[:-1]:
        target = target[key]
    target.pop(path[-1])
    document["projection_sha256"] = projection_sha256(document)
    with pytest.raises(SpecificationError, match="fields are invalid"):
        validate_effective_config_document(document)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("data_source", "dataset_version"), True, "must be an integer"),
        (("engine", "execution", "allow_limit_stop_on_entry_bar"), 1, "must be boolean"),
        (("engine", "data", "max_base_history"), True, "must be an integer"),
    ],
)
def test_extended_registered_profile_rejects_resealed_scalar_type_drift(
    tmp_path, path, value, message
) -> None:
    document = _extended_registered_profile_document(tmp_path)
    target: Any = document["effective_configuration"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    document["projection_sha256"] = projection_sha256(document)
    with pytest.raises(SpecificationError, match=message):
        validate_effective_config_document(document)


def test_file_only_profile_cannot_claim_registered_source(tmp_path) -> None:
    document = _extended_registered_profile_document(tmp_path)
    document["effective_configuration"]["engine_contract_profile"] = ENGINE_CONTRACT_FILE_ONLY_V1
    document["projection_sha256"] = projection_sha256(document)
    with pytest.raises(
        SpecificationError,
        match="file-only engine contract cannot describe registered data",
    ):
        validate_effective_config_document(document)


def test_actual_config_rejects_unreviewed_engine_field_shape(tmp_path, monkeypatch) -> None:
    prepared = prepared_effective_config(tmp_path)

    @dataclass(frozen=True, slots=True)
    class UnreviewedEngineConfig(EngineConfig):
        new_runtime_switch: bool = False

    original = BacktestAppConfig.core_engine_config

    def changed_profile(config):
        engine = original(config)
        values = {item.name: getattr(engine, item.name) for item in fields(EngineConfig)}
        return UnreviewedEngineConfig(**values)

    monkeypatch.setattr(BacktestAppConfig, "core_engine_config", changed_profile)
    with pytest.raises(SpecificationError, match="EngineConfig fields are unsupported"):
        verify_actual_backtest_configuration(prepared)

    document = json.loads(render_effective_config_document(prepared, engine_run_id="core-run"))
    current_profile = document["effective_configuration"]["engine_contract_profile"]
    document["effective_configuration"]["engine_contract_profile"] = (
        ENGINE_CONTRACT_FILE_ONLY_V1
        if current_profile == ENGINE_CONTRACT_EXTENDED_REGISTERED_V1
        else ENGINE_CONTRACT_EXTENDED_REGISTERED_V1
    )
    document["projection_sha256"] = projection_sha256(document)
    with pytest.raises(
        SpecificationError,
        match="fields are invalid|cannot describe registered data",
    ):
        validate_effective_config_document(document)


def test_actual_profile_rejects_non_official_and_mixed_nested_field_shapes(
    tmp_path, monkeypatch
) -> None:
    with pytest.raises(SpecificationError, match="official BacktestAppConfig"):
        official_engine_contract_profile(object())

    prepared = prepared_effective_config(tmp_path)
    original = BacktestAppConfig.core_engine_config
    execution_type = type(original(prepared.config).execution)

    @dataclass(frozen=True, slots=True)
    class MixedExecutionConfig(execution_type):
        unreviewed_switch: bool = False

    def mixed_nested_shape(config):
        engine = original(config)
        values = {
            item.name: getattr(engine.execution, item.name) for item in fields(execution_type)
        }
        return replace(engine, execution=MixedExecutionConfig(**values))

    monkeypatch.setattr(BacktestAppConfig, "core_engine_config", mixed_nested_shape)
    with pytest.raises(SpecificationError, match="field profile is unsupported"):
        verify_actual_backtest_configuration(prepared)


def test_actual_module_file_mutation_invalidates_effective_config_snapshot(tmp_path) -> None:
    prepared = prepared_effective_config(tmp_path)
    prepared.config.modules.write_text('{"modules": []}\n', encoding="utf-8")
    with pytest.raises(SpecificationError, match="actual module file differs"):
        verify_actual_backtest_configuration(prepared)

    prepared.config.modules.write_text("not JSON\n", encoding="utf-8")
    with pytest.raises(SpecificationError, match="module configuration is invalid JSON"):
        verify_actual_backtest_configuration(prepared)


@pytest.mark.parametrize(
    ("target", "attribute", "value", "message"),
    [
        ("data", "columns", {1: "close"}, "mapping keys must be strings"),
        ("data", "path", object(), "filesystem path-like"),
        ("data", "start", datetime(2026, 1, 1), "timezone-aware"),
        ("config", "initial_capital", float("nan"), "non-finite float"),
    ],
)
def test_actual_typed_config_rejects_malformed_mutated_fields(
    tmp_path, target, attribute, value, message
) -> None:
    prepared = prepared_effective_config(tmp_path)
    actual = prepared.config.data if target == "data" else prepared.config
    object.__setattr__(actual, attribute, value)
    with pytest.raises(SpecificationError, match=message):
        verify_actual_backtest_configuration(prepared)


@pytest.mark.parametrize(
    ("context_overrides", "message"),
    [
        ({"run_id": " padded "}, "canonical non-blank string"),
        ({"execution_performance_mode": "other"}, "performance mode is invalid"),
        ({"period_start": "2026-01-01T00:00:00+00:00"}, "canonical UTC timestamp"),
        ({"period_start": "2026-01-01T00:00:00Z"}, "canonical UTC timestamp"),
        ({"repetition_index": -1}, "non-negative integer"),
    ],
)
def test_prepared_effective_config_rejects_invalid_context(
    tmp_path, context_overrides, message
) -> None:
    with pytest.raises(SpecificationError, match=message):
        prepared_effective_config(tmp_path, context_overrides=context_overrides)


def test_prepared_effective_config_rejects_boolean_seed_context(tmp_path) -> None:
    prepared = prepared_effective_config(tmp_path)
    context = dict(prepared.context)
    context["seed"] = True
    with pytest.raises(SpecificationError, match="seed must be an integer"):
        replace(prepared, context=context)


def test_effective_config_rejects_context_disagreement_and_projection_hash_drift(tmp_path) -> None:
    prepared = prepared_effective_config(tmp_path)
    document = json.loads(render_effective_config_document(prepared, engine_run_id="core-run"))
    document["effective_configuration"]["app"]["seed"] = 9
    document["projection_sha256"] = projection_sha256(document)
    with pytest.raises(SpecificationError, match="disagree with their context"):
        validate_effective_config_document(document)

    document = json.loads(render_effective_config_document(prepared, engine_run_id="core-run"))
    document["projection_sha256"] = "a" * 64
    with pytest.raises(SpecificationError, match="projection SHA-256 does not match"):
        validate_effective_config_document(document)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (
            ("module_identity", "configuration", "rendered", "modules", 0, "class"),
            "pkg:Other",
            "rendered module configuration identity does not match",
        ),
        (("module_identity", "components"), [], "must be a non-empty list"),
        (("module_identity", "configuration_sha256"), "0" * 64, "digest does not match"),
    ],
)
def test_effective_config_rejects_module_identity_drift(tmp_path, path, value, message) -> None:
    prepared = prepared_effective_config(tmp_path)
    document = json.loads(render_effective_config_document(prepared, engine_run_id="core-run"))
    target: Any = document["effective_configuration"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    document["projection_sha256"] = projection_sha256(document)
    with pytest.raises(SpecificationError, match=message):
        validate_effective_config_document(document)


@pytest.mark.parametrize(
    ("identity_value", "rendered_value"),
    [(1, True), (0, False), (1, 1.0)],
)
def test_effective_config_rejects_python_equal_but_json_distinct_parameters(
    tmp_path, identity_value, rendered_value
) -> None:
    prepared = prepared_effective_config(tmp_path)
    document = json.loads(render_effective_config_document(prepared, engine_run_id="core-run"))
    module_identity = document["effective_configuration"]["module_identity"]
    identity = module_identity["configuration"]
    identity["modules"][0]["parameters"]["period"] = identity_value
    identity["rendered"]["modules"][0]["parameters"]["period"] = rendered_value
    module_identity["configuration_sha256"] = hashlib.sha256(
        canonical_json_bytes(identity["rendered"])
    ).hexdigest()
    document["projection_sha256"] = projection_sha256(document)
    with pytest.raises(SpecificationError, match="rendered module configuration identity"):
        validate_effective_config_document(document)
    with pytest.raises(SpecificationError, match="rendered module configuration identity"):
        PreparedBacktestConfiguration(
            EFFECTIVE_CONFIG_CONTRACT,
            prepared.config,
            prepared.context,
            document["effective_configuration"],
            prepared.performance_config,
        )


def test_effective_config_accepts_matching_nested_json_parameters(tmp_path) -> None:
    prepared = prepared_effective_config(tmp_path)
    document = json.loads(render_effective_config_document(prepared, engine_run_id="core-run"))
    module_identity = document["effective_configuration"]["module_identity"]
    identity = module_identity["configuration"]
    parameters = {"period": 1, "tuning": {"steps": [1, 2], "enabled": True}}
    identity["modules"][0]["parameters"] = parameters
    identity["rendered"]["modules"][0]["parameters"] = parameters
    module_identity["configuration_sha256"] = hashlib.sha256(
        canonical_json_bytes(identity["rendered"])
    ).hexdigest()
    document["projection_sha256"] = projection_sha256(document)
    validate_effective_config_document(document)


@pytest.mark.parametrize(("expected_value", "actual_value"), [(1, True), (0, False), (1, 1.0)])
def test_actual_module_file_comparison_preserves_json_scalar_types(
    tmp_path, expected_value, actual_value
) -> None:
    prepared = prepared_effective_config(tmp_path)
    effective = json.loads(prepared._effective_configuration_bytes)
    module_identity = effective["module_identity"]
    module_config = module_identity["configuration"]
    module_config["modules"][0]["parameters"]["period"] = expected_value
    module_config["rendered"]["modules"][0]["parameters"]["period"] = expected_value
    module_identity["configuration_sha256"] = hashlib.sha256(
        canonical_json_bytes(module_config["rendered"])
    ).hexdigest()
    actual_module = json.loads(prepared.config.modules.read_text(encoding="utf-8"))
    actual_module["modules"][0]["parameters"]["period"] = actual_value
    prepared.config.modules.write_bytes(canonical_json_bytes(actual_module))
    with pytest.raises(SpecificationError, match="actual module file differs"):
        verify_actual_backtest_configuration(replace(prepared, effective_configuration=effective))


def test_actual_config_projection_is_immutable_after_preparation(tmp_path) -> None:
    prepared = prepared_effective_config(tmp_path)
    prepared.effective_configuration["app"]["checkpoint"]["mode"] = "replay"
    document = json.loads(render_effective_config_document(prepared, engine_run_id="core-run"))
    assert document["effective_configuration"]["app"]["checkpoint"]["mode"] == "disabled"


def test_effective_config_runner_and_neutral_module_schema_agree(tmp_path: Path) -> None:
    """Both producer and independent consumer require the same Core identity fields."""
    from copy import deepcopy

    from effective_config_fixture import prepared_effective_config
    from modular_results_registry.core_adapter_import import (
        _validate_effective_configuration_schema,
    )
    from modular_results_registry.errors import IntegrityError

    from modular_research_runner.effective_config import validate_effective_configuration
    from modular_research_runner.models import SpecificationError

    prepared = prepared_effective_config(tmp_path)
    effective = json.loads(prepared._effective_configuration_bytes)
    validate_effective_configuration(effective)
    _validate_effective_configuration_schema(effective)
    identity = effective["module_identity"]["configuration"]["modules"][0]
    for key in identity:
        malformed = deepcopy(effective)
        malformed["module_identity"]["configuration"]["modules"][0].pop(key)
        with pytest.raises(SpecificationError):
            validate_effective_configuration(malformed)
        with pytest.raises(IntegrityError):
            _validate_effective_configuration_schema(malformed)
    malformed = deepcopy(effective)
    malformed["module_identity"]["configuration"]["modules"][0]["unknown_identity"] = True
    with pytest.raises(SpecificationError):
        validate_effective_configuration(malformed)
    with pytest.raises(IntegrityError):
        _validate_effective_configuration_schema(malformed)
