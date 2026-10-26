import gzip
import json
from pathlib import Path

import pytest

import modular_backtest.event_artifacts as event_artifacts
import modular_backtest.results as results_module
from modular_backtest.results import (
    ResultError,
    compare_results,
    inspect_result,
    verify_artifact_manifest,
    write_artifact_manifest,
)


def _result(
    path: Path,
    run_id: str | None,
    total_return: float,
    label: str = "same",
    *,
    enabled: bool = False,
    manifest_run_id: str | None = None,
) -> None:
    path.mkdir()
    manifest = {} if run_id is None else {"run_id": run_id}
    (path / "run.json").write_text(json.dumps({"manifest": manifest}), encoding="utf-8")
    (path / "metrics.json").write_text(
        json.dumps(
            {
                "total_return": total_return,
                "trades": 2,
                "label": label,
                "enabled": enabled,
            }
        ),
        encoding="utf-8",
    )
    write_artifact_manifest(path, manifest_run_id or run_id or path.name)


def test_inspect_and_compare(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    _result(left, "run-left", 0.1)
    _result(right, "run-right", 0.25, "changed", enabled=True)

    snapshot = inspect_result(left)
    assert snapshot.run_id == "run-left"
    assert snapshot.artifact_manifest["run_id"] == "run-left"

    comparison = compare_results(left, right)
    assert comparison["changes"]["total_return"]["delta"] == pytest.approx(0.15)
    assert comparison["changes"]["label"] == {"left": "same", "right": "changed"}
    assert comparison["changes"]["enabled"] == {"left": False, "right": True}
    assert "delta" not in comparison["changes"]["enabled"]


def test_missing_files_or_run_id_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ResultError, match="artifact-manifest.json"):
        inspect_result(tmp_path / "missing")

    missing_run_id = tmp_path / "missing-run-id"
    _result(missing_run_id, None, 0.0, manifest_run_id="run-expected")
    with pytest.raises(ResultError, match="manifest.run_id"):
        inspect_result(missing_run_id)


def test_manifest_run_id_must_match_run_json(tmp_path: Path) -> None:
    result = tmp_path / "mismatch"
    _result(result, "run-json", 0.0, manifest_run_id="run-manifest")

    with pytest.raises(ResultError, match="does not match"):
        inspect_result(result)


def test_tampered_or_extra_artifacts_fail_checksum_verification(tmp_path: Path) -> None:
    tampered = tmp_path / "tampered"
    _result(tampered, "run-tampered", 0.0)
    (tampered / "metrics.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ResultError, match="checksum mismatch"):
        inspect_result(tampered)

    extra = tmp_path / "extra"
    _result(extra, "run-extra", 0.0)
    (extra / "unexpected.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(ResultError, match="artifact set mismatch"):
        inspect_result(extra)


def test_nested_file_named_artifact_manifest_is_still_checksummed(tmp_path: Path) -> None:
    result = tmp_path / "nested"
    result.mkdir()
    (result / "run.json").write_text(
        json.dumps({"manifest": {"run_id": "run-nested"}}),
        encoding="utf-8",
    )
    (result / "metrics.json").write_text("{}", encoding="utf-8")
    nested = result / "nested"
    nested.mkdir()
    (nested / "artifact-manifest.json").write_text("nested evidence", encoding="utf-8")
    write_artifact_manifest(result, "run-nested")

    snapshot = inspect_result(result)

    assert "nested/artifact-manifest.json" in snapshot.artifact_manifest["files"]


def test_manifest_normalizes_oversized_gzip_into_verified_shards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = tmp_path / "sharded"
    result.mkdir()
    (result / "run.json").write_text(
        json.dumps({"manifest": {"run_id": "run-sharded"}}),
        encoding="utf-8",
    )
    (result / "metrics.json").write_text("{}", encoding="utf-8")
    canonical = b"".join(
        json.dumps(
            {
                "event_id": f"evt-{sequence:010d}",
                "sequence": sequence,
                "event_type": "feature.published",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
        for sequence in range(1, 201)
    )
    event_path = result / "events.jsonl.gz"
    with event_path.open("wb") as raw:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw,
            compresslevel=1,
            mtime=0,
        ) as handle:
            handle.write(canonical)
    compressed_bytes = event_path.stat().st_size
    (result / "journal.json").write_text(
        json.dumps(
            {
                "schema_version": "3",
                "event_stream": {
                    "path": "events.jsonl.gz",
                    "encoding": "gzip",
                    "count": 200,
                    "bytes": compressed_bytes,
                    "logical_bytes": len(canonical),
                    "semantic_sha256": "a" * 64,
                    "counts_by_type": {"feature.published": 200},
                    "retained_window": 200,
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (result / "environment.json").write_text(
        json.dumps({"artifact_schema_version": "3"}),
        encoding="utf-8",
    )

    real_sharder = event_artifacts.shard_large_gzip_event_stream

    def shard_with_test_limit(directory: str | Path) -> dict[str, object] | None:
        return real_sharder(
            directory,
            max_file_bytes=compressed_bytes,
            target_part_bytes=max(1, compressed_bytes // 3),
        )

    monkeypatch.setattr(results_module, "shard_large_gzip_event_stream", shard_with_test_limit)

    manifest = write_artifact_manifest(result, "run-sharded")
    files = manifest["files"]
    assert isinstance(files, dict)
    assert "events.jsonl.gz" not in files
    assert event_artifacts.SHARDED_EVENT_MANIFEST_PATH in files
    part_names = sorted(name for name in files if name.startswith("events.jsonl.gz.part-"))
    assert len(part_names) >= 2
    assert not event_path.exists()
    assert verify_artifact_manifest(result)["files"] == files
    assert b"".join((result / name).read_bytes() for name in part_names)

    journal = json.loads((result / "journal.json").read_text(encoding="utf-8"))
    environment = json.loads((result / "environment.json").read_text(encoding="utf-8"))
    assert journal["schema_version"] == "4"
    assert environment["artifact_schema_version"] == "4"


def test_manifest_rejects_unsafe_paths_and_invalid_hashes(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe"
    _result(unsafe, "run-unsafe", 0.0)
    unsafe_manifest_path = unsafe / "artifact-manifest.json"
    unsafe_manifest = json.loads(unsafe_manifest_path.read_text(encoding="utf-8"))
    unsafe_manifest["files"] = {"../escape": "0" * 64}
    unsafe_manifest_path.write_text(json.dumps(unsafe_manifest), encoding="utf-8")
    with pytest.raises(ResultError, match="unsafe path"):
        inspect_result(unsafe)

    invalid_hash = tmp_path / "invalid-hash"
    _result(invalid_hash, "run-invalid-hash", 0.0)
    hash_manifest_path = invalid_hash / "artifact-manifest.json"
    hash_manifest = json.loads(hash_manifest_path.read_text(encoding="utf-8"))
    hash_manifest["files"]["run.json"] = "x" * 64
    hash_manifest_path.write_text(json.dumps(hash_manifest), encoding="utf-8")
    with pytest.raises(ResultError, match="invalid SHA-256"):
        inspect_result(invalid_hash)
