from datetime import datetime, timezone

import pytest

from modular_core.audit import Journal
from modular_core.events import InMemoryEventStore
from modular_core.models import EquityPoint

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FailingStore(InMemoryEventStore):
    def append_many(self, events):  # type: ignore[no-untyped-def]
        raise OSError("projection append failed")


@pytest.mark.parametrize("action", ["skip", "diagnostic", "equity", "error"])
def test_projection_lists_change_only_after_event_acceptance(action: str) -> None:
    journal = Journal(run_id="run-test", event_store=FailingStore())
    with pytest.raises(OSError, match="projection append"):
        if action == "skip":
            journal.log_skip(NOW, "module", "reason")
        elif action == "diagnostic":
            journal.log_diagnostics(NOW, "module", {"value": 1})
        elif action == "equity":
            journal.record_equity(EquityPoint(NOW, 100, 0, 100))
        else:
            journal.log_error(NOW, "stage", RuntimeError("boom"))
    assert journal.skips == []
    assert journal.diagnostics == []
    assert journal.equity == []
    assert journal.errors == []
    assert journal.events == ()
