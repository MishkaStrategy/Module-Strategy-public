from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

from .canonical import sha256_canonical, sha256_file, utc_datetime
from .contracts import validate_source_uri
from .enums import MarketType
from .errors import StorageError, StructuredIngestionError
from .timeframes import is_timeframe_aligned, timeframe_delta, timeframe_seconds

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXTERNAL_KLINE_COLUMNS = (
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
class ExternalCorpusFile:
    symbol: str
    timeframe: str
    path: Path
    source_uri: str
    expected_rows: int
    expected_first_open_time: datetime
    expected_last_open_time: datetime
    expected_sha256: str | None = None

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if not symbol or not symbol.isalnum():
            raise ValueError("symbol must be a non-empty alphanumeric identifier")
        object.__setattr__(self, "symbol", symbol)
        timeframe = self.timeframe.strip().lower()
        timeframe_seconds(timeframe)
        object.__setattr__(self, "timeframe", timeframe)
        requested_path = self.path.expanduser()
        if requested_path.is_symlink() or not requested_path.is_file():
            raise ValueError("corpus path must be a regular non-symlink file")
        path = requested_path.resolve(strict=True)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "source_uri", validate_source_uri(self.source_uri))
        if (
            not isinstance(self.expected_rows, int)
            or isinstance(self.expected_rows, bool)
            or self.expected_rows <= 0
        ):
            raise ValueError("expected_rows must be a positive integer")
        object.__setattr__(
            self,
            "expected_first_open_time",
            utc_datetime(self.expected_first_open_time, "expected_first_open_time"),
        )
        object.__setattr__(
            self,
            "expected_last_open_time",
            utc_datetime(self.expected_last_open_time, "expected_last_open_time"),
        )
        if self.expected_first_open_time > self.expected_last_open_time:
            raise ValueError("expected first open time must not follow last open time")
        if self.expected_sha256 is not None and _SHA256.fullmatch(self.expected_sha256) is None:
            raise ValueError("expected_sha256 must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class ExternalCorpusSpec:
    exchange_id: str
    market_type: MarketType
    required_timeframes: tuple[str, ...]
    files: tuple[ExternalCorpusFile, ...]
    max_file_bytes: int = 256 * 1024 * 1024
    spec_version: int = 1

    def __post_init__(self) -> None:
        exchange_id = self.exchange_id.strip().lower()
        if not exchange_id or not exchange_id.replace("_", "").isalnum():
            raise ValueError("exchange_id contains unsupported characters")
        object.__setattr__(self, "exchange_id", exchange_id)
        if not isinstance(self.market_type, MarketType):
            raise TypeError("market_type must be MarketType")
        timeframes = tuple(item.strip().lower() for item in self.required_timeframes)
        for timeframe in timeframes:
            timeframe_seconds(timeframe)
        if not timeframes or len(timeframes) != len(set(timeframes)):
            raise ValueError("required_timeframes must be non-empty and unique")
        object.__setattr__(self, "required_timeframes", timeframes)
        if not self.files:
            raise ValueError("corpus spec must contain files")
        keys = tuple((item.symbol, item.timeframe) for item in self.files)
        if len(keys) != len(set(keys)):
            raise ValueError("corpus files must be unique by symbol and timeframe")
        expected = set(timeframes)
        by_symbol: dict[str, set[str]] = {}
        for item in self.files:
            by_symbol.setdefault(item.symbol, set()).add(item.timeframe)
        incomplete = {
            symbol: sorted(expected - observed)
            for symbol, observed in by_symbol.items()
            if observed != expected
        }
        extras = {
            symbol: sorted(observed - expected)
            for symbol, observed in by_symbol.items()
            if observed - expected
        }
        if incomplete or extras:
            raise ValueError(
                f"corpus timeframe matrix mismatch; missing={incomplete}, extras={extras}"
            )
        if (
            not isinstance(self.max_file_bytes, int)
            or isinstance(self.max_file_bytes, bool)
            or self.max_file_bytes <= 0
        ):
            raise ValueError("max_file_bytes must be a positive integer")
        if self.spec_version != 1:
            raise ValueError("unsupported corpus spec version")


@dataclass(frozen=True, slots=True)
class ExternalFileEvidence:
    symbol: str
    timeframe: str
    source_uri: str
    checksum_sha256: str
    byte_size: int
    rows: int
    first_open_time: datetime
    last_open_time: datetime
    gap_events: int
    duplicate_events: int
    schema_fingerprint: str

    def identity_payload(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "source_uri": self.source_uri,
            "checksum_sha256": self.checksum_sha256,
            "byte_size": self.byte_size,
            "rows": self.rows,
            "first_open_time": self.first_open_time.isoformat(),
            "last_open_time": self.last_open_time.isoformat(),
            "gap_events": self.gap_events,
            "duplicate_events": self.duplicate_events,
            "schema_fingerprint": self.schema_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class ExternalCorpusManifest:
    exchange_id: str
    market_type: MarketType
    required_timeframes: tuple[str, ...]
    files: tuple[ExternalFileEvidence, ...]
    corpus_hash: str
    verified_at: datetime
    manifest_version: int = 1

    def to_payload(self) -> dict[str, object]:
        return {
            "manifest_version": self.manifest_version,
            "exchange_id": self.exchange_id,
            "market_type": self.market_type.value,
            "required_timeframes": list(self.required_timeframes),
            "files": [item.identity_payload() for item in self.files],
            "corpus_hash": self.corpus_hash,
            "verified_at": self.verified_at.isoformat(),
        }


class ExternalCorpusVerifier:
    """Deep, bounded-memory verification for an existing OHLCV Parquet corpus."""

    def verify(self, spec: ExternalCorpusSpec) -> ExternalCorpusManifest:
        evidence = tuple(
            sorted(
                (self._verify_file(spec, item) for item in spec.files),
                key=lambda item: (item.symbol, timeframe_seconds(item.timeframe)),
            )
        )
        identity = {
            "manifest_version": 1,
            "exchange_id": spec.exchange_id,
            "market_type": spec.market_type.value,
            "required_timeframes": list(spec.required_timeframes),
            "files": [item.identity_payload() for item in evidence],
        }
        return ExternalCorpusManifest(
            exchange_id=spec.exchange_id,
            market_type=spec.market_type,
            required_timeframes=spec.required_timeframes,
            files=evidence,
            corpus_hash=sha256_canonical(identity),
            verified_at=datetime.now(UTC),
        )

    def _verify_file(
        self,
        spec: ExternalCorpusSpec,
        source: ExternalCorpusFile,
    ) -> ExternalFileEvidence:
        byte_size = source.path.stat().st_size
        if byte_size <= 0 or byte_size > spec.max_file_bytes:
            raise StructuredIngestionError(
                "EXTERNAL_CORPUS_FILE_SIZE_INVALID",
                "external corpus file exceeds the configured immutable size bound",
                False,
                "external_corpus_validation",
                {
                    "symbol": source.symbol,
                    "timeframe": source.timeframe,
                    "byte_size": byte_size,
                    "max_file_bytes": spec.max_file_bytes,
                },
            )
        checksum = sha256_file(source.path)
        if source.expected_sha256 is not None and checksum != source.expected_sha256:
            raise StructuredIngestionError(
                "EXTERNAL_CORPUS_CHECKSUM_MISMATCH",
                "external corpus file checksum differs from expected evidence",
                False,
                "external_corpus_validation",
                {"symbol": source.symbol, "timeframe": source.timeframe},
            )
        parquet = self._parquet_file(source.path)
        rows = int(parquet.metadata.num_rows)
        if rows != source.expected_rows:
            raise StructuredIngestionError(
                "EXTERNAL_CORPUS_ROW_COUNT_MISMATCH",
                "external corpus row count differs from summary evidence",
                False,
                "external_corpus_validation",
                {
                    "symbol": source.symbol,
                    "timeframe": source.timeframe,
                    "expected": source.expected_rows,
                    "observed": rows,
                },
            )
        expected_rows = (
            int(
                (source.expected_last_open_time - source.expected_first_open_time)
                / timeframe_delta(source.timeframe)
            )
            + 1
        )
        if expected_rows != source.expected_rows:
            raise StructuredIngestionError(
                "EXTERNAL_CORPUS_SUMMARY_ARITHMETIC_MISMATCH",
                "summary bounds and row count do not form a contiguous timeframe series",
                False,
                "external_corpus_validation",
                {"symbol": source.symbol, "timeframe": source.timeframe},
            )

        first: datetime | None = None
        last: datetime | None = None
        previous: datetime | None = None
        observed_rows = 0
        gaps = 0
        duplicates = 0
        step = timeframe_delta(source.timeframe)
        for batch in parquet.iter_batches(batch_size=65_536, columns=list(_EXTERNAL_KLINE_COLUMNS)):
            for row in batch.to_pylist():
                open_time = self._timestamp(row["open_time"], "open_time")
                close_time = self._timestamp(row["close_time"], "close_time")
                if not is_timeframe_aligned(open_time, source.timeframe):
                    self._fail(source, "EXTERNAL_CORPUS_TIMEFRAME_ALIGNMENT_INVALID")
                if close_time - open_time not in {
                    step,
                    step - timedelta(milliseconds=1),
                    step - timedelta(microseconds=1),
                }:
                    self._fail(source, "EXTERNAL_CORPUS_CLOSE_TIME_INVALID")
                self._validate_numeric_row(source, row)
                if previous is not None:
                    delta = open_time - previous
                    if delta == timedelta(0):
                        duplicates += 1
                    elif delta != step:
                        gaps += 1
                first = open_time if first is None else first
                last = open_time
                previous = open_time
                observed_rows += 1
        if observed_rows != rows or first is None or last is None:
            self._fail(source, "EXTERNAL_CORPUS_STREAM_COUNT_MISMATCH")
        if first != source.expected_first_open_time:
            self._fail(source, "EXTERNAL_CORPUS_START_MISMATCH")
        if last != source.expected_last_open_time:
            self._fail(source, "EXTERNAL_CORPUS_END_MISMATCH")
        if gaps or duplicates:
            raise StructuredIngestionError(
                "EXTERNAL_CORPUS_CONTINUITY_INVALID",
                "external corpus contains gaps or duplicate open timestamps",
                False,
                "external_corpus_validation",
                {
                    "symbol": source.symbol,
                    "timeframe": source.timeframe,
                    "gap_events": gaps,
                    "duplicate_events": duplicates,
                },
            )
        schema_fingerprint = sha256_canonical(
            {
                "columns": list(_EXTERNAL_KLINE_COLUMNS),
                "arrow_schema": str(parquet.schema_arrow),
            }
        )
        return ExternalFileEvidence(
            symbol=source.symbol,
            timeframe=source.timeframe,
            source_uri=source.source_uri,
            checksum_sha256=checksum,
            byte_size=byte_size,
            rows=rows,
            first_open_time=first,
            last_open_time=last,
            gap_events=gaps,
            duplicate_events=duplicates,
            schema_fingerprint=schema_fingerprint,
        )

    @staticmethod
    def _parquet_file(path: Path) -> Any:
        try:
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise StorageError(
                "PARQUET_DEPENDENCY_MISSING",
                "external corpus verification requires pyarrow; install [parquet]",
                False,
                "external_corpus_validation",
            ) from exc
        parquet = pq.ParquetFile(path)
        observed = tuple(parquet.schema_arrow.names)
        if observed != _EXTERNAL_KLINE_COLUMNS:
            raise StructuredIngestionError(
                "EXTERNAL_CORPUS_SCHEMA_MISMATCH",
                "external corpus columns differ from the supported Binance kline schema",
                False,
                "external_corpus_validation",
                {"expected": list(_EXTERNAL_KLINE_COLUMNS), "observed": list(observed)},
            )
        return parquet

    @staticmethod
    def _timestamp(value: object, name: str) -> datetime:
        if not isinstance(value, datetime):
            raise ValueError(f"{name} must be a timezone-aware Parquet timestamp")
        return utc_datetime(value, name)

    @staticmethod
    def _validate_numeric_row(source: ExternalCorpusFile, row: dict[str, object]) -> None:
        numeric = (
            "open",
            "high",
            "low",
            "close",
            "volume",
            "quote_volume",
            "taker_buy_base",
            "taker_buy_quote",
        )
        values: dict[str, float] = {}
        for name in numeric:
            value = row[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                ExternalCorpusVerifier._fail(source, "EXTERNAL_CORPUS_NUMERIC_TYPE_INVALID")
            number = float(value)
            if not math.isfinite(number):
                ExternalCorpusVerifier._fail(source, "EXTERNAL_CORPUS_NON_FINITE_VALUE")
            values[name] = number
        if min(values["open"], values["high"], values["low"], values["close"]) <= 0:
            ExternalCorpusVerifier._fail(source, "EXTERNAL_CORPUS_PRICE_NON_POSITIVE")
        if values["high"] < max(values["open"], values["close"], values["low"]):
            ExternalCorpusVerifier._fail(source, "EXTERNAL_CORPUS_OHLC_GEOMETRY_INVALID")
        if values["low"] > min(values["open"], values["close"], values["high"]):
            ExternalCorpusVerifier._fail(source, "EXTERNAL_CORPUS_OHLC_GEOMETRY_INVALID")
        for name in ("volume", "quote_volume", "taker_buy_base", "taker_buy_quote"):
            if values[name] < 0:
                ExternalCorpusVerifier._fail(source, "EXTERNAL_CORPUS_VOLUME_NEGATIVE")
        trades = row["trades"]
        if isinstance(trades, bool) or not isinstance(trades, int) or trades < 0:
            ExternalCorpusVerifier._fail(source, "EXTERNAL_CORPUS_TRADES_INVALID")

    @staticmethod
    def _fail(source: ExternalCorpusFile, code: str) -> NoReturn:
        raise StructuredIngestionError(
            code,
            "external corpus failed deep integrity validation",
            False,
            "external_corpus_validation",
            {"symbol": source.symbol, "timeframe": source.timeframe},
        )


def load_external_corpus_spec(path: Path) -> ExternalCorpusSpec:
    requested_path = path.expanduser()
    if requested_path.is_symlink() or not requested_path.is_file():
        raise ValueError("external corpus spec must be a regular non-symlink file")
    source = requested_path.resolve(strict=True)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("external corpus spec must be a JSON object")
    expected = {
        "spec_version",
        "exchange_id",
        "market_type",
        "required_timeframes",
        "max_file_bytes",
        "files",
    }
    if set(payload) != expected:
        raise ValueError(
            "external corpus spec fields mismatch; "
            f"missing={sorted(expected - set(payload))}, unknown={sorted(set(payload) - expected)}"
        )
    required_timeframes = payload["required_timeframes"]
    files_payload = payload["files"]
    if not isinstance(required_timeframes, list) or any(
        not isinstance(item, str) for item in required_timeframes
    ):
        raise TypeError("required_timeframes must be an array of strings")
    if not isinstance(files_payload, list):
        raise TypeError("files must be an array")
    file_keys = {
        "symbol",
        "timeframe",
        "path",
        "source_uri",
        "expected_rows",
        "expected_first_open_time",
        "expected_last_open_time",
        "expected_sha256",
    }
    files: list[ExternalCorpusFile] = []
    for item in files_payload:
        if not isinstance(item, dict) or set(item) != file_keys:
            raise ValueError("external corpus file fields mismatch")
        expected_rows = item["expected_rows"]
        if not isinstance(expected_rows, int) or isinstance(expected_rows, bool):
            raise TypeError("expected_rows must be an integer")
        expected_sha = item["expected_sha256"]
        if expected_sha is not None and not isinstance(expected_sha, str):
            raise TypeError("expected_sha256 must be a string or null")
        files.append(
            ExternalCorpusFile(
                symbol=_required_string(item["symbol"], "symbol"),
                timeframe=_required_string(item["timeframe"], "timeframe"),
                path=Path(_required_string(item["path"], "path")),
                source_uri=_required_string(item["source_uri"], "source_uri"),
                expected_rows=expected_rows,
                expected_first_open_time=_required_time(
                    item["expected_first_open_time"], "expected_first_open_time"
                ),
                expected_last_open_time=_required_time(
                    item["expected_last_open_time"], "expected_last_open_time"
                ),
                expected_sha256=expected_sha,
            )
        )
    spec_version = payload["spec_version"]
    max_file_bytes = payload["max_file_bytes"]
    if not isinstance(spec_version, int) or isinstance(spec_version, bool):
        raise TypeError("spec_version must be an integer")
    if not isinstance(max_file_bytes, int) or isinstance(max_file_bytes, bool):
        raise TypeError("max_file_bytes must be an integer")
    return ExternalCorpusSpec(
        exchange_id=_required_string(payload["exchange_id"], "exchange_id"),
        market_type=MarketType(_required_string(payload["market_type"], "market_type")),
        required_timeframes=tuple(required_timeframes),
        files=tuple(files),
        max_file_bytes=max_file_bytes,
        spec_version=spec_version,
    )


def write_external_corpus_manifest(path: Path, manifest: ExternalCorpusManifest) -> Path:
    requested_path = path.expanduser()
    if requested_path.is_symlink():
        raise ValueError("manifest destination must not be a symlink")
    destination = requested_path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    payload = (
        json.dumps(
            manifest.to_payload(),
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
            raise ValueError("manifest destination must be a regular file")
        if sha256_file(destination) == incoming:
            return destination
        existing = json.loads(destination.read_text(encoding="utf-8"))
        if isinstance(existing, dict) and existing.get("corpus_hash") == manifest.corpus_hash:
            return destination
        raise StorageError(
            "IMMUTABLE_CONFLICT",
            "external corpus manifest already exists with different content",
            False,
            "external_corpus_validation",
        )
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
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


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{name} must be a non-empty string")
    return value


def _required_time(value: object, name: str) -> datetime:
    text = _required_string(value, name)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return utc_datetime(parsed, name)
