#!/usr/bin/env python3
"""Publish bounded, path-free failure evidence for physical historical acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_ALLOWED_STAGES = frozenset(
    {
        "bootstrap",
        "candidate_geometry",
        "host_preflight",
        "dataset_discovery",
        "dataset_inventory",
        "canonical_prepare",
        "pretest_redaction",
        "canonical_smoke",
        "canonical_full",
        "result_hashing",
        "summary_write",
        "evidence_privacy",
        "summary_verification",
        "evidence_publish",
        "success_sentinel_verification",
    }
)


def _failure_category(stage: str) -> str:
    if stage.startswith("canonical_"):
        return "canonical_suite"
    if stage.startswith("dataset_") or stage == "host_preflight":
        return "physical_preflight"
    if stage in {"pretest_redaction", "result_hashing", "summary_write"}:
        return "historical_evidence_build"
    if stage in {
        "evidence_privacy",
        "summary_verification",
        "evidence_publish",
        "success_sentinel_verification",
    }:
        return "historical_evidence_validation"
    if stage == "candidate_geometry":
        return "candidate_geometry"
    return "bootstrap"


def _validate_sha(name: str, value: str) -> str:
    if not _SHA_RE.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase 40-hex SHA")
    return value


def _validate_exit_code(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 255:
        raise ValueError("exit_code must be an integer in [1, 255]")
    return value


def _optional_positive_int(name: str, value: str | None) -> int | None:
    if value in (None, ""):
        return None
    if not value.isascii() or not value.isdigit():
        raise ValueError(f"{name} must contain decimal digits only")
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _prepare_empty_output_dir(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("failure evidence output directory must not be a symlink")
    if path.exists():
        if not path.is_dir():
            raise ValueError("failure evidence output path must be a directory")
        if any(path.iterdir()):
            raise ValueError("failure evidence output directory must be empty")
    else:
        path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)


def _workflow_error_annotation(payload: dict[str, object]) -> str:
    """Render the same validated payload as a bounded GitHub check annotation."""

    fields = (
        f"status={payload['status']}",
        f"stage={payload['stage']}",
        f"failure_category={payload['failure_category']}",
        f"exit_code={payload['exit_code']}",
        f"candidate_sha={payload['candidate_sha']}",
        f"full_rc_sha={payload['full_rc_sha']}",
        f"run_id={payload['run_id']}",
        f"run_attempt={payload['run_attempt']}",
        f"paper_only={str(payload['paper_only']).lower()}",
        f"live_trading_authorized={str(payload['live_trading_authorized']).lower()}",
        f"raw_helper_log_uploaded={str(payload['raw_helper_log_uploaded']).lower()}",
        f"raw_market_data_uploaded={str(payload['raw_market_data_uploaded']).lower()}",
    )
    annotation = "physical_historical_failure " + " ".join(fields)
    if len(annotation.encode("utf-8")) > 1024:
        raise ValueError("workflow failure annotation unexpectedly exceeded the fixed size bound")
    if any(char in annotation for char in ("%", "\r", "\n", ":")):
        raise ValueError("workflow failure annotation contains a control character")
    return f"::error title=Physical historical acceptance failure::{annotation}"


def publish_failure_evidence(
    *,
    output_dir: Path,
    candidate_sha: str,
    full_rc_sha: str,
    stage: str,
    exit_code: int,
    run_id: str | None = None,
    run_attempt: str | None = None,
) -> dict[str, object]:
    """Write a closed two-file failure evidence inventory with no free-form text."""

    candidate_sha = _validate_sha("candidate_sha", candidate_sha)
    full_rc_sha = _validate_sha("full_rc_sha", full_rc_sha)
    if stage not in _ALLOWED_STAGES:
        raise ValueError("stage is not an approved physical historical acceptance stage")
    exit_code = _validate_exit_code(exit_code)
    parsed_run_id = _optional_positive_int("run_id", run_id)
    parsed_run_attempt = _optional_positive_int("run_attempt", run_attempt)

    _prepare_empty_output_dir(output_dir)

    payload: dict[str, object] = {
        "schema_version": 1,
        "status": "FAIL",
        "candidate_sha": candidate_sha,
        "full_rc_sha": full_rc_sha,
        "stage": stage,
        "failure_category": _failure_category(stage),
        "exit_code": exit_code,
        "run_id": parsed_run_id,
        "run_attempt": parsed_run_attempt,
        "paper_only": True,
        "live_trading_authorized": False,
        "raw_helper_log_uploaded": False,
        "raw_market_data_uploaded": False,
    }
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if len(serialized.encode("utf-8")) > 4096:
        raise ValueError("failure evidence unexpectedly exceeded the fixed size bound")

    failure_path = output_dir / "failure.json"
    checksum_path = output_dir / "SHA256SUMS.txt"
    if (
        failure_path.exists()
        or failure_path.is_symlink()
        or checksum_path.exists()
        or checksum_path.is_symlink()
    ):
        raise ValueError("failure evidence files must not pre-exist")

    failure_path.write_text(serialized, encoding="utf-8")
    failure_path.chmod(0o600)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    checksum_path.write_text(f"{digest}  failure.json\n", encoding="utf-8")
    checksum_path.chmod(0o600)

    actual = {
        item.name for item in output_dir.iterdir() if item.is_file() and not item.is_symlink()
    }
    if actual != {"failure.json", "SHA256SUMS.txt"}:
        raise ValueError("failure evidence inventory is not closed")
    if any(item.is_symlink() for item in output_dir.iterdir()):
        raise ValueError("failure evidence inventory contains a symlink")
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--full-rc-sha", required=True)
    parser.add_argument("--stage", choices=sorted(_ALLOWED_STAGES), required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--run-attempt", default=None)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        payload = publish_failure_evidence(
            output_dir=args.output_dir,
            candidate_sha=args.candidate_sha,
            full_rc_sha=args.full_rc_sha,
            stage=args.stage,
            exit_code=args.exit_code,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
        )
        print(_workflow_error_annotation(payload))
    except (OSError, ValueError) as exc:
        raise SystemExit(
            f"unable to publish sanitized historical failure evidence: {type(exc).__name__}"
        ) from None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
