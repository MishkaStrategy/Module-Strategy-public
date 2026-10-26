from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .contracts import ApiError, ApiErrorDetail
from .download_config_migration import (
    migrate_legacy_download_config,
    serialize_download_request,
    write_download_request,
)
from .errors import ApplicationError


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="market-data-v1-migrate",
        description="Convert a legacy Market Data download YAML file to CreateDownloadRequest JSON.",
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--as-of",
        type=_time,
        help="Required for latest_closed ranges; controls deterministic UTC flooring.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional new JSON file. Existing files and symlink traversal are refused.",
    )
    return parser


def _exit_code(error: ApplicationError) -> int:
    if error.code == "LEGACY_CONFIG_INVALID":
        return 2
    if error.code in {
        "LEGACY_CONFIG_AS_OF_INVALID",
        "LEGACY_CONFIG_DYNAMIC_END_REQUIRES_AS_OF",
        "LEGACY_CONFIG_REQUEST_ID_UNREPRESENTABLE",
        "LEGACY_CONFIG_RESUME_UNREPRESENTABLE",
        "LEGACY_CONFIG_RUNTIME_SETTINGS_UNREPRESENTABLE",
        "LEGACY_CONFIG_SECRET_FIELD_FORBIDDEN",
        "LEGACY_CONFIG_SOURCE_TOO_LARGE",
        "LEGACY_CONFIG_SOURCE_UNSAFE",
        "LEGACY_CONFIG_STORAGE_ROOT_UNREPRESENTABLE",
        "LEGACY_CONFIG_UNREPRESENTABLE",
        "LEGACY_CONFIG_VALIDATION_SETTINGS_UNREPRESENTABLE",
        "MIGRATION_OUTPUT_UNSAFE",
    }:
        return 7
    if error.code == "MIGRATION_OUTPUT_EXISTS":
        return 9
    return 1


def _emit_error(error: ApplicationError) -> None:
    payload = ApiError(
        error=ApiErrorDetail(
            code=error.code,
            message=error.message,
            details=error.details,
            correlation_id=f"corr_migrate_{uuid4().hex[:20]}",
        )
    )
    sys.stdout.write(
        json.dumps(
            payload.model_dump(mode="json"),
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        request = migrate_legacy_download_config(args.config, as_of=args.as_of)
        if args.output is not None:
            write_download_request(args.output, request)
        sys.stdout.write(serialize_download_request(request))
        return 0
    except ApplicationError as error:
        _emit_error(error)
        return _exit_code(error)


if __name__ == "__main__":
    raise SystemExit(main())
