from __future__ import annotations

import gzip
import json
from collections import namedtuple
from pathlib import Path

import pytest

import scripts.write_physical_event_storage_evidence as storage_module
from scripts.write_physical_event_storage_evidence import (
    REGISTRY_EVENT_FILE_LIMIT_BYTES,
    StorageEvidenceError,
    build_event_storage_evidence,
    write_event_storage_evidence,
)


def _write_fixture(
    tmp_path: Path,
    *,
    event_count: int = 100,
) -> tuple[Path, Path, Path, int, int]:
    result_dir = tmp_path / "results" / "full-btc"
    result_dir.mkdir(parents=True)
    canonical = b'{"event_type":"feature.published","symbol":"BTCUSDT"}\n' * event_count
    event_path = result_dir / "events.jsonl.gz"
    with gzip.open(event_path, "wb", compresslevel=6) as handle:
        handle.write(canonical)
    physical_bytes = event_path.stat().st_size
    logical_bytes = len(canonical)
    journal = {
        "schema_version": "3",
        "event_stream": {
            "path": "events.jsonl.gz",
            "encoding": "gzip",
            "count": event_count,
            "bytes": physical_bytes,
            "logical_bytes": logical_bytes,
            "semantic_sha256": "a" * 64,
            "counts_by_type": {"feature.published": event_count},
            "retained_window": min(event_count, 2_000),
        },
    }
    (result_dir / "journal.json").write_text(
        json.dumps(journal, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    pretest_report = tmp_path / "pretest-report.json"
    pretest_report.write_text(
        json.dumps({"disk": {"free_gib": 50.0}}) + "\n",
        encoding="utf-8",
    )
    return result_dir, pretest_report, tmp_path, physical_bytes, logical_bytes


def _nest_fixture_under_collection_root(result_dir: Path) -> Path:
    run_dir = result_dir / "run-test"
    run_dir.mkdir()
    for name in ("journal.json", "events.jsonl.gz"):
        (result_dir / name).replace(run_dir / name)
    return run_dir


def test_storage_evidence_records_physical_logical_and_result_disk_headroom(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_dir, pretest_report, disk_root, physical_bytes, logical_bytes = _write_fixture(tmp_path)
    disk_usage_record = namedtuple("DiskUsage", "total used free")
    monkeypatch.setattr(
        storage_module.shutil,
        "disk_usage",
        lambda _path: disk_usage_record(100 * 1024**3, 60 * 1024**3, 40 * 1024**3),
    )
    output = tmp_path / "event-storage.json"

    payload = write_event_storage_evidence(
        result_dir=result_dir,
        pretest_report=pretest_report,
        disk_root=disk_root,
        output=output,
    )

    assert payload["status"] == "PASS"
    stream = payload["event_stream"]
    assert isinstance(stream, dict)
    assert stream["path"] == "events.jsonl.gz"
    assert stream["encoding"] == "gzip"
    assert stream["count"] == 100
    assert stream["physical_bytes"] == physical_bytes
    assert stream["logical_bytes"] == logical_bytes
    assert stream["compression_ratio"] == round(logical_bytes / physical_bytes, 6)
    assert stream["semantic_sha256"] == "a" * 64
    assert isinstance(stream["allocated_bytes"], int)

    registry = payload["registry_file_budget"]
    assert isinstance(registry, dict)
    assert registry == {
        "limit_bytes": REGISTRY_EVENT_FILE_LIMIT_BYTES,
        "headroom_bytes": REGISTRY_EVENT_FILE_LIMIT_BYTES - physical_bytes,
        "within_limit": True,
    }
    disk = payload["disk_headroom"]
    assert isinstance(disk, dict)
    assert disk["measurement_scope"] == "result_filesystem"
    assert disk["post_full_free_gib"] == 40.0
    assert disk["event_stream_on_measured_filesystem"] is True
    assert disk["pretest_dataset_free_gib"] == 50.0
    assert disk["pretest_dataset_same_filesystem_not_claimed"] is True

    text = output.read_text(encoding="utf-8")
    assert str(tmp_path) not in text
    assert str(Path.home()) not in text
    assert payload["paper_only"] is True
    assert payload["live_trading_authorized"] is False
    assert payload["raw_market_data_uploaded"] is False


def test_storage_evidence_resolves_single_collection_child(tmp_path: Path) -> None:
    result_dir, pretest_report, disk_root, _, _ = _write_fixture(tmp_path)
    run_dir = _nest_fixture_under_collection_root(result_dir)

    payload = build_event_storage_evidence(
        result_dir=result_dir,
        pretest_report=pretest_report,
        disk_root=disk_root,
    )

    assert run_dir.is_dir()
    assert payload["status"] == "PASS"
    stream = payload["event_stream"]
    assert isinstance(stream, dict)
    assert stream["count"] == 100


def test_storage_evidence_rejects_empty_collection_root(tmp_path: Path) -> None:
    result_dir = tmp_path / "results" / "full-btc"
    result_dir.mkdir(parents=True)

    with pytest.raises(StorageEvidenceError, match="exactly one run directory, found 0"):
        build_event_storage_evidence(
            result_dir=result_dir,
            pretest_report=tmp_path / "unused-pretest.json",
            disk_root=tmp_path,
        )


def test_storage_evidence_rejects_multiple_collection_children(tmp_path: Path) -> None:
    result_dir = tmp_path / "results" / "full-btc"
    result_dir.mkdir(parents=True)
    (result_dir / "run-a").mkdir()
    (result_dir / "run-b").mkdir()

    with pytest.raises(StorageEvidenceError, match="exactly one run directory, found 2"):
        build_event_storage_evidence(
            result_dir=result_dir,
            pretest_report=tmp_path / "unused-pretest.json",
            disk_root=tmp_path,
        )


def test_storage_evidence_rejects_symlinked_collection_child(tmp_path: Path) -> None:
    result_dir = tmp_path / "results" / "full-btc"
    result_dir.mkdir(parents=True)
    outside = tmp_path / "outside-run"
    outside.mkdir()
    (result_dir / "run-link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(StorageEvidenceError, match="symlinked child"):
        build_event_storage_evidence(
            result_dir=result_dir,
            pretest_report=tmp_path / "unused-pretest.json",
            disk_root=tmp_path,
        )


def test_storage_evidence_rejects_journal_size_mismatch(tmp_path: Path) -> None:
    result_dir, pretest_report, disk_root, _, _ = _write_fixture(tmp_path)
    journal_path = result_dir / "journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["event_stream"]["bytes"] += 1
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    with pytest.raises(StorageEvidenceError, match="physical bytes"):
        build_event_storage_evidence(
            result_dir=result_dir,
            pretest_report=pretest_report,
            disk_root=disk_root,
        )


def test_storage_evidence_rejects_noncompressed_contract(tmp_path: Path) -> None:
    result_dir, pretest_report, disk_root, _, _ = _write_fixture(tmp_path)
    journal_path = result_dir / "journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["schema_version"] = "2"
    journal["event_stream"]["path"] = "events.jsonl"
    journal["event_stream"]["encoding"] = "identity"
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    with pytest.raises(StorageEvidenceError, match="schema v3"):
        build_event_storage_evidence(
            result_dir=result_dir,
            pretest_report=pretest_report,
            disk_root=disk_root,
        )


def test_storage_evidence_rejects_event_count_breakdown_mismatch(tmp_path: Path) -> None:
    result_dir, pretest_report, disk_root, _, _ = _write_fixture(tmp_path)
    journal_path = result_dir / "journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["event_stream"]["counts_by_type"]["feature.published"] -= 1
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    with pytest.raises(StorageEvidenceError, match="does not match total"):
        build_event_storage_evidence(
            result_dir=result_dir,
            pretest_report=pretest_report,
            disk_root=disk_root,
        )


def test_storage_evidence_rejects_registry_file_limit_exhaustion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_dir, pretest_report, disk_root, physical_bytes, _ = _write_fixture(tmp_path)
    monkeypatch.setattr(
        storage_module,
        "REGISTRY_EVENT_FILE_LIMIT_BYTES",
        physical_bytes,
    )

    with pytest.raises(StorageEvidenceError, match="16 GiB"):
        build_event_storage_evidence(
            result_dir=result_dir,
            pretest_report=pretest_report,
            disk_root=disk_root,
        )


def test_storage_evidence_refuses_existing_or_symlink_output(tmp_path: Path) -> None:
    result_dir, pretest_report, disk_root, _, _ = _write_fixture(tmp_path)
    output = tmp_path / "event-storage.json"
    output.write_text("stale", encoding="utf-8")

    with pytest.raises(StorageEvidenceError, match="must not already exist"):
        write_event_storage_evidence(
            result_dir=result_dir,
            pretest_report=pretest_report,
            disk_root=disk_root,
            output=output,
        )

    output.unlink()
    target = tmp_path / "target.json"
    target.write_text("private", encoding="utf-8")
    output.symlink_to(target)
    with pytest.raises(StorageEvidenceError, match="must not already exist"):
        write_event_storage_evidence(
            result_dir=result_dir,
            pretest_report=pretest_report,
            disk_root=disk_root,
            output=output,
        )
