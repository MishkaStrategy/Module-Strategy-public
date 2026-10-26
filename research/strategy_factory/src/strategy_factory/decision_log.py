"""Append-only hash-chained explanation journal."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .canonical import canonical_json_bytes, canonical_sha256, strict_json_loads
from .contracts import FactoryDecision
from .errors import CheckpointError, ContractError


@dataclass(frozen=True, slots=True)
class DecisionLogEntry:
    sequence: int
    previous_hash: str | None
    decision: FactoryDecision
    entry_hash: str


class DecisionLog:
    """Durable append-only decision log with complete-chain verification."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, decision: FactoryDecision) -> DecisionLogEntry:
        entries = self.read_all()
        if any(item.decision.decision_id == decision.decision_id for item in entries):
            existing = next(
                item for item in entries if item.decision.decision_id == decision.decision_id
            )
            if existing.decision != decision:
                raise ContractError("decision ID already exists with different content")
            return existing
        previous = entries[-1].entry_hash if entries else None
        sequence = len(entries) + 1
        digest = canonical_sha256(
            {"sequence": sequence, "previous_hash": previous, "decision": decision}
        )
        payload = {
            "sequence": sequence,
            "previous_hash": previous,
            "decision": json.loads(canonical_json_bytes(decision)),
            "entry_hash": digest,
        }
        with self._path.open("ab") as handle:
            handle.write(canonical_json_bytes(payload) + b"\n")
            handle.flush()
        return DecisionLogEntry(sequence, previous, decision, digest)

    def read_all(self) -> tuple[DecisionLogEntry, ...]:
        if not self._path.exists():
            return ()
        entries: list[DecisionLogEntry] = []
        previous: str | None = None
        try:
            lines = self._path.read_bytes().splitlines()
        except OSError as exc:
            raise CheckpointError("cannot read decision log") from exc
        for expected, raw in enumerate(lines, start=1):
            value = strict_json_loads(raw)
            if not isinstance(value, dict):
                raise CheckpointError("decision log entry must be an object")
            if value.get("sequence") != expected or value.get("previous_hash") != previous:
                raise CheckpointError("decision log chain ordering is invalid")
            decision = _decision_from_mapping(value.get("decision"))
            expected_hash = canonical_sha256(
                {"sequence": expected, "previous_hash": previous, "decision": decision}
            )
            if value.get("entry_hash") != expected_hash:
                raise CheckpointError("decision log hash mismatch")
            entries.append(DecisionLogEntry(expected, previous, decision, expected_hash))
            previous = expected_hash
        return tuple(entries)

    @property
    def chain_head(self) -> str | None:
        entries = self.read_all()
        return entries[-1].entry_hash if entries else None


def _decision_from_mapping(value: object) -> FactoryDecision:
    if not isinstance(value, dict):
        raise CheckpointError("invalid decision payload")
    from datetime import datetime

    from .contracts import DecisionAction, FactoryStage

    try:
        return FactoryDecision(
            decision_id=value["decision_id"],
            run_id=value["run_id"],
            stage=FactoryStage(value["stage"]),
            action=DecisionAction(value["action"]),
            subject_id=value["subject_id"],
            reason_codes=tuple(value["reason_codes"]),
            evidence=value["evidence"],
            alternatives=tuple(value["alternatives"]),
            budget_snapshot=value["budget_snapshot"],
            decided_at=datetime.fromisoformat(str(value["decided_at"]).replace("Z", "+00:00")),
            actor=value.get("actor", "strategy_factory"),
            schema_version=value.get("schema_version", 1),
        )
    except (KeyError, TypeError, ValueError, ContractError) as exc:
        raise CheckpointError("invalid decision payload") from exc
