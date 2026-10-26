"""Crash-safe Strategy Factory checkpoint persistence."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .canonical import canonical_json_bytes, canonical_sha256, strict_json_loads
from .contracts import FactoryStage, FactoryStatus, StrategyFactoryCheckpoint
from .errors import CheckpointError, ContractError


class AtomicCheckpointStore:
    """Write checksummed checkpoints atomically with no permissive recovery."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def save(self, checkpoint: StrategyFactoryCheckpoint) -> Path:
        target = self._path(checkpoint.run_id)
        payload = _checkpoint_payload(checkpoint)
        envelope = {
            "format": "strategy-factory-checkpoint-v1",
            "payload": payload,
            "payload_hash": canonical_sha256(payload),
        }
        raw = canonical_json_bytes(envelope)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            _fsync_directory(target.parent)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise CheckpointError(f"cannot persist checkpoint for {checkpoint.run_id}") from exc
        return target

    def load(self, run_id: str) -> StrategyFactoryCheckpoint:
        target = self._path(run_id)
        try:
            raw = target.read_bytes()
        except OSError as exc:
            raise CheckpointError(f"cannot read checkpoint for {run_id}") from exc
        value = strict_json_loads(raw)
        if not isinstance(value, dict) or value.get("format") != "strategy-factory-checkpoint-v1":
            raise CheckpointError("unsupported checkpoint envelope")
        payload = value.get("payload")
        if not isinstance(payload, dict):
            raise CheckpointError("checkpoint payload is missing")
        if value.get("payload_hash") != canonical_sha256(payload):
            raise CheckpointError("checkpoint payload hash mismatch")
        checkpoint = _checkpoint_from_payload(payload)
        if checkpoint.run_id != run_id:
            raise CheckpointError("checkpoint run identity mismatch")
        return checkpoint

    def exists(self, run_id: str) -> bool:
        return self._path(run_id).is_file()

    def _path(self, run_id: str) -> Path:
        if not run_id or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
            for character in run_id
        ):
            raise CheckpointError("run_id is not safe for a checkpoint path")
        return self._root / f"{run_id}.json"


def _checkpoint_payload(checkpoint: StrategyFactoryCheckpoint) -> dict[str, Any]:
    return {
        "run_id": checkpoint.run_id,
        "request_fingerprint": checkpoint.request_fingerprint,
        "status": checkpoint.status.value,
        "stage": checkpoint.stage.value,
        "stage_sequence": checkpoint.stage_sequence,
        "dataset_fingerprint": checkpoint.dataset_fingerprint,
        "commit_sha": checkpoint.commit_sha,
        "manifest_id": checkpoint.manifest_id,
        "completed_artifacts": dict(checkpoint.completed_artifacts),
        "submitted_batches": dict(checkpoint.submitted_batches),
        "processed_event_ids": sorted(checkpoint.processed_event_ids),
        "budget_state": dict(checkpoint.budget_state),
        "decision_chain_head": checkpoint.decision_chain_head,
        "updated_at": checkpoint.updated_at,
        "schema_version": checkpoint.schema_version,
    }


def _checkpoint_from_payload(value: dict[str, Any]) -> StrategyFactoryCheckpoint:
    try:
        updated_at = datetime.fromisoformat(str(value["updated_at"]).replace("Z", "+00:00"))
        completed = _string_mapping(value["completed_artifacts"])
        batches = _string_mapping(value["submitted_batches"])
        budget = value["budget_state"]
        if not isinstance(budget, dict):
            raise CheckpointError("budget_state must be an object")
        event_ids = value["processed_event_ids"]
        if not isinstance(event_ids, list) or not all(isinstance(item, str) for item in event_ids):
            raise CheckpointError("processed_event_ids must be a string list")
        return StrategyFactoryCheckpoint(
            run_id=value["run_id"],
            request_fingerprint=value["request_fingerprint"],
            status=FactoryStatus(value["status"]),
            stage=FactoryStage(value["stage"]),
            stage_sequence=value["stage_sequence"],
            dataset_fingerprint=value["dataset_fingerprint"],
            commit_sha=value["commit_sha"],
            manifest_id=value["manifest_id"],
            completed_artifacts=completed,
            submitted_batches=batches,
            processed_event_ids=frozenset(event_ids),
            budget_state=budget,
            decision_chain_head=value["decision_chain_head"],
            updated_at=updated_at,
            schema_version=value.get("schema_version", 1),
        )
    except (KeyError, TypeError, ValueError, ContractError) as exc:
        raise CheckpointError("invalid checkpoint contract") from exc


def _string_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise CheckpointError("checkpoint mapping must be an object")
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise CheckpointError("checkpoint mapping must contain strings")
    return dict(value)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
