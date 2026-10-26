from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import _futures_evidence_v1 as _impl
from .models import SpecificationError

_FUNDING_RUNTIME = _impl._FUNDING_RUNTIME
_VALUATION_RUNTIME = _impl._VALUATION_RUNTIME
_APPLICATION_RUN = _impl._APPLICATION_RUN
_MAX_EVIDENCE_BYTES = _impl._MAX_EVIDENCE_BYTES
_FUNDING_RUNTIME_FIELDS = set(_impl._FUNDING_RUNTIME_FIELDS) | {"event_stream_encoding"}
_impl._FUNDING_RUNTIME_FIELDS = _FUNDING_RUNTIME_FIELDS


def _canonical_source_sha256(value: Mapping[str, Any]) -> str:
    """Current funding-source identity uses canonical JSON without a final LF."""

    return _impl._canonical_json_sha256(value, trailing_newline=False)


def _validate_source(
    value: object,
    *,
    expected_fields: set[str],
    expected_identity: str,
    label: str,
) -> dict[str, Any]:
    """Validate current funding/mark sources with their distinct byte identities."""

    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise SpecificationError(f"{label} schema is invalid")
    source = dict(value)
    _impl._sha256(source.get("dataset_hash"), f"{label}.dataset_hash")
    lineage = _impl._validate_lineage(source.get("lineage"), f"{label}.lineage")
    for field, item in source.items():
        if isinstance(item, str):
            _impl._public_text(item, f"{label}.{field}")
    if expected_fields == _impl._FUNDING_SOURCE_FIELDS:
        _impl._validate_funding_source_values(source, lineage)
        actual_identity = _impl._canonical_json_sha256(source, trailing_newline=False)
    elif expected_fields == _impl._MARK_SOURCE_FIELDS:
        _impl._validate_mark_source_values(source, lineage)
        actual_identity = _impl._canonical_json_sha256(source, trailing_newline=True)
    else:
        raise SpecificationError(f"{label} uses an unsupported source contract")
    if actual_identity != expected_identity:
        raise SpecificationError(f"{label} identity hash does not match producer evidence")
    return source


def _expected_application_run_id(
    core_run_id: str,
    *,
    funding_source_identity_sha256: str | None,
    mark_source_identity_sha256: str | None,
) -> str:
    """Mirror current Backtest funding + mark application identity derivation."""

    application_id = core_run_id
    if funding_source_identity_sha256 is not None:
        payload: dict[str, Any] = {
            "schema_version": _impl._APPLICATION_IDENTITY_VERSION,
            "core_run_id": core_run_id,
            "funding_model": _impl._FUNDING_MODEL,
            "funding_source_identity_sha256": funding_source_identity_sha256,
        }
        digest = _impl._canonical_json_sha256(payload, trailing_newline=False)
        application_id = f"{core_run_id}-funding-{digest[:20]}"
    if mark_source_identity_sha256 is not None:
        payload = {
            "schema_version": _impl._APPLICATION_IDENTITY_VERSION,
            "base_application_run_id": application_id,
            "valuation_policy": "mark",
            "valuation_model": _impl._MARK_MODEL,
            "mark_source_identity_sha256": mark_source_identity_sha256,
        }
        digest = _impl._canonical_json_sha256(payload, trailing_newline=True)
        application_id = f"{application_id}-mark-{digest[:20]}"
    return application_id


_impl._canonical_source_sha256 = _canonical_source_sha256
_impl._validate_source = _validate_source
_impl._expected_application_run_id = _expected_application_run_id


def _event_stream_encoding(source_root: Path) -> str:
    candidates = (
        ("events.jsonl", "identity"),
        ("events.jsonl.gz", "gzip"),
        ("events.manifest.json", "gzip-byte-shards-v1"),
    )
    present = [encoding for name, encoding in candidates if (source_root / name).exists()]
    if len(present) != 1:
        raise SpecificationError(
            "funding evidence requires exactly one Backtest event-stream representation"
        )
    return present[0]


def _validate_current_funding_encoding(source_root: Path) -> None:
    runtime_path = source_root / _FUNDING_RUNTIME
    if not runtime_path.exists() and not runtime_path.is_symlink():
        return
    runtime = _json_mapping(runtime_path, _FUNDING_RUNTIME)
    encoding = runtime.get("event_stream_encoding")
    if encoding not in {"identity", "gzip", "gzip-byte-shards-v1"}:
        raise SpecificationError("funding event_stream_encoding is unsupported")
    if encoding != _event_stream_encoding(source_root):
        raise SpecificationError(
            "funding event_stream_encoding does not match the Backtest event artifact"
        )


def build_futures_producer_assumptions(
    source_root: Path,
    engine_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate current producer evidence without recomputing funding or valuation economics."""

    assumptions = _impl.build_futures_producer_assumptions(source_root, engine_manifest)
    _validate_current_funding_encoding(source_root)
    return assumptions


def _json_mapping(path: Path, label: str) -> dict[str, Any]:
    # Keep the public bounded-read test hook while delegating parsing to the audited validator.
    _impl._MAX_EVIDENCE_BYTES = _MAX_EVIDENCE_BYTES
    return _impl._json_mapping(path, label)


def _public_text(value: str, label: str) -> None:
    _impl._public_text(value, label)


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


__all__ = ["build_futures_producer_assumptions"]
