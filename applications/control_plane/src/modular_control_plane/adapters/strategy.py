from __future__ import annotations

import os
import re
from pathlib import Path

from ..strategy import (
    StrategyFactoryScan,
    StrategyFactorySnapshot,
    StrategyRunCorrupted,
    StrategyRunNotFound,
)

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class StrategyFactoryStateReader:
    """Read existing Strategy Factory checkpoints without creating or mutating run state."""

    def __init__(self, state_root: str | Path) -> None:
        raw = Path(state_root).expanduser()
        if raw.is_symlink():
            raise ValueError("strategy factory state root must not be a symlink")
        resolved = raw.resolve()
        if not resolved.is_dir():
            raise ValueError("strategy factory state root must be an existing directory")
        self._state_root = resolved

    def scan(self, *, max_runs: int) -> StrategyFactoryScan:
        if not 1 <= max_runs <= 2_000:
            raise ValueError("max_runs must be between 1 and 2000")
        self._ensure_dependency()
        snapshots: list[StrategyFactorySnapshot] = []
        invalid_count = 0
        ignored_count = 0
        truncated = False
        candidates = 0
        try:
            entries = os.scandir(self._state_root)
        except OSError as exc:
            raise RuntimeError("strategy factory state root cannot be scanned") from exc
        with entries:
            for entry in entries:
                name = entry.name
                if not _RUN_ID.fullmatch(name):
                    ignored_count += 1
                    continue
                try:
                    if entry.is_symlink():
                        invalid_count += 1
                        continue
                    if not entry.is_dir(follow_symlinks=False):
                        ignored_count += 1
                        continue
                except OSError:
                    invalid_count += 1
                    continue
                candidates += 1
                if candidates > max_runs:
                    truncated = True
                    break
                try:
                    snapshots.append(self._read_run(name))
                except (StrategyRunCorrupted, StrategyRunNotFound):
                    invalid_count += 1
        return StrategyFactoryScan(
            runs=tuple(snapshots),
            invalid_count=invalid_count,
            ignored_count=ignored_count,
            truncated=truncated,
        )

    def get(self, run_id: str) -> StrategyFactorySnapshot:
        self._ensure_dependency()
        safe_run_id = self._safe_run_id(run_id)
        run_root = self._state_root / safe_run_id
        if run_root.is_symlink() or not run_root.is_dir():
            raise StrategyRunNotFound("strategy run was not found")
        return self._read_run(safe_run_id)

    def _read_run(self, run_id: str) -> StrategyFactorySnapshot:
        from strategy_factory.checkpoint import AtomicCheckpointStore
        from strategy_factory.decision_log import DecisionLog

        safe_run_id = self._safe_run_id(run_id)
        run_root = self._state_root / safe_run_id
        checkpoint_root = run_root / "checkpoints"
        decisions_path = run_root / "decisions.jsonl"
        if run_root.is_symlink() or not run_root.is_dir():
            raise StrategyRunNotFound("strategy run was not found")
        if checkpoint_root.is_symlink() or not checkpoint_root.is_dir():
            raise StrategyRunCorrupted("strategy checkpoint directory is unavailable")
        if decisions_path.is_symlink():
            raise StrategyRunCorrupted("strategy decision log must not be a symlink")
        if decisions_path.exists() and not decisions_path.is_file():
            raise StrategyRunCorrupted("strategy decision log is not a regular file")
        try:
            checkpoint = AtomicCheckpointStore(checkpoint_root).load(safe_run_id)
            decisions = DecisionLog(decisions_path).read_all()
        except Exception as exc:
            raise StrategyRunCorrupted(
                "strategy governance state failed integrity validation"
            ) from exc
        if any(entry.decision.run_id != safe_run_id for entry in decisions):
            raise StrategyRunCorrupted("strategy decision log run identity mismatch")
        chain_head = decisions[-1].entry_hash if decisions else None
        if checkpoint.decision_chain_head != chain_head:
            raise StrategyRunCorrupted("strategy checkpoint and decision chain are inconsistent")
        return StrategyFactorySnapshot(
            run_id=checkpoint.run_id,
            factory_status=checkpoint.status.value,
            stage=checkpoint.stage.value,
            stage_sequence=checkpoint.stage_sequence,
            updated_at=checkpoint.updated_at,
            request_fingerprint=checkpoint.request_fingerprint,
            dataset_fingerprint=checkpoint.dataset_fingerprint,
            commit_sha=checkpoint.commit_sha,
            manifest_id=checkpoint.manifest_id,
            completed_artifact_count=len(checkpoint.completed_artifacts),
            submitted_batch_count=len(checkpoint.submitted_batches),
            processed_event_count=len(checkpoint.processed_event_ids),
            decision_count=len(decisions),
            decision_chain_head=chain_head,
            decision_chain_verified=True,
        )

    @staticmethod
    def _safe_run_id(run_id: str) -> str:
        normalized = run_id.strip()
        if _RUN_ID.fullmatch(normalized) is None:
            raise StrategyRunNotFound("strategy run was not found")
        return normalized

    @staticmethod
    def _ensure_dependency() -> None:
        try:
            import strategy_factory.checkpoint  # noqa: F401
            import strategy_factory.decision_log  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("Strategy Factory reader dependency is unavailable") from exc
