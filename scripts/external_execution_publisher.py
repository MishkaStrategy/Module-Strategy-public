"""Local reference publisher for controller-signed external execution receipts.

This adapter performs local receipt, admission, claim, and staged-output checks
and writes a local content-addressed evidence store. It is not a production
publisher: provider authentication, controller key custody, candidate isolation,
cross-instance global atomicity, and durable remote upload are absent. Production
must retrieve the admission and claim from protected storage, authenticate the
provider/controller boundary, pin the controller signing key, and enforce
global one-use admission reservation outside this adapter.
The immutable admission publication_deadline gates creation of a new outbox
reservation. An identical existing PREPARED or COMMITTED reservation may be
retried after that deadline only to recover its already-reserved local bytes;
recovery never refreshes or extends the admission deadline.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from scripts.external_admission_ledger import (
    Admission,
    AdmissionError,
    AdmissionLedger,
    Claim,
    NetworkPolicy,
    ResourcePolicy,
    admission_digest,
)
from scripts.verify_external_execution_receipt import (
    MAX_RECEIPT_BYTES,
    MAX_OUTPUTS,
    MAX_OUTPUT_BYTES,
    MAX_TOTAL_OUTPUT_BYTES,
    ReceiptError,
    REPOSITORY,
    _check_path_set,
    _valid_output_path,
    read_receipt_snapshot,
    verify_receipt_bytes,
)

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_UTC = re.compile(
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{1,6})?Z$"
)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
MAX_STAGED_ENTRIES = 4096
MAX_STAGED_DEPTH = 64


class PublicationError(ValueError):
    """Local publication preparation or commit failed closed."""


@dataclass(frozen=True)
class CommittedOutputSnapshot:
    path: str
    sha256: str
    size_bytes: int
    content: bytes


@dataclass(frozen=True)
class CommittedPublicationSnapshot:
    """Read-only byte snapshot of a locally committed publication."""

    publication_id: str
    admission: Admission
    claim: Claim
    bundle_bytes: bytes
    receipt_bytes: bytes
    outputs: tuple[CommittedOutputSnapshot, ...]


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _time(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise PublicationError("now must be an aware UTC datetime")
        return value
    if not isinstance(value, str) or not _UTC.fullmatch(value):
        raise PublicationError("now must be canonical UTC RFC 3339")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise PublicationError("now must be a valid canonical UTC timestamp") from exc


def _parse_receipt_time(value: str) -> datetime:
    if not isinstance(value, str) or not _UTC.fullmatch(value):
        raise PublicationError("receipt contains a noncanonical timestamp")
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _policy_digest(value: ResourcePolicy | NetworkPolicy) -> str:
    return hashlib.sha256(_canonical(value.to_mapping())).hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW | _CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PublicationError(f"evidence path is not a regular file: {path}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_directory(path: Path) -> None:
    created = False
    try:
        path.mkdir()
        created = True
    except FileExistsError:
        pass
    if path.is_symlink() or not path.is_dir():
        raise PublicationError(f"evidence directory is not a real directory: {path}")
    if created:
        _fsync_directory(path.parent)
    _fsync_directory(path)


def _expected(admission: Admission) -> dict[str, Any]:
    return {
        "repository": REPOSITORY,
        "source_sha": admission.source_sha,
        "admission_id": admission.admission_id,
        "workflow_digest": admission.workflow_digest,
        "runtime_id": admission.runtime_id,
        "material_digest": admission.material_digest,
        "policy_id": admission.policy_id,
        "policy_digest": admission.policy_digest,
        "workload_id": admission.workload_id,
        "workload_digest": admission.workload_digest,
        "environment_id": admission.environment_id,
        "image_digest": admission.image_digest,
        "image_os": admission.image_os,
        "image_arch": admission.image_arch,
        "admission_digest": admission_digest(admission),
        "provider_id": admission.provider_id,
        "provider_run_id": admission.provider_run_id,
        "provider_attempt": admission.provider_attempt,
        "resource_policy_digest": _policy_digest(admission.resource_policy),
        "network_policy_digest": _policy_digest(admission.network_policy),
    }


def _read_staged_file(root_fd: int, relative_path: str, expected_size: int) -> bytes:
    parts = relative_path.split("/")
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC | _NONBLOCK, dir_fd=current
            )
            os.close(current)
            current = child
        descriptor = os.open(
            parts[-1], os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK, dir_fd=current
        )
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise PublicationError(f"staged output is not a regular file: {relative_path}")
            if (
                isinstance(expected_size, bool)
                or not isinstance(expected_size, int)
                or not 0 <= expected_size <= MAX_OUTPUT_BYTES
                or before.st_size != expected_size
            ):
                raise PublicationError(
                    f"staged output size does not match the signed receipt: {relative_path}"
                )
            chunks: list[bytes] = []
            total = 0
            while total <= expected_size:
                chunk = os.read(descriptor, min(1024 * 1024, expected_size + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > expected_size:
                    raise PublicationError(
                        f"staged output changed or exceeds its signed size: {relative_path}"
                    )
            payload = b"".join(chunks)
            after = os.fstat(descriptor)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ) or len(payload) != expected_size:
                raise PublicationError(f"staged output changed while being read: {relative_path}")
            return payload
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise PublicationError(f"cannot safely read staged output {relative_path}: {exc}") from exc
    finally:
        os.close(current)


def _enumerate_staged_paths(directory_fd: int) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    stack: list[tuple[int, str, int]] = [(os.dup(directory_fd), "", 0)]
    entries_seen = 0
    try:
        while stack:
            current_fd, prefix, depth = stack.pop()
            try:
                with os.scandir(current_fd) as entries:
                    for entry in entries:
                        entries_seen += 1
                        if entries_seen > MAX_STAGED_ENTRIES:
                            raise PublicationError(
                                "staged tree exceeds the maximum enumerated entry count"
                            )
                        relative = f"{prefix}/{entry.name}" if prefix else entry.name
                        if entry.is_symlink():
                            raise PublicationError(
                                f"symlink in staged output inventory: {relative}"
                            )
                        if entry.is_dir(follow_symlinks=False):
                            if depth >= MAX_STAGED_DEPTH:
                                raise PublicationError(
                                    "staged tree exceeds the maximum directory depth"
                                )
                            expected = entry.stat(follow_symlinks=False)
                            child_fd = os.open(
                                entry.name,
                                os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
                                dir_fd=current_fd,
                            )
                            try:
                                actual = os.fstat(child_fd)
                                if not stat.S_ISDIR(actual.st_mode) or (
                                    actual.st_dev,
                                    actual.st_ino,
                                ) != (expected.st_dev, expected.st_ino):
                                    raise PublicationError(
                                        f"staged directory changed during enumeration: {relative}"
                                    )
                            except Exception:
                                os.close(child_fd)
                                raise
                            directories.add(relative)
                            stack.append((child_fd, relative, depth + 1))
                        elif entry.is_file(follow_symlinks=False):
                            files.add(relative)
                        else:
                            raise PublicationError(f"nonregular staged output entry: {relative}")
            finally:
                os.close(current_fd)
        return files, directories
    finally:
        for descriptor, _prefix, _depth in stack:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _read_staged_inventory(
    root: Path, expected_outputs: tuple[tuple[str, int], ...]
) -> dict[str, bytes]:
    if not _NOFOLLOW or not _DIRECTORY or os.open not in os.supports_dir_fd:
        raise PublicationError("platform lacks no-follow descriptor-relative filesystem support")
    if root.is_symlink() or not root.is_dir():
        raise PublicationError("staged output root must be a real directory")
    try:
        root_fd = os.open(root, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC)
    except OSError as exc:
        raise PublicationError(f"cannot safely open staged output root: {exc}") from exc
    try:
        enumerated, directories = _enumerate_staged_paths(root_fd)
        expected_set = {path for path, _size in expected_outputs}
        expected_directories = {
            "/".join(parts[:index])
            for path, _size in expected_outputs
            for parts in (path.split("/"),)
            for index in range(1, len(parts))
        }
        if enumerated != expected_set or directories != expected_directories:
            missing = sorted(expected_set - enumerated)
            extra = sorted(enumerated - expected_set)
            extra_directories = sorted(directories - expected_directories)
            raise PublicationError(
                f"staged output inventory differs; missing={missing}, extra={extra}, extra_directories={extra_directories}"
            )
        outputs: dict[str, bytes] = {}
        total = 0
        for relative_path, expected_size in expected_outputs:
            payload = _read_staged_file(root_fd, relative_path, expected_size)
            total += len(payload)
            if total > MAX_TOTAL_OUTPUT_BYTES:
                raise PublicationError("staged output inventory exceeds the total size limit")
            outputs[relative_path] = payload
        return outputs
    finally:
        os.close(root_fd)


class LocalExecutionPublisher:
    """Prepare and commit local-only, content-addressed execution evidence."""

    def __init__(
        self,
        ledger: AdmissionLedger,
        outbox_database: str | Path,
        evidence_store: str | Path,
        trusted_controller_keys: str | Path,
        *,
        openssl: str = "openssl",
    ) -> None:
        self.ledger = ledger
        self.outbox_database = str(outbox_database)
        self.evidence_store = Path(evidence_store)
        self.trusted_controller_keys = Path(trusted_controller_keys)
        self.openssl = openssl
        if self.outbox_database == ":memory:":
            raise PublicationError("use a file-backed SQLite outbox")
        Path(self.outbox_database).parent.mkdir(parents=True, exist_ok=True)
        root_was_present = self.evidence_store.exists()
        self.evidence_store.mkdir(parents=True, exist_ok=True)
        if self.evidence_store.is_symlink() or not self.evidence_store.is_dir():
            raise PublicationError("evidence store root must be a real directory")
        if not root_was_present:
            _fsync_directory(self.evidence_store.parent)
        _fsync_directory(self.evidence_store)
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS publications (
                    publication_id TEXT PRIMARY KEY,
                    admission_id TEXT NOT NULL UNIQUE,
                    receipt_digest TEXT NOT NULL,
                    bundle_digest TEXT NOT NULL,
                    bundle_json BLOB NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('PREPARED', 'COMMITTED'))
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS publication_blobs (
                    publication_id TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    data BLOB NOT NULL,
                    PRIMARY KEY (publication_id, digest),
                    FOREIGN KEY (publication_id) REFERENCES publications(publication_id)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS publication_receipts (
                    publication_id TEXT PRIMARY KEY,
                    digest TEXT NOT NULL,
                    data BLOB NOT NULL,
                    FOREIGN KEY (publication_id) REFERENCES publications(publication_id)
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.outbox_database, timeout=10.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _connect_readonly(self) -> sqlite3.Connection:
        """Open the live outbox read-only with normal SQLite WAL coordination.

        SQLite may create or update WAL/SHM coordination files while the
        connection is open. The main database and logical rows remain read-only.
        """
        uri = Path(self.outbox_database).resolve(strict=True).as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA query_only=ON")
        return connection

    def read_committed(self, publication_id: str) -> CommittedPublicationSnapshot:
        """Return verified bytes for an already committed local publication.

        This method is deliberately read-only. It refuses PREPARED rows and does
        not call ``commit`` or repair missing content-addressed files.
        """
        if not isinstance(publication_id, str) or not _SHA256.fullmatch(publication_id):
            raise PublicationError("publication_id must be a lowercase SHA-256 digest")
        connection = self._connect_readonly()
        try:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT admission_id, receipt_digest, bundle_digest, bundle_json, status "
                "FROM publications WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
            if row is None:
                raise PublicationError("unknown publication_id")
            if row["status"] != "COMMITTED":
                raise PublicationError("publication is not committed")
            raw_bundle = bytes(row["bundle_json"])
            try:
                bundle = json.loads(raw_bundle.decode("ascii"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise PublicationError("outbox bundle JSON is invalid") from exc
            if (
                not isinstance(bundle, dict)
                or set(bundle) != {"admission_id", "receipt_sha256", "outputs"}
                or _canonical(bundle) != raw_bundle
            ):
                raise PublicationError("outbox bundle descriptor is noncanonical or malformed")
            if bundle["admission_id"] != row["admission_id"]:
                raise PublicationError("outbox bundle admission_id mismatch")
            bundle_digest = hashlib.sha256(raw_bundle).hexdigest()
            receipt_digest = row["receipt_digest"]
            if (
                bundle_digest != row["bundle_digest"]
                or bundle["receipt_sha256"] != receipt_digest
                or hashlib.sha256(
                    f"{row['admission_id']}:{receipt_digest}:{bundle_digest}".encode("ascii")
                ).hexdigest()
                != publication_id
            ):
                raise PublicationError("committed publication identities do not match")
            output_rows = bundle["outputs"]
            if not isinstance(output_rows, list) or len(output_rows) > MAX_OUTPUTS:
                raise PublicationError("outbox bundle output inventory has an invalid size")
            expected_sizes: dict[str, int] = {}
            paths: list[str] = []
            total_size = 0
            for index, output in enumerate(output_rows):
                if not isinstance(output, dict) or set(output) != {"path", "sha256", "size_bytes"}:
                    raise PublicationError(f"outbox bundle output row {index} has an invalid shape")
                path, digest, size = output["path"], output["sha256"], output["size_bytes"]
                if not _valid_output_path(path):
                    raise PublicationError(f"outbox bundle output path {index} is invalid")
                if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                    raise PublicationError(f"outbox bundle output digest {index} is invalid")
                if (
                    isinstance(size, bool)
                    or not isinstance(size, int)
                    or not 0 <= size <= MAX_OUTPUT_BYTES
                ):
                    raise PublicationError(f"outbox bundle output size {index} is invalid")
                if digest in expected_sizes and expected_sizes[digest] != size:
                    raise PublicationError("same output digest has conflicting sizes")
                expected_sizes[digest] = size
                total_size += size
                paths.append(path)
            if total_size > MAX_TOTAL_OUTPUT_BYTES:
                raise PublicationError(
                    "outbox bundle output inventory exceeds the total size limit"
                )
            try:
                _check_path_set(paths, "outbox bundle outputs")
            except ReceiptError as exc:
                raise PublicationError(f"outbox bundle output paths conflict: {exc}") from exc
            blob_rows = connection.execute(
                "SELECT digest, data FROM publication_blobs WHERE publication_id = ? ORDER BY digest",
                (publication_id,),
            ).fetchall()
            receipt_row = connection.execute(
                "SELECT digest, data FROM publication_receipts WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
        finally:
            connection.close()
        if receipt_row is None:
            raise PublicationError("outbox is missing the verified receipt bytes")
        receipt_bytes = bytes(receipt_row["data"])
        if (
            receipt_row["digest"] != receipt_digest
            or hashlib.sha256(receipt_bytes).hexdigest() != receipt_digest
            or len(receipt_bytes) > MAX_RECEIPT_BYTES
        ):
            raise PublicationError("outbox receipt bytes do not match the committed digest")
        try:
            admission, claim = self.ledger.read_snapshot(row["admission_id"])
        except AdmissionError as exc:
            raise PublicationError(f"read-only admission snapshot failed: {exc}") from exc
        if (
            admission is None
            or claim is None
            or admission.admission_id != row["admission_id"]
            or claim.admission_id != admission.admission_id
        ):
            raise PublicationError("committed publication lacks its immutable admission or claim")
        if claim.execution_deadline != admission.execution_deadline:
            raise PublicationError("persisted claim deadline does not match immutable admission")
        try:
            receipt_payload = verify_receipt_bytes(
                receipt_bytes,
                self.trusted_controller_keys,
                self.openssl,
                _expected(admission),
                list(admission.required_output_paths),
            )
        except ReceiptError as exc:
            raise PublicationError(f"stored receipt no longer verifies: {exc}") from exc
        if receipt_payload["executor"]["executor_id"] != claim.executor_id:
            raise PublicationError("stored receipt executor differs from persisted claim")
        verified_outputs = [
            {"path": item["path"], "sha256": item["sha256"], "size_bytes": item["size_bytes"]}
            for item in sorted(receipt_payload["outputs"], key=lambda item: item["path"])
        ]
        if verified_outputs != output_rows:
            raise PublicationError("committed bundle outputs differ from the signed receipt")
        claimed_at = _parse_receipt_time(claim.claimed_at)
        created = _parse_receipt_time(receipt_payload["executor"]["created_at"])
        started = _parse_receipt_time(receipt_payload["started_at"])
        ended = _parse_receipt_time(receipt_payload["ended_at"])
        destroyed = _parse_receipt_time(receipt_payload["destroyed_at"])
        deadline = _parse_receipt_time(claim.execution_deadline)
        if not created <= claimed_at <= started <= ended <= destroyed <= deadline:
            raise PublicationError(
                "stored receipt lifecycle is outside the claimed admission deadline"
            )
        actual_blob_digests = {item["digest"] for item in blob_rows}
        if actual_blob_digests != set(expected_sizes):
            raise PublicationError("outbox blobs do not match the committed bundle inventory")
        blobs: dict[str, bytes] = {}
        for item in blob_rows:
            digest, data = item["digest"], bytes(item["data"])
            if (
                not _SHA256.fullmatch(digest)
                or hashlib.sha256(data).hexdigest() != digest
                or len(data) != expected_sizes[digest]
            ):
                raise PublicationError("outbox blob digest mismatch")
            blobs[digest] = data

        root_fd: int | None = None
        try:
            if not _NOFOLLOW:
                raise PublicationError("platform lacks no-follow evidence-store reads")
            root_fd = os.open(
                self.evidence_store,
                os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
            )
            if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
                raise PublicationError("evidence store root is not a directory")
            stored_bundle = _read_staged_file(
                root_fd, f"bundles/{bundle_digest}.json", len(raw_bundle)
            )
            stored_receipt = _read_staged_file(
                root_fd, f"receipts/{receipt_digest}.json", len(receipt_bytes)
            )
            if stored_bundle != raw_bundle or stored_receipt != receipt_bytes:
                raise PublicationError(
                    "content-addressed bundle or receipt differs from outbox bytes"
                )
            outputs: list[CommittedOutputSnapshot] = []
            for item in output_rows:
                data = blobs[item["sha256"]]
                stored = _read_staged_file(root_fd, f"blobs/{item['sha256']}", item["size_bytes"])
                if stored != data:
                    raise PublicationError("content-addressed output differs from outbox bytes")
                outputs.append(
                    CommittedOutputSnapshot(
                        path=item["path"],
                        sha256=item["sha256"],
                        size_bytes=item["size_bytes"],
                        content=data,
                    )
                )
        except OSError as exc:
            raise PublicationError(f"cannot safely read committed evidence: {exc}") from exc
        finally:
            if root_fd is not None:
                os.close(root_fd)
        return CommittedPublicationSnapshot(
            publication_id=publication_id,
            admission=admission,
            claim=claim,
            bundle_bytes=raw_bundle,
            receipt_bytes=receipt_bytes,
            outputs=tuple(outputs),
        )

    def prepare(
        self,
        admission_id: str,
        receipt_path: str | Path,
        staged_outputs: str | Path,
        now: Callable[[], datetime | str],
    ) -> str:
        if not callable(now):
            raise PublicationError("now must be a callable clock")
        admission = self.ledger.get(admission_id)
        claim = self.ledger.get_claim(admission_id)
        if admission is None or claim is None:
            raise PublicationError("immutable admission and claimed executor are required")
        if claim.execution_deadline != admission.execution_deadline:
            raise PublicationError("persisted claim deadline does not match immutable admission")
        current = _time(now())
        receipt_path = Path(receipt_path)
        try:
            receipt_bytes = read_receipt_snapshot(receipt_path)
            if len(receipt_bytes) > MAX_RECEIPT_BYTES:
                raise PublicationError("receipt exceeds the maximum size")
            payload = verify_receipt_bytes(
                receipt_bytes,
                self.trusted_controller_keys,
                self.openssl,
                _expected(admission),
                list(admission.required_output_paths),
            )
        except (OSError, ReceiptError) as exc:
            raise PublicationError(f"receipt verification failed: {exc}") from exc
        if payload["admission_id"] != admission_id:
            raise PublicationError("receipt admission_id mismatch")
        if payload["observed_provider"] != {
            "provider_id": admission.provider_id,
            "run_id": admission.provider_run_id,
            "attempt": admission.provider_attempt,
        }:
            raise PublicationError("receipt provider observation does not match admission")
        executor = payload["executor"]
        if executor["executor_id"] != claim.executor_id:
            raise PublicationError("receipt executor does not match the persisted claim")
        claimed_at = _parse_receipt_time(claim.claimed_at)
        started = _parse_receipt_time(payload["started_at"])
        ended = _parse_receipt_time(payload["ended_at"])
        destroyed = _parse_receipt_time(payload["destroyed_at"])
        created = _parse_receipt_time(executor["created_at"])
        deadline = _parse_receipt_time(claim.execution_deadline)
        if not created <= claimed_at <= started <= ended <= destroyed <= deadline:
            raise PublicationError(
                "receipt execution lifecycle is outside the claimed admission deadline"
            )
        if destroyed > current:
            raise PublicationError("receipt execution has not ended by publisher preparation time")
        receipt_rows = payload["outputs"]
        receipt_paths = tuple(row["path"] for row in receipt_rows)
        if len(receipt_paths) != len(set(path.casefold() for path in receipt_paths)):
            raise PublicationError("receipt output inventory contains case-insensitive collisions")
        expected_staged = tuple((row["path"], row["size_bytes"]) for row in receipt_rows)
        staged = _read_staged_inventory(Path(staged_outputs), expected_staged)
        if {row["path"] for row in receipt_rows} != set(staged):
            raise PublicationError("staged output inventory does not exactly match receipt outputs")
        total = 0
        blobs: dict[str, bytes] = {}
        for row in receipt_rows:
            data = staged[row["path"]]
            digest = hashlib.sha256(data).hexdigest()
            if digest != row["sha256"] or len(data) != row["size_bytes"]:
                raise PublicationError(
                    f"staged output bytes do not match signed receipt: {row['path']}"
                )
            total += len(data)
            if total > MAX_TOTAL_OUTPUT_BYTES:
                raise PublicationError("staged output inventory exceeds the total size limit")
            blobs[digest] = data
        descriptor = {
            "admission_id": admission_id,
            "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "outputs": [
                {"path": row["path"], "sha256": row["sha256"], "size_bytes": row["size_bytes"]}
                for row in sorted(receipt_rows, key=lambda item: item["path"])
            ],
        }
        bundle_json = _canonical(descriptor)
        bundle_digest = hashlib.sha256(bundle_json).hexdigest()
        receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
        publication_id = hashlib.sha256(
            f"{admission_id}:{receipt_digest}:{bundle_digest}".encode("ascii")
        ).hexdigest()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT publication_id, receipt_digest, bundle_digest, status FROM publications WHERE admission_id = ?",
                (admission_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["receipt_digest"] != receipt_digest
                    or existing["bundle_digest"] != bundle_digest
                ):
                    raise PublicationError(
                        "admission already reserved with a conflicting receipt or bundle"
                    )
                # An identical PREPARED/COMMITTED retry after publication_deadline is
                # recovery of the existing reservation, not a new authorization.
                connection.commit()
                return existing["publication_id"]
            publication_deadline = _parse_receipt_time(admission.publication_deadline)
            reservation_time = _time(now())
            if reservation_time >= publication_deadline:
                raise PublicationError(
                    "admission publication deadline expired before new reservation"
                )
            connection.execute(
                "INSERT INTO publications VALUES (?, ?, ?, ?, ?, 'PREPARED')",
                (publication_id, admission_id, receipt_digest, bundle_digest, bundle_json),
            )
            connection.executemany(
                "INSERT INTO publication_blobs VALUES (?, ?, ?)",
                [(publication_id, digest, data) for digest, data in blobs.items()],
            )
            connection.execute(
                "INSERT INTO publication_receipts VALUES (?, ?, ?)",
                (publication_id, receipt_digest, receipt_bytes),
            )
            connection.commit()
            return publication_id
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def commit(self, publication_id: str) -> Path:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT admission_id, receipt_digest, bundle_digest, bundle_json, status FROM publications WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise PublicationError("unknown publication_id")
        try:
            bundle = json.loads(bytes(row["bundle_json"]).decode("ascii"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise PublicationError("outbox bundle JSON is invalid") from exc
        if not isinstance(bundle, dict) or set(bundle) != {
            "admission_id",
            "receipt_sha256",
            "outputs",
        }:
            raise PublicationError("outbox bundle descriptor has an invalid shape")
        bundle_digest = row["bundle_digest"]
        if hashlib.sha256(_canonical(bundle)).hexdigest() != bundle_digest:
            raise PublicationError("outbox bundle digest mismatch")
        if bundle["admission_id"] != row["admission_id"]:
            raise PublicationError("outbox bundle admission_id mismatch")
        if bundle["receipt_sha256"] != row["receipt_digest"]:
            raise PublicationError("outbox bundle does not bind the verified receipt digest")
        rows = bundle["outputs"]
        if not isinstance(rows, list) or len(rows) > MAX_OUTPUTS:
            raise PublicationError("outbox bundle output inventory has an invalid size")
        expected_blob_sizes: dict[str, int] = {}
        output_paths: list[str] = []
        total_size = 0
        for index, output in enumerate(rows):
            if not isinstance(output, dict) or set(output) != {"path", "sha256", "size_bytes"}:
                raise PublicationError(f"outbox bundle output row {index} has an invalid shape")
            path, digest, size = output["path"], output["sha256"], output["size_bytes"]
            if not _valid_output_path(path):
                raise PublicationError(f"outbox bundle output path {index} is invalid")
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise PublicationError(f"outbox bundle output digest {index} is invalid")
            if (
                isinstance(size, bool)
                or not isinstance(size, int)
                or not 0 <= size <= MAX_OUTPUT_BYTES
            ):
                raise PublicationError(f"outbox bundle output size {index} is invalid")
            if digest in expected_blob_sizes and expected_blob_sizes[digest] != size:
                raise PublicationError("same output digest has conflicting sizes")
            expected_blob_sizes[digest] = size
            total_size += size
            output_paths.append(path)
        if total_size > MAX_TOTAL_OUTPUT_BYTES:
            raise PublicationError("outbox bundle output inventory exceeds the total size limit")
        try:
            _check_path_set(output_paths, "outbox bundle outputs")
        except ReceiptError as exc:
            raise PublicationError(f"outbox bundle output paths conflict: {exc}") from exc
        connection = self._connect()
        try:
            blob_rows = connection.execute(
                "SELECT digest, data FROM publication_blobs WHERE publication_id = ? ORDER BY digest",
                (publication_id,),
            ).fetchall()
            receipt_row = connection.execute(
                "SELECT digest, data FROM publication_receipts WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
        finally:
            connection.close()
        if receipt_row is None:
            raise PublicationError("outbox is missing the verified receipt bytes")
        receipt_bytes = bytes(receipt_row["data"])
        if (
            receipt_row["digest"] != row["receipt_digest"]
            or hashlib.sha256(receipt_bytes).hexdigest() != row["receipt_digest"]
            or len(receipt_bytes) > MAX_RECEIPT_BYTES
        ):
            raise PublicationError("outbox receipt bytes do not match the verified receipt digest")
        actual_blob_digests = {blob_row["digest"] for blob_row in blob_rows}
        if actual_blob_digests != set(expected_blob_sizes):
            raise PublicationError("outbox blobs do not match the bundle output inventory")
        blobs: dict[str, bytes] = {}
        for blob_row in blob_rows:
            digest, data = blob_row["digest"], bytes(blob_row["data"])
            if (
                not _SHA256.fullmatch(digest)
                or hashlib.sha256(data).hexdigest() != digest
                or len(data) != expected_blob_sizes[digest]
            ):
                raise PublicationError("outbox blob digest mismatch")
            blobs[digest] = data
        admission = self.ledger.get(row["admission_id"])
        claim = self.ledger.get_claim(row["admission_id"])
        if admission is None or claim is None:
            raise PublicationError("committed publication lacks its immutable admission or claim")
        if claim.execution_deadline != admission.execution_deadline:
            raise PublicationError("persisted claim deadline does not match immutable admission")
        try:
            verified_payload = verify_receipt_bytes(
                receipt_bytes,
                self.trusted_controller_keys,
                self.openssl,
                _expected(admission),
                list(admission.required_output_paths),
            )
        except ReceiptError as exc:
            raise PublicationError(f"stored receipt no longer verifies: {exc}") from exc
        verified_outputs = [
            {"path": item["path"], "sha256": item["sha256"], "size_bytes": item["size_bytes"]}
            for item in sorted(verified_payload["outputs"], key=lambda item: item["path"])
        ]
        if verified_outputs != rows:
            raise PublicationError("outbox bundle outputs differ from the signed receipt")
        if verified_payload["executor"]["executor_id"] != claim.executor_id:
            raise PublicationError("stored receipt executor differs from persisted claim")
        claimed_at = _parse_receipt_time(claim.claimed_at)
        created = _parse_receipt_time(verified_payload["executor"]["created_at"])
        started = _parse_receipt_time(verified_payload["started_at"])
        ended = _parse_receipt_time(verified_payload["ended_at"])
        destroyed = _parse_receipt_time(verified_payload["destroyed_at"])
        execution_deadline = _parse_receipt_time(claim.execution_deadline)
        if not created <= claimed_at <= started <= ended <= destroyed <= execution_deadline:
            raise PublicationError(
                "stored receipt lifecycle is outside the claimed admission deadline"
            )

        # All descriptor, receipt, and complete blob checks finish before any evidence-store writes.
        bundle_dir = self.evidence_store / "bundles"
        blob_dir = self.evidence_store / "blobs"
        receipt_dir = self.evidence_store / "receipts"
        _ensure_directory(self.evidence_store)
        for directory in (bundle_dir, blob_dir, receipt_dir):
            _ensure_directory(directory)
        receipt_path = receipt_dir / f"{row['receipt_digest']}.json"
        self._write_content_addressed(receipt_path, receipt_bytes)
        for digest, data in blobs.items():
            self._write_content_addressed(blob_dir / digest, data)
        bundle_path = bundle_dir / f"{bundle_digest}.json"
        self._write_content_addressed(bundle_path, _canonical(bundle))
        if hashlib.sha256(bundle_path.read_bytes()).hexdigest() != bundle_digest:
            raise PublicationError("stored bundle digest mismatch")
        if row["status"] != "COMMITTED":
            self._mark_committed(publication_id)
        return bundle_path

    def _mark_committed(self, publication_id: str) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE publications SET status = 'COMMITTED' WHERE publication_id = ? AND status = 'PREPARED'",
                (publication_id,),
            )
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _write_content_addressed(path: Path, data: bytes) -> None:
        if path.exists():
            if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
                raise PublicationError("content-addressed evidence path contains conflicting bytes")
            _fsync_file(path)
            _fsync_directory(path.parent)
            return
        fd, temporary = tempfile.mkstemp(prefix=".evidence-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.is_symlink() or path.read_bytes() != data:
                    raise PublicationError(
                        "content-addressed evidence path contains conflicting bytes"
                    )
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        _fsync_file(path)
        _fsync_directory(path.parent)
