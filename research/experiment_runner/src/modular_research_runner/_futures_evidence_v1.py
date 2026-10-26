from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NoReturn

from modular_core.data import timeframe_to_seconds

from .models import SpecificationError

_FUNDING_RUNTIME = "funding-runtime.json"
_VALUATION_RUNTIME = "valuation-runtime.json"
_APPLICATION_RUN = "application-run.json"
_MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_USDT_SYMBOL = re.compile(r"^[A-Z0-9]{2,30}USDT$")
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_FUNDING_MODEL = "registered-owner-funding-runtime-v2"
_MARK_MODEL = "registered-mark-reference-v1"
_APPLICATION_IDENTITY_VERSION = "1"

_FUNDING_SOURCE_FIELDS = {
    "schema_version",
    "dataset_id",
    "dataset_version",
    "dataset_hash",
    "symbol",
    "coverage_start_inclusive",
    "coverage_end_exclusive",
    "coverage_semantics",
    "event_count",
    "source_record_count",
    "lineage",
}
_MARK_SOURCE_FIELDS = {
    "schema_version",
    "model_version",
    "dataset_id",
    "dataset_version",
    "dataset_hash",
    "symbol",
    "exchange_id",
    "market_type",
    "data_type",
    "timeframe",
    "window_start_inclusive",
    "window_end_exclusive",
    "coverage_semantics",
    "row_count",
    "owner_row_count",
    "owner_source_record_count",
    "lineage",
}
_LINEAGE_FIELDS = {
    "artifact_id",
    "source_kind",
    "checksum_sha256",
    "record_count",
    "start",
    "end",
}
_FUNDING_RUNTIME_FIELDS = {
    "schema_version",
    "application_run_id",
    "model_version",
    "source_identity_sha256",
    "source",
    "journaled_settlement_count",
    "first_journaled_ordinal",
    "last_journaled_ordinal",
    "cumulative_journaled_cashflow",
    "event_keys_sha256",
}
_VALUATION_RUNTIME_FIELDS = {
    "schema_version",
    "application_run_id",
    "valuation_policy",
    "valuation_model",
    "mark_source_identity_sha256",
    "mark_source",
    "execution_price_source",
    "execution_risk_reference",
    "post_bar_reference",
}
_VALUATION_APPLICATION_FIELDS = {
    "valuation_policy",
    "valuation_model",
    "mark_source_identity_sha256",
    "mark_source",
    "execution_price_source",
    "execution_risk_reference",
    "post_bar_reference",
    "valuation_runtime_evidence",
}
_FUNDING_APPLICATION_FIELDS = {
    "funding_source_identity_sha256",
    "funding_source",
    "funding_runtime_evidence",
}

LineageRow = tuple[str, datetime, datetime, int]


def _reject_nonfinite_json_constant(value: str) -> NoReturn:
    raise SpecificationError(f"producer evidence contains non-finite JSON constant: {value}")


def _read_evidence_text(path: Path, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise SpecificationError(f"Backtest Engine output is missing {label}")
    data: bytes | None = None
    try:
        with path.open("rb") as handle:
            data = handle.read(_MAX_EVIDENCE_BYTES + 1)
    except OSError:
        pass
    if data is None:
        raise SpecificationError(f"Backtest Engine {label} cannot be read")
    if len(data) > _MAX_EVIDENCE_BYTES:
        raise SpecificationError(f"Backtest Engine {label} exceeds safe evidence size")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise SpecificationError(f"Backtest Engine {label} is not UTF-8") from None


def _json_mapping(path: Path, label: str) -> dict[str, Any]:
    text = _read_evidence_text(path, label)
    try:
        value = json.loads(text, parse_constant=_reject_nonfinite_json_constant)
    except SpecificationError:
        raise
    except json.JSONDecodeError:
        raise SpecificationError(f"Backtest Engine {label} is invalid") from None
    if not isinstance(value, dict):
        raise SpecificationError(f"Backtest Engine {label} must be an object")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpecificationError(f"{label} must be non-empty text")
    return value


def _sha256(value: object, label: str) -> str:
    text = _text(value, label)
    if _SHA256.fullmatch(text) is None:
        raise SpecificationError(f"{label} must be lowercase SHA-256")
    return text


def _nonnegative_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SpecificationError(f"{label} must be a non-negative integer")
    return value


def _positive_int(value: object, label: str) -> int:
    result = _nonnegative_int(value, label)
    if result == 0:
        raise SpecificationError(f"{label} must be positive")
    return result


def _optional_nonnegative_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value, label)


def _finite_decimal_text(value: object, label: str) -> Decimal:
    if not isinstance(value, str) or not value:
        raise SpecificationError(f"{label} must be exact decimal text")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise SpecificationError(f"{label} must be valid decimal text") from None
    if not result.is_finite():
        raise SpecificationError(f"{label} must be finite decimal text")
    return result


def _public_text(value: str, label: str) -> None:
    lowered = value.lower()
    if (
        value.startswith("/")
        or _WINDOWS_ABSOLUTE.match(value) is not None
        or "://" in lowered
        or "\\\\" in value
        or "\x00" in value
    ):
        raise SpecificationError(f"{label} contains private storage metadata")


def _utc_timestamp(value: object, label: str) -> datetime:
    text = _text(value, label)
    _public_text(text, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise SpecificationError(f"{label} must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SpecificationError(f"{label} must be timezone-aware UTC")
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise SpecificationError(f"{label} must use UTC offset")
    return parsed.astimezone(UTC)


def _validate_funding_runtime_values(
    runtime: Mapping[str, Any],
    source: Mapping[str, Any],
) -> None:
    count = _nonnegative_int(
        runtime.get("journaled_settlement_count"),
        "funding journaled settlement count",
    )
    first = _optional_nonnegative_int(
        runtime.get("first_journaled_ordinal"),
        "funding first journaled ordinal",
    )
    last = _optional_nonnegative_int(
        runtime.get("last_journaled_ordinal"),
        "funding last journaled ordinal",
    )
    event_keys_sha256 = _sha256(
        runtime.get("event_keys_sha256"),
        "funding event key digest",
    )
    cumulative_cashflow = _finite_decimal_text(
        runtime.get("cumulative_journaled_cashflow"),
        "funding cumulative journaled cashflow",
    )

    if count == 0:
        if first is not None or last is not None:
            raise SpecificationError(
                "zero funding settlement count requires null first/last ordinals"
            )
        if event_keys_sha256 != _EMPTY_SHA256:
            raise SpecificationError(
                "zero funding settlement count requires empty event-key digest"
            )
        if cumulative_cashflow != 0:
            raise SpecificationError(
                "zero funding settlement count requires zero cumulative cashflow"
            )
    else:
        if first is None or last is None:
            raise SpecificationError(
                "positive funding settlement count requires first/last ordinals"
            )
        if last < first or last - first + 1 != count:
            raise SpecificationError("funding settlement count and ordinal range are inconsistent")
        if event_keys_sha256 == _EMPTY_SHA256:
            raise SpecificationError(
                "positive funding settlement count requires non-empty event-key digest"
            )

    event_count = _positive_int(source.get("event_count"), "funding source event_count")
    source_record_count = _positive_int(
        source.get("source_record_count"),
        "funding source_record_count",
    )
    if source_record_count != event_count:
        raise SpecificationError("funding source record/event counts are inconsistent")
    if count > event_count:
        raise SpecificationError("funding settlement count exceeds source event_count")
    if count > 0 and (first is None or last is None or first >= event_count or last >= event_count):
        raise SpecificationError("funding settlement ordinal range exceeds source event_count")


def _canonical_json_sha256(value: Mapping[str, Any], *, trailing_newline: bool) -> str:
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


def _canonical_source_sha256(value: Mapping[str, Any]) -> str:
    return _canonical_json_sha256(value, trailing_newline=True)


def _expected_application_run_id(
    core_run_id: str,
    *,
    funding_source_identity_sha256: str | None,
    mark_source_identity_sha256: str | None,
) -> str:
    application_id = core_run_id
    if funding_source_identity_sha256 is not None:
        payload: dict[str, Any] = {
            "schema_version": _APPLICATION_IDENTITY_VERSION,
            "core_run_id": core_run_id,
            "funding_model": _FUNDING_MODEL,
            "funding_source_identity_sha256": funding_source_identity_sha256,
        }
        digest = _canonical_json_sha256(payload, trailing_newline=False)
        application_id = f"{core_run_id}-funding-{digest[:20]}"
    if mark_source_identity_sha256 is not None:
        payload = {
            "schema_version": _APPLICATION_IDENTITY_VERSION,
            "base_application_run_id": application_id,
            "valuation_policy": "mark",
            "valuation_model": _MARK_MODEL,
            "mark_source_identity_sha256": mark_source_identity_sha256,
        }
        digest = _canonical_json_sha256(payload, trailing_newline=True)
        application_id = f"{application_id}-mark-{digest[:20]}"
    return application_id


def _validate_lineage(value: object, label: str) -> tuple[LineageRow, ...]:
    if not isinstance(value, list) or not value:
        raise SpecificationError(f"{label} must be a non-empty list")
    rows: list[LineageRow] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != _LINEAGE_FIELDS:
            raise SpecificationError(f"{label} item schema is invalid")
        artifact_id = _text(item.get("artifact_id"), f"{label}.artifact_id")
        source_kind = _text(item.get("source_kind"), f"{label}.source_kind")
        _public_text(artifact_id, f"{label}.artifact_id")
        _public_text(source_kind, f"{label}.source_kind")
        _sha256(item.get("checksum_sha256"), f"{label}.checksum_sha256")
        record_count = _positive_int(item.get("record_count"), f"{label}.record_count")
        start = _utc_timestamp(item.get("start"), f"{label}.start")
        end = _utc_timestamp(item.get("end"), f"{label}.end")
        if end <= start:
            raise SpecificationError(f"{label} interval must be non-empty")
        rows.append((artifact_id, start, end, record_count))
    return tuple(rows)


def _validate_lineage_order(rows: tuple[LineageRow, ...], label: str) -> None:
    canonical = tuple(sorted(rows, key=lambda row: (row[1], row[2], row[0])))
    if rows != canonical:
        raise SpecificationError(f"{label} must be canonically ordered")
    covered_until = rows[0][2]
    for _, start, end, _ in rows[1:]:
        if start > covered_until:
            raise SpecificationError(f"{label} contains an uncovered interval")
        if end > covered_until:
            covered_until = end


def _validate_funding_source_values(
    source: Mapping[str, Any],
    lineage: tuple[LineageRow, ...],
) -> None:
    schema_version = source.get("schema_version")
    if schema_version != 1 or isinstance(schema_version, bool):
        raise SpecificationError("funding source schema_version must be 1")
    _text(source.get("dataset_id"), "funding source dataset_id")
    _positive_int(source.get("dataset_version"), "funding source dataset_version")
    symbol = _text(source.get("symbol"), "funding source symbol")
    if _USDT_SYMBOL.fullmatch(symbol) is None:
        raise SpecificationError("funding source symbol must be an uppercase USDT symbol")
    if source.get("coverage_semantics") != "[start,end)":
        raise SpecificationError("funding source coverage semantics changed")
    start = _utc_timestamp(
        source.get("coverage_start_inclusive"),
        "funding coverage start",
    )
    end = _utc_timestamp(
        source.get("coverage_end_exclusive"),
        "funding coverage end",
    )
    if end <= start:
        raise SpecificationError("funding coverage interval must be non-empty")
    event_count = _positive_int(source.get("event_count"), "funding source event_count")
    source_record_count = _positive_int(
        source.get("source_record_count"),
        "funding source_record_count",
    )
    if source_record_count != event_count:
        raise SpecificationError("funding source record/event counts are inconsistent")
    if sum(row[3] for row in lineage) != source_record_count:
        raise SpecificationError("funding lineage counts differ from source_record_count")
    _validate_lineage_order(lineage, "funding source lineage")
    if lineage[0][1] != start:
        raise SpecificationError("funding lineage start differs from source coverage")
    covered_until = max(row[2] for row in lineage)
    if covered_until != end:
        raise SpecificationError("funding lineage end differs from source coverage")


def _validate_mark_source_values(
    source: Mapping[str, Any],
    lineage: tuple[LineageRow, ...],
) -> None:
    schema_version = source.get("schema_version")
    if schema_version != 1 or isinstance(schema_version, bool):
        raise SpecificationError("mark source schema_version must be 1")
    if source.get("model_version") != _MARK_MODEL:
        raise SpecificationError("mark source model_version changed")
    _text(source.get("dataset_id"), "mark source dataset_id")
    _positive_int(source.get("dataset_version"), "mark source dataset_version")
    symbol = _text(source.get("symbol"), "mark source symbol")
    if _USDT_SYMBOL.fullmatch(symbol) is None:
        raise SpecificationError("mark source symbol must be an uppercase USDT symbol")
    if source.get("exchange_id") != "binance" or source.get("market_type") != "usdt_m_perpetual":
        raise SpecificationError("mark source must be Binance USDT-M perpetual")
    if source.get("data_type") != "mark_price":
        raise SpecificationError("mark source data_type must be mark_price")
    timeframe = _text(source.get("timeframe"), "mark source timeframe")
    seconds = timeframe_to_seconds(timeframe)
    if seconds is None or seconds <= 0:
        raise SpecificationError("mark source timeframe is unsupported")
    if source.get("coverage_semantics") != "[start,end)":
        raise SpecificationError("mark source coverage semantics changed")
    start = _utc_timestamp(
        source.get("window_start_inclusive"),
        "mark window start",
    )
    end = _utc_timestamp(
        source.get("window_end_exclusive"),
        "mark window end",
    )
    if end <= start:
        raise SpecificationError("mark window must be non-empty")
    row_count = _positive_int(source.get("row_count"), "mark source row_count")
    owner_row_count = _positive_int(
        source.get("owner_row_count"),
        "mark source owner_row_count",
    )
    owner_source_record_count = _positive_int(
        source.get("owner_source_record_count"),
        "mark source owner_source_record_count",
    )
    if owner_row_count != owner_source_record_count:
        raise SpecificationError("mark owner normalized/source counts differ")
    if row_count > owner_row_count:
        raise SpecificationError("mark selected row_count exceeds owner row evidence")
    if start + timedelta(seconds=seconds * row_count) != end:
        raise SpecificationError("mark row_count does not exactly cover selected window")
    if sum(row[3] for row in lineage) != owner_source_record_count:
        raise SpecificationError("mark lineage counts differ from owner source count")
    _validate_lineage_order(lineage, "mark source lineage")
    if lineage[0][1] > start or max(row[2] for row in lineage) < end:
        raise SpecificationError("mark lineage does not cover selected run window")


def _validate_source(
    value: object,
    *,
    expected_fields: set[str],
    expected_identity: str,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise SpecificationError(f"{label} schema is invalid")
    source = dict(value)
    _sha256(source.get("dataset_hash"), f"{label}.dataset_hash")
    lineage = _validate_lineage(source.get("lineage"), f"{label}.lineage")
    for field, item in source.items():
        if isinstance(item, str):
            _public_text(item, f"{label}.{field}")
    if expected_fields == _FUNDING_SOURCE_FIELDS:
        _validate_funding_source_values(source, lineage)
    elif expected_fields == _MARK_SOURCE_FIELDS:
        _validate_mark_source_values(source, lineage)
    else:
        raise SpecificationError(f"{label} uses an unsupported source contract")
    if _canonical_source_sha256(source) != expected_identity:
        raise SpecificationError(f"{label} identity hash does not match producer evidence")
    return source


def _legacy_baseline_assumptions(
    application: Mapping[str, Any],
    source_root: Path,
    engine_manifest: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Preserve pre-futures Backtest packages without inventing producer evidence."""

    if "core_run_id" in application or "application_run_id" in application:
        return None
    if engine_manifest.get("application_run_id") is not None:
        raise SpecificationError("legacy Backtest output cannot bind an application id")
    core_run_id = _text(engine_manifest.get("run_id"), "Backtest Engine manifest run_id")
    funding_present = (source_root / _FUNDING_RUNTIME).exists() or (
        source_root / _FUNDING_RUNTIME
    ).is_symlink()
    valuation_present = (source_root / _VALUATION_RUNTIME).exists() or (
        source_root / _VALUATION_RUNTIME
    ).is_symlink()
    if funding_present or valuation_present:
        raise SpecificationError("legacy Backtest output cannot carry futures runtime evidence")
    unexpected = sorted(
        (_FUNDING_APPLICATION_FIELDS | _VALUATION_APPLICATION_FIELDS).intersection(application)
    )
    if unexpected:
        raise SpecificationError(
            "legacy Backtest output contains partial futures evidence: " + ",".join(unexpected)
        )
    funding_model = application.get("funding_model")
    if funding_model not in (None, "disabled"):
        raise SpecificationError(
            "legacy Backtest output cannot enable funding without bound identity"
        )
    explicit_baseline = funding_model == "disabled"
    return {
        "schema_version": "1",
        "core_run_id": core_run_id,
        "application_run_id": None,
        "funding": {
            "model": "disabled" if explicit_baseline else "legacy-unspecified",
            "source_identity_sha256": None,
            "source": None,
            "runtime_artifact": None,
        },
        "valuation": {
            "policy": "last" if explicit_baseline else "legacy-unspecified",
            "model": "traded-last-baseline" if explicit_baseline else "legacy-unspecified",
            "mark_source_identity_sha256": None,
            "mark_source": None,
            "execution_price_source": "traded_ohlc" if explicit_baseline else None,
            "execution_risk_reference": "traded_open" if explicit_baseline else None,
            "post_bar_reference": "traded_close" if explicit_baseline else None,
            "runtime_artifact": None,
        },
    }


def build_futures_producer_assumptions(
    source_root: Path,
    engine_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate immutable Backtest futures evidence without recomputing economics."""

    application = _json_mapping(source_root / _APPLICATION_RUN, _APPLICATION_RUN)
    legacy = _legacy_baseline_assumptions(application, source_root, engine_manifest)
    if legacy is not None:
        return legacy

    core_run_id = _text(application.get("core_run_id"), "application-run core_run_id")
    application_run_id = _text(
        application.get("application_run_id"),
        "application-run application_run_id",
    )
    if engine_manifest.get("run_id") != core_run_id:
        raise SpecificationError("Backtest application/core run identity has changed")
    manifest_application_id = engine_manifest.get("application_run_id")
    if application_run_id == core_run_id:
        if manifest_application_id is not None:
            raise SpecificationError(
                "baseline Backtest manifest unexpectedly binds an application id"
            )
    elif manifest_application_id != application_run_id:
        raise SpecificationError(
            "Backtest application run identity does not match artifact manifest"
        )

    funding_identity: str | None = None
    funding_model = _text(application.get("funding_model"), "funding model")
    funding_source_id = application.get("funding_source_identity_sha256")
    funding_source_raw = application.get("funding_source")
    funding_embedded = application.get("funding_runtime_evidence")
    funding_path = source_root / _FUNDING_RUNTIME
    funding_present = funding_path.exists() or funding_path.is_symlink()
    if funding_model == "disabled":
        if any(
            value is not None for value in (funding_source_id, funding_source_raw, funding_embedded)
        ):
            raise SpecificationError("disabled funding evidence contains an active source")
        if funding_present:
            raise SpecificationError(
                "disabled funding run unexpectedly contains funding-runtime.json"
            )
        funding_summary: dict[str, Any] = {
            "model": "disabled",
            "source_identity_sha256": None,
            "source": None,
            "runtime_artifact": None,
        }
    else:
        if funding_model != _FUNDING_MODEL:
            raise SpecificationError("funding model_version changed")
        source_identity = _sha256(funding_source_id, "funding source identity")
        funding_identity = source_identity
        funding_source = _validate_source(
            funding_source_raw,
            expected_fields=_FUNDING_SOURCE_FIELDS,
            expected_identity=source_identity,
            label="funding source",
        )
        if not funding_present:
            raise SpecificationError(
                "funding-enabled Backtest output is missing funding-runtime.json"
            )
        funding_runtime = _json_mapping(funding_path, _FUNDING_RUNTIME)
        if set(funding_runtime) != _FUNDING_RUNTIME_FIELDS:
            raise SpecificationError("funding runtime evidence schema is invalid")
        if not isinstance(funding_embedded, Mapping) or dict(funding_embedded) != funding_runtime:
            raise SpecificationError("funding runtime evidence differs from application-run.json")
        if (
            funding_runtime.get("schema_version") != "1"
            or funding_runtime.get("application_run_id") != application_run_id
            or funding_runtime.get("model_version") != funding_model
            or funding_runtime.get("source_identity_sha256") != source_identity
            or funding_runtime.get("source") != funding_source
        ):
            raise SpecificationError("funding runtime identity/provenance is inconsistent")
        _validate_funding_runtime_values(funding_runtime, funding_source)
        funding_summary = {
            "model": funding_model,
            "source_identity_sha256": source_identity,
            "source": funding_source,
            "runtime_artifact": _FUNDING_RUNTIME,
        }

    mark_identity: str | None = None
    valuation_path = source_root / _VALUATION_RUNTIME
    valuation_present = valuation_path.exists() or valuation_path.is_symlink()
    if not valuation_present:
        unexpected = sorted(_VALUATION_APPLICATION_FIELDS.intersection(application))
        if unexpected:
            raise SpecificationError(
                "last-valued Backtest output contains partial mark evidence: "
                + ",".join(unexpected)
            )
        valuation_summary: dict[str, Any] = {
            "policy": "last",
            "model": "traded-last-baseline",
            "mark_source_identity_sha256": None,
            "mark_source": None,
            "execution_price_source": "traded_ohlc",
            "execution_risk_reference": "traded_open",
            "post_bar_reference": "traded_close",
            "runtime_artifact": None,
        }
    else:
        valuation_runtime = _json_mapping(valuation_path, _VALUATION_RUNTIME)
        if set(valuation_runtime) != _VALUATION_RUNTIME_FIELDS:
            raise SpecificationError("valuation runtime evidence schema is invalid")
        valuation_embedded = application.get("valuation_runtime_evidence")
        if (
            not isinstance(valuation_embedded, Mapping)
            or dict(valuation_embedded) != valuation_runtime
        ):
            raise SpecificationError("valuation runtime evidence differs from application-run.json")
        if application.get("valuation_policy") != "mark":
            raise SpecificationError("valuation runtime requires explicit mark policy")
        mark_source_id = _sha256(
            application.get("mark_source_identity_sha256"),
            "mark source identity",
        )
        mark_identity = mark_source_id
        mark_source = _validate_source(
            application.get("mark_source"),
            expected_fields=_MARK_SOURCE_FIELDS,
            expected_identity=mark_source_id,
            label="mark source",
        )
        valuation_model = _text(application.get("valuation_model"), "valuation model")
        if valuation_model != _MARK_MODEL:
            raise SpecificationError("valuation model_version changed")
        expected_semantics = {
            "execution_price_source": "traded_ohlc",
            "execution_risk_reference": "mark_open",
            "post_bar_reference": "mark_close",
        }
        if any(application.get(key) != value for key, value in expected_semantics.items()):
            raise SpecificationError("mark valuation execution/reference semantics changed")
        if (
            valuation_runtime.get("schema_version") != "1"
            or valuation_runtime.get("application_run_id") != application_run_id
            or valuation_runtime.get("valuation_policy") != "mark"
            or valuation_runtime.get("valuation_model") != valuation_model
            or valuation_runtime.get("mark_source_identity_sha256") != mark_source_id
            or valuation_runtime.get("mark_source") != mark_source
            or any(valuation_runtime.get(key) != value for key, value in expected_semantics.items())
        ):
            raise SpecificationError("valuation runtime identity/provenance is inconsistent")
        valuation_summary = {
            "policy": "mark",
            "model": valuation_model,
            "mark_source_identity_sha256": mark_source_id,
            "mark_source": mark_source,
            **expected_semantics,
            "runtime_artifact": _VALUATION_RUNTIME,
        }

    expected_application_id = _expected_application_run_id(
        core_run_id,
        funding_source_identity_sha256=funding_identity,
        mark_source_identity_sha256=mark_identity,
    )
    if application_run_id != expected_application_id:
        raise SpecificationError("Backtest application run identity derivation has changed")

    return {
        "schema_version": "1",
        "core_run_id": core_run_id,
        "application_run_id": application_run_id,
        "funding": funding_summary,
        "valuation": valuation_summary,
    }
