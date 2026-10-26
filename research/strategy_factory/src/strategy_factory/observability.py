"""Structured logging and dependency-free Prometheus-compatible metrics."""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .errors import ContractError

_SECRET_PATTERN = re.compile(r"(secret|token|api[_-]?key|private[_-]?key|password)", re.IGNORECASE)
_METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")


class JsonLogFormatter(logging.Formatter):
    """Produce bounded JSON log events with recursive secret redaction."""

    def __init__(self, *, maximum_field_length: int = 2_000) -> None:
        super().__init__()
        if maximum_field_length < 64:
            raise ContractError("maximum_field_length must be at least 64")
        self._maximum = maximum_field_length

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": self._bound(record.getMessage()),
        }
        for name in (
            "run_id",
            "correlation_id",
            "experiment_id",
            "stage",
            "candidate_id",
            "event_id",
        ):
            if hasattr(record, name):
                payload[name] = self._bound(getattr(record, name))
        if hasattr(record, "event"):
            payload["event"] = _redact(record.__dict__["event"], self._maximum)
        if record.exc_info:
            exception_type, exception_value, _ = record.exc_info
            if exception_type is not None:
                payload["exception_type"] = exception_type.__name__
            if exception_value is not None:
                payload["exception_message"] = self._bound(str(exception_value))
        return json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True)

    def _bound(self, value: object) -> str:
        text = str(value)
        if len(text) <= self._maximum:
            return text
        return text[: self._maximum] + "…[truncated]"


@dataclass(slots=True)
class FactoryMetrics:
    """Thread-safe bounded metric registry for orchestrator health."""

    _values: dict[tuple[str, tuple[tuple[str, str], ...]], Decimal] = field(default_factory=dict)
    _types: dict[str, str] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def increment(
        self,
        name: str,
        amount: Decimal = Decimal("1"),
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self._validate(name, amount, labels)
        key = (name, _labels(labels))
        with self._lock:
            self._types.setdefault(name, "counter")
            if self._types[name] != "counter":
                raise ContractError("metric type conflict")
            self._values[key] = self._values.get(key, Decimal("0")) + amount

    def set_gauge(
        self,
        name: str,
        value: Decimal,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self._validate(name, value, labels)
        key = (name, _labels(labels))
        with self._lock:
            self._types.setdefault(name, "gauge")
            if self._types[name] != "gauge":
                raise ContractError("metric type conflict")
            self._values[key] = value

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return {
                _render_key(name, labels): format(value, "f")
                for (name, labels), value in sorted(self._values.items())
            }

    def render_prometheus(self) -> str:
        with self._lock:
            lines: list[str] = []
            for name in sorted(self._types):
                lines.append(f"# TYPE {name} {self._types[name]}")
                for (metric_name, labels), value in sorted(self._values.items()):
                    if metric_name == name:
                        lines.append(f"{_render_key(metric_name, labels)} {format(value, 'f')}")
            return "\n".join(lines) + ("\n" if lines else "")

    @staticmethod
    def _validate(
        name: str,
        value: Decimal,
        labels: Mapping[str, str] | None,
    ) -> None:
        if _METRIC_NAME.fullmatch(name) is None:
            raise ContractError("invalid metric name")
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ContractError("metric value must be a finite Decimal")
        if labels is not None:
            if len(labels) > 8:
                raise ContractError("too many metric labels")
            for key, item in labels.items():
                if _METRIC_NAME.fullmatch(key) is None or len(item) > 128:
                    raise ContractError("invalid metric label")


def _labels(value: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((value or {}).items()))


def _render_key(name: str, labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return name
    encoded = ",".join(
        f'{key}="{item.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
        for key, item in labels
    )
    return f"{name}{{{encoded}}}"


def _redact(value: object, maximum: int, *, depth: int = 0) -> object:
    if depth > 12:
        return "[maximum-depth]"
    if isinstance(value, Mapping):
        output: dict[str, object] = {}
        for key, item in value.items():
            text_key = str(key)
            output[text_key] = (
                "[REDACTED]"
                if _SECRET_PATTERN.search(text_key)
                else _redact(item, maximum, depth=depth + 1)
            )
        return output
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        return [_redact(item, maximum, depth=depth + 1) for item in items[:100]]
    text = str(value)
    return text if len(text) <= maximum else text[:maximum] + "…[truncated]"
