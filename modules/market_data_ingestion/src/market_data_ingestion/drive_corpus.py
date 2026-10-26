from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_SUMMARY_COLUMNS = (
    "symbol",
    "timeframe",
    "status",
    "rows",
    "first_open_time",
    "last_open_time",
    "gap_events",
    "estimated_missing_5m_rows",
)
_INDEX_FIELDS = {
    "schema_version",
    "folder_id",
    "summary_file_id",
    "summary_sha256",
    "exchange_id",
    "market_type",
    "required_timeframes",
    "files",
}
_FILE_FIELDS = {
    "symbol",
    "timeframe",
    "drive_file_id",
    "filename",
    "byte_size",
}


@dataclass(frozen=True, slots=True)
class DriveFile:
    symbol: str
    timeframe: str
    drive_file_id: str
    filename: str
    byte_size: int


@dataclass(frozen=True, slots=True)
class DriveIndex:
    folder_id: str
    summary_file_id: str
    summary_sha256: str
    exchange_id: str
    market_type: str
    required_timeframes: tuple[str, ...]
    files: tuple[DriveFile, ...]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _drive_id(value: object, name: str) -> str:
    result = _text(value, name)
    if not (10 <= len(result) <= 128) or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for character in result
    ):
        raise ValueError(f"{name} contains unsupported characters")
    return result


def _sha(value: object, name: str) -> str:
    result = _text(value, name)
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise ValueError(f"{name} must be lowercase SHA-256")
    return result


def _timestamp(value: str, name: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{name} must be UTC")
    return parsed.astimezone(UTC)


def load_drive_index(path: Path) -> DriveIndex:
    requested_path = path.expanduser()
    if requested_path.is_symlink() or not requested_path.is_file():
        raise ValueError("Drive index must be a regular non-symlink file")
    source = requested_path.resolve(strict=True)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != _INDEX_FIELDS:
        raise ValueError("Drive index fields mismatch")
    if payload["schema_version"] != 1:
        raise ValueError("unsupported Drive index schema")
    timeframes = payload["required_timeframes"]
    files_payload = payload["files"]
    if not isinstance(timeframes, list) or any(not isinstance(item, str) for item in timeframes):
        raise TypeError("required_timeframes must be an array of strings")
    normalized_timeframes = tuple(_text(item, "timeframe").lower() for item in timeframes)
    if not normalized_timeframes or len(normalized_timeframes) != len(set(normalized_timeframes)):
        raise ValueError("required_timeframes must be non-empty and unique")
    if not isinstance(files_payload, list) or not files_payload:
        raise TypeError("files must be a non-empty array")

    files: list[DriveFile] = []
    for item in files_payload:
        if not isinstance(item, dict) or set(item) != _FILE_FIELDS:
            raise ValueError("Drive index file fields mismatch")
        symbol = _text(item["symbol"], "symbol").upper()
        timeframe = _text(item["timeframe"], "timeframe").lower()
        if timeframe not in normalized_timeframes:
            raise ValueError(f"unsupported timeframe for {symbol}: {timeframe}")
        filename = _text(item["filename"], "filename")
        if filename != f"{symbol}_{timeframe}.parquet" or Path(filename).name != filename:
            raise ValueError(f"non-canonical filename for {symbol}/{timeframe}")
        files.append(
            DriveFile(
                symbol=symbol,
                timeframe=timeframe,
                drive_file_id=_drive_id(item["drive_file_id"], "drive_file_id"),
                filename=filename,
                byte_size=_positive_int(item["byte_size"], "byte_size"),
            )
        )

    keys = tuple((item.symbol, item.timeframe) for item in files)
    if len(keys) != len(set(keys)):
        raise ValueError("Drive index contains duplicate symbol/timeframe entries")
    expected = set(normalized_timeframes)
    observed: dict[str, set[str]] = {}
    for item in files:
        observed.setdefault(item.symbol, set()).add(item.timeframe)
    incomplete = {
        symbol: sorted(expected - actual)
        for symbol, actual in observed.items()
        if actual != expected
    }
    if incomplete:
        raise ValueError(f"Drive index timeframe matrix incomplete: {incomplete}")

    return DriveIndex(
        folder_id=_drive_id(payload["folder_id"], "folder_id"),
        summary_file_id=_drive_id(payload["summary_file_id"], "summary_file_id"),
        summary_sha256=_sha(payload["summary_sha256"], "summary_sha256"),
        exchange_id=_text(payload["exchange_id"], "exchange_id").lower(),
        market_type=_text(payload["market_type"], "market_type"),
        required_timeframes=normalized_timeframes,
        files=tuple(files),
    )


def _safe_regular_file(root: Path, relative: Path) -> Path:
    candidate = root / relative
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"expected regular non-symlink file is missing: {relative.as_posix()}")
    resolved = candidate.resolve(strict=True)
    if resolved.parent != (root / relative.parent).resolve(strict=True):
        raise ValueError(f"file escaped expected corpus directory: {relative.as_posix()}")
    if root not in resolved.parents:
        raise ValueError(f"file escaped corpus root: {relative.as_posix()}")
    return resolved


def _read_summary(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != _SUMMARY_COLUMNS:
            raise ValueError("download_summary.csv columns mismatch")
        rows = list(reader)
    result: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        symbol = _text(row["symbol"], "summary symbol").upper()
        timeframe = _text(row["timeframe"], "summary timeframe").lower()
        key = (symbol, timeframe)
        if key in result:
            raise ValueError(f"duplicate summary row: {symbol}/{timeframe}")
        if row["status"].strip().lower() != "ok":
            raise ValueError(f"summary status is not ok: {symbol}/{timeframe}")
        rows_count = int(_text(row["rows"], "summary rows"))
        if rows_count <= 0:
            raise ValueError(f"summary rows must be positive: {symbol}/{timeframe}")
        first = _timestamp(_text(row["first_open_time"], "first_open_time"), "first_open_time")
        last = _timestamp(_text(row["last_open_time"], "last_open_time"), "last_open_time")
        if first > last:
            raise ValueError(f"summary bounds invalid: {symbol}/{timeframe}")
        if timeframe == "5m" and (
            row["gap_events"].strip() != "0" or row["estimated_missing_5m_rows"].strip() != "0"
        ):
            raise ValueError(f"5m summary continuity is not clean: {symbol}")
        result[key] = row
    return result


def build_external_corpus_spec(
    *,
    corpus_root: Path,
    drive_index_path: Path,
    output_path: Path,
    report_path: Path,
) -> tuple[Path, Path]:
    requested_root = corpus_root.expanduser()
    if requested_root.is_symlink() or not requested_root.is_dir():
        raise ValueError("corpus root must be a regular non-symlink directory")
    root = requested_root.resolve(strict=True)
    index = load_drive_index(drive_index_path)
    summary = _safe_regular_file(root, Path("download_summary.csv"))
    observed_summary_sha = sha256_file(summary)
    if observed_summary_sha != index.summary_sha256:
        raise ValueError("download_summary.csv SHA-256 differs from the Drive index")

    summary_rows = _read_summary(summary)
    index_keys = {(item.symbol, item.timeframe) for item in index.files}
    if set(summary_rows) != index_keys:
        missing = sorted(index_keys - set(summary_rows))
        extra = sorted(set(summary_rows) - index_keys)
        raise ValueError(f"summary/index matrix mismatch; missing={missing}, extra={extra}")

    spec_files: list[dict[str, object]] = []
    total_bytes = 0
    total_rows = 0
    for item in sorted(
        index.files,
        key=lambda value: (
            value.symbol,
            index.required_timeframes.index(value.timeframe),
        ),
    ):
        relative = Path(item.symbol) / item.filename
        local_path = _safe_regular_file(root, relative)
        observed_size = local_path.stat().st_size
        if observed_size != item.byte_size:
            raise ValueError(
                f"Drive file size mismatch for {item.symbol}/{item.timeframe}: "
                f"expected {item.byte_size}, observed {observed_size}"
            )
        summary_row = summary_rows[(item.symbol, item.timeframe)]
        expected_rows = int(summary_row["rows"])
        total_bytes += observed_size
        total_rows += expected_rows
        spec_files.append(
            {
                "symbol": item.symbol,
                "timeframe": item.timeframe,
                "path": str(local_path),
                "source_uri": f"gdrive://{item.drive_file_id}/{item.filename}",
                "expected_rows": expected_rows,
                "expected_first_open_time": _timestamp(
                    summary_row["first_open_time"], "first_open_time"
                ).isoformat(),
                "expected_last_open_time": _timestamp(
                    summary_row["last_open_time"], "last_open_time"
                ).isoformat(),
                "expected_sha256": None,
            }
        )

    spec = {
        "spec_version": 1,
        "exchange_id": index.exchange_id,
        "market_type": index.market_type,
        "required_timeframes": list(index.required_timeframes),
        "max_file_bytes": 256 * 1024 * 1024,
        "files": spec_files,
    }
    report = {
        "schema_version": 1,
        "folder_id": index.folder_id,
        "summary_file_id": index.summary_file_id,
        "summary_sha256": observed_summary_sha,
        "drive_index_sha256": sha256_file(drive_index_path),
        "file_count": len(spec_files),
        "symbol_count": len({item["symbol"] for item in spec_files}),
        "timeframe_count": len(index.required_timeframes),
        "total_bytes": total_bytes,
        "total_rows": total_rows,
        "source_uri_scheme": "gdrive",
        "contains_local_paths": False,
    }
    _atomic_json(output_path, spec, mode=0o600)
    _atomic_json(report_path, report, mode=0o640)
    return output_path.resolve(), report_path.resolve()


def _atomic_json(path: Path, payload: dict[str, object], *, mode: int) -> None:
    requested_path = path.expanduser()
    if requested_path.is_symlink():
        raise ValueError("output path must not be a symlink")
    destination = requested_path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a strict local external-corpus spec from immutable Drive metadata."
    )
    parser.add_argument("--corpus-root", type=Path, required=True)
    parser.add_argument("--drive-index", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    spec, report = build_external_corpus_spec(
        corpus_root=args.corpus_root,
        drive_index_path=args.drive_index,
        output_path=args.output,
        report_path=args.report,
    )
    print(
        json.dumps(
            {
                "status": "READY",
                "spec_path": str(spec),
                "report_path": str(report),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
