from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .canonical import primitive, sha256_canonical
from .contracts import (
    DatasetDescriptor,
    DatasetManifest,
    DataTypeValidationReport,
    DataValidationReport,
    DownloadChunk,
    ExchangeReference,
    InstrumentReference,
    NormalizedPartitionDescriptor,
    RawArtifactDescriptor,
)
from .enums import DataType, SourceKind, ValidationStatus
from .errors import StorageError, StructuredIngestionError
from .external_corpus import (
    ExternalCorpusFile,
    ExternalCorpusManifest,
    ExternalCorpusSpec,
    ExternalCorpusVerifier,
    ExternalFileEvidence,
    load_external_corpus_spec,
)
from .hashing import dataset_hash
from .integrity import verify_registered_dataset
from .manifest_io import load_manifest
from .records import KlineRecord
from .registry import SQLiteDatasetRegistry, descriptor_from_dict
from .storage import LocalStorage
from .timeframes import timeframe_delta, timeframe_seconds
from .version import MANIFEST_SCHEMA_VERSION, NORMALIZATION_VERSION

EXTERNAL_ADAPTER_VERSION = "external-corpus-v1"
EXTERNAL_REGISTRATION_POLICY_VERSION = "external-corpus-registration-v1"
_BATCH_SIZE = 65_536
_EXTERNAL_COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_buy_base",
    "taker_buy_quote",
)


@dataclass(frozen=True, slots=True)
class RegisteredExternalDataset:
    descriptor: DatasetDescriptor
    partition_count: int
    raw_artifact_count: int
    reused: bool

    def to_payload(self) -> dict[str, object]:
        descriptor = self.descriptor
        return {
            "dataset_id": descriptor.dataset_id,
            "version": descriptor.version,
            "dataset_hash": descriptor.dataset_hash,
            "instrument_id": descriptor.instrument_id,
            "timeframes": list(descriptor.timeframes),
            "start": descriptor.start.isoformat(),
            "end": descriptor.end.isoformat(),
            "validation_status": descriptor.validation_status.value,
            "partition_count": self.partition_count,
            "raw_artifact_count": self.raw_artifact_count,
            "reused": self.reused,
        }


@dataclass(frozen=True, slots=True)
class ExternalRegistrationResult:
    corpus_hash: str
    datasets: tuple[RegisteredExternalDataset, ...]
    total_rows: int
    registered_at: datetime
    registration_version: int = 1

    def to_payload(self) -> dict[str, object]:
        return {
            "registration_version": self.registration_version,
            "status": "PASS",
            "corpus_hash": self.corpus_hash,
            "dataset_count": len(self.datasets),
            "reused_dataset_count": sum(item.reused for item in self.datasets),
            "partition_count": sum(item.partition_count for item in self.datasets),
            "raw_artifact_count": sum(item.raw_artifact_count for item in self.datasets),
            "total_rows": self.total_rows,
            "registered_at": self.registered_at.isoformat(),
            "raw_market_data_uploaded": False,
            "datasets": [item.to_payload() for item in self.datasets],
        }


class ExternalCorpusRegistrar:
    """Normalize and register a previously verified external Parquet corpus."""

    def __init__(self, *, storage: LocalStorage, registry: SQLiteDatasetRegistry) -> None:
        self._storage = storage
        self._registry = registry

    def register(
        self,
        spec: ExternalCorpusSpec,
        *,
        compression: str = "zstd",
    ) -> ExternalRegistrationResult:
        verified = ExternalCorpusVerifier().verify(spec)
        return self.register_verified(spec, verified, compression=compression)

    def register_verified(
        self,
        spec: ExternalCorpusSpec,
        manifest: ExternalCorpusManifest,
        *,
        compression: str = "zstd",
    ) -> ExternalRegistrationResult:
        self._validate_verified_identity(spec, manifest)
        evidence_by_key = {(item.symbol, item.timeframe): item for item in manifest.files}
        sources_by_symbol: dict[str, list[ExternalCorpusFile]] = {}
        for source in spec.files:
            sources_by_symbol.setdefault(source.symbol, []).append(source)

        registered: list[RegisteredExternalDataset] = []
        lock_identity = "external-corpus-registration"
        with self._storage.lock(lock_identity):
            for symbol in sorted(sources_by_symbol):
                sources = tuple(
                    sorted(
                        sources_by_symbol[symbol],
                        key=lambda item: timeframe_seconds(item.timeframe),
                    )
                )
                evidence = tuple(evidence_by_key[(item.symbol, item.timeframe)] for item in sources)
                registered.append(
                    self._register_symbol(
                        spec=spec,
                        manifest=manifest,
                        symbol=symbol,
                        sources=sources,
                        evidence=evidence,
                        compression=compression,
                    )
                )

        return ExternalRegistrationResult(
            corpus_hash=manifest.corpus_hash,
            datasets=tuple(registered),
            total_rows=sum(item.rows for item in manifest.files),
            registered_at=datetime.now(UTC),
        )

    def _register_symbol(
        self,
        *,
        spec: ExternalCorpusSpec,
        manifest: ExternalCorpusManifest,
        symbol: str,
        sources: tuple[ExternalCorpusFile, ...],
        evidence: tuple[ExternalFileEvidence, ...],
        compression: str,
    ) -> RegisteredExternalDataset:
        dataset_id = self._dataset_id(spec, symbol)
        lineage_marker = f"external-corpus:{manifest.corpus_hash}"
        existing_payload = self._registry.inspect(dataset_id)
        if existing_payload is not None:
            existing = descriptor_from_dict(existing_payload)
            if lineage_marker in existing.lineage:
                report = verify_registered_dataset(
                    self._storage.root,
                    existing_payload,
                    deep=True,
                )
                if report["status"] != "ok":
                    raise StructuredIngestionError(
                        "EXTERNAL_REGISTRATION_EXISTING_INVALID",
                        "an existing registration for this corpus failed deep integrity validation",
                        False,
                        "external_corpus_registration",
                        {"dataset_id": dataset_id, "issues": report["issues"]},
                    )
                existing_manifest = load_manifest(self._storage.root, existing)
                return RegisteredExternalDataset(
                    descriptor=existing,
                    partition_count=len(existing_manifest.partitions),
                    raw_artifact_count=len(existing_manifest.source_artifacts),
                    reused=True,
                )

        version = self._registry.next_version(dataset_id)
        instrument = InstrumentReference(
            exchange_id=spec.exchange_id,
            market_type=spec.market_type,
            symbol=symbol,
            canonical_id=f"{spec.exchange_id}:{spec.market_type.value}:{symbol.lower()}",
        )
        run_id = f"external-{manifest.corpus_hash[:24]}"
        raw_artifacts: list[RawArtifactDescriptor] = []
        partitions: list[NormalizedPartitionDescriptor] = []
        reports: list[DataTypeValidationReport] = []

        for source, file_evidence in zip(sources, evidence, strict=True):
            payload = source.path.read_bytes()
            actual_checksum = hashlib.sha256(payload).hexdigest()
            if actual_checksum != file_evidence.checksum_sha256:
                raise StructuredIngestionError(
                    "EXTERNAL_CORPUS_CHANGED_AFTER_VERIFICATION",
                    "external corpus bytes changed after verification and before registration",
                    False,
                    "external_corpus_registration",
                    {"symbol": symbol, "timeframe": source.timeframe},
                )
            chunk = DownloadChunk(
                chunk_id=(
                    f"external:{symbol.lower()}:{source.timeframe}:"
                    f"{file_evidence.checksum_sha256[:16]}"
                ),
                data_type=DataType.KLINES,
                start=file_evidence.first_open_time,
                end=file_evidence.last_open_time + timeframe_delta(source.timeframe),
                source=SourceKind.OFFICIAL_EXPORT,
                timeframe=source.timeframe,
                expected_uri=file_evidence.source_uri,
            )
            raw = self._storage.write_raw(
                run_id=run_id,
                instrument=instrument,
                chunk=chunk,
                payload=payload,
                source_uri=file_evidence.source_uri,
                adapter_version=EXTERNAL_ADAPTER_VERSION,
                response_metadata={
                    "records": file_evidence.rows,
                    "symbol": symbol,
                    "timeframe": source.timeframe,
                    "corpus_hash": manifest.corpus_hash,
                    "schema_fingerprint": file_evidence.schema_fingerprint,
                    "gap_events": file_evidence.gap_events,
                    "duplicate_events": file_evidence.duplicate_events,
                    "verified_at": manifest.verified_at.isoformat(),
                },
                upstream_checksum=file_evidence.checksum_sha256,
                source_kind=SourceKind.OFFICIAL_EXPORT,
            )
            if (
                raw.checksum_sha256 != file_evidence.checksum_sha256
                or raw.byte_size != file_evidence.byte_size
                or raw.record_count != file_evidence.rows
            ):
                raise StructuredIngestionError(
                    "EXTERNAL_RAW_IMPORT_MISMATCH",
                    "immutable raw import differs from verified external evidence",
                    False,
                    "external_corpus_registration",
                    {"symbol": symbol, "timeframe": source.timeframe},
                )
            raw_artifacts.append(raw)

            timeframe_partitions = tuple(
                self._write_normalized_partitions(
                    payload=payload,
                    source=source,
                    evidence=file_evidence,
                    dataset_id=dataset_id,
                    version=version,
                    instrument=instrument,
                    source_artifact_id=raw.artifact_id,
                    compression=compression,
                )
            )
            observed_rows = sum(item.row_count for item in timeframe_partitions)
            if observed_rows != file_evidence.rows:
                raise StructuredIngestionError(
                    "EXTERNAL_NORMALIZED_ROW_COUNT_MISMATCH",
                    "normalized partitions differ from verified external row count",
                    False,
                    "external_corpus_registration",
                    {
                        "symbol": symbol,
                        "timeframe": source.timeframe,
                        "expected": file_evidence.rows,
                        "observed": observed_rows,
                    },
                )
            partitions.extend(timeframe_partitions)
            reports.append(
                DataTypeValidationReport(
                    data_type=DataType.KLINES,
                    timeframe=source.timeframe,
                    report=DataValidationReport(
                        status=ValidationStatus.VALID,
                        issues=(),
                        gaps=(),
                        duplicates=(),
                        rows_checked=file_evidence.rows,
                        created_at=manifest.verified_at,
                    ),
                )
            )

        ordered_partitions = tuple(
            sorted(
                partitions,
                key=lambda item: (
                    timeframe_seconds(item.timeframe) if item.timeframe is not None else 0,
                    item.start,
                    item.end,
                    item.partition_id,
                ),
            )
        )
        ordered_artifacts = tuple(
            sorted(raw_artifacts, key=lambda item: (item.start, item.artifact_id))
        )
        validation = DataValidationReport(
            status=ValidationStatus.VALID,
            issues=(),
            gaps=(),
            duplicates=(),
            rows_checked=sum(item.rows for item in evidence),
            created_at=manifest.verified_at,
        )
        timeframes = tuple(sorted({item.timeframe for item in sources}, key=timeframe_seconds))
        dataset_manifest = DatasetManifest(
            dataset_id=dataset_id,
            version=version,
            exchange=ExchangeReference(spec.exchange_id, EXTERNAL_ADAPTER_VERSION),
            market_type=spec.market_type,
            instrument=instrument,
            data_types=(DataType.KLINES,),
            timeframes=timeframes,
            start=min(item.start for item in ordered_partitions),
            end=max(item.end for item in ordered_partitions),
            partitions=ordered_partitions,
            source_artifacts=ordered_artifacts,
            validation=validation,
            normalization_version=NORMALIZATION_VERSION,
            validation_policy_version=EXTERNAL_REGISTRATION_POLICY_VERSION,
            schema_version=MANIFEST_SCHEMA_VERSION,
            lineage=(
                lineage_marker,
                f"external-manifest:v{manifest.manifest_version}",
                f"registration-policy:{EXTERNAL_REGISTRATION_POLICY_VERSION}",
                f"normalization:{NORMALIZATION_VERSION}",
                *(f"raw:{item.checksum_sha256}" for item in ordered_artifacts),
            ),
            created_at=manifest.verified_at,
            data_type_reports=tuple(reports),
        )
        digest = dataset_hash(dataset_manifest)
        manifest_path = self._storage.write_manifest(dataset_id, version, dataset_manifest)
        descriptor = DatasetDescriptor(
            dataset_id=dataset_id,
            version=version,
            dataset_hash=digest,
            manifest_uri=manifest_path.relative_to(self._storage.root).as_posix(),
            storage_uri=self._storage.root.as_uri(),
            exchange_id=spec.exchange_id,
            market_type=spec.market_type,
            instrument_id=instrument.canonical_id,
            data_types=(DataType.KLINES,),
            timeframes=timeframes,
            start=dataset_manifest.start,
            end=dataset_manifest.end,
            schema_version=dataset_manifest.schema_version,
            validation_status=ValidationStatus.VALID,
            unresolved_issues=(),
            lineage=dataset_manifest.lineage,
        )
        descriptor_payload = primitive(descriptor)
        if not isinstance(descriptor_payload, dict):
            raise TypeError("dataset descriptor must serialize to an object")
        integrity = verify_registered_dataset(
            self._storage.root,
            descriptor_payload,
            deep=True,
        )
        if integrity["status"] != "ok":
            raise StructuredIngestionError(
                "EXTERNAL_REGISTRATION_INTEGRITY_FAILED",
                "prepared registration failed deep registered-dataset integrity validation",
                False,
                "external_corpus_registration",
                {"dataset_id": dataset_id, "issues": integrity["issues"]},
            )
        self._registry.register(descriptor)
        return RegisteredExternalDataset(
            descriptor=descriptor,
            partition_count=len(ordered_partitions),
            raw_artifact_count=len(ordered_artifacts),
            reused=False,
        )

    def _write_normalized_partitions(
        self,
        *,
        payload: bytes,
        source: ExternalCorpusFile,
        evidence: ExternalFileEvidence,
        dataset_id: str,
        version: int,
        instrument: InstrumentReference,
        source_artifact_id: str,
        compression: str,
    ) -> Iterator[NormalizedPartitionDescriptor]:
        for records in self._month_groups(payload, source, evidence, instrument):
            yield self._storage.write_kline_partition(
                dataset_id=dataset_id,
                version=version,
                layer="normalized",
                instrument=instrument,
                timeframe=source.timeframe,
                records=records,
                source_artifact_ids=(source_artifact_id,),
                format_name="parquet",
                compression=compression,
            )

    @staticmethod
    def _month_groups(
        payload: bytes,
        source: ExternalCorpusFile,
        evidence: ExternalFileEvidence,
        instrument: InstrumentReference,
    ) -> Iterator[tuple[KlineRecord, ...]]:
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise StorageError(
                "PARQUET_DEPENDENCY_MISSING",
                "external corpus registration requires pyarrow; install [parquet]",
                False,
                "external_corpus_registration",
            ) from exc

        parquet = pq.ParquetFile(pa.BufferReader(payload))
        if tuple(parquet.schema_arrow.names) != _EXTERNAL_COLUMNS:
            raise StructuredIngestionError(
                "EXTERNAL_CORPUS_SCHEMA_MISMATCH",
                "external corpus columns changed before normalization",
                False,
                "external_corpus_registration",
                {"symbol": source.symbol, "timeframe": source.timeframe},
            )
        current_month: tuple[int, int] | None = None
        records: list[KlineRecord] = []
        observed_rows = 0
        for batch in parquet.iter_batches(batch_size=_BATCH_SIZE, columns=list(_EXTERNAL_COLUMNS)):
            for row in batch.to_pylist():
                record = KlineRecord(
                    open_time=row["open_time"],
                    close_time=row["close_time"],
                    open=str(row["open"]),
                    high=str(row["high"]),
                    low=str(row["low"]),
                    close=str(row["close"]),
                    volume=str(row["volume"]),
                    quote_volume=str(row["quote_volume"]),
                    trade_count=int(row["trades"]),
                    taker_buy_base_volume=str(row["taker_buy_base"]),
                    taker_buy_quote_volume=str(row["taker_buy_quote"]),
                    symbol=instrument.symbol,
                    timeframe=source.timeframe,
                    exchange_id=instrument.exchange_id,
                    market_type=instrument.market_type.value,
                    source_kind=SourceKind.OFFICIAL_EXPORT.value,
                    is_closed=True,
                )
                month = (record.open_time.year, record.open_time.month)
                if current_month is not None and month != current_month:
                    yield tuple(records)
                    records = []
                current_month = month
                records.append(record)
                observed_rows += 1
        if records:
            yield tuple(records)
        if observed_rows != evidence.rows:
            raise StructuredIngestionError(
                "EXTERNAL_NORMALIZATION_STREAM_COUNT_MISMATCH",
                "normalization stream count differs from verified evidence",
                False,
                "external_corpus_registration",
                {
                    "symbol": source.symbol,
                    "timeframe": source.timeframe,
                    "expected": evidence.rows,
                    "observed": observed_rows,
                },
            )

    @staticmethod
    def _dataset_id(spec: ExternalCorpusSpec, symbol: str) -> str:
        market = spec.market_type.value.replace("_", "-")
        base_timeframe = min(spec.required_timeframes, key=timeframe_seconds)
        return f"{spec.exchange_id}_{market}_{symbol.lower()}_{base_timeframe}"

    @staticmethod
    def _validate_verified_identity(
        spec: ExternalCorpusSpec,
        manifest: ExternalCorpusManifest,
    ) -> None:
        if (
            manifest.exchange_id != spec.exchange_id
            or manifest.market_type is not spec.market_type
            or manifest.required_timeframes != spec.required_timeframes
        ):
            raise StructuredIngestionError(
                "EXTERNAL_MANIFEST_SPEC_MISMATCH",
                "verified external manifest identity differs from the registration spec",
                False,
                "external_corpus_registration",
            )
        expected_keys = {(item.symbol, item.timeframe) for item in spec.files}
        observed_keys = {(item.symbol, item.timeframe) for item in manifest.files}
        if observed_keys != expected_keys:
            raise StructuredIngestionError(
                "EXTERNAL_MANIFEST_MATRIX_MISMATCH",
                "verified external manifest file matrix differs from the registration spec",
                False,
                "external_corpus_registration",
            )
        spec_by_key = {(item.symbol, item.timeframe): item for item in spec.files}
        for item in manifest.files:
            source = spec_by_key[(item.symbol, item.timeframe)]
            if (
                item.source_uri != source.source_uri
                or item.rows != source.expected_rows
                or item.first_open_time != source.expected_first_open_time
                or item.last_open_time != source.expected_last_open_time
                or item.gap_events != 0
                or item.duplicate_events != 0
                or (
                    source.expected_sha256 is not None
                    and item.checksum_sha256 != source.expected_sha256
                )
            ):
                raise StructuredIngestionError(
                    "EXTERNAL_MANIFEST_EVIDENCE_MISMATCH",
                    "verified external file evidence differs from the registration spec",
                    False,
                    "external_corpus_registration",
                    {"symbol": item.symbol, "timeframe": item.timeframe},
                )
        identity = {
            "manifest_version": manifest.manifest_version,
            "exchange_id": manifest.exchange_id,
            "market_type": manifest.market_type.value,
            "required_timeframes": list(manifest.required_timeframes),
            "files": [item.identity_payload() for item in manifest.files],
        }
        if sha256_canonical(identity) != manifest.corpus_hash:
            raise StructuredIngestionError(
                "EXTERNAL_MANIFEST_HASH_MISMATCH",
                "verified external manifest hash does not match its canonical identity",
                False,
                "external_corpus_registration",
            )


def write_external_registration_evidence(
    path: Path,
    result: ExternalRegistrationResult,
) -> Path:
    requested_path = path.expanduser()
    if requested_path.is_symlink():
        raise ValueError("registration evidence destination must not be a symlink")
    destination = requested_path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    payload = (
        json.dumps(
            result.to_payload(),
            sort_keys=True,
            indent=2,
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    incoming = hashlib.sha256(payload).hexdigest()
    if destination.exists():
        if not destination.is_file():
            raise ValueError("registration evidence destination must be a regular file")
        if hashlib.sha256(destination.read_bytes()).hexdigest() == incoming:
            return destination
        existing = json.loads(destination.read_text(encoding="utf-8"))
        if isinstance(existing, dict) and existing.get("corpus_hash") == result.corpus_hash:
            return destination
        raise StorageError(
            "IMMUTABLE_CONFLICT",
            "external registration evidence already exists with different content",
            False,
            "external_corpus_registration",
        )
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m market_data_ingestion.external_registration",
        description="Verify, normalize and register an external historical Parquet corpus",
    )
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--storage-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compression", default="zstd")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        spec = load_external_corpus_spec(args.spec)
        storage = LocalStorage(args.storage_root)
        registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
        result = ExternalCorpusRegistrar(storage=storage, registry=registry).register(
            spec,
            compression=args.compression,
        )
        target = write_external_registration_evidence(args.output, result)
        payload = result.to_payload()
        payload["evidence_path"] = str(target)
        print(json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False))
        return 0
    except (StructuredIngestionError, ValueError, OSError) as exc:
        if isinstance(exc, StructuredIngestionError):
            payload = {"status": "error", **exc.to_dict()}
        else:
            payload = {
                "status": "error",
                "code": type(exc).__name__,
                "message": str(exc),
            }
        print(json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
