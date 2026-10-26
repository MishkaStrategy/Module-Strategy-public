from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from modular_core.manifest import ModuleManifest
from modular_core.models import Candle
from modular_core.run_manifest import RunManifest, build_run_manifest


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bar(index: int = 0) -> Candle:
    return Candle(
        timestamp=NOW + timedelta(minutes=5 * index),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )


def _module(name: str = "probe") -> ModuleManifest:
    return ModuleManifest(name=name, version="1.0.0")


def _manifest(**overrides: object) -> RunManifest:
    values: dict[str, object] = {
        "kernel_version": "0.4.2",
        "created_at": NOW,
        "bars": (_bar(0), _bar(1)),
        "config": {"risk": 0.005},
        "manifests": (_module(),),
        "module_parameters": ({"window": 20},),
    }
    values.update(overrides)
    return build_run_manifest(**values)  # type: ignore[arg-type]


def test_run_manifest_detaches_and_freezes_parameter_evidence() -> None:
    parameters = {"nested": {"values": [1, 2]}}

    manifest = _manifest(module_parameters=(parameters,))
    parameters["nested"]["values"].append(3)  # type: ignore[index,union-attr]

    stored = manifest.module_parameters[0]
    assert isinstance(stored, MappingProxyType)
    assert stored["nested"]["values"] == (1, 2)  # type: ignore[index]


def test_run_manifest_normalizes_created_at_without_changing_identity() -> None:
    offset = timezone(timedelta(hours=3))

    utc_manifest = _manifest(created_at=NOW)
    offset_manifest = _manifest(created_at=NOW.astimezone(offset))

    assert offset_manifest.created_at == NOW
    assert offset_manifest.run_id == utc_manifest.run_id


def test_run_manifest_fills_immutable_empty_parameters_for_each_module() -> None:
    manifest = _manifest(
        manifests=(_module("first"), _module("second")),
        module_parameters=(),
    )

    assert len(manifest.module_parameters) == 2
    assert all(isinstance(item, MappingProxyType) for item in manifest.module_parameters)


@pytest.mark.parametrize("value", [True, 1.5, "2"])
def test_run_manifest_rejects_non_integer_bar_count(value: object) -> None:
    manifest = _manifest()

    with pytest.raises(TypeError, match="bars must be int"):
        replace(manifest, bars=value)  # type: ignore[arg-type]


def test_run_manifest_rejects_nonpositive_bar_count() -> None:
    with pytest.raises(ValueError, match="bars must be positive"):
        replace(_manifest(), bars=0)


def test_run_manifest_rejects_inconsistent_run_id() -> None:
    with pytest.raises(ValueError, match="run_id is inconsistent"):
        replace(_manifest(), run_id="run-forged")


def test_run_manifest_rejects_invalid_base_timeframe() -> None:
    with pytest.raises(ValueError, match="invalid base timeframe"):
        replace(_manifest(), base_timeframe="unknown")


def test_run_manifest_rejects_duplicate_module_names() -> None:
    manifest = _manifest()
    duplicate = _module()

    with pytest.raises(ValueError, match="unique names"):
        replace(
            manifest,
            module_manifests=(duplicate, duplicate),
            module_parameters=({}, {}),
        )


def test_run_manifest_rejects_parameter_alignment_mismatch() -> None:
    with pytest.raises(ValueError, match="must align with manifests"):
        replace(_manifest(), module_parameters=({}, {}))


@pytest.mark.parametrize("value", ["parameters", (1,), (["not-a-mapping"],)])
def test_run_manifest_rejects_invalid_parameter_collections(value: object) -> None:
    with pytest.raises(TypeError, match="module_parameters"):
        replace(_manifest(), module_parameters=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["bars", (object(),), (_bar(), object())])
def test_build_run_manifest_rejects_non_candle_elements(value: object) -> None:
    with pytest.raises(TypeError, match="bars must contain Candle"):
        _manifest(bars=value)


def test_build_run_manifest_rejects_non_manifest_elements() -> None:
    with pytest.raises(TypeError, match="must contain ModuleManifest"):
        _manifest(manifests=(object(),))


def test_build_run_manifest_rejects_mixed_symbols_or_timeframes() -> None:
    other_symbol = Candle(
        timestamp=NOW + timedelta(minutes=5),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        symbol="ETHUSDT",
        timeframe="5m",
    )

    with pytest.raises(ValueError, match="share symbol and timeframe"):
        _manifest(bars=(_bar(), other_symbol))
