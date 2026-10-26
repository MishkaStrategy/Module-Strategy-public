from __future__ import annotations

import json
from pathlib import Path

import pytest

from modular_research_runner.futures_evidence import build_futures_producer_assumptions
from modular_research_runner.models import SpecificationError


def _write_application(root: Path, value: dict[str, object]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "application-run.json").write_text(
        json.dumps(value, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _manifest(run_id: str = "legacy-core-run") -> dict[str, object]:
    return {"schema_version": "1", "run_id": run_id, "files": {}}


def test_explicit_pre_futures_baseline_remains_compatible(tmp_path: Path) -> None:
    _write_application(
        tmp_path,
        {
            "application_version": "0.1.0",
            "funding_model": "disabled",
            "market_type": "binance-usdt-m-perpetual",
        },
    )

    assumptions = build_futures_producer_assumptions(tmp_path, _manifest())

    assert assumptions["core_run_id"] == "legacy-core-run"
    assert assumptions["application_run_id"] is None
    assert assumptions["funding"]["model"] == "disabled"
    assert assumptions["valuation"]["policy"] == "last"
    assert assumptions["valuation"]["execution_price_source"] == "traded_ohlc"


def test_unversioned_historical_output_does_not_invent_economics(tmp_path: Path) -> None:
    _write_application(tmp_path, {"artifact": "application-run.json"})

    assumptions = build_futures_producer_assumptions(tmp_path, _manifest())

    assert assumptions["core_run_id"] == "legacy-core-run"
    assert assumptions["application_run_id"] is None
    assert assumptions["funding"]["model"] == "legacy-unspecified"
    assert assumptions["valuation"]["policy"] == "legacy-unspecified"
    assert assumptions["valuation"]["execution_price_source"] is None


def test_legacy_output_cannot_enable_funding_without_bound_identity(tmp_path: Path) -> None:
    _write_application(tmp_path, {"funding_model": "registered-owner-funding-runtime-v2"})

    with pytest.raises(SpecificationError, match="cannot enable funding"):
        build_futures_producer_assumptions(tmp_path, _manifest())


@pytest.mark.parametrize(
    ("application", "runtime_name"),
    [
        ({"funding_model": "disabled", "valuation_policy": "mark"}, None),
        ({"funding_model": "disabled"}, "funding-runtime.json"),
        ({"funding_model": "disabled"}, "valuation-runtime.json"),
    ],
)
def test_legacy_output_rejects_partial_futures_evidence(
    tmp_path: Path,
    application: dict[str, object],
    runtime_name: str | None,
) -> None:
    _write_application(tmp_path, application)
    if runtime_name is not None:
        (tmp_path / runtime_name).write_text("{}\n", encoding="utf-8")

    with pytest.raises(SpecificationError, match="futures"):
        build_futures_producer_assumptions(tmp_path, _manifest())
