from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from .artifact_store import FileLimits, read_verified_bytes
from .canonical import canonical_json_bytes
from .errors import ValidationError
from .observability import JsonLineLogger, build_log_event
from .safe_io import atomic_write_bytes

_JSON_INPUT_LIMITS = FileLimits(max_file_bytes=256 * 1024 * 1024)


def _write_json(value: Any, output: str | None) -> None:
    data = canonical_json_bytes(value) + b"\n"
    if output is None or output == "-":
        sys.stdout.buffer.write(data)
    else:
        atomic_write_bytes(output, data)


def _load_json_object(path: str | Path, *, max_bytes: int | None = None) -> dict[str, Any]:
    limits = _JSON_INPUT_LIMITS if max_bytes is None else FileLimits(max_file_bytes=max_bytes)
    _, data = read_verified_bytes(path, limits=limits)

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant is forbidden: {value}")

    try:
        value = json.loads(
            data.decode("utf-8"),
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationError("JSON input is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValidationError("JSON input must be an object")
    return value


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{name} must be an object")
    return value


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationError(f"{name} must be a list")
    return value


def _string(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        qualifier = "a string" if allow_empty else "a non-empty string"
        raise ValidationError(f"{name} must be {qualifier}")
    return value


def _optional_string(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _string(value, name)


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{name} must be a boolean")
    return value


def _nonnegative_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{name} must be a non-negative integer")
    return value


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValidationError(f"{name} must be a finite decimal-compatible value")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValidationError(f"{name} must be decimal-compatible") from exc
    if not result.is_finite():
        raise ValidationError(f"{name} must be finite")
    return result


def _datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be an ISO timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{name} is invalid") from exc


def _filters(values: Sequence[str]) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for value in values:
        if "=" not in value:
            raise ValidationError("filters must use key=value")
        key, item = value.split("=", 1)
        if not key or key in result:
            raise ValidationError("filter keys must be unique and non-empty")
        result[key] = None if item == "null" else item
    return result


def build_parser() -> argparse.ArgumentParser:
    from .cli_parser import build_parser as implementation

    return implementation()


def _profile(document: Mapping[str, Any]) -> Any:
    from .cli_analytics import profile

    return profile(document)


def _comparison_context(value: Any, name: str) -> Any:
    from .cli_analytics import comparison_context

    return comparison_context(value, name)


def _run_compare(document: Mapping[str, Any]) -> Any:
    from .cli_analytics import run_compare

    return run_compare(document)


def _run_slices(document: Mapping[str, Any]) -> Any:
    from .cli_analytics import run_slices

    return run_slices(document)


def _run_pareto(document: Mapping[str, Any]) -> Any:
    from .cli_analytics import run_pareto

    return run_pareto(document)


def _run_findings(document: Mapping[str, Any]) -> Mapping[str, Any]:
    from .cli_analytics import run_findings

    return run_findings(document)


def _finding_from_mapping(raw: Mapping[str, Any]) -> Any:
    from .cli_analytics import finding_from_mapping

    return finding_from_mapping(raw)


def _build_audit_package(document: Mapping[str, Any]) -> Any:
    from .cli_analytics import build_audit_package

    return build_audit_package(document)


_INTEGRITY_BOOLEAN_FIELDS = (
    "audit_sequence_contiguous",
    "audit_revision_monotonic",
    "audit_revision_not_ahead",
    "audit_revision_coverage",
    "audit_event_ids_valid",
    "audit_bindings_valid",
    "audit_timestamps_valid",
    "audit_timestamp_monotonic",
    "operation_revision_coverage",
    "quarantine_records_valid",
    "entity_records_valid",
    "relation_records_valid",
    "annotation_records_valid",
    "annotation_actor_scope_constraint",
    "migration_records_valid",
    "append_only_triggers_present",
    "registered_revisions_valid",
)


def _integrity_pass(report: Mapping[str, Any]) -> bool:
    return (
        report.get("sqlite_integrity") == "ok"
        and report.get("foreign_key_violations") == 0
        and all(report.get(field) is True for field in _INTEGRITY_BOOLEAN_FIELDS)
    )


def _execute(args: argparse.Namespace) -> int:
    from .cli_execute import execute

    return execute(args)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started = time.perf_counter()
    code = _execute(args)
    if args.log_jsonl:
        with JsonLineLogger.open(args.log_jsonl, fsync=True) as logger:
            logger.emit(
                build_log_event(
                    level="INFO" if code == 0 else "ERROR",
                    operation=f"cli.{args.command}",
                    status="PASS" if code == 0 else "FAIL",
                    reason_code="CLI_COMPLETED" if code == 0 else f"CLI_EXIT_{code}",
                    correlation_id=f"cli-{args.command}",
                    duration_seconds=time.perf_counter() - started,
                    details={"exit_code": code},
                )
            )
    return code
