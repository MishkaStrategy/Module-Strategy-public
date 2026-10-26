from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest

from modular_backtest.event_artifacts import (
    EventArtifactError,
    SHARDED_EVENT_ENCODING,
    SHARDED_EVENT_MANIFEST_PATH,
    SHARDED_EVENT_SPLIT_STRATEGY,
    iter_event_stream_lines,
    shard_large_gzip_event_stream,
    validate_sharded_event_stream,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_payload_bytes(paths: list[Path]) -> int:
    inodes: dict[tuple[int, int], int] = {}
    for path in paths:
        if not path.exists():
            continue
        stat = path.stat()
        inodes[(stat.st_dev, stat.st_ino)] = stat.st_size
    return sum(inodes.values())


def _fixture(tmp_path: Path, *, event_count: int = 250) -> tuple[Path, bytes, bytes]:
    root = tmp_path / "result"
    root.mkdir()
    canonical = b"".join(
        json.dumps(
            {
                "event_id": f"evt-{sequence:010d}",
                "sequence": sequence,
                "event_type": "feature.published",
                "payload": {"value": hashlib.sha256(str(sequence).encode()).hexdigest()},
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
        for sequence in range(1, event_count + 1)
    )
    event_path = root / "events.jsonl.gz"
    with event_path.open("wb") as raw:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw,
            compresslevel=1,
            mtime=0,
        ) as handle:
            handle.write(canonical)
    compressed = event_path.read_bytes()
    journal = {
        "schema_version": "3",
        "event_stream": {
            "path": "events.jsonl.gz",
            "encoding": "gzip",
            "count": event_count,
            "bytes": len(compressed),
            "logical_bytes": len(canonical),
            "semantic_sha256": "a" * 64,
            "counts_by_type": {"feature.published": event_count},
            "retained_window": min(event_count, 2_000),
        },
    }
    (root / "journal.json").write_text(
        json.dumps(journal, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (root / "environment.json").write_text(
        json.dumps(
            {
                "artifact_schema_version": "3",
                "performance_mode": "research_fast",
                "python": "test",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "application-run.json").write_text(
        json.dumps(
            {
                "application_run_id": "test-run",
                "artifact_schema_version": "3",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    checksum_names = ("environment.json", "events.jsonl.gz", "journal.json")
    (root / "SHA256SUMS.txt").write_text(
        "".join(f"{_sha256(root / name)}  {name}\n" for name in checksum_names),
        encoding="utf-8",
    )
    return root, canonical, compressed


def _shard(root: Path, compressed: bytes) -> dict[str, object]:
    max_file_bytes = len(compressed)
    target_part_bytes = max(1, max_file_bytes // 3)
    assert target_part_bytes < max_file_bytes
    manifest = shard_large_gzip_event_stream(
        root,
        max_file_bytes=max_file_bytes,
        target_part_bytes=target_part_bytes,
    )
    assert manifest is not None
    return manifest


def test_oversized_gzip_is_sharded_without_changing_compressed_or_decoded_identity(
    tmp_path: Path,
) -> None:
    root, canonical, compressed = _fixture(tmp_path)
    manifest = _shard(root, compressed)

    assert not (root / "events.jsonl.gz").exists()
    assert (root / SHARDED_EVENT_MANIFEST_PATH).is_file()
    assert manifest["encoding"] == SHARDED_EVENT_ENCODING
    assert manifest["split_strategy"] == SHARDED_EVENT_SPLIT_STRATEGY
    assert manifest["publication_extra_bytes_bound"] == max(1, len(compressed) // 3)
    assert manifest["compressed_bytes"] == len(compressed)
    assert manifest["compressed_sha256"] == hashlib.sha256(compressed).hexdigest()

    raw_parts = manifest["parts"]
    assert isinstance(raw_parts, list)
    assert len(raw_parts) >= 2
    reconstructed = b"".join((root / str(part["path"])).read_bytes() for part in raw_parts)
    assert reconstructed == compressed
    assert "".join(iter_event_stream_lines(root)).encode("utf-8") == canonical

    journal = json.loads((root / "journal.json").read_text(encoding="utf-8"))
    assert journal["schema_version"] == "4"
    stream = journal["event_stream"]
    assert stream["path"] == SHARDED_EVENT_MANIFEST_PATH
    assert stream["encoding"] == SHARDED_EVENT_ENCODING
    assert stream["count"] == 250
    assert stream["logical_bytes"] == len(canonical)
    assert stream["semantic_sha256"] == "a" * 64
    assert stream["compressed_sha256"] == hashlib.sha256(compressed).hexdigest()
    assert stream["counts_by_type"] == {"feature.published": 250}

    environment = json.loads((root / "environment.json").read_text(encoding="utf-8"))
    application = json.loads((root / "application-run.json").read_text(encoding="utf-8"))
    assert environment["artifact_schema_version"] == "4"
    assert environment["performance_mode"] == "research_fast"
    assert application["artifact_schema_version"] == "4"
    assert application["application_run_id"] == "test-run"

    checksum_rows = (root / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
    checksum_names = [row.split("  ", 1)[1] for row in checksum_rows]
    expected_names = {
        "environment.json",
        "journal.json",
        SHARDED_EVENT_MANIFEST_PATH,
        *(str(part["path"]) for part in raw_parts),
    }
    assert set(checksum_names) == expected_names
    assert checksum_names == sorted(checksum_names)
    assert "application-run.json" not in checksum_names
    for row in checksum_rows:
        expected_digest, name = row.split("  ", 1)
        assert _sha256(root / name) == expected_digest


def test_hardlinked_spool_tail_split_bounds_transient_unique_payload_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _, compressed = _fixture(tmp_path)
    source = root / "events.jsonl.gz"
    spool = tmp_path / "runtime-spool.gz"
    source.replace(spool)
    source.hardlink_to(spool)
    assert source.stat().st_ino == spool.stat().st_ino

    target_part_bytes = max(1, len(compressed) // 3)
    observed_unique_bytes: list[int] = []
    original_replace = Path.replace

    def tracked_replace(path: Path, target: Path | str) -> Path:
        result = original_replace(path, target)
        target_path = Path(target)
        if target_path.parent == root and target_path.name.startswith("events.jsonl.gz.part-"):
            live_paths = [spool, source, *sorted(root.glob("events.jsonl.gz.part-*"))]
            observed_unique_bytes.append(_unique_payload_bytes(live_paths))
        return result

    monkeypatch.setattr(Path, "replace", tracked_replace)
    manifest = shard_large_gzip_event_stream(
        root,
        max_file_bytes=len(compressed),
        target_part_bytes=target_part_bytes,
    )
    assert manifest is not None
    assert observed_unique_bytes
    assert max(observed_unique_bytes) <= len(compressed) + target_part_bytes
    assert manifest["publication_extra_bytes_bound"] == target_part_bytes

    raw_parts = manifest["parts"]
    assert isinstance(raw_parts, list)
    parts = [root / str(part["path"]) for part in raw_parts]
    assert spool.stat().st_ino == parts[0].stat().st_ino
    assert spool.stat().st_size == parts[0].stat().st_size
    assert _unique_payload_bytes([spool, *parts]) == len(compressed)


def test_small_gzip_keeps_legacy_single_file_layout(tmp_path: Path) -> None:
    root, _, compressed = _fixture(tmp_path)
    before_journal = (root / "journal.json").read_bytes()
    before_environment = (root / "environment.json").read_bytes()
    before_application = (root / "application-run.json").read_bytes()
    before_checksums = (root / "SHA256SUMS.txt").read_bytes()

    result = shard_large_gzip_event_stream(
        root,
        max_file_bytes=len(compressed) + 1,
        target_part_bytes=max(1, len(compressed) // 2),
    )

    assert result is None
    assert (root / "events.jsonl.gz").read_bytes() == compressed
    assert (root / "journal.json").read_bytes() == before_journal
    assert (root / "environment.json").read_bytes() == before_environment
    assert (root / "application-run.json").read_bytes() == before_application
    assert (root / "SHA256SUMS.txt").read_bytes() == before_checksums
    assert not (root / SHARDED_EVENT_MANIFEST_PATH).exists()


def test_sharding_rejects_tampered_journal_checksum_scope_before_mutation(tmp_path: Path) -> None:
    root, _, compressed = _fixture(tmp_path)
    checksum_path = root / "SHA256SUMS.txt"
    rows = checksum_path.read_text(encoding="utf-8").splitlines()
    digest, name = rows[0].split("  ", 1)
    replacement = ("0" if digest[0] != "0" else "1") + digest[1:]
    rows[0] = f"{replacement}  {name}"
    checksum_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    with pytest.raises(EventArtifactError, match="checksum mismatch"):
        _shard(root, compressed)

    assert (root / "events.jsonl.gz").read_bytes() == compressed
    assert not (root / SHARDED_EVENT_MANIFEST_PATH).exists()
    assert not list(root.glob("events.jsonl.gz.part-*"))


def test_shard_validation_rejects_tamper_missing_extra_and_manifest_reorder(
    tmp_path: Path,
) -> None:
    root, _, compressed = _fixture(tmp_path)
    manifest = _shard(root, compressed)
    raw_parts = manifest["parts"]
    assert isinstance(raw_parts, list)
    first = root / str(raw_parts[0]["path"])

    original = first.read_bytes()
    first.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    with pytest.raises(EventArtifactError, match="size/SHA"):
        validate_sharded_event_stream(root, max_file_bytes=len(compressed))
    first.write_bytes(original)

    missing = root / str(raw_parts[-1]["path"])
    saved = missing.read_bytes()
    missing.unlink()
    with pytest.raises(EventArtifactError, match="missing or symlinked"):
        validate_sharded_event_stream(root, max_file_bytes=len(compressed))
    missing.write_bytes(saved)

    extra = root / "events.jsonl.gz.part-999999"
    extra.write_bytes(b"extra")
    with pytest.raises(EventArtifactError, match="inventory mismatch"):
        validate_sharded_event_stream(root, max_file_bytes=len(compressed))
    extra.unlink()

    manifest_path = root / SHARDED_EVENT_MANIFEST_PATH
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["parts"][0]["index"] = 2
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(EventArtifactError, match="indices are not contiguous"):
        validate_sharded_event_stream(root, max_file_bytes=len(compressed))


def test_shard_validation_rejects_split_strategy_and_bound_drift(tmp_path: Path) -> None:
    root, _, compressed = _fixture(tmp_path)
    _shard(root, compressed)
    manifest_path = root / SHARDED_EVENT_MANIFEST_PATH
    document = json.loads(manifest_path.read_text(encoding="utf-8"))

    document["split_strategy"] = "copy-full-stream-v1"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(EventArtifactError, match="split strategy"):
        validate_sharded_event_stream(root, max_file_bytes=len(compressed))

    document["split_strategy"] = SHARDED_EVENT_SPLIT_STRATEGY
    document["publication_extra_bytes_bound"] = len(compressed)
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(EventArtifactError, match="retain Registry headroom"):
        validate_sharded_event_stream(root, max_file_bytes=len(compressed))


def test_shard_validation_rejects_journal_environment_and_application_identity_drift(
    tmp_path: Path,
) -> None:
    root, _, compressed = _fixture(tmp_path)
    _shard(root, compressed)

    journal_path = root / "journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["event_stream"]["count"] += 1
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    with pytest.raises(EventArtifactError, match="event count"):
        validate_sharded_event_stream(root, max_file_bytes=len(compressed))

    journal["event_stream"]["count"] -= 1
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    environment_path = root / "environment.json"
    environment = json.loads(environment_path.read_text(encoding="utf-8"))
    environment["artifact_schema_version"] = "3"
    environment_path.write_text(json.dumps(environment), encoding="utf-8")
    with pytest.raises(EventArtifactError, match="environment.json artifact schema"):
        validate_sharded_event_stream(root, max_file_bytes=len(compressed))

    environment["artifact_schema_version"] = "4"
    environment_path.write_text(json.dumps(environment), encoding="utf-8")
    application_path = root / "application-run.json"
    application = json.loads(application_path.read_text(encoding="utf-8"))
    application["artifact_schema_version"] = "3"
    application_path.write_text(json.dumps(application), encoding="utf-8")
    with pytest.raises(EventArtifactError, match="application-run.json artifact schema"):
        validate_sharded_event_stream(root, max_file_bytes=len(compressed))


def test_shard_validation_rejects_symlink_part(tmp_path: Path) -> None:
    root, _, compressed = _fixture(tmp_path)
    manifest = _shard(root, compressed)
    raw_parts = manifest["parts"]
    assert isinstance(raw_parts, list)
    first = root / str(raw_parts[0]["path"])
    target = root / "outside.bin"
    first.replace(target)
    try:
        first.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(EventArtifactError, match="missing or symlinked"):
        validate_sharded_event_stream(root, max_file_bytes=len(compressed))
