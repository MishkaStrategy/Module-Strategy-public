from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from modular_core import AtomicJsonStateRepository, JsonLinesEventStore

_STATE_DIRECTORY = "state"
_JOURNAL_DIRECTORY = "journal"
_JOURNAL_FILENAME = "events.jsonl"


@dataclass(frozen=True, slots=True)
class TradingRunnerDurableStores:
    """Canonical file-backed Trading Runner stores rooted in one trusted owner directory.

    The existing Core persistence formats remain authoritative. Runtime state,
    operator-lifecycle idempotency records, and orchestration checkpoints share one
    ``AtomicJsonStateRepository`` namespace. Runner events use one fsync-backed
    ``JsonLinesEventStore``. The directory layout and journal filename are fixed by
    this owner composition. State filenames remain Core's validated state-key mapping;
    state keys cannot select child directories or contain path separators.
    """

    root: Path
    state_repository: AtomicJsonStateRepository
    event_store: JsonLinesEventStore

    @classmethod
    def open(
        cls,
        root: str | Path,
        *,
        max_state_bytes: int | None = 16 * 1024 * 1024,
        max_event_line_bytes: int | None = 4 * 1024 * 1024,
        max_event_log_bytes: int | None = 256 * 1024 * 1024,
    ) -> TradingRunnerDurableStores:
        owner_root = _prepare_real_directory(Path(root), "Trading Runner owner root")
        state_directory = _prepare_real_directory(
            owner_root / _STATE_DIRECTORY,
            "Trading Runner state directory",
            expected_parent=owner_root,
        )
        journal_directory = _prepare_real_directory(
            owner_root / _JOURNAL_DIRECTORY,
            "Trading Runner journal directory",
            expected_parent=owner_root,
        )
        journal_path = journal_directory / _JOURNAL_FILENAME
        if journal_path.is_symlink():
            raise ValueError("Trading Runner journal file must not be a symbolic link")
        if journal_path.exists() and not journal_path.is_file():
            raise ValueError("Trading Runner journal path must be a regular file")

        state_repository = AtomicJsonStateRepository(
            state_directory,
            max_state_bytes=max_state_bytes,
        )
        event_store = JsonLinesEventStore(
            journal_path,
            fsync=True,
            max_line_bytes=max_event_line_bytes,
            max_log_bytes=max_event_log_bytes,
            recover_truncated_tail=False,
        )
        return cls(
            root=owner_root,
            state_repository=state_repository,
            event_store=event_store,
        )

    @property
    def runtime_state_store(self) -> AtomicJsonStateRepository:
        """Store consumed by ``TradingRunner`` and ``TradingRunnerOperatorLifecycle``."""

        return self.state_repository

    @property
    def checkpoint_repository(self) -> AtomicJsonStateRepository:
        """Store consumed by ``RuntimeCheckpointCoordinator``."""

        return self.state_repository

    @property
    def state_directory(self) -> Path:
        return self.root / _STATE_DIRECTORY

    @property
    def journal_path(self) -> Path:
        return self.root / _JOURNAL_DIRECTORY / _JOURNAL_FILENAME


def _prepare_real_directory(
    path: Path,
    name: str,
    *,
    expected_parent: Path | None = None,
) -> Path:
    requested = path.expanduser()
    if requested.is_symlink():
        raise ValueError(f"{name} must not be a symbolic link")
    requested.mkdir(parents=True, exist_ok=True)
    if requested.is_symlink() or not requested.is_dir():
        raise ValueError(f"{name} must be a real directory")
    resolved = requested.resolve(strict=True)
    if expected_parent is not None and resolved.parent != expected_parent:
        raise ValueError(f"{name} escapes the Trading Runner owner root")
    return resolved
