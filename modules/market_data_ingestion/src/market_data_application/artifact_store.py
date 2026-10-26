from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import IO, Any

from market_data_ingestion.canonical import primitive, sha256_canonical, sha256_file
from market_data_ingestion.contracts import DatasetDescriptor, DatasetManifest
from market_data_ingestion.hashing import dataset_hash
from market_data_ingestion.integrity import RegisteredPathError, resolve_registered_path
from market_data_ingestion.storage import LocalStorage

from .artifact_contracts import ArtifactContent, ArtifactDescriptor
from .errors import (
    ArtifactIntegrityError,
    ArtifactLimitError,
    ArtifactNotFoundError,
    ArtifactStorageError,
)

_CHUNK_SIZE = 1024 * 1024


class ArtifactStore:
    """Immutable, deterministic ZIP artifacts rooted below the Market Data storage root."""

    def __init__(
        self,
        storage: LocalStorage,
        *,
        max_artifact_bytes: int,
        retention_count: int,
        retention_total_bytes: int,
    ) -> None:
        self.storage = storage
        self.max_artifact_bytes = max_artifact_bytes
        self.retention_count = retention_count
        self.retention_total_bytes = retention_total_bytes
        self.root = storage.root / "artifacts"
        if self.root.is_symlink():
            raise ArtifactStorageError("artifact root must not be a symbolic link")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o750)
        if self.root.resolve().parent != storage.root:
            raise ArtifactStorageError("artifact root escapes the configured storage root")

    @staticmethod
    def artifact_id(descriptor: DatasetDescriptor) -> str:
        digest = sha256_canonical(
            {
                "dataset_id": descriptor.dataset_id,
                "version": descriptor.version,
                "dataset_hash": descriptor.dataset_hash,
                "format": "portable-dataset-zip-v1",
            }
        )
        return f"mdi_artifact_{digest[:24]}"

    def export(
        self, descriptor: DatasetDescriptor, manifest: DatasetManifest
    ) -> ArtifactDescriptor:
        self._validate_identity(descriptor, manifest)
        artifact_id = self.artifact_id(descriptor)
        lock_name = f"export-{artifact_id}"
        with self.storage.lock(lock_name):
            existing = self._complete_existing(artifact_id)
            if existing is not None:
                return existing
            built, temporary = self._build_candidate(descriptor, manifest, artifact_id)
            try:
                published = self._publish_or_recover(built, temporary)
            finally:
                temporary.unlink(missing_ok=True)
            self._enforce_retention(protected=published.artifact_id)
            return published

    def get(self, artifact_id: str) -> ArtifactDescriptor:
        self._validate_artifact_id(artifact_id)
        metadata_path = self._metadata_path(artifact_id)
        payload_path = self._payload_path(artifact_id)
        if not metadata_path.exists() and not payload_path.exists():
            raise ArtifactNotFoundError(artifact_id)
        if not metadata_path.is_file() or not payload_path.is_file():
            raise ArtifactIntegrityError(
                artifact_id,
                "artifact ZIP and metadata sidecar must both exist as regular files",
            )
        if metadata_path.is_symlink() or payload_path.is_symlink():
            raise ArtifactIntegrityError(artifact_id, "artifact paths must not be symbolic links")
        descriptor = self._read_metadata(metadata_path, artifact_id)
        self._verify_payload(payload_path, descriptor)
        return descriptor

    def content(self, artifact_id: str) -> ArtifactContent:
        descriptor = self.get(artifact_id)
        return ArtifactContent(descriptor=descriptor, path=self._payload_path(artifact_id))

    def _complete_existing(self, artifact_id: str) -> ArtifactDescriptor | None:
        metadata = self._metadata_path(artifact_id)
        payload = self._payload_path(artifact_id)
        if not metadata.exists() or not payload.exists():
            return None
        return self.get(artifact_id)

    def _build_candidate(
        self,
        descriptor: DatasetDescriptor,
        manifest: DatasetManifest,
        artifact_id: str,
    ) -> tuple[ArtifactDescriptor, Path]:
        members = self._members(descriptor, manifest)
        fd, name = tempfile.mkstemp(
            prefix=f".{artifact_id}.",
            suffix=".zip.tmp",
            dir=self.root,
        )
        temporary = Path(name)
        os.close(fd)
        try:
            with zipfile.ZipFile(
                temporary,
                mode="w",
                compression=zipfile.ZIP_STORED,
                allowZip64=True,
            ) as archive:
                for archive_name, source in members:
                    info = self._zip_info(archive_name, manifest.created_at)
                    with closing(archive.open(info, mode="w", force_zip64=True)) as output:
                        if isinstance(source, bytes):
                            output.write(source)
                        else:
                            self._copy_file(
                                source,
                                output,
                                expected_checksum=self._source_checksum(
                                    archive_name,
                                    manifest,
                                ),
                            )
            self._fsync_file(temporary)
            byte_size = temporary.stat().st_size
            if byte_size > self.max_artifact_bytes:
                raise ArtifactLimitError(
                    "generated artifact exceeds the configured byte-size limit",
                    observed=byte_size,
                    limit=self.max_artifact_bytes,
                )
            checksum = sha256_file(temporary)
            result = ArtifactDescriptor(
                artifact_id=artifact_id,
                dataset_id=descriptor.dataset_id,
                version=descriptor.version,
                dataset_hash=descriptor.dataset_hash,
                filename=f"{artifact_id}.zip",
                checksum_sha256=checksum,
                byte_size=byte_size,
                file_count=len(members),
                created_at=manifest.created_at,
            )
            return result, temporary
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _members(
        self,
        descriptor: DatasetDescriptor,
        manifest: DatasetManifest,
    ) -> tuple[tuple[str, bytes | Path], ...]:
        descriptor_payload = self._json_bytes(primitive(descriptor))
        manifest_payload = primitive(manifest)
        if not isinstance(manifest_payload, dict):
            raise ArtifactIntegrityError(
                self.artifact_id(descriptor),
                "dataset manifest did not serialize to an object",
            )
        partitions = manifest_payload.get("partitions")
        if not isinstance(partitions, list) or len(partitions) != len(manifest.partitions):
            raise ArtifactIntegrityError(
                self.artifact_id(descriptor),
                "dataset manifest partitions could not be normalized for transport",
            )

        members: dict[str, bytes | Path] = {
            "descriptor.json": descriptor_payload,
        }
        source_total = len(descriptor_payload)
        for index, partition in enumerate(manifest.partitions):
            try:
                source = resolve_registered_path(
                    self.storage.root,
                    partition.storage_uri,
                    kind="partition",
                )
            except RegisteredPathError as exc:
                raise ArtifactIntegrityError(
                    self.artifact_id(descriptor),
                    "registered partition path is unsafe",
                ) from exc
            if source.is_symlink() or not source.is_file():
                raise ArtifactIntegrityError(
                    self.artifact_id(descriptor),
                    "registered partition is missing or is not a regular file",
                )
            actual_checksum = sha256_file(source)
            if actual_checksum != partition.checksum_sha256:
                raise ArtifactIntegrityError(
                    self.artifact_id(descriptor),
                    "registered partition checksum does not match its manifest",
                )
            archive_name = self._partition_archive_name(partition.storage_uri)
            if archive_name in members:
                raise ArtifactIntegrityError(
                    self.artifact_id(descriptor),
                    "registered partitions produce duplicate archive member names",
                )
            source_size = source.stat().st_size
            source_total += source_size
            if source_total > self.max_artifact_bytes:
                raise ArtifactLimitError(
                    "dataset export sources exceed the configured byte-size limit",
                    observed=source_total,
                    limit=self.max_artifact_bytes,
                )
            members[archive_name] = source
            item = partitions[index]
            if not isinstance(item, dict):
                raise ArtifactIntegrityError(
                    self.artifact_id(descriptor),
                    "dataset manifest partition is not an object",
                )
            item["storage_uri"] = archive_name

        transport_manifest = self._json_bytes(manifest_payload)
        source_total += len(transport_manifest)
        if source_total > self.max_artifact_bytes:
            raise ArtifactLimitError(
                "dataset export sources exceed the configured byte-size limit",
                observed=source_total,
                limit=self.max_artifact_bytes,
            )
        members["manifest.json"] = transport_manifest
        return tuple(sorted(members.items()))

    def _source_checksum(self, archive_name: str, manifest: DatasetManifest) -> str:
        for partition in manifest.partitions:
            if self._partition_archive_name(partition.storage_uri) == archive_name:
                return partition.checksum_sha256
        raise ArtifactStorageError("artifact source checksum was not found in the manifest")

    @staticmethod
    def _partition_archive_name(storage_uri: str) -> str:
        pure = PurePosixPath(storage_uri)
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            raise ArtifactStorageError("registered partition URI is unsafe for export")
        return (PurePosixPath("data") / pure).as_posix()

    @staticmethod
    def _zip_info(name: str, created_at: datetime) -> zipfile.ZipInfo:
        value = created_at.astimezone(UTC)
        year = min(max(value.year, 1980), 2107)
        info = zipfile.ZipInfo(
            filename=name,
            date_time=(
                year,
                value.month,
                value.day,
                value.hour,
                value.minute,
                value.second // 2 * 2,
            ),
        )
        info.compress_type = zipfile.ZIP_STORED
        info.create_system = 3
        info.external_attr = 0o100640 << 16
        info.flag_bits |= 0x800
        return info

    def _copy_file(
        self,
        source: Path,
        output: IO[bytes],
        *,
        expected_checksum: str,
    ) -> None:
        copied = 0
        digest = hashlib.sha256()
        with source.open("rb") as input_file:
            while chunk := input_file.read(_CHUNK_SIZE):
                copied += len(chunk)
                if copied > self.max_artifact_bytes:
                    raise ArtifactLimitError(
                        "partition exceeds the configured artifact byte-size limit",
                        observed=copied,
                        limit=self.max_artifact_bytes,
                    )
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != expected_checksum:
            raise ArtifactIntegrityError(
                "unknown",
                "registered partition changed while the artifact was being built",
            )

    def _publish_or_recover(
        self,
        descriptor: ArtifactDescriptor,
        temporary: Path,
    ) -> ArtifactDescriptor:
        payload = self._payload_path(descriptor.artifact_id)
        metadata = self._metadata_path(descriptor.artifact_id)
        if payload.is_symlink() or metadata.is_symlink():
            raise ArtifactIntegrityError(
                descriptor.artifact_id,
                "artifact paths must not be symbolic links",
            )

        existing_metadata: ArtifactDescriptor | None = None
        if metadata.exists():
            if not metadata.is_file():
                raise ArtifactIntegrityError(
                    descriptor.artifact_id,
                    "existing artifact metadata is not a regular file",
                )
            existing_metadata = self._read_metadata(metadata, descriptor.artifact_id)
            if existing_metadata != descriptor:
                raise ArtifactIntegrityError(
                    descriptor.artifact_id,
                    "artifact metadata differs from deterministic rebuild",
                )

        if payload.exists():
            if not payload.is_file():
                raise ArtifactIntegrityError(
                    descriptor.artifact_id,
                    "existing artifact payload is not a safe regular file",
                )
            if sha256_file(payload) != descriptor.checksum_sha256:
                raise ArtifactIntegrityError(
                    descriptor.artifact_id,
                    "deterministic artifact rebuild does not match the existing ZIP",
                )
            if payload.stat().st_size != descriptor.byte_size:
                raise ArtifactIntegrityError(
                    descriptor.artifact_id,
                    "existing artifact ZIP size differs from deterministic rebuild",
                )
        else:
            os.replace(temporary, payload)
            self._fsync_directory()

        if existing_metadata is None:
            self._atomic_metadata_write(metadata, descriptor)
        return self.get(descriptor.artifact_id)

    def _atomic_metadata_write(
        self,
        destination: Path,
        descriptor: ArtifactDescriptor,
    ) -> None:
        fd, name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=self.root,
        )
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(self._json_bytes(descriptor.model_dump(mode="json")))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o640)
            if destination.exists() or destination.is_symlink():
                raise ArtifactIntegrityError(
                    descriptor.artifact_id,
                    "artifact metadata appeared concurrently",
                )
            os.replace(temporary, destination)
            self._fsync_directory()
        finally:
            temporary.unlink(missing_ok=True)

    def _read_metadata(self, path: Path, artifact_id: str) -> ArtifactDescriptor:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            descriptor = ArtifactDescriptor.model_validate(payload)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ArtifactIntegrityError(artifact_id, "artifact metadata is corrupt") from exc
        if descriptor.artifact_id != artifact_id:
            raise ArtifactIntegrityError(artifact_id, "artifact metadata identity mismatch")
        if descriptor.filename != self._payload_path(artifact_id).name:
            raise ArtifactIntegrityError(artifact_id, "artifact metadata filename mismatch")
        return descriptor

    @staticmethod
    def _verify_payload(path: Path, descriptor: ArtifactDescriptor) -> None:
        if path.stat().st_size != descriptor.byte_size:
            raise ArtifactIntegrityError(descriptor.artifact_id, "artifact byte size mismatch")
        if sha256_file(path) != descriptor.checksum_sha256:
            raise ArtifactIntegrityError(descriptor.artifact_id, "artifact checksum mismatch")
        try:
            with zipfile.ZipFile(path, "r") as archive:
                names = archive.namelist()
                if len(names) != descriptor.file_count or len(names) != len(set(names)):
                    raise ArtifactIntegrityError(
                        descriptor.artifact_id,
                        "artifact ZIP file count or member uniqueness is invalid",
                    )
                if any(
                    PurePosixPath(name).is_absolute()
                    or any(part in {"", ".", ".."} for part in PurePosixPath(name).parts)
                    for name in names
                ):
                    raise ArtifactIntegrityError(
                        descriptor.artifact_id,
                        "artifact ZIP contains an unsafe member path",
                    )
                if archive.testzip() is not None:
                    raise ArtifactIntegrityError(
                        descriptor.artifact_id,
                        "artifact ZIP CRC verification failed",
                    )
        except zipfile.BadZipFile as exc:
            raise ArtifactIntegrityError(descriptor.artifact_id, "artifact ZIP is corrupt") from exc

    def _enforce_retention(self, *, protected: str) -> None:
        descriptors: list[ArtifactDescriptor] = []
        for path in self.root.glob("mdi_artifact_*.json"):
            if path.is_symlink() or not path.is_file():
                continue
            artifact_id = path.stem
            try:
                descriptors.append(self.get(artifact_id))
            except (ArtifactIntegrityError, ArtifactNotFoundError):
                continue
        descriptors.sort(key=lambda item: (item.created_at, item.artifact_id))
        total = sum(item.byte_size for item in descriptors)
        while len(descriptors) > self.retention_count or total > self.retention_total_bytes:
            candidate = next(
                (item for item in descriptors if item.artifact_id != protected),
                None,
            )
            if candidate is None:
                break
            self._metadata_path(candidate.artifact_id).unlink(missing_ok=True)
            self._payload_path(candidate.artifact_id).unlink(missing_ok=True)
            descriptors.remove(candidate)
            total -= candidate.byte_size
        self._fsync_directory()

    def _payload_path(self, artifact_id: str) -> Path:
        self._validate_artifact_id(artifact_id)
        return self.root / f"{artifact_id}.zip"

    def _metadata_path(self, artifact_id: str) -> Path:
        self._validate_artifact_id(artifact_id)
        return self.root / f"{artifact_id}.json"

    @staticmethod
    def _validate_artifact_id(artifact_id: str) -> None:
        prefix = "mdi_artifact_"
        digest = artifact_id.removeprefix(prefix)
        if not artifact_id.startswith(prefix) or len(digest) != 24:
            raise ArtifactNotFoundError(artifact_id)
        if any(character not in "0123456789abcdef" for character in digest):
            raise ArtifactNotFoundError(artifact_id)

    @staticmethod
    def _validate_identity(
        descriptor: DatasetDescriptor,
        manifest: DatasetManifest,
    ) -> None:
        artifact_id = ArtifactStore.artifact_id(descriptor)
        if descriptor.dataset_id != manifest.dataset_id or descriptor.version != manifest.version:
            raise ArtifactIntegrityError(
                artifact_id,
                "Dataset Registry descriptor and manifest identity differ",
            )
        if dataset_hash(manifest) != descriptor.dataset_hash:
            raise ArtifactIntegrityError(
                artifact_id,
                "registered manifest hash differs from the Dataset Registry descriptor",
            )

    @staticmethod
    def _json_bytes(value: Mapping[str, Any] | dict[str, Any]) -> bytes:
        return (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")

    @staticmethod
    def _fsync_file(path: Path) -> None:
        with path.open("rb") as handle:
            os.fsync(handle.fileno())

    def _fsync_directory(self) -> None:
        try:
            descriptor = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            return
