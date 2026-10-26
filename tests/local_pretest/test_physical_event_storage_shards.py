from __future__ import annotations

import gzip
import hashlib
import json
from collections import namedtuple
from pathlib import Path

import pytest

import scripts.write_physical_event_storage_evidence as storage_module
from modular_backtest.event_artifacts import (
    SHARDED_EVENT_ENCODING,
    SHARDED_EVENT_MANIFEST_PATH,
    SHARDED_EVENT_SPLIT_STRATEGY,
    shard_large_gzip_event_stream,
)
from scripts.write_physical_event_storage_evidence import (
    StorageEvidenceError,
    build_event_storage_evidence,
)


def _sharded_fixture(tmp_path: Path) -> tuple[Path, Path, Path, bytes]:
    result_dir = tmp_path / "results" / "full-btc"
    result_dir.mkdir(parents=True)
    canonical = b"".join(
        json.dumps(
            {
                "event_id": f"evt-{sequence:010d}",
                "sequence": sequence,
                "event_type": "feature.published",
                "payload": {"digest": hashlib.sha256(str(sequence).encode()).hexdigest()},
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
        for sequence in range(1, 501)
    )
    event_path = result_dir / "events.jsonl.gz"
    with event_path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=1, mtime=0) as handle:
            handle.write(canonical)
    compressed = event_path.read_bytes()
    journal = {
        "schema_version": "3",
        "event_stream": {
            "path": "events.jsonl.gz",
            "encoding": "gzip",
            "count": 500,
            "bytes": len(compressed),
            "logical_bytes": len(canonical),
            "semantic_sha256": "d" * 64,
            "counts_by_type": {"feature.published": 500},
            "retained_window": 500,
        },
    }
    (result_dir / "journal.json").write_text(
        json.dumps(journal, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    shard_large_gzip_event_stream(
        result_dir,
        max_file_bytes=len(compressed),
        target_part_bytes=max(1, len(compressed) // 3),
    )
    pretest_report = tmp_path / "pretest-report.json"
    pretest_report.write_text(json.dumps({"disk": {"free_gib": 50.0}}), encoding="utf-8")
    return result_dir, pretest_report, tmp_path, compressed


def test_physical_evidence_accepts_verified_sharded_event_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_dir, pretest_report, disk_root, compressed = _sharded_fixture(tmp_path)
    disk_usage_record = namedtuple("DiskUsage", "total used free")
    monkeypatch.setattr(
        storage_module.shutil,
        "disk_usage",
        lambda _path: disk_usage_record(100 * 1024**3, 60 * 1024**3, 40 * 1024**3),
    )

    payload = build_event_storage_evidence(
        result_dir=result_dir,
        pretest_report=pretest_report,
        disk_root=disk_root,
    )

    assert payload["status"] == "PASS"
    stream = payload["event_stream"]
    assert isinstance(stream, dict)
    assert stream["path"] == SHARDED_EVENT_MANIFEST_PATH
    assert stream["encoding"] == SHARDED_EVENT_ENCODING
    assert stream["count"] == 500
    assert stream["physical_bytes"] == len(compressed)
    assert stream["part_count"] >= 2
    assert stream["max_part_bytes"] < storage_module.REGISTRY_EVENT_FILE_LIMIT_BYTES
    assert stream["compressed_sha256"] == hashlib.sha256(compressed).hexdigest()
    assert stream["publication_split_strategy"] == SHARDED_EVENT_SPLIT_STRATEGY
    assert stream["publication_extra_bytes_bound"] == max(1, len(compressed) // 3)
    assert stream["max_part_bytes"] <= stream["publication_extra_bytes_bound"]
    assert payload["registry_file_budget"]["within_limit"] is True


def test_physical_evidence_rejects_tampered_shard(tmp_path: Path) -> None:
    result_dir, pretest_report, disk_root, _ = _sharded_fixture(tmp_path)
    manifest = json.loads((result_dir / SHARDED_EVENT_MANIFEST_PATH).read_text(encoding="utf-8"))
    first = result_dir / manifest["parts"][0]["path"]
    data = first.read_bytes()
    first.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))

    with pytest.raises(StorageEvidenceError, match="failed artifact validation"):
        build_event_storage_evidence(
            result_dir=result_dir,
            pretest_report=pretest_report,
            disk_root=disk_root,
        )


def test_physical_evidence_rejects_part_at_registry_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_dir, pretest_report, disk_root, _ = _sharded_fixture(tmp_path)
    manifest = json.loads((result_dir / SHARDED_EVENT_MANIFEST_PATH).read_text(encoding="utf-8"))
    max_part = max(int(item["bytes"]) for item in manifest["parts"])
    monkeypatch.setattr(storage_module, "REGISTRY_EVENT_FILE_LIMIT_BYTES", max_part)

    with pytest.raises(StorageEvidenceError, match="failed artifact validation"):
        build_event_storage_evidence(
            result_dir=result_dir,
            pretest_report=pretest_report,
            disk_root=disk_root,
        )


def test_physical_evidence_rejects_publication_strategy_drift(tmp_path: Path) -> None:
    result_dir, pretest_report, disk_root, _ = _sharded_fixture(tmp_path)
    manifest_path = result_dir / SHARDED_EVENT_MANIFEST_PATH
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["split_strategy"] = "copy-full-stream-v1"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(StorageEvidenceError, match="failed artifact validation"):
        build_event_storage_evidence(
            result_dir=result_dir,
            pretest_report=pretest_report,
            disk_root=disk_root,
        )
