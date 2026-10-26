from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from modular_results_registry.core_adapter import adapt_core_v04_export
from modular_results_registry.errors import UnsupportedSchemaError

from test_core_adapter_extended import _context, _write_core_export


def _context_for_core_version(kernel_version: str):
    base = _context()
    experiment = replace(
        base.experiment,
        module_versions={
            **dict(base.experiment.module_versions),
            "core": kernel_version,
        },
    )
    return _context(experiment=experiment)


def test_core_adapter_accepts_explicit_integrated_0_4_2_patch(
    tmp_path: Path,
) -> None:
    export = tmp_path / "export"
    _write_core_export(export, kernel_version="0.4.2")
    context = _context_for_core_version("0.4.2")

    first = adapt_core_v04_export(export, context)
    second = adapt_core_v04_export(export, context)

    assert first.package == second.package
    run = first.package.runs[0]
    assert run.backtest_engine_version == "0.4.2"
    assert run.metric_schema_version == "core.metrics.v0.4.0"
    assert first.package.artifacts[0].producer_module_version == "0.4.2"
    assert {definition.definition_version for definition in first.package.metric_definitions} == {
        "core.metrics.v0.4.0"
    }
    assert first.package.package_id.startswith("package-")


@pytest.mark.parametrize(
    "kernel_version",
    ["0.3.0", "0.4.1", "0.4.3", "0.5.0"],
)
def test_core_adapter_rejects_unlisted_patch_versions(
    tmp_path: Path,
    kernel_version: str,
) -> None:
    export = tmp_path / kernel_version.replace(".", "-")
    _write_core_export(export, kernel_version=kernel_version)

    with pytest.raises(UnsupportedSchemaError, match="unsupported Core version"):
        adapt_core_v04_export(
            export,
            _context_for_core_version(kernel_version),
        )
