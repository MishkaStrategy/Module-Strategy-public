from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import NOW, build_auditor

from strategy_audit_validation import AuditOutcome

DECISION_COUNT = 64


def test_long_history_survives_restart_and_full_signature_sweep(
    tmp_path: Path,
    valid_evidence: dict[str, Any],
) -> None:
    path = tmp_path / "audit-long-history.sqlite3"
    auditor, store = build_auditor(path, now=NOW)
    decisions = []
    for index in range(DECISION_COUNT):
        decisions.append(
            auditor.audit(
                evidence_package_reference=f"artifact:long-history:{index:04d}",
                evidence=valid_evidence,
            )
        )
    store.verify_integrity()
    assert all(decision.outcome is AuditOutcome.APPROVED_FOR_PAPER for decision in decisions)
    assert len({decision.audit_id for decision in decisions}) == DECISION_COUNT
    store.close()

    restarted, restarted_store = build_auditor(path, now=NOW)
    restarted_store.verify_integrity()
    first = restarted.audit(
        evidence_package_reference="artifact:long-history:0000",
        evidence=valid_evidence,
    )
    last = restarted.audit(
        evidence_package_reference=f"artifact:long-history:{DECISION_COUNT - 1:04d}",
        evidence=valid_evidence,
    )

    assert first == decisions[0]
    assert last == decisions[-1]
    restarted_store.verify_integrity()
    rows = restarted_store._connection.execute(
        "SELECT sequence, previous_record_hash, record_hash FROM audit_decisions ORDER BY sequence"
    ).fetchall()
    assert len(rows) == DECISION_COUNT
    assert [int(row["sequence"]) for row in rows] == list(range(1, DECISION_COUNT + 1))
    assert rows[0]["previous_record_hash"] is None
    assert all(
        rows[index]["previous_record_hash"] == rows[index - 1]["record_hash"]
        for index in range(1, DECISION_COUNT)
    )
    restarted_store.close()
