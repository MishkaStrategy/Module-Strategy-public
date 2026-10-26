from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
from collections.abc import Buffer, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO, TextIO


LEGACY_PLAIN_EVENT_PATH = "events.jsonl"
LEGACY_GZIP_EVENT_PATH = "events.jsonl.gz"
SHARDED_EVENT_MANIFEST_PATH = "events.manifest.json"
SHARDED_EVENT_ENCODING = "gzip-byte-shards-v1"
SHARDED_EVENT_SCHEMA_VERSION = "1"
SHARDED_EVENT_SPLIT_STRATEGY = "tail-copy-truncate-v1"
SHARDED_JOURNAL_SCHEMA_VERSION = "4"
REGISTRY_MAX_FILE_BYTES = 16 * 1024**3
DEFAULT_EVENT_PART_BYTES = 8 * 1024**3
_COPY_CHUNK_BYTES = 4 * 1024**2
_HEX = frozenset("0123456789abcdef")


class EventArtifactError(ValueError):
    """Raised when an event artifact layout cannot be proven safely."""


def _positive_int(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise EventArtifactError(f"{label} must be a positive integer")
    return value


def _lower_sha256(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        raise EventArtifactError(f"{label} must be a lowercase SHA-256")
    return value


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise EventArtifactError(f"{label} is missing or symlinked")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EventArtifactError(f"{label} is unreadable or invalid JSON") from exc
    if not isinstance(value, dict):
        raise EventArtifactError(f"{label} must contain a JSON object")
    return value


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _part_name(index: int) -> str:
    return f"events.jsonl.gz.part-{index:06d}"


def _file_digest(path: Path) -> tuple[str, int]:
    if path.is_symlink() or not path.is_file():
        raise EventArtifactError(f"artifact is not a regular file: {path.name}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_COPY_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _read_checksum_scope(root: Path) -> tuple[str, ...] | None:
    checksum_path = root / "SHA256SUMS.txt"
    if not checksum_path.exists() and not checksum_path.is_symlink():
        return None
    if checksum_path.is_symlink() or not checksum_path.is_file():
        raise EventArtifactError("SHA256SUMS.txt must be a regular file")
    try:
        lines = checksum_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise EventArtifactError("SHA256SUMS.txt is unreadable") from exc
    if not lines:
        raise EventArtifactError("SHA256SUMS.txt must not be empty")

    names: list[str] = []
    seen: set[str] = set()
    for line in lines:
        digest, separator, name = line.partition("  ")
        if not separator or not name or "  " in name:
            raise EventArtifactError("SHA256SUMS.txt contains an invalid row")
        _lower_sha256(digest, label=f"SHA256SUMS.txt digest for {name}")
        unsafe = (
            "/" in name
            or "\\" in name
            or Path(name).is_absolute()
            or Path(name).name != name
            or name in {".", "..", "SHA256SUMS.txt"}
        )
        if unsafe:
            raise EventArtifactError("SHA256SUMS.txt contains an unsafe artifact name")
        if name in seen:
            raise EventArtifactError("SHA256SUMS.txt contains a duplicate artifact")
        seen.add(name)
        actual_digest, _ = _file_digest(root / name)
        if actual_digest != digest:
            raise EventArtifactError(f"SHA256SUMS.txt checksum mismatch: {name}")
        names.append(name)

    if names != sorted(names):
        raise EventArtifactError("SHA256SUMS.txt artifact order is not canonical")
    if LEGACY_GZIP_EVENT_PATH not in seen:
        raise EventArtifactError("SHA256SUMS.txt does not cover events.jsonl.gz")
    return tuple(names)


def _rewrite_schema_identity(path: Path, *, label: str) -> None:
    if not path.exists() and not path.is_symlink():
        return
    payload = _read_json_object(path, label=label)
    if payload.get("artifact_schema_version") != "3":
        raise EventArtifactError(f"{label} artifact schema must match compressed journal v3")
    payload["artifact_schema_version"] = SHARDED_JOURNAL_SCHEMA_VERSION
    temporary = path.with_name(f".{path.name}.shard-tmp")
    if temporary.exists() or temporary.is_symlink():
        raise EventArtifactError(f"{label} sharding temporary path already exists")
    _write_json(temporary, payload)
    temporary.replace(path)


def _rewrite_sha256s(
    root: Path,
    checksum_scope: Sequence[str] | None,
    part_records: Sequence[Mapping[str, object]],
) -> None:
    if checksum_scope is None:
        return
    checksum_path = root / "SHA256SUMS.txt"
    if checksum_path.is_symlink() or not checksum_path.is_file():
        raise EventArtifactError("SHA256SUMS.txt changed during event sharding")

    names = set(checksum_scope)
    names.remove(LEGACY_GZIP_EVENT_PATH)
    names.add(SHARDED_EVENT_MANIFEST_PATH)
    for expected_index, record in enumerate(part_records, start=1):
        path = record.get("path")
        if path != _part_name(expected_index):
            raise EventArtifactError("event shard checksum inventory is invalid")
        names.add(str(path))

    rows: list[str] = []
    for name in sorted(names):
        digest, _ = _file_digest(root / name)
        rows.append(f"{digest}  {name}")
    checksum_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    with checksum_path.open("rb") as handle:
        os.fsync(handle.fileno())


def _copy_tail_part(
    source: Path,
    temporary: Path,
    *,
    offset: int,
    size: int,
) -> tuple[str, int]:
    if offset < 0 or size <= 0:
        raise EventArtifactError("event shard range is invalid")
    digest = hashlib.sha256()
    copied = 0
    with source.open("rb") as input_handle, temporary.open("xb") as output_handle:
        input_handle.seek(offset)
        while copied < size:
            chunk = input_handle.read(min(_COPY_CHUNK_BYTES, size - copied))
            if not chunk:
                raise EventArtifactError("compressed event stream ended before declared size")
            output_handle.write(chunk)
            digest.update(chunk)
            copied += len(chunk)
        output_handle.flush()
        os.fsync(output_handle.fileno())
    if copied != size:
        raise EventArtifactError("event shard copy size changed")
    return digest.hexdigest(), copied


def _truncate_source(source: Path, size: int) -> None:
    if size <= 0:
        raise EventArtifactError("event shard truncation size must be positive")
    with source.open("r+b") as handle:
        handle.truncate(size)
        handle.flush()
        os.fsync(handle.fileno())
    if source.stat().st_size != size:
        raise EventArtifactError("compressed event stream truncation failed")


def shard_large_gzip_event_stream(
    result_directory: str | Path,
    *,
    max_file_bytes: int = REGISTRY_MAX_FILE_BYTES,
    target_part_bytes: int = DEFAULT_EVENT_PART_BYTES,
) -> dict[str, object] | None:
    """Replace one oversized gzip event file with deterministic bounded byte shards.

    Shards are byte ranges of the original gzip stream, not independently recompressed
    streams. Concatenating the ordered parts reproduces the original gzip file exactly,
    so decoded JSONL bytes and runtime semantic identity are unchanged. Small streams
    retain the legacy single-file representation.

    Oversized streams are split from the tail. Each tail range is copied and fsynced
    before the source inode is truncated by the same range. Publication therefore needs
    at most one ``target_part_bytes`` temporary data allocation rather than a second
    complete event stream. This remains true when ``events.jsonl.gz`` is a hardlink to
    the runtime spool: truncation shrinks the shared source inode after every committed
    tail part.
    """

    max_bytes = _positive_int(max_file_bytes, label="max_file_bytes")
    target_bytes = _positive_int(target_part_bytes, label="target_part_bytes")
    if target_bytes >= max_bytes:
        raise EventArtifactError("target_part_bytes must be strictly below max_file_bytes")

    root = Path(result_directory)
    if root.is_symlink() or not root.is_dir():
        raise EventArtifactError("result directory is missing or symlinked")

    journal_path = root / "journal.json"
    journal = _read_json_object(journal_path, label="journal.json")
    stream = journal.get("event_stream")
    if not isinstance(stream, dict):
        raise EventArtifactError("journal event_stream must be an object")
    if journal.get("schema_version") != "3":
        raise EventArtifactError("gzip sharding requires compressed journal schema v3")
    if stream.get("path") != LEGACY_GZIP_EVENT_PATH or stream.get("encoding") != "gzip":
        raise EventArtifactError("gzip sharding requires the legacy events.jsonl.gz layout")

    source = root / LEGACY_GZIP_EVENT_PATH
    if source.is_symlink() or not source.is_file():
        raise EventArtifactError("legacy compressed event stream is missing or symlinked")
    source_size = source.stat().st_size
    declared_size = _positive_int(stream.get("bytes"), label="journal event bytes")
    if declared_size != source_size:
        raise EventArtifactError("journal event bytes do not match events.jsonl.gz")
    if source_size < max_bytes:
        return None

    manifest_path = root / SHARDED_EVENT_MANIFEST_PATH
    if manifest_path.exists() or manifest_path.is_symlink():
        raise EventArtifactError("sharded event manifest already exists")
    stale = sorted(root.glob("events.jsonl.gz.part-*"))
    if stale:
        raise EventArtifactError("stale event shard files already exist")

    checksum_scope = _read_checksum_scope(root)
    event_count = _positive_int(stream.get("count"), label="journal event count")
    logical_bytes = _positive_int(stream.get("logical_bytes"), label="journal logical bytes")
    semantic_sha256 = _lower_sha256(stream.get("semantic_sha256"), label="journal semantic_sha256")
    compressed_sha256, verified_size = _file_digest(source)
    if verified_size != source_size:
        raise EventArtifactError("compressed event stream changed before sharding")

    part_count = (source_size + target_bytes - 1) // target_bytes
    if part_count < 2:
        raise EventArtifactError("oversized event stream did not produce multiple shards")

    records_by_index: dict[int, dict[str, object]] = {}
    temporary_paths: list[Path] = []
    try:
        for index in range(part_count, 1, -1):
            expected_current_size = min(source_size, index * target_bytes)
            current_size = source.stat().st_size
            if current_size != expected_current_size:
                raise EventArtifactError("compressed event stream changed during tail split")
            offset = (index - 1) * target_bytes
            part_size = current_size - offset
            if part_size <= 0 or part_size > target_bytes or part_size >= max_bytes:
                raise EventArtifactError("event shard is empty or exceeds the per-file limit")

            name = _part_name(index)
            final_path = root / name
            temporary = root / f".{name}.tmp"
            if (
                final_path.exists()
                or final_path.is_symlink()
                or temporary.exists()
                or temporary.is_symlink()
            ):
                raise EventArtifactError(f"event shard destination already exists: {name}")
            temporary_paths.append(temporary)
            digest, copied = _copy_tail_part(
                source,
                temporary,
                offset=offset,
                size=part_size,
            )
            temporary.replace(final_path)
            temporary_paths.remove(temporary)
            actual_digest, actual_size = _file_digest(final_path)
            if actual_digest != digest or actual_size != copied:
                raise EventArtifactError(f"event shard failed post-copy verification: {name}")

            _truncate_source(source, offset)
            records_by_index[index] = {
                "index": index,
                "path": name,
                "bytes": actual_size,
                "sha256": actual_digest,
            }

        first_name = _part_name(1)
        first_path = root / first_name
        if first_path.exists() or first_path.is_symlink():
            raise EventArtifactError(f"event shard destination already exists: {first_name}")
        first_digest, first_size = _file_digest(source)
        if first_size <= 0 or first_size > target_bytes or first_size >= max_bytes:
            raise EventArtifactError("first event shard is empty or exceeds the per-file limit")
        source.replace(first_path)
        records_by_index[1] = {
            "index": 1,
            "path": first_name,
            "bytes": first_size,
            "sha256": first_digest,
        }

        part_records = [records_by_index[index] for index in range(1, part_count + 1)]
        sharded_bytes = sum(
            _positive_int(part.get("bytes"), label="event shard bytes") for part in part_records
        )
        if sharded_bytes != source_size:
            raise EventArtifactError("event shard bytes do not reconstruct the source stream")

        manifest: dict[str, object] = {
            "schema_version": SHARDED_EVENT_SCHEMA_VERSION,
            "encoding": SHARDED_EVENT_ENCODING,
            "split_strategy": SHARDED_EVENT_SPLIT_STRATEGY,
            "publication_extra_bytes_bound": target_bytes,
            "source_encoding": "gzip",
            "source_path": LEGACY_GZIP_EVENT_PATH,
            "compressed_bytes": source_size,
            "compressed_sha256": compressed_sha256,
            "logical_bytes": logical_bytes,
            "event_count": event_count,
            "semantic_sha256": semantic_sha256,
            "parts": part_records,
        }
        _write_json(manifest_path, manifest)

        updated_stream = dict(stream)
        updated_stream.update(
            {
                "path": SHARDED_EVENT_MANIFEST_PATH,
                "encoding": SHARDED_EVENT_ENCODING,
                "bytes": source_size,
                "part_count": len(part_records),
                "compressed_sha256": compressed_sha256,
            }
        )
        updated_journal = dict(journal)
        updated_journal["schema_version"] = SHARDED_JOURNAL_SCHEMA_VERSION
        updated_journal["event_stream"] = updated_stream
        journal_tmp = root / ".journal.json.shard-tmp"
        if journal_tmp.exists() or journal_tmp.is_symlink():
            raise EventArtifactError("journal sharding temporary path already exists")
        _write_json(journal_tmp, updated_journal)
        journal_tmp.replace(journal_path)
        _rewrite_schema_identity(root / "environment.json", label="environment.json")
        _rewrite_schema_identity(root / "application-run.json", label="application-run.json")

        _rewrite_sha256s(root, checksum_scope, part_records)
        validate_sharded_event_stream(root, max_file_bytes=max_bytes)
        return manifest
    except Exception:
        for path in temporary_paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise


def validate_sharded_event_stream(
    result_directory: str | Path,
    *,
    max_file_bytes: int = REGISTRY_MAX_FILE_BYTES,
) -> dict[str, object]:
    """Validate the exact ordered shard inventory and aggregate compressed identity."""

    max_bytes = _positive_int(max_file_bytes, label="max_file_bytes")
    root = Path(result_directory)
    if root.is_symlink() or not root.is_dir():
        raise EventArtifactError("result directory is missing or symlinked")
    manifest = _read_json_object(
        root / SHARDED_EVENT_MANIFEST_PATH,
        label="events.manifest.json",
    )
    if manifest.get("schema_version") != SHARDED_EVENT_SCHEMA_VERSION:
        raise EventArtifactError("unsupported sharded event manifest schema")
    if (
        manifest.get("encoding") != SHARDED_EVENT_ENCODING
        or manifest.get("source_encoding") != "gzip"
    ):
        raise EventArtifactError("unsupported sharded event encoding")
    if manifest.get("split_strategy") != SHARDED_EVENT_SPLIT_STRATEGY:
        raise EventArtifactError("unsupported sharded event split strategy")
    publication_extra_bytes_bound = _positive_int(
        manifest.get("publication_extra_bytes_bound"),
        label="publication_extra_bytes_bound",
    )
    if publication_extra_bytes_bound >= max_bytes:
        raise EventArtifactError("publication extra-byte bound must retain Registry headroom")
    if manifest.get("source_path") != LEGACY_GZIP_EVENT_PATH:
        raise EventArtifactError("sharded event source_path is invalid")

    declared_total = _positive_int(manifest.get("compressed_bytes"), label="compressed_bytes")
    logical_bytes = _positive_int(manifest.get("logical_bytes"), label="logical_bytes")
    event_count = _positive_int(manifest.get("event_count"), label="event_count")
    declared_aggregate = _lower_sha256(manifest.get("compressed_sha256"), label="compressed_sha256")
    semantic_sha256 = _lower_sha256(manifest.get("semantic_sha256"), label="semantic_sha256")

    raw_parts = manifest.get("parts")
    if not isinstance(raw_parts, list) or len(raw_parts) < 2:
        raise EventArtifactError("sharded event manifest requires at least two parts")

    expected_names: list[str] = []
    aggregate = hashlib.sha256()
    total = 0
    for expected_index, item in enumerate(raw_parts, start=1):
        if not isinstance(item, dict):
            raise EventArtifactError("event shard manifest part must be an object")
        if item.get("index") != expected_index:
            raise EventArtifactError("event shard indices are not contiguous and ordered")
        expected_name = _part_name(expected_index)
        if item.get("path") != expected_name:
            raise EventArtifactError("event shard path is not deterministic")
        declared_size = _positive_int(item.get("bytes"), label=f"{expected_name} bytes")
        if declared_size > publication_extra_bytes_bound:
            raise EventArtifactError("event shard exceeds the publication extra-byte bound")
        if declared_size >= max_bytes:
            raise EventArtifactError("event shard exceeds the per-file limit")
        declared_digest = _lower_sha256(item.get("sha256"), label=f"{expected_name} sha256")
        path = root / expected_name
        actual_digest = hashlib.sha256()
        actual_size = 0
        if path.is_symlink() or not path.is_file():
            raise EventArtifactError(f"event shard is missing or symlinked: {expected_name}")
        with path.open("rb") as handle:
            while chunk := handle.read(_COPY_CHUNK_BYTES):
                actual_digest.update(chunk)
                aggregate.update(chunk)
                actual_size += len(chunk)
        if actual_size != declared_size or actual_digest.hexdigest() != declared_digest:
            raise EventArtifactError(f"event shard failed size/SHA verification: {expected_name}")
        expected_names.append(expected_name)
        total += actual_size

    actual_names = sorted(
        path.name
        for path in root.glob("events.jsonl.gz.part-*")
        if path.exists() or path.is_symlink()
    )
    if actual_names != expected_names:
        raise EventArtifactError(
            f"event shard inventory mismatch; expected={expected_names}, actual={actual_names}"
        )
    if total != declared_total:
        raise EventArtifactError("event shard total bytes do not match manifest")
    if aggregate.hexdigest() != declared_aggregate:
        raise EventArtifactError("event shard aggregate compressed SHA-256 mismatch")
    if (root / LEGACY_GZIP_EVENT_PATH).exists() or (root / LEGACY_GZIP_EVENT_PATH).is_symlink():
        raise EventArtifactError("sharded layout must not retain legacy events.jsonl.gz")
    if (root / LEGACY_PLAIN_EVENT_PATH).exists() or (root / LEGACY_PLAIN_EVENT_PATH).is_symlink():
        raise EventArtifactError("sharded layout is ambiguous with legacy events.jsonl")

    journal = _read_json_object(root / "journal.json", label="journal.json")
    stream = journal.get("event_stream")
    if journal.get("schema_version") != SHARDED_JOURNAL_SCHEMA_VERSION or not isinstance(
        stream, dict
    ):
        raise EventArtifactError("sharded event stream requires journal schema v4")
    expected_pairs = (
        (stream.get("path"), SHARDED_EVENT_MANIFEST_PATH, "path"),
        (stream.get("encoding"), SHARDED_EVENT_ENCODING, "encoding"),
        (stream.get("bytes"), declared_total, "compressed bytes"),
        (stream.get("logical_bytes"), logical_bytes, "logical bytes"),
        (stream.get("count"), event_count, "event count"),
        (stream.get("semantic_sha256"), semantic_sha256, "semantic SHA-256"),
        (stream.get("compressed_sha256"), declared_aggregate, "compressed SHA-256"),
        (stream.get("part_count"), len(raw_parts), "part count"),
    )
    for actual, expected, label in expected_pairs:
        if actual != expected:
            raise EventArtifactError(f"journal sharded event {label} does not match manifest")

    for name in ("environment.json", "application-run.json"):
        path = root / name
        if path.exists() or path.is_symlink():
            payload = _read_json_object(path, label=name)
            if payload.get("artifact_schema_version") != SHARDED_JOURNAL_SCHEMA_VERSION:
                raise EventArtifactError(f"{name} artifact schema does not match journal schema v4")
    return manifest


class _ConcatenatedParts(io.RawIOBase):
    def __init__(self, root: Path, names: list[str]) -> None:
        super().__init__()
        self._root = root
        self._names = names
        self._index = 0
        self._handle: BinaryIO | None = None

    def readable(self) -> bool:
        return True

    def _advance(self) -> bool:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        if self._index >= len(self._names):
            return False
        self._handle = (self._root / self._names[self._index]).open("rb")
        self._index += 1
        return True

    def readinto(self, buffer: Buffer) -> int:
        view = memoryview(buffer).cast("B")
        total = 0
        while total < len(view):
            if self._handle is None and not self._advance():
                break
            assert self._handle is not None
            chunk = self._handle.read(len(view) - total)
            if not chunk:
                self._advance()
                continue
            end = total + len(chunk)
            view[total:end] = chunk
            total = end
        return total

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        super().close()


def open_event_stream_text(result_directory: str | Path) -> TextIO:
    """Open legacy or sharded exported events as one ordered decoded text stream."""

    root = Path(result_directory)
    if root.is_symlink() or not root.is_dir():
        raise EventArtifactError("result directory is missing or symlinked")
    plain = root / LEGACY_PLAIN_EVENT_PATH
    gzip_path = root / LEGACY_GZIP_EVENT_PATH
    manifest_path = root / SHARDED_EVENT_MANIFEST_PATH
    present = [
        path for path in (plain, gzip_path, manifest_path) if path.exists() or path.is_symlink()
    ]
    if len(present) != 1:
        raise EventArtifactError(
            "event output must contain exactly one legacy stream or shard manifest"
        )
    selected = present[0]
    if selected.is_symlink() or not selected.is_file():
        raise EventArtifactError("event stream selector must be a regular file")
    if selected == plain:
        return plain.open("r", encoding="utf-8")
    if selected == gzip_path:
        return gzip.open(gzip_path, "rt", encoding="utf-8")

    manifest = validate_sharded_event_stream(root)
    raw_parts = manifest["parts"]
    assert isinstance(raw_parts, list)
    names = [str(item["path"]) for item in raw_parts if isinstance(item, dict)]
    raw = _ConcatenatedParts(root, names)
    buffered = io.BufferedReader(raw, buffer_size=_COPY_CHUNK_BYTES)
    compressed = gzip.GzipFile(fileobj=buffered, mode="rb")
    return io.TextIOWrapper(compressed, encoding="utf-8")


def iter_event_stream_lines(result_directory: str | Path) -> Iterator[str]:
    with open_event_stream_text(result_directory) as handle:
        yield from handle
