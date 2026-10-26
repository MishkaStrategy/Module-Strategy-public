from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest

from modular_backtest.event_artifacts import (
    EventArtifactError,
    LEGACY_GZIP_EVENT_PATH,
    LEGACY_PLAIN_EVENT_PATH,
    SHARDED_EVENT_MANIFEST_PATH,
    open_event_stream_text,
    shard_large_gzip_event_stream,
    validate_sharded_event_stream,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _legacy_result(
    tmp_path: Path,
    name: str,
    *,
    checksums: bool = False,
) -> tuple[Path, bytes, bytes]:
    root = tmp_path / name
    root.mkdir()
    rows = [
        {"event_type": "feature.published", "sequence": index, "payload": {"value": index}}
        for index in range(1, 9)
    ]
    canonical = b"".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        for row in rows
    )
    event_path = root / LEGACY_GZIP_EVENT_PATH
    with event_path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=1, mtime=0) as handle:
            handle.write(canonical)
    compressed = event_path.read_bytes()
    _write_json(
        root / "journal.json",
        {
            "schema_version": "3",
            "event_stream": {
                "path": LEGACY_GZIP_EVENT_PATH,
                "encoding": "gzip",
                "count": len(rows),
                "bytes": len(compressed),
                "logical_bytes": len(canonical),
                "semantic_sha256": "a" * 64,
                "counts_by_type": {"feature.published": len(rows)},
                "retained_window": len(rows),
            },
        },
    )
    _write_json(
        root / "environment.json",
        {"artifact_schema_version": "3", "performance_mode": "research_fast"},
    )
    _write_json(
        root / "application-run.json",
        {"application_run_id": name, "artifact_schema_version": "3"},
    )
    if checksums:
        names = ("environment.json", LEGACY_GZIP_EVENT_PATH, "journal.json")
        (root / "SHA256SUMS.txt").write_text(
            "".join(f"{_sha256(root / item)}  {item}\n" for item in names),
            encoding="utf-8",
        )
    return root, canonical, compressed


def _shard(root: Path, compressed: bytes) -> dict[str, object]:
    manifest = shard_large_gzip_event_stream(
        root,
        max_file_bytes=len(compressed),
        target_part_bytes=max(1, len(compressed) // 3),
    )
    assert manifest is not None
    return manifest


def _journal(root: Path) -> dict[str, object]:
    payload = json.loads((root / "journal.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _manifest(root: Path) -> dict[str, object]:
    payload = json.loads((root / SHARDED_EVENT_MANIFEST_PATH).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_open_event_stream_supports_legacy_plain_and_gzip_and_rejects_ambiguity(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(EventArtifactError, match="result directory"):
        open_event_stream_text(missing)

    plain_root = tmp_path / "plain"
    plain_root.mkdir()
    (plain_root / LEGACY_PLAIN_EVENT_PATH).write_text('{"event":1}\n', encoding="utf-8")
    with open_event_stream_text(plain_root) as handle:
        assert handle.read() == '{"event":1}\n'

    gzip_root, canonical, _ = _legacy_result(tmp_path, "gzip")
    with open_event_stream_text(gzip_root) as handle:
        assert handle.read().encode("utf-8") == canonical

    (gzip_root / LEGACY_PLAIN_EVENT_PATH).write_text("{}\n", encoding="utf-8")
    with pytest.raises(EventArtifactError, match="exactly one"):
        open_event_stream_text(gzip_root)


def test_open_event_stream_rejects_symlink_selector(tmp_path: Path) -> None:
    root = tmp_path / "symlink-selector"
    root.mkdir()
    target = root / "target.jsonl"
    target.write_text("{}\n", encoding="utf-8")
    selector = root / LEGACY_PLAIN_EVENT_PATH
    try:
        selector.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")
    with pytest.raises(EventArtifactError, match="regular file"):
        open_event_stream_text(root)


def test_sharding_rejects_invalid_limits_and_missing_root(tmp_path: Path) -> None:
    root, _, compressed = _legacy_result(tmp_path, "limits")
    with pytest.raises(EventArtifactError, match="max_file_bytes"):
        shard_large_gzip_event_stream(root, max_file_bytes=0, target_part_bytes=1)
    with pytest.raises(EventArtifactError, match="target_part_bytes"):
        shard_large_gzip_event_stream(root, max_file_bytes=len(compressed), target_part_bytes=0)
    with pytest.raises(EventArtifactError, match="strictly below"):
        shard_large_gzip_event_stream(
            root,
            max_file_bytes=len(compressed),
            target_part_bytes=len(compressed),
        )
    with pytest.raises(EventArtifactError, match="result directory"):
        shard_large_gzip_event_stream(
            tmp_path / "absent",
            max_file_bytes=len(compressed),
            target_part_bytes=1,
        )


def test_sharding_rejects_invalid_legacy_contract(tmp_path: Path) -> None:
    cases = (
        ("no-stream", lambda journal: journal.pop("event_stream"), "event_stream"),
        ("schema", lambda journal: journal.__setitem__("schema_version", "2"), "schema v3"),
        (
            "path",
            lambda journal: journal["event_stream"].__setitem__("path", "other.gz"),
            "legacy events.jsonl.gz",
        ),
        (
            "encoding",
            lambda journal: journal["event_stream"].__setitem__("encoding", "identity"),
            "legacy events.jsonl.gz",
        ),
        (
            "bytes",
            lambda journal: journal["event_stream"].__setitem__("bytes", 1),
            "do not match",
        ),
        (
            "count",
            lambda journal: journal["event_stream"].__setitem__("count", 0),
            "event count",
        ),
        (
            "logical",
            lambda journal: journal["event_stream"].__setitem__("logical_bytes", 0),
            "logical bytes",
        ),
        (
            "semantic",
            lambda journal: journal["event_stream"].__setitem__("semantic_sha256", "bad"),
            "semantic_sha256",
        ),
    )
    for name, mutate, message in cases:
        root, _, compressed = _legacy_result(tmp_path, name)
        journal = _journal(root)
        mutate(journal)
        _write_json(root / "journal.json", journal)
        with pytest.raises(EventArtifactError, match=message):
            shard_large_gzip_event_stream(
                root,
                max_file_bytes=len(compressed),
                target_part_bytes=max(1, len(compressed) // 3),
            )

    missing_source, _, compressed = _legacy_result(tmp_path, "missing-source")
    (missing_source / LEGACY_GZIP_EVENT_PATH).unlink()
    with pytest.raises(EventArtifactError, match="missing or symlinked"):
        shard_large_gzip_event_stream(
            missing_source,
            max_file_bytes=len(compressed),
            target_part_bytes=1,
        )

    existing_manifest, _, compressed = _legacy_result(tmp_path, "existing-manifest")
    (existing_manifest / SHARDED_EVENT_MANIFEST_PATH).write_text("{}\n", encoding="utf-8")
    with pytest.raises(EventArtifactError, match="manifest already exists"):
        _shard(existing_manifest, compressed)

    stale_part, _, compressed = _legacy_result(tmp_path, "stale-part")
    (stale_part / "events.jsonl.gz.part-000001").write_bytes(b"stale")
    with pytest.raises(EventArtifactError, match="stale event shard"):
        _shard(stale_part, compressed)


def test_checksum_scope_rejects_noncanonical_or_unsafe_rows(tmp_path: Path) -> None:
    scenarios: list[tuple[str, list[str], str]] = []
    root, _, compressed = _legacy_result(tmp_path, "checksum-template")
    environment = _sha256(root / "environment.json")
    events = _sha256(root / LEGACY_GZIP_EVENT_PATH)
    journal = _sha256(root / "journal.json")
    scenarios.extend(
        [
            ("empty", [], "must not be empty"),
            ("malformed", [f"{events} {LEGACY_GZIP_EVENT_PATH}"], "invalid row"),
            ("bad-digest", [f"bad  {LEGACY_GZIP_EVENT_PATH}"], "lowercase SHA-256"),
            ("unsafe", [f"{events}  ../events.jsonl.gz"], "unsafe artifact name"),
            (
                "duplicate",
                [f"{events}  {LEGACY_GZIP_EVENT_PATH}", f"{events}  {LEGACY_GZIP_EVENT_PATH}"],
                "duplicate artifact",
            ),
            (
                "mismatch",
                [f"{'0' * 64}  {LEGACY_GZIP_EVENT_PATH}"],
                "checksum mismatch",
            ),
            (
                "unordered",
                [
                    f"{journal}  journal.json",
                    f"{events}  {LEGACY_GZIP_EVENT_PATH}",
                    f"{environment}  environment.json",
                ],
                "order is not canonical",
            ),
            (
                "missing-events",
                [f"{environment}  environment.json", f"{journal}  journal.json"],
                "does not cover events.jsonl.gz",
            ),
        ]
    )

    for name, rows, message in scenarios:
        case_root, _, case_compressed = _legacy_result(tmp_path, f"checksum-{name}")
        if name not in {"empty", "malformed", "bad-digest", "unsafe", "duplicate", "mismatch"}:
            case_environment = _sha256(case_root / "environment.json")
            case_events = _sha256(case_root / LEGACY_GZIP_EVENT_PATH)
            case_journal = _sha256(case_root / "journal.json")
            if name == "unordered":
                rows = [
                    f"{case_journal}  journal.json",
                    f"{case_events}  {LEGACY_GZIP_EVENT_PATH}",
                    f"{case_environment}  environment.json",
                ]
            else:
                rows = [
                    f"{case_environment}  environment.json",
                    f"{case_journal}  journal.json",
                ]
        (case_root / "SHA256SUMS.txt").write_text(
            "" if not rows else "\n".join(rows) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(EventArtifactError, match=message):
            shard_large_gzip_event_stream(
                case_root,
                max_file_bytes=len(case_compressed),
                target_part_bytes=max(1, len(case_compressed) // 3),
            )


def test_validate_shards_rejects_manifest_contract_drift(tmp_path: Path) -> None:
    scenarios = (
        ("schema", lambda doc: doc.__setitem__("schema_version", "2"), "manifest schema"),
        ("encoding", lambda doc: doc.__setitem__("encoding", "other"), "encoding"),
        ("source", lambda doc: doc.__setitem__("source_encoding", "identity"), "encoding"),
        ("source-path", lambda doc: doc.__setitem__("source_path", "other.gz"), "source_path"),
        ("total", lambda doc: doc.__setitem__("compressed_bytes", 0), "compressed_bytes"),
        ("logical", lambda doc: doc.__setitem__("logical_bytes", 0), "logical_bytes"),
        ("count", lambda doc: doc.__setitem__("event_count", 0), "event_count"),
        ("aggregate", lambda doc: doc.__setitem__("compressed_sha256", "bad"), "compressed_sha256"),
        ("semantic", lambda doc: doc.__setitem__("semantic_sha256", "bad"), "semantic_sha256"),
        ("parts", lambda doc: doc.__setitem__("parts", []), "at least two parts"),
    )
    for name, mutate, message in scenarios:
        root, _, compressed = _legacy_result(tmp_path, f"manifest-{name}")
        _shard(root, compressed)
        document = _manifest(root)
        mutate(document)
        _write_json(root / SHARDED_EVENT_MANIFEST_PATH, document)
        with pytest.raises(EventArtifactError, match=message):
            validate_sharded_event_stream(root, max_file_bytes=len(compressed))


def test_validate_shards_rejects_part_and_journal_contract_drift(tmp_path: Path) -> None:
    root, _, compressed = _legacy_result(tmp_path, "part-not-object")
    _shard(root, compressed)
    document = _manifest(root)
    document["parts"][0] = "bad"
    _write_json(root / SHARDED_EVENT_MANIFEST_PATH, document)
    with pytest.raises(EventArtifactError, match="part must be an object"):
        validate_sharded_event_stream(root, max_file_bytes=len(compressed))

    root, _, compressed = _legacy_result(tmp_path, "part-path")
    _shard(root, compressed)
    document = _manifest(root)
    document["parts"][0]["path"] = "other"
    _write_json(root / SHARDED_EVENT_MANIFEST_PATH, document)
    with pytest.raises(EventArtifactError, match="path is not deterministic"):
        validate_sharded_event_stream(root, max_file_bytes=len(compressed))

    root, _, compressed = _legacy_result(tmp_path, "part-size")
    _shard(root, compressed)
    document = _manifest(root)
    document["parts"][0]["bytes"] = len(compressed)
    _write_json(root / SHARDED_EVENT_MANIFEST_PATH, document)
    with pytest.raises(EventArtifactError, match="publication extra-byte bound"):
        validate_sharded_event_stream(root, max_file_bytes=len(compressed))

    root, _, compressed = _legacy_result(tmp_path, "part-sha")
    _shard(root, compressed)
    document = _manifest(root)
    document["parts"][0]["sha256"] = "bad"
    _write_json(root / SHARDED_EVENT_MANIFEST_PATH, document)
    with pytest.raises(EventArtifactError, match="lowercase SHA-256"):
        validate_sharded_event_stream(root, max_file_bytes=len(compressed))

    root, _, compressed = _legacy_result(tmp_path, "legacy-gzip")
    _shard(root, compressed)
    (root / LEGACY_GZIP_EVENT_PATH).write_bytes(b"ambiguous")
    with pytest.raises(EventArtifactError, match="must not retain legacy"):
        validate_sharded_event_stream(root, max_file_bytes=len(compressed))

    root, _, compressed = _legacy_result(tmp_path, "legacy-plain")
    _shard(root, compressed)
    (root / LEGACY_PLAIN_EVENT_PATH).write_text("{}\n", encoding="utf-8")
    with pytest.raises(EventArtifactError, match="ambiguous"):
        validate_sharded_event_stream(root, max_file_bytes=len(compressed))

    root, _, compressed = _legacy_result(tmp_path, "journal-schema")
    _shard(root, compressed)
    journal = _journal(root)
    journal["schema_version"] = "3"
    _write_json(root / "journal.json", journal)
    with pytest.raises(EventArtifactError, match="journal schema v4"):
        validate_sharded_event_stream(root, max_file_bytes=len(compressed))

    root, _, compressed = _legacy_result(tmp_path, "journal-path")
    _shard(root, compressed)
    journal = _journal(root)
    journal["event_stream"]["path"] = "other"
    _write_json(root / "journal.json", journal)
    with pytest.raises(EventArtifactError, match="journal sharded event path"):
        validate_sharded_event_stream(root, max_file_bytes=len(compressed))
