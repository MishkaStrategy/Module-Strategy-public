from __future__ import annotations

from datetime import datetime, timezone

import pytest

from modular_core.audit import Journal
from modular_core.serialization import redact_sensitive_text

NOW = datetime(2026, 7, 23, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("{'api_key': 'python-secret'}", "python-secret"),
        ('{"access_token": "json-secret"}', "json-secret"),
        ("ValueError({'password': 'hunter2'})", "hunter2"),
        (
            "{'auth': {'client_secret': 'nested-secret'}, 'safe': 1}",
            "nested-secret",
        ),
    ],
)
def test_redactor_removes_secrets_from_mapping_representations(text: str, secret: str) -> None:
    redacted = redact_sensitive_text(text)
    assert secret not in redacted
    assert "[REDACTED]" in redacted


def test_journal_redacts_mapping_payloads_from_exception_evidence() -> None:
    journal = Journal("run-mapping-secret")
    error = RuntimeError(
        {
            "api_key": "supersecret",
            "nested": {"access_token": "nested-token"},
        }
    )

    journal.log_error(NOW, "module:probe", error)

    evidence = repr(journal.errors) + repr(journal.events)
    assert "supersecret" not in evidence
    assert "nested-token" not in evidence
    assert evidence.count("[REDACTED]") >= 2


def test_redactor_does_not_change_non_sensitive_mapping_values() -> None:
    text = "{'period': 14, 'token_count': 3, 'safe': 'value'}"
    assert redact_sensitive_text(text) == text
