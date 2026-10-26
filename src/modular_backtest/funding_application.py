from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import IO

from modular_core.audit import Journal
from modular_core.config import EngineConfig
from modular_core.execution import BrokerSimulator
from modular_core.instruments import InstrumentSpec

from .event_artifacts import (
    EventArtifactError,
    SHARDED_EVENT_ENCODING,
    SHARDED_EVENT_MANIFEST_PATH,
    open_event_stream_text,
)
from .funding import RegisteredFundingSource
from .futures_execution import FUNDING_RUNTIME_MODEL_VERSION, FundingAwareBrokerSimulator


FUNDING_APPLICATION_IDENTITY_VERSION = "1"


class FundingApplicationError(ValueError):
    """Raised when a funding-enabled application run cannot be proven safely."""


ExecutionFactory = Callable[[EngineConfig, Journal, InstrumentSpec], BrokerSimulator]


def funding_execution_factory(source: RegisteredFundingSource) -> ExecutionFactory:
    """Return the one Core execution factory for an owner-backed funding run."""

    if not isinstance(source, RegisteredFundingSource):
        raise TypeError("funding source must be RegisteredFundingSource")

    def create(
        config: EngineConfig,
        journal: Journal,
        instrument: InstrumentSpec,
    ) -> BrokerSimulator:
        return FundingAwareBrokerSimulator(
            config,
            journal,
            instrument,
            funding_source=source,
        )

    return create


def application_run_id(
    core_run_id: str,
    funding_source: RegisteredFundingSource | None,
) -> str:
    """Build a filesystem-safe application identity without changing Core identity."""

    if not isinstance(core_run_id, str) or not core_run_id.strip():
        raise FundingApplicationError("core run_id must be non-empty")
    if funding_source is None:
        return core_run_id
    payload = {
        "schema_version": FUNDING_APPLICATION_IDENTITY_VERSION,
        "core_run_id": core_run_id,
        "funding_model": FUNDING_RUNTIME_MODEL_VERSION,
        "funding_source_identity_sha256": funding_source.identity_sha256(),
    }
    digest = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    return f"{core_run_id}-funding-{digest[:20]}"


def funding_application_metadata(
    funding_source: RegisteredFundingSource | None,
) -> dict[str, object]:
    if funding_source is None:
        return {
            "funding_model": "disabled",
            "funding_source_identity_sha256": None,
            "funding_source": None,
        }
    return {
        "funding_model": FUNDING_RUNTIME_MODEL_VERSION,
        "funding_source_identity_sha256": funding_source.identity_sha256(),
        "funding_source": dict(funding_source.identity()),
    }


def _open_event_stream(output: Path) -> tuple[str, IO[str]]:
    plain = output / "events.jsonl"
    compressed = output / "events.jsonl.gz"
    sharded = output / SHARDED_EVENT_MANIFEST_PATH
    present = [path for path in (plain, compressed, sharded) if path.exists() or path.is_symlink()]
    if not present:
        raise FundingApplicationError(
            "funding evidence requires a regular events.jsonl/events.jsonl.gz stream or shard manifest"
        )
    if len(present) != 1:
        raise FundingApplicationError(
            "funding evidence requires exactly one legacy event stream or shard manifest"
        )
    selected = present[0]
    try:
        handle = open_event_stream_text(output)
    except EventArtifactError as exc:
        raise FundingApplicationError("funding event evidence failed artifact validation") from exc
    if selected == compressed:
        return "gzip", handle
    if selected == sharded:
        return SHARDED_EVENT_ENCODING, handle
    return "identity", handle


def collect_funding_runtime_evidence(
    output: Path,
    funding_source: RegisteredFundingSource,
    *,
    application_id: str,
) -> Mapping[str, object]:
    """Verify exported funding events and return sanitized deterministic evidence."""

    if not isinstance(funding_source, RegisteredFundingSource):
        raise TypeError("funding source must be RegisteredFundingSource")
    if not isinstance(application_id, str) or not application_id:
        raise FundingApplicationError("application_id must be non-empty")

    expected_source = funding_source.identity_sha256()
    ordinals: list[int] = []
    event_keys: list[str] = []
    cumulative = Decimal("0")
    stream_encoding = "unknown"
    try:
        stream_encoding, handle = _open_event_stream(output)
        with handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    row = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise FundingApplicationError(
                        f"event stream contains invalid JSON at line {line_number}"
                    ) from exc
                if not isinstance(row, dict) or row.get("event_type") != "portfolio.funding":
                    continue
                if row.get("source") != "backtest_funding":
                    raise FundingApplicationError("funding event has an unexpected source")
                payload = row.get("payload")
                if not isinstance(payload, dict):
                    raise FundingApplicationError("funding event payload is invalid")
                funding = payload.get("funding")
                if not isinstance(funding, dict):
                    raise FundingApplicationError("funding event has no funding payload")
                if funding.get("model_version") != FUNDING_RUNTIME_MODEL_VERSION:
                    raise FundingApplicationError("funding event model version changed")
                if funding.get("source_identity_sha256") != expected_source:
                    raise FundingApplicationError("funding event source identity changed")
                ordinal = funding.get("ordinal")
                if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
                    raise FundingApplicationError("funding event ordinal is invalid")
                event_key = funding.get("event_key")
                if not isinstance(event_key, str) or len(event_key) != 64:
                    raise FundingApplicationError("funding event key is invalid")
                cashflow = funding.get("cashflow")
                if not isinstance(cashflow, str):
                    raise FundingApplicationError("funding event cashflow must be exact text")
                try:
                    cashflow_value = Decimal(cashflow)
                except InvalidOperation as exc:
                    raise FundingApplicationError("funding event cashflow is invalid") from exc
                if not cashflow_value.is_finite():
                    raise FundingApplicationError("funding event cashflow must be finite")
                ordinals.append(ordinal)
                event_keys.append(event_key)
                cumulative += cashflow_value
    except FundingApplicationError:
        raise
    except (OSError, EOFError) as exc:
        raise FundingApplicationError("cannot read funding event evidence") from exc

    if len(ordinals) != len(set(ordinals)):
        raise FundingApplicationError("funding event ordinals are duplicated")
    if ordinals != sorted(ordinals):
        raise FundingApplicationError("funding event ordinals are not chronological")
    if ordinals and ordinals != list(range(ordinals[0], ordinals[-1] + 1)):
        raise FundingApplicationError("active-runtime funding event ordinals contain a gap")

    keys_sha256 = hashlib.sha256("\n".join(event_keys).encode("utf-8")).hexdigest()
    return {
        "schema_version": "1",
        "application_run_id": application_id,
        "model_version": FUNDING_RUNTIME_MODEL_VERSION,
        "source_identity_sha256": expected_source,
        "source": dict(funding_source.identity()),
        "journaled_settlement_count": len(ordinals),
        "first_journaled_ordinal": ordinals[0] if ordinals else None,
        "last_journaled_ordinal": ordinals[-1] if ordinals else None,
        "cumulative_journaled_cashflow": str(cumulative),
        "event_keys_sha256": keys_sha256,
        "event_stream_encoding": stream_encoding,
    }


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
