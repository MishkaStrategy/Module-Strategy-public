from __future__ import annotations

import json
from pathlib import Path

import pytest

from strategy_factory.artifacts import ContentAddressedArtifactStore
from strategy_factory.errors import CheckpointError


def test_artifact_round_trip_and_existing_identity(tmp_path: Path) -> None:
    store = ContentAddressedArtifactStore(tmp_path)
    payload = {"candidate": "candidate-1", "metrics": {"profit_factor": "1.4"}}
    artifact_id = store.put(artifact_type="candidate_evidence", value=payload)

    assert store.exists(artifact_id)
    assert store.get(artifact_id) == payload
    assert store.put(artifact_type="candidate_evidence", value=payload) == artifact_id
    assert store.path_for(artifact_id).is_file()


def test_artifact_payload_corruption_is_rejected(tmp_path: Path) -> None:
    store = ContentAddressedArtifactStore(tmp_path)
    artifact_id = store.put(artifact_type="evidence", value={"value": 1})
    path = store.path_for(artifact_id)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["payload"] = {"value": 2}
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(CheckpointError, match="payload hash"):
        store.get(artifact_id)


def test_artifact_identity_and_envelope_fail_closed(tmp_path: Path) -> None:
    store = ContentAddressedArtifactStore(tmp_path)
    for invalid in ("", "missing-digest", "../escape-" + "a" * 64, "type-" + "A" * 64):
        with pytest.raises(CheckpointError):
            store.path_for(invalid)

    artifact_id = store.put(artifact_type="evidence", value={"value": 1})
    path = store.path_for(artifact_id)
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(CheckpointError, match="envelope"):
        store.get(artifact_id)
