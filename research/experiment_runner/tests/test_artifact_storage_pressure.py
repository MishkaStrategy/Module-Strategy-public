from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from modular_research_runner.artifacts import AtomicJsonArtifactStore


def _temporary_files(target: Path) -> list[Path]:
    return sorted(target.parent.glob(f".{target.name}.*"))


def test_enospc_during_file_fsync_leaves_no_partial_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "artifacts"
    store = AtomicJsonArtifactStore(root)
    target = root / "runs" / "result.json"
    real_fsync = os.fsync
    calls = 0

    def fail_first_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.ENOSPC, "simulated storage exhaustion")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_first_fsync)

    with pytest.raises(OSError) as captured:
        store.write_json("runs/result.json", {"status": "candidate"})

    assert captured.value.errno == errno.ENOSPC
    assert not target.exists()
    assert _temporary_files(target) == []

    reference = store.write_json("runs/result.json", {"status": "recovered"})
    assert store.read_json(reference) == {"status": "recovered"}
    assert _temporary_files(target) == []


def test_enospc_before_replace_preserves_existing_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "artifacts"
    store = AtomicJsonArtifactStore(root)
    target = root / "runs" / "result.json"
    original = store.write_json("runs/result.json", {"status": "accepted"})
    original_bytes = target.read_bytes()
    real_replace = os.replace
    calls = 0

    def fail_first_replace(
        source: os.PathLike[str] | str,
        destination: os.PathLike[str] | str,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.ENOSPC, "simulated storage exhaustion")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_first_replace)

    with pytest.raises(OSError) as captured:
        store.write_json("runs/result.json", {"status": "replacement"})

    assert captured.value.errno == errno.ENOSPC
    assert target.read_bytes() == original_bytes
    assert store.read_json(original) == {"status": "accepted"}
    assert _temporary_files(target) == []

    replacement = store.write_json("runs/result.json", {"status": "recovered"})
    assert store.read_json(replacement) == {"status": "recovered"}
    assert _temporary_files(target) == []
