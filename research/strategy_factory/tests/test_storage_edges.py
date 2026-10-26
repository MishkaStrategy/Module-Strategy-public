from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from test_recovery import checkpoint

from strategy_factory.artifacts import ContentAddressedArtifactStore
from strategy_factory.checkpoint import (
    AtomicCheckpointStore,
    _checkpoint_from_payload,
    _checkpoint_payload,
    _fsync_directory,
    _string_mapping,
)
from strategy_factory.errors import CheckpointError


def test_artifact_type_and_identity_validation(tmp_path: Path) -> None:
    store = ContentAddressedArtifactStore(tmp_path)
    for invalid in ("", "bad/type", "white space", "💥"):
        with pytest.raises(CheckpointError, match="artifact_type"):
            store.put(artifact_type=invalid, value={})
    for invalid in (None, 1, "", "missing", "type-short", "type-" + "A" * 64):
        with pytest.raises(CheckpointError):
            store.path_for(invalid)  # type: ignore[arg-type]


def test_existing_artifact_collision_and_read_failures(tmp_path: Path, monkeypatch) -> None:
    store = ContentAddressedArtifactStore(tmp_path)
    artifact_id = store.put(artifact_type="evidence", value={"value": 1})
    target = store.path_for(artifact_id)
    target.write_bytes(b"different")
    with pytest.raises(CheckpointError, match="collision or storage corruption"):
        store.put(artifact_type="evidence", value={"value": 1})

    original_read_bytes = Path.read_bytes

    def fail_read(path: Path):
        if path == target:
            raise OSError("boom")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_read)
    with pytest.raises(CheckpointError, match="cannot verify existing artifact"):
        store.put(artifact_type="evidence", value={"value": 1})
    with pytest.raises(CheckpointError, match="cannot read artifact"):
        store.get(artifact_id)


def test_artifact_invalid_json_fields_and_identity(tmp_path: Path) -> None:
    store = ContentAddressedArtifactStore(tmp_path)
    artifact_id = store.put(artifact_type="evidence", value={"value": 1})
    target = store.path_for(artifact_id)

    target.write_text("{", encoding="utf-8")
    with pytest.raises(CheckpointError, match="JSON is invalid"):
        store.get(artifact_id)

    envelope = {
        "artifact_type": "evidence",
        "artifact_id": artifact_id,
        "payload_hash": artifact_id.rsplit("-", 1)[1],
        "payload": {"value": 1},
    }
    target.write_text(json.dumps({**envelope, "extra": True}), encoding="utf-8")
    with pytest.raises(CheckpointError, match="fields are invalid"):
        store.get(artifact_id)

    target.write_text(json.dumps({**envelope, "artifact_type": "other"}), encoding="utf-8")
    with pytest.raises(CheckpointError, match="identity mismatch"):
        store.get(artifact_id)

    target.write_text(json.dumps({**envelope, "payload_hash": "b" * 64}), encoding="utf-8")
    with pytest.raises(CheckpointError, match="envelope hash mismatch"):
        store.get(artifact_id)


def test_artifact_link_race_conflict_and_persist_failure(tmp_path: Path, monkeypatch) -> None:
    store = ContentAddressedArtifactStore(tmp_path)
    original_link = os.link

    def race_link(source, target):
        Path(target).write_bytes(b"conflict")
        raise FileExistsError

    monkeypatch.setattr(os, "link", race_link)
    with pytest.raises(CheckpointError, match="identity conflict"):
        store.put(artifact_type="race", value={"value": 1})

    monkeypatch.setattr(os, "link", original_link)
    original_open = Path.open

    def fail_open(path: Path, *args, **kwargs):
        if path.parent.name == "failure" and path.name.startswith("."):
            raise OSError("disk failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_open)
    with pytest.raises(CheckpointError, match="cannot persist artifact"):
        store.put(artifact_type="failure", value={"value": 2})


def test_checkpoint_path_read_envelope_and_identity_failures(
    factory_request, tmp_path: Path
) -> None:
    store = AtomicCheckpointStore(tmp_path)
    original = checkpoint(factory_request)
    for invalid in ("", "../escape", "bad/path", "white space"):
        with pytest.raises(CheckpointError, match="not safe"):
            store.exists(invalid)

    with pytest.raises(CheckpointError, match="cannot read checkpoint"):
        store.load("missing")

    path = store.save(original)
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(CheckpointError, match="unsupported checkpoint envelope"):
        store.load(original.run_id)

    path.write_text(json.dumps({"format": "strategy-factory-checkpoint-v1"}), encoding="utf-8")
    with pytest.raises(CheckpointError, match="payload is missing"):
        store.load(original.run_id)

    different = replace(original, run_id="different-run")
    different_path = store.save(different)
    path.write_bytes(different_path.read_bytes())
    with pytest.raises(CheckpointError, match="run identity mismatch"):
        store.load(original.run_id)


def test_checkpoint_payload_rejects_malformed_collections(factory_request) -> None:
    original = checkpoint(factory_request)
    base = _checkpoint_payload(original)
    cases = [
        ({**base, "completed_artifacts": []}, "mapping must be an object"),
        ({**base, "submitted_batches": {"key": 1}}, "mapping must contain strings"),
        ({**base, "budget_state": []}, "budget_state must be an object"),
        ({**base, "processed_event_ids": [1]}, "string list"),
        (
            {key: value for key, value in base.items() if key != "status"},
            "invalid checkpoint contract",
        ),
        ({**base, "status": "UNKNOWN"}, "invalid checkpoint contract"),
    ]
    for payload, message in cases:
        with pytest.raises(CheckpointError, match=message):
            _checkpoint_from_payload(payload)  # type: ignore[arg-type]


def test_string_mapping_and_directory_fsync_fail_closed(tmp_path: Path, monkeypatch) -> None:
    assert _string_mapping({"a": "b"}) == {"a": "b"}
    with pytest.raises(CheckpointError, match="must be an object"):
        _string_mapping([])
    with pytest.raises(CheckpointError, match="must contain strings"):
        _string_mapping({1: "b"})

    monkeypatch.setattr(os, "open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))
    _fsync_directory(tmp_path)


def test_checkpoint_save_failure_cleans_temporary(
    factory_request, tmp_path: Path, monkeypatch
) -> None:
    store = AtomicCheckpointStore(tmp_path)
    original = checkpoint(factory_request)
    original_replace = os.replace

    def fail_replace(source, target):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(CheckpointError, match="cannot persist checkpoint"):
        store.save(original)
    assert not tuple(tmp_path.glob(".*.tmp"))
    monkeypatch.setattr(os, "replace", original_replace)


def test_checkpoint_contract_error_is_wrapped(factory_request) -> None:
    payload = _checkpoint_payload(checkpoint(factory_request))
    payload["stage_sequence"] = -1
    with pytest.raises(CheckpointError, match="invalid checkpoint contract"):
        _checkpoint_from_payload(payload)
