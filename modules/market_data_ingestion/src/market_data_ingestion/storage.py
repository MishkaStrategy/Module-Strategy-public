from __future__ import annotations

import csv
import errno
import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from itertools import chain
from pathlib import Path, PurePosixPath

from .canonical import primitive, sha256_canonical, sha256_file
from .contracts import (
    DownloadChunk,
    InstrumentReference,
    NormalizedPartitionDescriptor,
    RawArtifactDescriptor,
    validate_source_uri,
)
from .enums import DataType, SourceKind
from .errors import StorageError
from .records import (
    KlineRecord,
    NormalizedRecord,
    normalized_record_fieldnames,
    normalized_record_from_row,
)
from .serialization import json_bytes, read_json
from .timeframes import timeframe_delta


def _safe_segment(value: str, name: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise StorageError("UNSAFE_PATH", f"unsafe {name}", False, "storage")
    if PurePosixPath(value).name != value:
        raise StorageError("UNSAFE_PATH", f"unsafe {name}", False, "storage")
    return value


class LocalStorage:
    """Local immutable raw/normalized/derived storage with atomic publication."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o750)
        for layer in (
            "raw",
            "normalized",
            "derived",
            "manifests",
            "checkpoints",
            "runs",
            "cancellations",
            "quarantine",
            "repair_audits",
            "staging",
            "locks",
        ):
            (self.root / layer).mkdir(parents=True, exist_ok=True, mode=0o750)

    def _under_root(self, path: Path) -> Path:
        candidate = path if path.is_absolute() else self.root / path
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as exc:
            raise StorageError(
                "PATH_TRAVERSAL",
                "storage path escapes configured root",
                False,
                "storage",
            ) from exc
        cursor = self.root
        for segment in relative.parts[:-1]:
            cursor = cursor / segment
            if cursor.exists() and cursor.is_symlink():
                raise StorageError(
                    "SYMLINK_PATH",
                    "storage path contains a symbolic-link directory",
                    False,
                    "storage",
                )
        if candidate.is_symlink():
            raise StorageError(
                "SYMLINK_PATH",
                "storage destination must not be a symbolic link",
                False,
                "storage",
            )
        resolved_parent = candidate.parent.resolve()
        try:
            resolved_parent.relative_to(self.root)
        except ValueError as exc:
            raise StorageError(
                "PATH_TRAVERSAL",
                "storage path escapes configured root",
                False,
                "storage",
            ) from exc
        return candidate

    @contextmanager
    def lock(self, identity: str) -> Iterator[None]:
        safe = _safe_segment(identity, "lock identity")
        path = self.root / "locks" / f"{safe}.lock"
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd: int | None = None
        for _ in range(2):
            try:
                fd = os.open(path, flags, 0o600)
                break
            except FileExistsError as exc:
                self._reclaim_stale_lock(path, safe, exc)
        if fd is None:
            raise StorageError(
                "CONCURRENT_RUN",
                f"storage lock already exists: {safe}",
                True,
                "storage",
            )
        inode = os.fstat(fd).st_ino
        try:
            payload = (f"pid={os.getpid()}\ncreated_at={datetime.now(UTC).isoformat()}\n").encode()
            os.write(fd, payload)
            os.fsync(fd)
            yield
        finally:
            os.close(fd)
            try:
                current = path.lstat()
            except FileNotFoundError:
                return
            if current.st_ino == inode and not path.is_symlink():
                path.unlink(missing_ok=True)

    @staticmethod
    def _process_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _reclaim_stale_lock(self, path: Path, identity: str, original: Exception) -> None:
        if path.is_symlink():
            raise StorageError(
                "LOCK_PATH_UNSAFE",
                "storage lock path is a symbolic link",
                False,
                "storage",
            ) from original
        try:
            text = path.read_text(encoding="utf-8")
            values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
            pid = int(values["pid"])
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise StorageError(
                "LOCK_CORRUPT",
                f"storage lock cannot be validated: {identity}",
                False,
                "storage",
            ) from exc
        if self._process_alive(pid):
            raise StorageError(
                "CONCURRENT_RUN",
                f"storage lock already exists: {identity}",
                True,
                "storage",
                {"pid": pid},
            ) from original
        try:
            path.unlink()
        except FileNotFoundError:
            return

    @staticmethod
    def _filesystem_error(exc: OSError, *, operation: str, path: Path) -> StorageError:
        details = {
            "operation": operation,
            "path": str(path),
            "errno": exc.errno,
        }
        if exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)}:
            return StorageError(
                "DISK_SPACE_EXHAUSTED",
                f"insufficient storage capacity while {operation}",
                True,
                "storage",
                details,
            )
        if exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
            return StorageError(
                "STORAGE_PERMISSION_DENIED",
                f"storage permission denied while {operation}",
                False,
                "storage",
                details,
            )
        return StorageError(
            "STORAGE_IO_ERROR",
            f"storage I/O failure while {operation}",
            True,
            "storage",
            details,
        )

    def _atomic_write(
        self,
        destination: Path,
        payload: bytes,
        *,
        immutable: bool,
        expected_checksum: str | None = None,
    ) -> Path:
        destination = self._under_root(destination)
        temporary: Path | None = None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
            if destination.exists() and not destination.is_file():
                raise StorageError(
                    "DESTINATION_TYPE_INVALID",
                    "storage destination exists but is not a regular file",
                    False,
                    "storage",
                )
            if immutable and destination.exists():
                existing = sha256_file(destination)
                incoming = expected_checksum or hashlib.sha256(payload).hexdigest()
                if existing == incoming:
                    return destination
                raise StorageError(
                    "IMMUTABLE_CONFLICT",
                    f"immutable artifact already exists with different content: {destination}",
                    False,
                    "storage",
                )
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
            )
            temporary = Path(temporary_name)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o640)
            os.replace(temporary, destination)
            temporary = None
            try:
                directory_fd = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError as exc:
                if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EBADF}:
                    raise
        except StorageError:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise
        except OSError as exc:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise self._filesystem_error(
                exc, operation="publishing an atomic artifact", path=destination
            ) from exc
        except Exception:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise
        return destination

    def raw_path(self, instrument: InstrumentReference, chunk: DownloadChunk) -> Path:
        year = f"{chunk.start.year:04d}"
        month = f"{chunk.start.month:02d}"
        extension = ".raw"
        return (
            self.root
            / "raw"
            / _safe_segment(instrument.exchange_id, "exchange")
            / _safe_segment(instrument.market_type.value, "market")
            / _safe_segment(instrument.symbol, "symbol")
            / _safe_segment(chunk.data_type.value, "data_type")
            / year
            / month
            / f"{_safe_segment(chunk.chunk_id, 'chunk_id')}{extension}"
        )

    @staticmethod
    def _raw_artifact_id(
        *,
        checksum_sha256: str,
        instrument: InstrumentReference,
        chunk: DownloadChunk,
        source: SourceKind,
        source_uri: str,
        adapter_version: str,
    ) -> str:
        identity = sha256_canonical(
            {
                "checksum_sha256": checksum_sha256,
                "exchange_id": instrument.exchange_id,
                "market_type": instrument.market_type,
                "instrument_id": instrument.canonical_id,
                "data_type": chunk.data_type,
                "timeframe": chunk.timeframe,
                "start": chunk.start,
                "end": chunk.end,
                "source": source,
                "source_uri": source_uri,
                "adapter_version": adapter_version,
            }
        )
        return f"raw:{identity[:24]}"

    @staticmethod
    def _read_raw_metadata(metadata_path: Path) -> RawArtifactDescriptor:
        if metadata_path.is_symlink():
            raise StorageError(
                "SYMLINK_PATH",
                "raw metadata path must not be a symbolic link",
                False,
                "restore",
                {"metadata_path": str(metadata_path)},
            )
        try:
            metadata = read_json(metadata_path)
            return LocalStorage._raw_descriptor(metadata)
        except StorageError:
            raise
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StorageError(
                "RAW_METADATA_CORRUPT",
                "raw metadata cannot be decoded or validated",
                False,
                "restore",
                {
                    "error_type": type(exc).__name__,
                    "metadata_path": str(metadata_path),
                },
            ) from exc

    def read_raw(
        self, instrument: InstrumentReference, chunk: DownloadChunk
    ) -> tuple[bytes, RawArtifactDescriptor] | None:
        path = self.raw_path(instrument, chunk)
        metadata_path = path.with_name(path.name + ".meta.json")
        if path.is_symlink() or metadata_path.is_symlink():
            raise StorageError(
                "SYMLINK_PATH",
                "raw artifact paths must not be symbolic links",
                False,
                "restore",
            )
        if not path.is_file() or not metadata_path.is_file():
            legacy_extension = ".zip" if chunk.source is SourceKind.OFFICIAL_ARCHIVE else ".json"
            legacy = path.with_suffix(legacy_extension)
            legacy_metadata = legacy.with_name(legacy.name + ".meta.json")
            if legacy.is_symlink() or legacy_metadata.is_symlink():
                raise StorageError(
                    "SYMLINK_PATH",
                    "legacy raw artifact paths must not be symbolic links",
                    False,
                    "restore",
                )
            if not legacy.is_file() or not legacy_metadata.is_file():
                return None
            path = legacy
            metadata_path = legacy_metadata
        descriptor = self._read_raw_metadata(metadata_path)
        payload = path.read_bytes()
        actual_checksum = sha256_file(path)
        if actual_checksum != descriptor.checksum_sha256:
            raise StorageError(
                "RAW_CHECKSUM_MISMATCH",
                "immutable raw artifact no longer matches its descriptor",
                False,
                "restore",
            )
        mismatches: dict[str, object] = {}
        actual_size = path.stat().st_size
        if descriptor.byte_size != actual_size:
            mismatches["byte_size"] = {
                "expected": descriptor.byte_size,
                "observed": actual_size,
            }
        if descriptor.start != chunk.start:
            mismatches["start"] = {
                "expected": chunk.start.isoformat(),
                "observed": descriptor.start.isoformat(),
            }
        if descriptor.end != chunk.end:
            mismatches["end"] = {
                "expected": chunk.end.isoformat(),
                "observed": descriptor.end.isoformat(),
            }
        expected_artifact_id = self._raw_artifact_id(
            checksum_sha256=actual_checksum,
            instrument=instrument,
            chunk=chunk,
            source=descriptor.source,
            source_uri=descriptor.source_uri,
            adapter_version=descriptor.adapter_version,
        )
        if descriptor.artifact_id != expected_artifact_id:
            mismatches["artifact_id"] = {
                "expected": expected_artifact_id,
                "observed": descriptor.artifact_id,
            }
        if mismatches:
            raise StorageError(
                "RAW_DESCRIPTOR_CONTEXT_MISMATCH",
                "raw descriptor no longer matches its immutable context",
                False,
                "restore",
                mismatches,
            )
        return payload, descriptor

    @staticmethod
    def _raw_descriptor(metadata: object) -> RawArtifactDescriptor:
        if not isinstance(metadata, dict):
            raise TypeError("raw metadata must be an object")
        required = {
            "artifact_id",
            "source",
            "source_uri",
            "retrieved_at",
            "checksum_sha256",
            "byte_size",
            "record_count",
            "start",
            "end",
            "adapter_version",
            "response_metadata",
        }
        missing = required - set(metadata)
        if missing:
            raise KeyError(min(missing))
        response_metadata = metadata["response_metadata"]
        if not isinstance(response_metadata, dict):
            raise TypeError("response_metadata must be an object")
        record_count = metadata["record_count"]
        if record_count is not None and (
            not isinstance(record_count, int) or isinstance(record_count, bool)
        ):
            raise TypeError("record_count must be an integer or null")
        byte_size = metadata["byte_size"]
        if not isinstance(byte_size, int) or isinstance(byte_size, bool):
            raise TypeError("byte_size must be an integer")
        return RawArtifactDescriptor(
            artifact_id=metadata["artifact_id"],
            source=SourceKind(metadata["source"]),
            source_uri=metadata["source_uri"],
            retrieved_at=datetime.fromisoformat(metadata["retrieved_at"].replace("Z", "+00:00")),
            checksum_sha256=metadata["checksum_sha256"],
            byte_size=byte_size,
            record_count=record_count,
            start=datetime.fromisoformat(metadata["start"].replace("Z", "+00:00")),
            end=datetime.fromisoformat(metadata["end"].replace("Z", "+00:00")),
            adapter_version=metadata["adapter_version"],
            response_metadata=dict(response_metadata),
        )

    def find_raw_artifact(self, artifact_id: str) -> tuple[bytes, RawArtifactDescriptor] | None:
        """Find an immutable raw artifact by manifest identity without following links."""

        _safe_segment(artifact_id.replace(":", "_"), "artifact_id")
        raw_root = self.root / "raw"
        for directory, directories, files in os.walk(raw_root, followlinks=False):
            base = Path(directory)
            directories[:] = [name for name in directories if not (base / name).is_symlink()]
            for name in files:
                if not name.endswith(".meta.json"):
                    continue
                metadata_path = base / name
                if metadata_path.is_symlink():
                    continue
                try:
                    metadata = read_json(metadata_path)
                    descriptor = self._raw_descriptor(metadata)
                except (OSError, KeyError, TypeError, ValueError, StorageError):
                    continue
                if descriptor.artifact_id != artifact_id:
                    continue
                payload_path = metadata_path.with_name(name[: -len(".meta.json")])
                if payload_path.is_symlink() or not payload_path.is_file():
                    raise StorageError(
                        "RAW_ARTIFACT_MISSING",
                        "raw descriptor exists without a safe payload",
                        False,
                        "restore",
                    )
                payload = payload_path.read_bytes()
                if sha256_file(payload_path) != descriptor.checksum_sha256:
                    raise StorageError(
                        "RAW_CHECKSUM_MISMATCH",
                        "immutable raw artifact no longer matches its descriptor",
                        False,
                        "restore",
                    )
                return payload, descriptor
        return None

    def _index_raw_artifacts(
        self, artifact_ids: Iterable[str]
    ) -> dict[str, tuple[Path, RawArtifactDescriptor]]:
        """Index the first safe descriptor path for requested IDs in one raw-tree walk."""

        requested = tuple(artifact_ids)
        for artifact_id in requested:
            _safe_segment(artifact_id.replace(":", "_"), "artifact_id")
        remaining = set(requested)
        if not remaining:
            return {}

        indexed: dict[str, tuple[Path, RawArtifactDescriptor]] = {}
        raw_root = self.root / "raw"
        for directory, directories, files in os.walk(raw_root, followlinks=False):
            base = Path(directory)
            directories[:] = [name for name in directories if not (base / name).is_symlink()]
            for name in files:
                if not name.endswith(".meta.json"):
                    continue
                metadata_path = base / name
                if metadata_path.is_symlink():
                    continue
                try:
                    metadata = read_json(metadata_path)
                    descriptor = self._raw_descriptor(metadata)
                except (OSError, KeyError, TypeError, ValueError, StorageError):
                    continue
                if descriptor.artifact_id in remaining:
                    indexed[descriptor.artifact_id] = (metadata_path, descriptor)
                    remaining.remove(descriptor.artifact_id)
                    if not remaining:
                        return indexed
        return indexed

    def _read_indexed_raw_artifact(
        self,
        artifact_id: str,
        metadata_path: Path,
        indexed_descriptor: RawArtifactDescriptor,
    ) -> tuple[bytes, RawArtifactDescriptor]:
        """Read a selected indexed artifact, failing closed if its metadata changed."""

        descriptor = self._read_raw_metadata(metadata_path)
        if descriptor.artifact_id != artifact_id or descriptor != indexed_descriptor:
            raise StorageError(
                "RAW_METADATA_CHANGED",
                "raw descriptor changed after the validation index was built",
                False,
                "restore",
            )
        payload_path = metadata_path.with_name(metadata_path.name[: -len(".meta.json")])
        if payload_path.is_symlink() or not payload_path.is_file():
            raise StorageError(
                "RAW_ARTIFACT_MISSING",
                "raw descriptor exists without a safe payload",
                False,
                "restore",
            )
        payload = payload_path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != descriptor.checksum_sha256:
            raise StorageError(
                "RAW_CHECKSUM_MISMATCH",
                "immutable raw artifact no longer matches its descriptor",
                False,
                "restore",
            )
        return payload, descriptor

    def write_raw(
        self,
        *,
        run_id: str,
        instrument: InstrumentReference,
        chunk: DownloadChunk,
        payload: bytes,
        source_uri: str,
        adapter_version: str,
        response_metadata: dict[str, object],
        upstream_checksum: str | None,
        source_kind: SourceKind | None = None,
    ) -> RawArtifactDescriptor:
        _safe_segment(run_id, "run_id")
        if not isinstance(payload, bytes):
            raise TypeError("raw payload must be bytes")
        if not isinstance(response_metadata, dict):
            raise TypeError("response_metadata must be a dictionary")
        safe_source_uri = validate_source_uri(source_uri)
        resolved_source = source_kind or chunk.source
        if not isinstance(resolved_source, SourceKind):
            raise TypeError("source_kind must be SourceKind or None")
        checksum = hashlib.sha256(payload).hexdigest()
        artifact_id = self._raw_artifact_id(
            checksum_sha256=checksum,
            instrument=instrument,
            chunk=chunk,
            source=resolved_source,
            source_uri=safe_source_uri,
            adapter_version=adapter_version,
        )
        metadata = dict(response_metadata)
        if upstream_checksum is not None:
            metadata["upstream_checksum"] = upstream_checksum
        metadata["run_id"] = run_id
        record_count_value = response_metadata.get("records", 0)
        if isinstance(record_count_value, bool) or not isinstance(record_count_value, int):
            raise TypeError("response metadata records must be an integer")
        descriptor = RawArtifactDescriptor(
            artifact_id=artifact_id,
            source=resolved_source,
            source_uri=safe_source_uri,
            retrieved_at=datetime.now(UTC),
            checksum_sha256=checksum,
            byte_size=len(payload),
            record_count=record_count_value,
            start=chunk.start,
            end=chunk.end,
            adapter_version=adapter_version,
            response_metadata=metadata,
        )
        path = self.raw_path(instrument, chunk)
        published = self._atomic_write(
            path,
            payload,
            immutable=True,
            expected_checksum=checksum,
        )
        metadata_path = published.with_name(published.name + ".meta.json")
        if metadata_path.exists():
            restored = self.read_raw(instrument, chunk)
            if restored is None:
                raise StorageError(
                    "RAW_METADATA_MISSING",
                    "raw metadata disappeared",
                    True,
                    "storage",
                )
            return restored[1]
        try:
            self._atomic_write(
                metadata_path,
                json_bytes(descriptor, pretty=True),
                immutable=True,
            )
        except BaseException:
            if published.exists() and not metadata_path.exists():
                published.unlink(missing_ok=True)
            raise
        return descriptor

    def write_kline_partition(
        self,
        *,
        dataset_id: str,
        version: int,
        layer: str,
        instrument: InstrumentReference,
        timeframe: str,
        records: Iterable[KlineRecord],
        source_artifact_ids: tuple[str, ...],
        format_name: str = "parquet",
        compression: str = "zstd",
    ) -> NormalizedPartitionDescriptor:
        return self.write_partition(
            dataset_id=dataset_id,
            version=version,
            layer=layer,
            instrument=instrument,
            data_type=DataType.KLINES,
            timeframe=timeframe,
            records=records,
            source_artifact_ids=source_artifact_ids,
            format_name=format_name,
            compression=compression,
        )

    def write_partition(
        self,
        *,
        dataset_id: str,
        version: int,
        layer: str,
        instrument: InstrumentReference,
        data_type: DataType,
        timeframe: str | None,
        records: Iterable[NormalizedRecord],
        source_artifact_ids: tuple[str, ...],
        format_name: str = "parquet",
        compression: str = "zstd",
    ) -> NormalizedPartitionDescriptor:
        if layer not in {"normalized", "derived"}:
            raise StorageError(
                "LAYER_INVALID",
                "partition layer must be normalized or derived",
                False,
                "storage",
            )
        if format_name not in {"csv", "parquet"}:
            raise StorageError(
                "FORMAT_UNSUPPORTED",
                f"unsupported storage format: {format_name}",
                False,
                "storage",
            )
        iterator = iter(records)
        try:
            first = next(iterator)
        except StopIteration as exc:
            raise StorageError(
                "EMPTY_PARTITION",
                "cannot publish an empty partition",
                False,
                "storage",
            ) from exc
        self._validate_partition_record(first, data_type, timeframe)
        path = self._partition_path(
            version=version,
            layer=layer,
            instrument=instrument,
            data_type=data_type,
            timeframe=timeframe,
            first=first,
            format_name=format_name,
        )
        if format_name == "csv":
            return self._write_csv_partition(
                path=path,
                first=first,
                remaining=iterator,
                data_type=data_type,
                timeframe=timeframe,
                source_artifact_ids=source_artifact_ids,
            )

        rows = (first, *tuple(iterator))
        self._validate_partition_sequence(rows, data_type, timeframe)
        payload = self._parquet_bytes(rows, compression)
        checksum = hashlib.sha256(payload).hexdigest()
        published = self._atomic_write(
            path,
            payload,
            immutable=True,
            expected_checksum=checksum,
        )
        return self._partition_descriptor(
            published=published,
            checksum=checksum,
            rows=rows,
            data_type=data_type,
            timeframe=timeframe,
            source_artifact_ids=source_artifact_ids,
        )

    def _partition_path(
        self,
        *,
        version: int,
        layer: str,
        instrument: InstrumentReference,
        data_type: DataType,
        timeframe: str | None,
        first: NormalizedRecord,
        format_name: str,
    ) -> Path:
        year = f"{first.timestamp.year:04d}"
        month = f"{first.timestamp.month:02d}"
        timeframe_segment = timeframe or "events"
        return (
            self.root
            / layer
            / _safe_segment(instrument.exchange_id, "exchange")
            / _safe_segment(instrument.market_type.value, "market")
            / _safe_segment(instrument.symbol, "symbol")
            / _safe_segment(data_type.value, "data_type")
            / _safe_segment(timeframe_segment, "timeframe")
            / f"v{version}"
            / year
            / month
            / (f"part-{first.timestamp.strftime('%Y%m%dT%H%M%SZ')}.{format_name}")
        )

    @staticmethod
    def _validate_partition_record(
        record: NormalizedRecord,
        data_type: DataType,
        timeframe: str | None,
    ) -> None:
        if record.data_type is not data_type:
            raise StorageError(
                "DATA_TYPE_MIX",
                "partition records contain mixed data types",
                False,
                "storage",
            )
        if record.record_timeframe != timeframe:
            raise StorageError(
                "TIMEFRAME_MIX",
                "partition records contain mixed timeframes",
                False,
                "storage",
            )

    @classmethod
    def _validate_partition_sequence(
        cls,
        rows: tuple[NormalizedRecord, ...],
        data_type: DataType,
        timeframe: str | None,
    ) -> None:
        first_month = (rows[0].timestamp.year, rows[0].timestamp.month)
        previous = (rows[0].timestamp, rows[0].end_time)
        for index, row in enumerate(rows):
            cls._validate_partition_record(row, data_type, timeframe)
            if (row.timestamp.year, row.timestamp.month) != first_month:
                raise StorageError(
                    "MONTH_MIX",
                    "a monthly partition cannot contain records from another month",
                    False,
                    "storage",
                )
            current = (row.timestamp, row.end_time)
            if index and current < previous:
                raise StorageError(
                    "PARTITION_ORDER_INVALID",
                    "partition records must be sorted by timestamp and end time",
                    False,
                    "storage",
                )
            previous = current

    @staticmethod
    def _partition_end(record: NormalizedRecord, timeframe: str | None) -> datetime:
        if timeframe is not None:
            return record.timestamp + timeframe_delta(timeframe)
        end = record.end_time
        if end <= record.timestamp:
            return record.timestamp + timedelta(microseconds=1)
        return end

    def _write_csv_partition(
        self,
        *,
        path: Path,
        first: NormalizedRecord,
        remaining: Iterator[NormalizedRecord],
        data_type: DataType,
        timeframe: str | None,
        source_artifact_ids: tuple[str, ...],
    ) -> NormalizedPartitionDescriptor:
        destination = self._under_root(path)
        temporary: Path | None = None
        row_count = 0
        last_end = self._partition_end(first, timeframe)
        first_month = (first.timestamp.year, first.timestamp.month)
        previous = (first.timestamp, first.end_time)
        fieldnames = normalized_record_fieldnames(data_type)
        if tuple(first.to_row()) != fieldnames:
            raise StorageError(
                "PARTITION_SCHEMA_MISMATCH",
                "first partition record does not match the normalized schema",
                False,
                "storage",
            )
        try:
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
            if destination.exists() and not destination.is_file():
                raise StorageError(
                    "DESTINATION_TYPE_INVALID",
                    "storage destination exists but is not a regular file",
                    False,
                    "storage",
                )
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
            )
            temporary = Path(temporary_name)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
                writer.writeheader()
                for index, record in enumerate(chain((first,), remaining)):
                    self._validate_partition_record(record, data_type, timeframe)
                    if (
                        record.timestamp.year,
                        record.timestamp.month,
                    ) != first_month:
                        raise StorageError(
                            "MONTH_MIX",
                            "a monthly partition cannot contain records from another month",
                            False,
                            "storage",
                        )
                    current = (record.timestamp, record.end_time)
                    if index and current < previous:
                        raise StorageError(
                            "PARTITION_ORDER_INVALID",
                            "partition records must be sorted by timestamp and end time",
                            False,
                            "storage",
                        )
                    row = record.to_row()
                    if tuple(row) != fieldnames:
                        raise StorageError(
                            "PARTITION_SCHEMA_MIX",
                            "partition records contain inconsistent row schemas",
                            False,
                            "storage",
                        )
                    for name, value in tuple(row.items()):
                        row[name] = self._csv_scalar(value)
                    writer.writerow(row)
                    row_count += 1
                    last_end = max(last_end, self._partition_end(record, timeframe))
                    previous = current
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o640)
            checksum = sha256_file(temporary)
            if destination.exists():
                if sha256_file(destination) == checksum:
                    temporary.unlink()
                    temporary = None
                    published = destination
                else:
                    raise StorageError(
                        "IMMUTABLE_CONFLICT",
                        f"immutable partition already exists with different content: {destination}",
                        False,
                        "storage",
                    )
            else:
                os.replace(temporary, destination)
                temporary = None
                published = destination
                try:
                    directory_fd = os.open(destination.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError as exc:
                    if exc.errno not in {
                        errno.EINVAL,
                        errno.ENOTSUP,
                        errno.EBADF,
                    }:
                        raise
        except StorageError:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise
        except OSError as exc:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise self._filesystem_error(
                exc,
                operation="publishing a streaming CSV partition",
                path=destination,
            ) from exc
        except Exception:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise
        return NormalizedPartitionDescriptor(
            partition_id=f"part:{checksum[:24]}",
            data_type=data_type,
            timeframe=timeframe,
            storage_uri=published.relative_to(self.root).as_posix(),
            checksum_sha256=checksum,
            row_count=row_count,
            start=first.timestamp,
            end=last_end,
            schema_version=1,
            source_artifact_ids=source_artifact_ids,
        )

    @staticmethod
    def _csv_scalar(value: object) -> object:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return ""
        if isinstance(value, (tuple, list, dict)):
            return json.dumps(
                primitive(value),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
        return value

    def _partition_descriptor(
        self,
        *,
        published: Path,
        checksum: str,
        rows: tuple[NormalizedRecord, ...],
        data_type: DataType,
        timeframe: str | None,
        source_artifact_ids: tuple[str, ...],
    ) -> NormalizedPartitionDescriptor:
        return NormalizedPartitionDescriptor(
            partition_id=f"part:{checksum[:24]}",
            data_type=data_type,
            timeframe=timeframe,
            storage_uri=published.relative_to(self.root).as_posix(),
            checksum_sha256=checksum,
            row_count=len(rows),
            start=rows[0].timestamp,
            end=max(self._partition_end(row, timeframe) for row in rows),
            schema_version=1,
            source_artifact_ids=source_artifact_ids,
        )

    @staticmethod
    def _parquet_bytes(rows: tuple[NormalizedRecord, ...], compression: str) -> bytes:
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise StorageError(
                "PARQUET_DEPENDENCY_MISSING",
                "Parquet output requires pyarrow; install the package with [parquet]",
                False,
                "storage",
            ) from exc
        import io

        columns: dict[str, list[object]] = {name: [] for name in rows[0].to_row()}
        for record in rows:
            for name, value in record.to_row().items():
                columns[name].append(value)
        table = pa.table(columns)
        sink = io.BytesIO()
        pq.write_table(table, sink, compression=compression, row_group_size=65_536)
        return sink.getvalue()

    def iter_partition_records(
        self, partition: NormalizedPartitionDescriptor
    ) -> Iterator[NormalizedRecord]:
        """Read a registered partition as a bounded-memory typed stream."""

        from .integrity import resolve_registered_path

        path = resolve_registered_path(self.root, partition.storage_uri, kind="partition")
        if not path.is_file():
            raise StorageError(
                "PARTITION_MISSING",
                "registered partition file is missing",
                False,
                "storage",
            )
        if sha256_file(path) != partition.checksum_sha256:
            raise StorageError(
                "PARTITION_CHECKSUM_MISMATCH",
                "registered partition checksum does not match",
                False,
                "storage",
            )
        count = 0
        if path.suffix.lower() == ".csv":
            with path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                expected_fields = normalized_record_fieldnames(partition.data_type)
                observed_fields = tuple(reader.fieldnames or ())
                if observed_fields != expected_fields:
                    raise StorageError(
                        "PARTITION_SCHEMA_MISMATCH",
                        "CSV header differs from the normalized schema",
                        False,
                        "storage",
                        {
                            "expected": list(expected_fields),
                            "observed": list(observed_fields),
                        },
                    )
                try:
                    for row in reader:
                        if None in row:
                            raise StorageError(
                                "PARTITION_SCHEMA_MISMATCH",
                                "CSV row contains fields beyond the declared header",
                                False,
                                "storage",
                            )
                        record = normalized_record_from_row(partition.data_type, dict(row))
                        if record.record_timeframe != partition.timeframe:
                            raise StorageError(
                                "PARTITION_TIMEFRAME_MISMATCH",
                                "partition row timeframe differs from descriptor",
                                False,
                                "storage",
                            )
                        count += 1
                        yield record
                except (KeyError, TypeError, ValueError, csv.Error) as exc:
                    raise StorageError(
                        "PARTITION_ROW_INVALID",
                        "CSV partition contains an invalid normalized row",
                        False,
                        "storage",
                        {"row_number": count + 2},
                    ) from exc
        elif path.suffix.lower() == ".parquet":
            try:
                import pyarrow.parquet as pq
            except ImportError as exc:
                raise StorageError(
                    "PARQUET_DEPENDENCY_MISSING",
                    "reading Parquet partitions requires pyarrow",
                    False,
                    "storage",
                ) from exc
            parquet = pq.ParquetFile(path)
            expected_fields = normalized_record_fieldnames(partition.data_type)
            observed_fields = tuple(parquet.schema_arrow.names)
            if observed_fields != expected_fields:
                raise StorageError(
                    "PARTITION_SCHEMA_MISMATCH",
                    "Parquet columns differ from the normalized schema",
                    False,
                    "storage",
                    {
                        "expected": list(expected_fields),
                        "observed": list(observed_fields),
                    },
                )
            for batch in parquet.iter_batches(batch_size=65_536):
                for row in batch.to_pylist():
                    record = normalized_record_from_row(partition.data_type, dict(row))
                    if record.record_timeframe != partition.timeframe:
                        raise StorageError(
                            "PARTITION_TIMEFRAME_MISMATCH",
                            "partition row timeframe differs from descriptor",
                            False,
                            "storage",
                        )
                    count += 1
                    yield record
        else:
            raise StorageError(
                "PARTITION_FORMAT_UNSUPPORTED",
                "registered partition format is unsupported",
                False,
                "storage",
            )
        if count != partition.row_count:
            raise StorageError(
                "PARTITION_ROW_COUNT_MISMATCH",
                "partition row count differs from descriptor",
                False,
                "storage",
                {"expected": partition.row_count, "observed": count},
            )

    def write_manifest(self, dataset_id: str, version: int, manifest: object) -> Path:
        path = (
            self.root / "manifests" / _safe_segment(dataset_id, "dataset_id") / f"v{version}.json"
        )
        return self._atomic_write(path, json_bytes(manifest, pretty=True), immutable=True)

    def write_checkpoint(self, run_id: str, checkpoint: object) -> Path:
        path = self.root / "checkpoints" / f"{_safe_segment(run_id, 'run_id')}.json"
        return self._atomic_write(path, json_bytes(checkpoint, pretty=True), immutable=False)

    def write_resume_spec(self, run_id: str, spec: object) -> Path:
        path = self.root / "runs" / f"{_safe_segment(run_id, 'run_id')}.json"
        return self._atomic_write(path, json_bytes(spec, pretty=True), immutable=True)

    def request_cancel(self, run_id: str) -> Path:
        path = self.root / "cancellations" / f"{_safe_segment(run_id, 'run_id')}.cancel"
        return self._atomic_write(
            path,
            f"cancel_requested_at={datetime.now(UTC).isoformat()}\n".encode(),
            immutable=False,
        )

    def is_cancel_requested(self, run_id: str) -> bool:
        path = self.root / "cancellations" / f"{_safe_segment(run_id, 'run_id')}.cancel"
        return path.is_file()

    def clear_cancel(self, run_id: str) -> None:
        path = self.root / "cancellations" / f"{_safe_segment(run_id, 'run_id')}.cancel"
        path.unlink(missing_ok=True)

    def write_repair_audit(self, name: str, audit: object) -> Path:
        path = self.root / "repair_audits" / f"{_safe_segment(name, 'repair audit')}.json"
        return self._atomic_write(path, json_bytes(audit, pretty=True), immutable=True)

    def quarantine(self, name: str, payload: bytes) -> Path:
        path = self.root / "quarantine" / _safe_segment(name, "quarantine name")
        return self._atomic_write(path, payload, immutable=True)
