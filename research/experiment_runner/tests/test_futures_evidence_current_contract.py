from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from test_registry_package import _context, _publish, _sha256

from modular_research_runner.futures_evidence import _expected_application_run_id
from modular_research_runner.models import ResearchSpecification, SpecificationError

_FUNDING_MODEL = "registered-owner-funding-runtime-v2"
_EMPTY_KEYS_SHA256 = hashlib.sha256(b"").hexdigest()


def _canonical_sha256(value: dict[str, Any], *, trailing_newline: bool = False) -> str:
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


def _funding_source() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "dataset_id": "funding-btcusdt-current",
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
                "artifact_id": "funding-owner-artifact-current",
                "source_kind": "official_rest",
                "checksum_sha256": "b" * 64,
                "record_count": 1,
                "start": "2026-01-01T00:00:00+00:00",
                "end": "2026-01-02T00:00:00+00:00",
            }
        ],
    }


def _documents(
    core_run_id: str,
    *,
    event_stream_encoding: str,
    stale_newline_identity: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    source = _funding_source()
    source_identity = _canonical_sha256(source, trailing_newline=stale_newline_identity)
    application_run_id = _expected_application_run_id(
        core_run_id,
        funding_source_identity_sha256=source_identity,
        mark_source_identity_sha256=None,
    )
    runtime = {
        "schema_version": "1",
        "application_run_id": application_run_id,
        "model_version": _FUNDING_MODEL,
        "source_identity_sha256": source_identity,
        "source": source,
        "journaled_settlement_count": 0,
        "first_journaled_ordinal": None,
        "last_journaled_ordinal": None,
        "cumulative_journaled_cashflow": "0",
        "event_keys_sha256": _EMPTY_KEYS_SHA256,
        "event_stream_encoding": event_stream_encoding,
    }
    application = {
        "application_run_id": application_run_id,
        "core_run_id": core_run_id,
        "funding_model": _FUNDING_MODEL,
        "funding_source_identity_sha256": source_identity,
        "funding_source": source,
        "funding_runtime_evidence": runtime,
    }
    return application, runtime


def _write_current_funding_evidence(
    output: Path,
    *,
    core_run_id: str,
    event_stream_encoding: str = "identity",
    stale_newline_identity: bool = False,
) -> None:
    application, runtime = _documents(
        core_run_id,
        event_stream_encoding=event_stream_encoding,
        stale_newline_identity=stale_newline_identity,
    )
    (output / "application-run.json").write_text(
        json.dumps(application, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output / "funding-runtime.json").write_text(
        json.dumps(runtime, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    files = {
        path.relative_to(output).as_posix(): _sha256(path)
        for path in sorted(output.rglob("*"))
        if path.is_file() and path.name != "artifact-manifest.json"
    }
    (output / "artifact-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "run_id": core_run_id,
                "application_run_id": application["application_run_id"],
                "files": files,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def test_registry_preserves_current_funding_runtime_bytes(
    tmp_path: Path,
    specification: ResearchSpecification,
) -> None:
    _, manifest, job, envelope, output = _context(tmp_path, specification)
    _write_current_funding_evidence(output, core_run_id=envelope.engine.engine_run_id)
    expected_bytes = (output / "funding-runtime.json").read_bytes()

    reference = _publish(
        tmp_path,
        envelope=envelope,
        manifest=manifest,
        specification=specification,
        job=job,
    )

    copied = reference.package_directory / "funding-runtime.json"
    assert copied.read_bytes() == expected_bytes
    package = json.loads(
        (reference.package_directory / "artifact-package.json").read_text(encoding="utf-8")
    )
    inventory = {item["relative_path"]: item for item in package["artifact_inventory"]}
    assert inventory["funding-runtime.json"]["media_type"] == "application/json"
    assert inventory["funding-runtime.json"]["sha256"] == _sha256(output / "funding-runtime.json")


def test_registry_rejects_stale_newline_source_identity(
    tmp_path: Path,
    specification: ResearchSpecification,
) -> None:
    _, manifest, job, envelope, output = _context(tmp_path, specification)
    _write_current_funding_evidence(
        output,
        core_run_id=envelope.engine.engine_run_id,
        stale_newline_identity=True,
    )

    with pytest.raises(SpecificationError, match="identity hash does not match producer evidence"):
        _publish(
            tmp_path,
            envelope=envelope,
            manifest=manifest,
            specification=specification,
            job=job,
        )


def test_registry_rejects_funding_event_encoding_drift(
    tmp_path: Path,
    specification: ResearchSpecification,
) -> None:
    _, manifest, job, envelope, output = _context(tmp_path, specification)
    _write_current_funding_evidence(
        output,
        core_run_id=envelope.engine.engine_run_id,
        event_stream_encoding="gzip",
    )

    with pytest.raises(SpecificationError, match="does not match the Backtest event artifact"):
        _publish(
            tmp_path,
            envelope=envelope,
            manifest=manifest,
            specification=specification,
            job=job,
        )
