from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from modular_backtest.event_artifacts import (
    EventArtifactError,
    SHARDED_EVENT_ENCODING,
    SHARDED_EVENT_MANIFEST_PATH,
    SHARDED_EVENT_SPLIT_STRATEGY,
    validate_sharded_event_stream,
)


REGISTRY_EVENT_FILE_LIMIT_BYTES = 16 * 1024**3


class StorageEvidenceError(ValueError):
    """Raised when physical event-storage evidence cannot be proven safely."""


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise StorageEvidenceError(f"{label} is missing or symlinked")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageEvidenceError(f"{label} is unreadable or invalid JSON") from exc
    if not isinstance(payload, dict):
        raise StorageEvidenceError(f"{label} must be a JSON object")
    return payload


def _positive_int(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise StorageEvidenceError(f"{label} must be a positive integer")
    return value


def _semantic_sha256(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise StorageEvidenceError("event semantic SHA-256 is invalid")
    if any(character not in "0123456789abcdef" for character in value):
        raise StorageEvidenceError("event semantic SHA-256 must be lowercase hex")
    return value


def _resolve_full_result_dir(result_dir: Path) -> Path:
    if not result_dir.is_dir() or result_dir.is_symlink():
        raise StorageEvidenceError("full result directory is missing or symlinked")

    direct_artifacts = (
        result_dir / "journal.json",
        result_dir / "events.jsonl.gz",
        result_dir / SHARDED_EVENT_MANIFEST_PATH,
    )
    if any(path.exists() or path.is_symlink() for path in direct_artifacts):
        return result_dir

    entries = list(result_dir.iterdir())
    if any(entry.is_symlink() for entry in entries):
        raise StorageEvidenceError("full result collection contains a symlinked child")

    candidates = [entry for entry in entries if entry.is_dir()]
    if len(candidates) != 1:
        raise StorageEvidenceError(
            f"full result collection must contain exactly one run directory, found {len(candidates)}"
        )

    collection_root = result_dir.resolve(strict=True)
    resolved = candidates[0].resolve(strict=True)
    if resolved.parent != collection_root:
        raise StorageEvidenceError("full result run directory escaped collection root")
    return resolved


def _validate_counts(stream: dict[str, Any], event_count: int) -> None:
    counts_by_type = stream.get("counts_by_type")
    if not isinstance(counts_by_type, dict) or not counts_by_type:
        raise StorageEvidenceError("event counts_by_type must be a non-empty object")
    counted_events = 0
    for event_type, count in counts_by_type.items():
        if not isinstance(event_type, str) or not event_type:
            raise StorageEvidenceError("event counts_by_type contains an invalid event type")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise StorageEvidenceError("event counts_by_type contains an invalid count")
        counted_events += count
    if counted_events != event_count:
        raise StorageEvidenceError("event counts_by_type does not match total event count")


def _allocated_bytes(path: Path) -> int:
    stat = path.stat()
    blocks = getattr(stat, "st_blocks", None)
    if isinstance(blocks, int) and blocks >= 0:
        return int(blocks) * 512
    return int(stat.st_size)


def build_event_storage_evidence(
    *,
    result_dir: Path,
    pretest_report: Path,
    disk_root: Path,
) -> dict[str, object]:
    result_dir = _resolve_full_result_dir(result_dir)
    if not disk_root.is_dir() or disk_root.is_symlink():
        raise StorageEvidenceError("disk measurement root is missing or symlinked")

    journal = _read_json_object(result_dir / "journal.json", label="full journal")
    schema_version = journal.get("schema_version")
    if schema_version not in {"3", "4"}:
        raise StorageEvidenceError("full journal must use compressed artifact schema v3 or v4")
    stream = journal.get("event_stream")
    if not isinstance(stream, dict):
        raise StorageEvidenceError("full journal event_stream must be an object")

    event_count = _positive_int(stream.get("count"), label="event count")
    physical_bytes = _positive_int(stream.get("bytes"), label="event physical bytes")
    logical_bytes = _positive_int(stream.get("logical_bytes"), label="event logical bytes")
    if logical_bytes < physical_bytes:
        raise StorageEvidenceError("event logical bytes cannot be smaller than physical bytes")
    semantic_sha256 = _semantic_sha256(stream.get("semantic_sha256"))
    _validate_counts(stream, event_count)

    disk_stat = disk_root.stat()
    allocated_bytes = 0
    event_paths: list[Path]
    stream_path: str
    stream_encoding: str
    compressed_sha256: str | None = None
    publication_split_strategy: str | None = None
    publication_extra_bytes_bound: int | None = None
    part_count = 1
    max_part_bytes = physical_bytes

    if schema_version == "3":
        if stream.get("path") != "events.jsonl.gz" or stream.get("encoding") != "gzip":
            raise StorageEvidenceError(
                "compressed schema v3 event stream must be events.jsonl.gz with gzip encoding"
            )
        event_path = result_dir / "events.jsonl.gz"
        if not event_path.is_file() or event_path.is_symlink():
            raise StorageEvidenceError("compressed full event stream is missing or symlinked")
        event_stat = event_path.stat()
        if event_stat.st_size != physical_bytes:
            raise StorageEvidenceError(
                "journal physical bytes do not match compressed event file size"
            )
        with event_path.open("rb") as handle:
            if handle.read(2) != b"\x1f\x8b":
                raise StorageEvidenceError("compressed full event stream does not have gzip magic")
        if physical_bytes >= REGISTRY_EVENT_FILE_LIMIT_BYTES:
            raise StorageEvidenceError(
                "compressed event stream exceeds the Registry 16 GiB file limit"
            )
        event_paths = [event_path]
        allocated_bytes = _allocated_bytes(event_path)
        stream_path = "events.jsonl.gz"
        stream_encoding = "gzip"
    else:
        if (
            stream.get("path") != SHARDED_EVENT_MANIFEST_PATH
            or stream.get("encoding") != SHARDED_EVENT_ENCODING
        ):
            raise StorageEvidenceError(
                "compressed schema v4 event stream must use the sharded event manifest"
            )
        try:
            shard_manifest = validate_sharded_event_stream(
                result_dir,
                max_file_bytes=REGISTRY_EVENT_FILE_LIMIT_BYTES,
            )
        except EventArtifactError as exc:
            raise StorageEvidenceError("sharded event stream failed artifact validation") from exc
        if shard_manifest.get("compressed_bytes") != physical_bytes:
            raise StorageEvidenceError("journal physical bytes do not match shard manifest")
        if shard_manifest.get("logical_bytes") != logical_bytes:
            raise StorageEvidenceError("journal logical bytes do not match shard manifest")
        if shard_manifest.get("event_count") != event_count:
            raise StorageEvidenceError("journal event count does not match shard manifest")
        if shard_manifest.get("semantic_sha256") != semantic_sha256:
            raise StorageEvidenceError("journal semantic SHA-256 does not match shard manifest")
        compressed_sha256 = shard_manifest.get("compressed_sha256")
        if not isinstance(compressed_sha256, str) or len(compressed_sha256) != 64:
            raise StorageEvidenceError("shard aggregate compressed SHA-256 is invalid")
        publication_split_strategy = shard_manifest.get("split_strategy")
        if publication_split_strategy != SHARDED_EVENT_SPLIT_STRATEGY:
            raise StorageEvidenceError("sharded event publication split strategy is invalid")
        publication_extra_bytes_bound = _positive_int(
            shard_manifest.get("publication_extra_bytes_bound"),
            label="publication extra-byte bound",
        )
        if publication_extra_bytes_bound >= REGISTRY_EVENT_FILE_LIMIT_BYTES:
            raise StorageEvidenceError(
                "publication extra-byte bound does not retain Registry headroom"
            )
        raw_parts = shard_manifest.get("parts")
        if not isinstance(raw_parts, list) or len(raw_parts) < 2:
            raise StorageEvidenceError("sharded event stream requires multiple parts")
        event_paths = []
        part_sizes: list[int] = []
        for item in raw_parts:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise StorageEvidenceError("sharded event part inventory is invalid")
            part = result_dir / item["path"]
            if part.is_symlink() or not part.is_file():
                raise StorageEvidenceError("sharded event part is missing or symlinked")
            size = part.stat().st_size
            if size > publication_extra_bytes_bound:
                raise StorageEvidenceError("event shard exceeds publication extra-byte bound")
            if size >= REGISTRY_EVENT_FILE_LIMIT_BYTES:
                raise StorageEvidenceError("event shard exceeds the Registry 16 GiB file limit")
            event_paths.append(part)
            part_sizes.append(size)
            allocated_bytes += _allocated_bytes(part)
        if sum(part_sizes) != physical_bytes:
            raise StorageEvidenceError("event shard physical bytes do not match journal total")
        with event_paths[0].open("rb") as handle:
            if handle.read(2) != b"\x1f\x8b":
                raise StorageEvidenceError("sharded compressed event stream lacks gzip magic")
        part_count = len(event_paths)
        max_part_bytes = max(part_sizes)
        stream_path = SHARDED_EVENT_MANIFEST_PATH
        stream_encoding = SHARDED_EVENT_ENCODING

    for event_path in event_paths:
        if event_path.stat().st_dev != disk_stat.st_dev:
            raise StorageEvidenceError(
                "event stream and result-filesystem measurement root are on different filesystems"
            )

    disk_usage = shutil.disk_usage(disk_root)
    post_full_free_bytes = int(disk_usage.free)
    if post_full_free_bytes <= 0:
        raise StorageEvidenceError("post-full result filesystem free space is not positive")

    pretest = _read_json_object(pretest_report, label="full-suite pretest report")
    disk = pretest.get("disk")
    if not isinstance(disk, dict):
        raise StorageEvidenceError("full-suite pretest report disk evidence is missing")
    pretest_dataset_free_gib = disk.get("free_gib")
    if (
        not isinstance(pretest_dataset_free_gib, (int, float))
        or isinstance(pretest_dataset_free_gib, bool)
        or pretest_dataset_free_gib <= 0
    ):
        raise StorageEvidenceError("pretest dataset free_gib is invalid")

    registry_headroom = REGISTRY_EVENT_FILE_LIMIT_BYTES - max_part_bytes
    post_full_free_gib = post_full_free_bytes / 1024**3
    compression_ratio = logical_bytes / physical_bytes

    event_payload: dict[str, object] = {
        "path": stream_path,
        "encoding": stream_encoding,
        "count": event_count,
        "physical_bytes": physical_bytes,
        "allocated_bytes": allocated_bytes,
        "logical_bytes": logical_bytes,
        "compression_ratio": round(compression_ratio, 6),
        "semantic_sha256": semantic_sha256,
        "part_count": part_count,
        "max_part_bytes": max_part_bytes,
    }
    if compressed_sha256 is not None:
        event_payload["compressed_sha256"] = compressed_sha256
    if publication_split_strategy is not None:
        event_payload["publication_split_strategy"] = publication_split_strategy
    if publication_extra_bytes_bound is not None:
        event_payload["publication_extra_bytes_bound"] = publication_extra_bytes_bound

    return {
        "schema_version": 1,
        "status": "PASS",
        "event_stream": event_payload,
        "registry_file_budget": {
            "limit_bytes": REGISTRY_EVENT_FILE_LIMIT_BYTES,
            "headroom_bytes": registry_headroom,
            "within_limit": True,
        },
        "disk_headroom": {
            "measurement_scope": "result_filesystem",
            "post_full_free_bytes": post_full_free_bytes,
            "post_full_free_gib": round(post_full_free_gib, 3),
            "event_stream_on_measured_filesystem": True,
            "pretest_dataset_free_gib": float(pretest_dataset_free_gib),
            "pretest_dataset_same_filesystem_not_claimed": True,
        },
        "paper_only": True,
        "live_trading_authorized": False,
        "raw_market_data_uploaded": False,
    }


def write_event_storage_evidence(
    *,
    result_dir: Path,
    pretest_report: Path,
    disk_root: Path,
    output: Path,
) -> dict[str, object]:
    if output.exists() or output.is_symlink():
        raise StorageEvidenceError("event-storage evidence output must not already exist")
    if not output.parent.is_dir() or output.parent.is_symlink():
        raise StorageEvidenceError("event-storage evidence parent is missing or symlinked")
    payload = build_event_storage_evidence(
        result_dir=result_dir,
        pretest_report=pretest_report,
        disk_root=disk_root,
    )
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write sanitized physical event-stream storage/headroom evidence."
    )
    parser.add_argument("--result-dir", type=Path, required=True)
    parser.add_argument("--pretest-report", type=Path, required=True)
    parser.add_argument("--disk-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        write_event_storage_evidence(
            result_dir=args.result_dir,
            pretest_report=args.pretest_report,
            disk_root=args.disk_root,
            output=args.output,
        )
    except (OSError, StorageEvidenceError) as exc:
        print(f"physical event-storage evidence failed: {exc}")
        return 1
    print("physical event-storage evidence PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
