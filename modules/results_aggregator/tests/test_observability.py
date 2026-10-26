from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from modular_results_registry.errors import ValidationError
from modular_results_registry.observability import (
    JsonLineLogger,
    StructuredLogEvent,
    build_log_event,
)


def test_structured_logger_redacts_sensitive_fields_and_hashes_event() -> None:
    stream = io.StringIO()
    logger = JsonLineLogger(stream)
    event = build_log_event(
        level="INFO",
        operation="artifact.validate",
        status="PASS",
        reason_code="HASH_MATCH",
        correlation_id="pkg-123",
        row_count=2,
        byte_count=10,
        details={"token": "must-not-leak", "nested": {"password": "hidden", "safe": 1}},
        timestamp=datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    logger.emit(event)
    payload = json.loads(stream.getvalue())
    assert payload["details"]["token"] == "[REDACTED]"
    assert payload["details"]["nested"]["password"] == "[REDACTED]"
    assert payload["details"]["nested"]["safe"] == 1
    assert len(payload["event_hash"]) == 64


def test_structured_event_rejects_hash_tampering() -> None:
    with pytest.raises(ValidationError, match="hash mismatch"):
        StructuredLogEvent(
            timestamp_utc="2026-07-23T00:00:00Z",
            level="INFO",
            operation="verify",
            status="PASS",
            reason_code="OK",
            correlation_id="corr-1",
            event_hash="0" * 64,
        )


def test_cli_appends_structured_log(tmp_path: Path) -> None:
    from modular_results_registry.cli import main

    database = tmp_path / "registry.db"
    log = tmp_path / "operations.jsonl"
    assert main(["--log-jsonl", str(log), "init", str(database)]) == 0
    payload = json.loads(log.read_text(encoding="utf-8"))
    assert payload["operation"] == "cli.init"
    assert payload["status"] == "PASS"
    assert payload["details"] == {"exit_code": 0}


def test_structured_event_validation_and_entity_normalization() -> None:
    import math

    base = dict(
        timestamp_utc="2026-07-23T00:00:00Z",
        level="INFO",
        operation="verify",
        status="PASS",
        reason_code="OK",
        correlation_id="corr-1",
    )
    with pytest.raises(ValidationError, match="ISO-8601"):
        StructuredLogEvent(**{**base, "timestamp_utc": "bad"})
    with pytest.raises(ValidationError, match="timezone-aware"):
        StructuredLogEvent(**{**base, "timestamp_utc": "2026-07-23T00:00:00"})
    with pytest.raises(ValidationError, match="identity fields"):
        StructuredLogEvent(**{**base, "operation": ""})
    with pytest.raises(ValidationError, match="finite"):
        StructuredLogEvent(**base, duration_seconds=math.nan)
    with pytest.raises(ValidationError, match="finite"):
        StructuredLogEvent(**base, duration_seconds=-1)
    with pytest.raises(ValidationError, match="negative"):
        StructuredLogEvent(**base, row_count=-1)
    with pytest.raises(ValidationError, match="negative"):
        StructuredLogEvent(**base, byte_count=-1)
    event = StructuredLogEvent(
        **base,
        entity_ids={"run_id": "run-1", "empty": ""},
        details={"custom": object()},
    )
    assert event.entity_ids == {"run_id": "run-1"}
    assert isinstance(event.details["custom"], str)


def test_build_log_event_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        build_log_event(
            level="INFO",
            operation="verify",
            status="PASS",
            reason_code="OK",
            correlation_id="corr-1",
            timestamp=datetime(2026, 7, 23),
        )


def test_json_logger_fsync_context_and_log_injection_guard(tmp_path: Path, monkeypatch) -> None:
    fsync_calls: list[int] = []
    monkeypatch.setattr("modular_results_registry.observability.os.fsync", fsync_calls.append)
    path = tmp_path / "events.jsonl"
    with JsonLineLogger.open(path, fsync=True) as logger:
        logger.emit(
            build_log_event(
                level="INFO",
                operation="verify",
                status="PASS",
                reason_code="OK",
                correlation_id="corr-1",
                details={"message": "line1\nline2"},
                timestamp=datetime(2026, 7, 23, tzinfo=timezone.utc),
            )
        )
    assert fsync_calls
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1

    stream = io.StringIO()
    logger = JsonLineLogger(stream)
    monkeypatch.setattr(
        "modular_results_registry.observability.json.dumps", lambda *a, **k: "bad\nline"
    )
    with pytest.raises(ValidationError, match="newline"):
        logger.emit(
            build_log_event(
                level="INFO",
                operation="verify",
                status="PASS",
                reason_code="OK",
                correlation_id="corr-1",
                timestamp=datetime(2026, 7, 23, tzinfo=timezone.utc),
            )
        )
