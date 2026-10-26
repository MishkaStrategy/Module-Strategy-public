from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from test_registry_package import _context, _publish, _sha256

from modular_research_runner.futures_evidence import _expected_application_run_id
from modular_research_runner.models import ResearchSpecification, SpecificationError

_FUNDING_MODEL = "registered-owner-funding-runtime-v2"
_MARK_MODEL = "registered-mark-reference-v1"
_EMPTY_KEYS_SHA = hashlib.sha256(b"").hexdigest()


def _canonical_sha256(value: dict[str, Any], *, trailing_newline: bool) -> str:
    text = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if trailing_newline:
        text += "\n"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _funding_source() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "dataset_id": "funding-btcusdt-registry-current",
        "dataset_version": 7,
        "dataset_hash": "a" * 64,
        "symbol": "BTCUSDT",
        "coverage_start_inclusive": "2026-01-01T00:00:00+00:00",
        "coverage_end_exclusive": "2026-01-02T00:00:00+00:00",
        "coverage_semantics": "[start,end)",
        "event_count": 1,
        "source_record_count": 1,
        "lineage": [
            {
                "artifact_id": "funding-owner-artifact-registry-current",
                "source_kind": "official_rest",
                "checksum_sha256": "b" * 64,
                "record_count": 1,
                "start": "2026-01-01T00:00:00+00:00",
                "end": "2026-01-02T00:00:00+00:00",
            }
        ],
    }


def _mark_source() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "model_version": _MARK_MODEL,
        "dataset_id": "mark-btcusdt-registry-current",
        "dataset_version": 4,
        "dataset_hash": "c" * 64,
        "symbol": "BTCUSDT",
        "exchange_id": "binance",
        "market_type": "usdt_m_perpetual",
        "data_type": "mark_price",
        "timeframe": "5m",
        "window_start_inclusive": "2026-01-01T00:00:00+00:00",
        "window_end_exclusive": "2026-01-01T00:05:00+00:00",
        "coverage_semantics": "[start,end)",
        "row_count": 1,
        "owner_row_count": 1,
        "owner_source_record_count": 1,
        "lineage": [
            {
                "artifact_id": "mark-owner-artifact-registry-current",
                "source_kind": "official_rest",
                "checksum_sha256": "d" * 64,
                "record_count": 1,
                "start": "2026-01-01T00:00:00+00:00",
                "end": "2026-01-01T00:05:00+00:00",
            }
        ],
    }


def _documents(
    core_run_id: str,
    *,
    funding: bool,
    mark: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None, str]:
    funding_source = _funding_source() if funding else None
    funding_identity = (
        _canonical_sha256(funding_source, trailing_newline=False)
        if funding_source is not None
        else None
    )
    mark_source = _mark_source() if mark else None
    mark_identity = (
        _canonical_sha256(mark_source, trailing_newline=True) if mark_source is not None else None
    )
    application_run_id = _expected_application_run_id(
        core_run_id,
        funding_source_identity_sha256=funding_identity,
        mark_source_identity_sha256=mark_identity,
    )
    application: dict[str, Any] = {
        "application_run_id": application_run_id,
        "core_run_id": core_run_id,
    }

    funding_runtime: dict[str, Any] | None = None
    if funding_source is not None and funding_identity is not None:
        funding_runtime = {
            "schema_version": "1",
            "application_run_id": application_run_id,
            "model_version": _FUNDING_MODEL,
            "source_identity_sha256": funding_identity,
            "source": funding_source,
            "journaled_settlement_count": 0,
            "first_journaled_ordinal": None,
            "last_journaled_ordinal": None,
            "cumulative_journaled_cashflow": "0",
            "event_keys_sha256": _EMPTY_KEYS_SHA,
            "event_stream_encoding": "identity",
        }
        application.update(
            {
                "funding_model": _FUNDING_MODEL,
                "funding_source_identity_sha256": funding_identity,
                "funding_source": funding_source,
                "funding_runtime_evidence": funding_runtime,
            }
        )
    else:
        application.update(
            {
                "funding_model": "disabled",
                "funding_source_identity_sha256": None,
                "funding_source": None,
                "funding_runtime_evidence": None,
            }
        )

    valuation_runtime: dict[str, Any] | None = None
    if mark_source is not None and mark_identity is not None:
        semantics = {
            "execution_price_source": "traded_ohlc",
            "execution_risk_reference": "mark_open",
            "post_bar_reference": "mark_close",
        }
        valuation_runtime = {
            "schema_version": "1",
            "application_run_id": application_run_id,
            "valuation_policy": "mark",
            "valuation_model": _MARK_MODEL,
            "mark_source_identity_sha256": mark_identity,
            "mark_source": mark_source,
            **semantics,
        }
        application.update(
            {
                "valuation_policy": "mark",
                "valuation_model": _MARK_MODEL,
                "mark_source_identity_sha256": mark_identity,
                "mark_source": mark_source,
                "valuation_runtime_evidence": valuation_runtime,
                **semantics,
            }
        )
    return application, funding_runtime, valuation_runtime, application_run_id


def _refresh_manifest(output: Path, *, core_run_id: str, application_run_id: str) -> None:
    files = {
        path.relative_to(output).as_posix(): _sha256(path)
        for path in sorted(output.rglob("*"))
        if path.is_file() and path.name != "artifact-manifest.json"
    }
    manifest: dict[str, Any] = {
        "schema_version": "1",
        "run_id": core_run_id,
        "files": files,
    }
    if application_run_id != core_run_id:
        manifest["application_run_id"] = application_run_id
    _write_json(output / "artifact-manifest.json", manifest)


def _install_documents(
    output: Path,
    *,
    core_run_id: str,
    funding: bool,
    mark: bool,
) -> str:
    application, funding_runtime, valuation_runtime, application_run_id = _documents(
        core_run_id,
        funding=funding,
        mark=mark,
    )
    _write_json(output / "application-run.json", application)
    if funding_runtime is not None:
        _write_json(output / "funding-runtime.json", funding_runtime)
    else:
        (output / "funding-runtime.json").unlink(missing_ok=True)
    if valuation_runtime is not None:
        _write_json(output / "valuation-runtime.json", valuation_runtime)
    else:
        (output / "valuation-runtime.json").unlink(missing_ok=True)
    _refresh_manifest(
        output,
        core_run_id=core_run_id,
        application_run_id=application_run_id,
    )
    return application_run_id


@pytest.mark.parametrize(
    ("funding", "mark", "expected_funding", "expected_policy"),
    [
        (False, False, "disabled", "last"),
        (True, False, _FUNDING_MODEL, "last"),
        (False, True, "disabled", "mark"),
        (True, True, _FUNDING_MODEL, "mark"),
    ],
)
def test_registry_preserves_current_futures_compositions(
    tmp_path: Path,
    specification: ResearchSpecification,
    funding: bool,
    mark: bool,
    expected_funding: str,
    expected_policy: str,
) -> None:
    _, manifest, job, envelope, output = _context(tmp_path, specification)
    core_run_id = envelope.engine.engine_run_id
    application_run_id = _install_documents(
        output,
        core_run_id=core_run_id,
        funding=funding,
        mark=mark,
    )
    source_application = (output / "application-run.json").read_bytes()
    source_funding = (output / "funding-runtime.json").read_bytes() if funding else None
    source_valuation = (output / "valuation-runtime.json").read_bytes() if mark else None

    reference = _publish(
        tmp_path,
        envelope=envelope,
        manifest=manifest,
        specification=specification,
        job=job,
    )
    packaged_application = reference.package_directory / "application-run.json"
    assert packaged_application.read_bytes() == source_application
    packaged = json.loads(packaged_application.read_text(encoding="utf-8"))
    assert packaged["application_run_id"] == application_run_id
    assert packaged["funding_model"] == expected_funding
    assert ("valuation_policy" in packaged) is mark
    if mark:
        assert packaged["valuation_policy"] == expected_policy

    package = json.loads(
        (reference.package_directory / "artifact-package.json").read_text(encoding="utf-8")
    )
    assert "producer_assumptions" not in package
    inventory = {item["relative_path"]: item for item in package["artifact_inventory"]}
    if funding:
        assert (reference.package_directory / "funding-runtime.json").read_bytes() == source_funding
        assert inventory["funding-runtime.json"]["schema_id"] == "modular-backtest.funding-runtime"
    else:
        assert "funding-runtime.json" not in inventory
    if mark:
        assert (
            reference.package_directory / "valuation-runtime.json"
        ).read_bytes() == source_valuation
        assert (
            inventory["valuation-runtime.json"]["schema_id"] == "modular-backtest.valuation-runtime"
        )
    else:
        assert "valuation-runtime.json" not in inventory


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("application-id", "identity derivation has changed"),
        ("funding-runtime", "funding runtime evidence differs"),
        ("mark-runtime", "valuation runtime evidence differs"),
        ("mark-semantics", "execution/reference semantics"),
        ("partial-mark", "partial mark evidence"),
    ],
)
def test_registry_rejects_current_identity_runtime_and_semantic_tamper(
    tmp_path: Path,
    specification: ResearchSpecification,
    mutation: str,
    message: str,
) -> None:
    _, manifest, job, envelope, output = _context(tmp_path, specification)
    core_run_id = envelope.engine.engine_run_id
    application_run_id = _install_documents(
        output,
        core_run_id=core_run_id,
        funding=True,
        mark=True,
    )
    application_path = output / "application-run.json"
    application = json.loads(application_path.read_text(encoding="utf-8"))

    if mutation == "application-id":
        tampered_id = "consistent-but-derived-wrong-application-id"
        application["application_run_id"] = tampered_id
        funding_runtime = deepcopy(application["funding_runtime_evidence"])
        funding_runtime["application_run_id"] = tampered_id
        valuation_runtime = deepcopy(application["valuation_runtime_evidence"])
        valuation_runtime["application_run_id"] = tampered_id
        application["funding_runtime_evidence"] = funding_runtime
        application["valuation_runtime_evidence"] = valuation_runtime
        _write_json(output / "funding-runtime.json", funding_runtime)
        _write_json(output / "valuation-runtime.json", valuation_runtime)
        application_run_id = tampered_id
    elif mutation == "funding-runtime":
        runtime = json.loads((output / "funding-runtime.json").read_text(encoding="utf-8"))
        runtime["cumulative_journaled_cashflow"] = "1"
        _write_json(output / "funding-runtime.json", runtime)
    elif mutation == "mark-runtime":
        runtime = json.loads((output / "valuation-runtime.json").read_text(encoding="utf-8"))
        runtime["valuation_model"] = "changed"
        _write_json(output / "valuation-runtime.json", runtime)
    elif mutation == "mark-semantics":
        application["execution_price_source"] = "mark"
    else:
        (output / "valuation-runtime.json").unlink()

    _write_json(application_path, application)
    _refresh_manifest(
        output,
        core_run_id=core_run_id,
        application_run_id=application_run_id,
    )
    with pytest.raises(SpecificationError, match=message):
        _publish(
            tmp_path,
            envelope=envelope,
            manifest=manifest,
            specification=specification,
            job=job,
        )


def test_registry_rejects_current_private_storage_metadata(
    tmp_path: Path,
    specification: ResearchSpecification,
) -> None:
    _, manifest, job, envelope, output = _context(tmp_path, specification)
    core_run_id = envelope.engine.engine_run_id
    application_run_id = _install_documents(
        output,
        core_run_id=core_run_id,
        funding=True,
        mark=False,
    )
    application_path = output / "application-run.json"
    application = json.loads(application_path.read_text(encoding="utf-8"))
    source = deepcopy(application["funding_source"])
    source["lineage"][0]["artifact_id"] = "/private/funding.json"
    source_identity = _canonical_sha256(source, trailing_newline=False)
    application["funding_source"] = source
    application["funding_source_identity_sha256"] = source_identity
    runtime = deepcopy(application["funding_runtime_evidence"])
    runtime["source"] = source
    runtime["source_identity_sha256"] = source_identity
    application["funding_runtime_evidence"] = runtime
    application_run_id = _expected_application_run_id(
        core_run_id,
        funding_source_identity_sha256=source_identity,
        mark_source_identity_sha256=None,
    )
    application["application_run_id"] = application_run_id
    runtime["application_run_id"] = application_run_id
    application["funding_runtime_evidence"] = runtime
    _write_json(application_path, application)
    _write_json(output / "funding-runtime.json", runtime)
    _refresh_manifest(
        output,
        core_run_id=core_run_id,
        application_run_id=application_run_id,
    )
    with pytest.raises(SpecificationError, match="private storage metadata"):
        _publish(
            tmp_path,
            envelope=envelope,
            manifest=manifest,
            specification=specification,
            job=job,
        )
