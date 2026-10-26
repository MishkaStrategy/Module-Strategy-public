from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .contracts import DownloadCheckpoint
from .enums import RunState
from .errors import StorageError


def load_checkpoint(path: Path) -> DownloadCheckpoint:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return DownloadCheckpoint(
            run_id=payload["run_id"],
            request_hash=payload["request_hash"],
            state=RunState(payload["state"]),
            completed_chunks=tuple(payload["completed_chunks"]),
            pending_chunks=tuple(payload["pending_chunks"]),
            updated_at=datetime.fromisoformat(payload["updated_at"].replace("Z", "+00:00")),
            checkpoint_version=int(payload["checkpoint_version"]),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StorageError(
            "CHECKPOINT_CORRUPT", "checkpoint cannot be decoded safely", False, "checkpoint"
        ) from exc
