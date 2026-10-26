#!/usr/bin/env python3
"""Verify a controller-signed receipt for an isolated external execution.

This verifies authenticity and local policy bindings; it cannot prove global
non-replay. The controller signs observed execution receipts. The publisher
pins the controller public key and atomically enforces global admission_id
replay protection; the candidate process must never sign observations.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

REPOSITORY = "https://github.com/MishkaStrategy/Module-Strategy.git"
SHA_RE = re.compile(r"^[a-f0-9]{40}$")
HEX256_RE = re.compile(r"^[a-f0-9]{64}$")
KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
UTC_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{1,6})?Z$"
)
OUTPUT_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
WINDOWS_RESERVED_RE = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?$", re.IGNORECASE
)
MAX_RECEIPT_BYTES = 1_048_576
MAX_OUTPUTS = 256
MAX_OUTPUT_BYTES = 1_073_741_824
MAX_TOTAL_OUTPUT_BYTES = 2_147_483_648
EXPECTED_BINDING_FIELDS = {
    "repository",
    "source_sha",
    "admission_id",
    "workflow_digest",
    "runtime_id",
    "material_digest",
    "policy_id",
    "policy_digest",
    "workload_id",
    "workload_digest",
    "environment_id",
    "image_digest",
    "image_os",
    "image_arch",
    "admission_digest",
    "provider_id",
    "provider_run_id",
    "provider_attempt",
    "resource_policy_digest",
    "network_policy_digest",
}


class ReceiptError(ValueError):
    """Receipt verification failed closed."""


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReceiptError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise ReceiptError(f"nonfinite JSON value is forbidden: {value}")


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ReceiptError(f"payload cannot be canonicalized: {exc}") from exc


def _exact_keys(value: Any, required: set[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReceiptError(f"{where} must be an object")
    missing = required - value.keys()
    extra = value.keys() - required
    if missing or extra:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if extra:
            details.append("unknown " + ", ".join(sorted(extra)))
        raise ReceiptError(f"{where}: " + "; ".join(details))
    return value


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not UTC_TIMESTAMP_RE.fullmatch(value):
        raise ReceiptError(f"{label} must be canonical UTC RFC 3339 ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ReceiptError(f"{label} must be a valid canonical UTC RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReceiptError(f"{label} must include a timezone")
    return parsed


def _valid_platform(executor: dict[str, Any]) -> None:
    os_name = executor["observed_os"]
    arch = executor["observed_arch"]
    normalized_arch = {"arm64": "aarch64", "AMD64": "x86_64"}.get(arch, arch)
    image_os = executor["image_os"]
    image_arch = {"arm64": "aarch64", "AMD64": "x86_64"}.get(
        executor["image_arch"], executor["image_arch"]
    )
    if (os_name, normalized_arch) not in {
        ("linux", "x86_64"),
        ("linux", "aarch64"),
        ("darwin", "x86_64"),
        ("darwin", "aarch64"),
        ("windows", "x86_64"),
    }:
        raise ReceiptError("unsupported observed OS/architecture mapping")
    if image_os != os_name or image_arch != normalized_arch:
        raise ReceiptError(
            "observed OS/architecture does not match the declared executor image platform"
        )


def _valid_output_path(path: Any) -> bool:
    if not isinstance(path, str) or not path or len(path) > 512:
        return False
    segments = path.split("/")
    if any(
        not OUTPUT_SEGMENT_RE.fullmatch(segment) or segment.endswith(".") for segment in segments
    ):
        return False
    if any(WINDOWS_RESERVED_RE.fullmatch(segment) for segment in segments):
        return False
    return True


def _check_path_set(paths: list[str], label: str) -> set[str]:
    folded: set[str] = set()
    for path in paths:
        normalized = path.casefold()
        if normalized in folded:
            raise ReceiptError(f"{label} contains a case-insensitive path collision: {path}")
        parts = normalized.split("/")
        parents = {"/".join(parts[:index]) for index in range(1, len(parts))}
        if parents & folded or any(existing.startswith(normalized + "/") for existing in folded):
            raise ReceiptError(f"{label} contains a file/directory prefix conflict: {path}")
        folded.add(normalized)
    return set(paths)


def _validate_expected_bindings(expected: Any) -> dict[str, Any]:
    if not isinstance(expected, dict):
        raise ReceiptError("expected bindings must be an object")
    missing = EXPECTED_BINDING_FIELDS - expected.keys()
    unknown = expected.keys() - EXPECTED_BINDING_FIELDS
    if missing or unknown:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            details.append("unknown " + ", ".join(sorted(unknown)))
        raise ReceiptError("expected bindings: " + "; ".join(details))
    for field, value in expected.items():
        if field == "provider_attempt":
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ReceiptError("expected provider_attempt must be a positive integer")
        elif not isinstance(value, str) or not value:
            raise ReceiptError(f"expected binding {field} must be a nonempty string")
    if expected["repository"] != REPOSITORY:
        raise ReceiptError("expected repository must be the pinned repository")
    if not SHA_RE.fullmatch(expected["source_sha"]):
        raise ReceiptError("expected source_sha must be a lowercase 40-character Git SHA")
    for field in (
        "workflow_digest",
        "material_digest",
        "policy_digest",
        "workload_digest",
        "image_digest",
        "admission_digest",
        "resource_policy_digest",
        "network_policy_digest",
    ):
        if not HEX256_RE.fullmatch(expected[field]):
            raise ReceiptError(f"expected {field} must be a lowercase SHA-256 digest")
    try:
        import uuid

        if str(uuid.UUID(expected["admission_id"])) != expected["admission_id"]:
            raise ValueError
    except (ValueError, AttributeError, TypeError) as exc:
        raise ReceiptError("expected admission_id must be a UUID string") from exc
    for field in (
        "runtime_id",
        "policy_id",
        "workload_id",
        "environment_id",
        "provider_id",
        "provider_run_id",
    ):
        if len(expected[field]) > 128:
            raise ReceiptError(f"expected {field} exceeds its allowed length")
    if expected["image_os"] not in {"linux", "darwin", "windows"}:
        raise ReceiptError("expected image_os is invalid")
    if expected["image_arch"] not in {"x86_64", "aarch64", "arm64", "AMD64"}:
        raise ReceiptError("expected image_arch is invalid")
    return expected


def _load_expected_output_paths(path: Path) -> list[str]:
    # The caller must source this inventory from the immutable protected admission record.
    try:
        if path.stat().st_size > 200_000:
            raise ReceiptError("expected output inventory exceeds the maximum size")
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except ReceiptError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"cannot load expected output inventory: {exc}") from exc
    if not isinstance(value, list) or len(value) > MAX_OUTPUTS:
        raise ReceiptError(
            f"expected output inventory must be a JSON array with at most {MAX_OUTPUTS} paths"
        )
    if any(not _valid_output_path(item) for item in value):
        raise ReceiptError(
            "expected output inventory contains a path outside the portable relative-path dialect"
        )
    _check_path_set(value, "expected output inventory")
    return value


def validate_payload(
    payload: Any, expected: dict[str, Any], required_output_paths: list[str]
) -> None:
    expected = _validate_expected_bindings(expected)
    if not isinstance(required_output_paths, list) or len(required_output_paths) > MAX_OUTPUTS:
        raise ReceiptError(
            f"required output paths must be a list with at most {MAX_OUTPUTS} entries"
        )
    if any(not _valid_output_path(path) for path in required_output_paths):
        raise ReceiptError("required output inventory contains an invalid path")
    required_paths = _check_path_set(required_output_paths, "required output inventory")
    fields = {
        "admission_id",
        "repository",
        "source_sha",
        "workflow_digest",
        "runtime_id",
        "workload_id",
        "workload_digest",
        "material_digest",
        "policy_id",
        "policy_digest",
        "environment_id",
        "admission_digest",
        "observed_provider",
        "observed_resource_policy_digest",
        "observed_network_policy_digest",
        "executor",
        "started_at",
        "ended_at",
        "destroyed_at",
        "outcome",
        "exit_code",
        "outputs",
    }
    data = _exact_keys(payload, fields, "payload")
    if data["repository"] != expected["repository"] or data["repository"] != REPOSITORY:
        raise ReceiptError("repository does not match the pinned expected repository")
    if data["source_sha"] != expected["source_sha"]:
        raise ReceiptError("source SHA mismatch")
    for field in (
        "admission_id",
        "workflow_digest",
        "runtime_id",
        "policy_id",
        "policy_digest",
        "workload_id",
        "workload_digest",
        "material_digest",
        "environment_id",
    ):
        if data[field] != expected[field]:
            raise ReceiptError(f"{field} mismatch")
    if not SHA_RE.fullmatch(data["source_sha"]):
        raise ReceiptError("source_sha must be a lowercase 40-character Git SHA")
    for field in ("workflow_digest", "workload_digest", "material_digest", "policy_digest"):
        if not isinstance(data[field], str) or not HEX256_RE.fullmatch(data[field]):
            raise ReceiptError(f"{field} must be a lowercase SHA-256 digest")
    for field in (
        "admission_digest",
        "observed_resource_policy_digest",
        "observed_network_policy_digest",
    ):
        if not isinstance(data[field], str) or not HEX256_RE.fullmatch(data[field]):
            raise ReceiptError(f"{field} must be a lowercase SHA-256 digest")
    if data["admission_digest"] != expected["admission_digest"]:
        raise ReceiptError("admission_digest mismatch")
    provider = _exact_keys(
        data["observed_provider"], {"provider_id", "run_id", "attempt"}, "observed_provider"
    )
    provider_expected = {
        "provider_id": expected["provider_id"],
        "run_id": expected["provider_run_id"],
        "attempt": expected["provider_attempt"],
    }
    if provider != provider_expected:
        raise ReceiptError("observed_provider does not match immutable admission")
    if isinstance(provider["attempt"], bool) or not isinstance(provider["attempt"], int):
        raise ReceiptError("observed_provider.attempt must be a positive integer")
    for field, expected_field in (
        ("observed_resource_policy_digest", "resource_policy_digest"),
        ("observed_network_policy_digest", "network_policy_digest"),
    ):
        if data[field] != expected[expected_field]:
            raise ReceiptError(f"{field} mismatch")
    if not isinstance(data["admission_id"], str):
        raise ReceiptError("admission_id must be a UUID string")
    try:
        import uuid

        if str(uuid.UUID(data["admission_id"])) != data["admission_id"]:
            raise ValueError
    except (ValueError, AttributeError, TypeError) as exc:
        raise ReceiptError("admission_id must be a UUID string") from exc
    for field in ("workload_id", "policy_id", "environment_id"):
        if not isinstance(data[field], str) or not data[field] or len(data[field]) > 128:
            raise ReceiptError(f"{field} must be a nonempty bounded string")

    executor_fields = {
        "executor_id",
        "ephemeral",
        "image_digest",
        "image_os",
        "image_arch",
        "observed_os",
        "observed_arch",
        "created_at",
    }
    executor = _exact_keys(data["executor"], executor_fields, "executor")
    for field in ("image_digest", "image_os", "image_arch"):
        if executor[field] != expected[field]:
            raise ReceiptError(f"executor.{field} mismatch")
    if (
        not isinstance(executor["executor_id"], str)
        or not executor["executor_id"]
        or len(executor["executor_id"]) > 256
    ):
        raise ReceiptError("executor_id must be a nonempty bounded string")
    if executor["ephemeral"] is not True:
        raise ReceiptError("executor must be declared ephemeral")
    if not HEX256_RE.fullmatch(
        executor["image_digest"] if isinstance(executor["image_digest"], str) else ""
    ):
        raise ReceiptError("image_digest must be a lowercase SHA-256 digest")
    if (
        not isinstance(executor["image_os"], str)
        or executor["image_os"] not in {"linux", "darwin", "windows"}
        or not isinstance(executor["observed_os"], str)
        or executor["observed_os"] not in {"linux", "darwin", "windows"}
    ):
        raise ReceiptError("executor OS is invalid")
    architectures = {"x86_64", "aarch64", "arm64", "AMD64"}
    if (
        not isinstance(executor["image_arch"], str)
        or executor["image_arch"] not in architectures
        or not isinstance(executor["observed_arch"], str)
        or executor["observed_arch"] not in architectures
    ):
        raise ReceiptError("executor architecture is invalid")
    _valid_platform(executor)

    created = _timestamp(executor["created_at"], "executor.created_at")
    started = _timestamp(data["started_at"], "started_at")
    ended = _timestamp(data["ended_at"], "ended_at")
    destroyed = _timestamp(data["destroyed_at"], "destroyed_at")
    if not created <= started <= ended <= destroyed:
        raise ReceiptError("executor timestamps must satisfy created <= start <= end <= destroy")
    outcome, code = data["outcome"], data["exit_code"]
    if not isinstance(outcome, str) or outcome not in {
        "passed",
        "failed",
        "cancelled",
        "timed_out",
    }:
        raise ReceiptError("outcome is not a recognized terminal outcome")
    if code is not None and (
        isinstance(code, bool) or not isinstance(code, int) or not 0 <= code <= 255
    ):
        raise ReceiptError("exit_code must be null or an integer from 0 through 255")
    if (outcome == "passed" and code != 0) or (outcome == "failed" and (code is None or code == 0)):
        raise ReceiptError("outcome and exit_code disagree")

    outputs = data["outputs"]
    if not isinstance(outputs, list) or len(outputs) > MAX_OUTPUTS:
        raise ReceiptError(f"outputs must be an array with at most {MAX_OUTPUTS} rows")
    paths: list[str] = []
    total_size = 0
    for index, output in enumerate(outputs):
        row = _exact_keys(output, {"path", "sha256", "size_bytes"}, f"outputs[{index}]")
        path = row["path"]
        if not _valid_output_path(path):
            raise ReceiptError(
                f"outputs[{index}].path is outside the portable relative-path dialect"
            )
        paths.append(path)
        if not isinstance(row["sha256"], str) or not HEX256_RE.fullmatch(row["sha256"]):
            raise ReceiptError(f"outputs[{index}].sha256 must be a lowercase SHA-256 digest")
        size = row["size_bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_OUTPUT_BYTES:
            raise ReceiptError(f"outputs[{index}].size_bytes is outside the allowed bound")
        total_size += size
    if total_size > MAX_TOTAL_OUTPUT_BYTES:
        raise ReceiptError("total output size exceeds the allowed bound")
    actual_paths = _check_path_set(paths, "receipt outputs")
    if actual_paths - required_paths:
        raise ReceiptError(
            "receipt contains output paths absent from the protected admission inventory"
        )
    if data["outcome"] == "passed" and actual_paths != required_paths:
        missing = sorted(required_paths - actual_paths)
        raise ReceiptError("passed receipt is missing required outputs: " + ", ".join(missing))


def _load_json_bytes(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, bytes) or len(raw) > MAX_RECEIPT_BYTES:
        raise ReceiptError("receipt exceeds the maximum size")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except ReceiptError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"cannot load receipt: {exc}") from exc
    if not isinstance(value, dict):
        raise ReceiptError("receipt root must be an object")
    return value


def _load_json(path: Path) -> dict[str, Any]:
    return _load_json_bytes(read_receipt_snapshot(path))


def read_receipt_snapshot(path: Path) -> bytes:
    """Read one bounded, no-follow regular-file snapshot for exact verification."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise ReceiptError("platform lacks no-follow receipt file support")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ReceiptError("receipt must be a regular file")
        if before.st_size < 0 or before.st_size > MAX_RECEIPT_BYTES:
            raise ReceiptError("receipt exceeds the maximum size")
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_RECEIPT_BYTES:
            chunk = os.read(descriptor, min(64 * 1024, MAX_RECEIPT_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > MAX_RECEIPT_BYTES:
            raise ReceiptError("receipt exceeds the maximum size")
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or total != before.st_size:
            raise ReceiptError("receipt changed while being read")
        return b"".join(chunks)
    except ReceiptError:
        raise
    except OSError as exc:
        raise ReceiptError(f"cannot load receipt: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def verify_receipt(
    receipt_path: Path,
    trusted_keys: Path,
    openssl: str,
    expected: dict[str, Any],
    required_output_paths: list[str],
) -> dict[str, Any]:
    return _verify_envelope(
        _load_json(receipt_path), trusted_keys, openssl, expected, required_output_paths
    )


def verify_receipt_bytes(
    receipt_bytes: bytes,
    trusted_keys: Path,
    openssl: str,
    expected: dict[str, Any],
    required_output_paths: list[str],
) -> dict[str, Any]:
    """Verify one bounded receipt byte snapshot without reopening its path."""
    return _verify_envelope(
        _load_json_bytes(receipt_bytes), trusted_keys, openssl, expected, required_output_paths
    )


def _verify_envelope(
    value: dict[str, Any],
    trusted_keys: Path,
    openssl: str,
    expected: dict[str, Any],
    required_output_paths: list[str],
) -> dict[str, Any]:
    envelope = _exact_keys(value, {"schema_version", "key_id", "payload", "signature"}, "receipt")
    if envelope["schema_version"] != 1 or isinstance(envelope["schema_version"], bool):
        raise ReceiptError("unsupported schema_version")
    key_id = envelope["key_id"]
    if not isinstance(key_id, str) or not KEY_ID_RE.fullmatch(key_id):
        raise ReceiptError("invalid key_id")
    if trusted_keys.is_symlink() or not trusted_keys.is_dir():
        raise ReceiptError("pinned controller signing key directory must be a real directory")
    key_path = trusted_keys / f"{key_id}.pub.pem"
    if key_path.is_symlink() or not key_path.is_file():
        raise ReceiptError(
            "controller signing key pinned by publisher is missing or is not a regular file"
        )
    try:
        public_key_pem = key_path.read_bytes()
    except OSError as exc:
        raise ReceiptError(
            f"cannot read controller signing key pinned by publisher: {exc}"
        ) from exc
    if not public_key_pem.startswith(b"-----BEGIN PUBLIC KEY-----"):
        raise ReceiptError(
            "controller signing key pinned by publisher must use SubjectPublicKeyInfo PEM encoding"
        )
    validate_payload(envelope["payload"], expected, required_output_paths)
    signature_text = envelope["signature"]
    if not isinstance(signature_text, str):
        raise ReceiptError("signature must be base64 text")
    try:
        signature = base64.b64decode(signature_text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ReceiptError("signature is not valid base64") from exc
    if len(signature) != 64:
        raise ReceiptError("Ed25519 signature must be exactly 64 bytes")
    signed = canonical_json_bytes(
        {
            "schema_version": envelope["schema_version"],
            "key_id": envelope["key_id"],
            "payload": envelope["payload"],
        }
    )
    try:
        key_info = subprocess.run(
            [openssl, "pkey", "-pubin", "-in", str(key_path), "-text", "-noout"],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError as exc:
        raise ReceiptError(
            f"cannot inspect controller signing key pinned by publisher: {exc}"
        ) from exc
    if key_info.returncode != 0 or not key_info.stdout.startswith("ED25519 Public-Key:"):
        raise ReceiptError(
            "controller signing key pinned by publisher must be an Ed25519 public key"
        )
    # OpenSSL needs separate payload and signature files; use private temporary files
    # so the signed bytes are passed exactly as canonicalized above.
    try:
        with tempfile.TemporaryDirectory(prefix="external-receipt-") as directory:
            sig_file = Path(directory) / "signature.bin"
            data_file = Path(directory) / "payload.json"
            sig_file.write_bytes(signature)
            data_file.write_bytes(signed)
            result = subprocess.run(
                [
                    openssl,
                    "pkeyutl",
                    "-verify",
                    "-pubin",
                    "-inkey",
                    str(key_path),
                    "-rawin",
                    "-sigfile",
                    str(sig_file),
                    "-in",
                    str(data_file),
                ],
                capture_output=True,
                check=False,
            )
    except OSError as exc:
        raise ReceiptError(f"cannot run OpenSSL: {exc}") from exc
    if result.returncode != 0:
        raise ReceiptError("Ed25519 signature verification failed")
    return envelope["payload"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a controller-signed external execution receipt."
    )
    parser.add_argument("receipt", type=Path)
    parser.add_argument(
        "--trusted-keys",
        type=Path,
        required=True,
        help="publisher-pinned controller Ed25519 public keys as <key_id>.pub.pem",
    )
    parser.add_argument(
        "--expected-outputs",
        type=Path,
        required=True,
        help="JSON array from the immutable protected admission record, containing required output paths",
    )
    parser.add_argument("--repository", default=REPOSITORY)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--admission-id", required=True)
    parser.add_argument("--admission-digest", required=True)
    parser.add_argument("--workflow-digest", required=True)
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument("--material-digest", required=True)
    parser.add_argument("--policy-id", required=True)
    parser.add_argument("--policy-digest", required=True)
    parser.add_argument("--workload-id", required=True)
    parser.add_argument("--workload-digest", required=True)
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--image-os", required=True)
    parser.add_argument("--image-arch", required=True)
    parser.add_argument("--provider-id", required=True)
    parser.add_argument("--provider-run-id", required=True)
    parser.add_argument("--provider-attempt", type=int, required=True)
    parser.add_argument("--resource-policy-digest", required=True)
    parser.add_argument("--network-policy-digest", required=True)
    parser.add_argument("--openssl", default="openssl")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    fields = (
        "repository",
        "source-sha",
        "admission-id",
        "admission-digest",
        "workflow-digest",
        "runtime-id",
        "material-digest",
        "policy-id",
        "policy-digest",
        "workload-id",
        "workload-digest",
        "environment-id",
        "image-digest",
        "image-os",
        "image-arch",
        "provider-id",
        "provider-run-id",
        "provider-attempt",
        "resource-policy-digest",
        "network-policy-digest",
    )
    expected = {name.replace("-", "_"): getattr(args, name.replace("-", "_")) for name in fields}
    try:
        required_output_paths = _load_expected_output_paths(args.expected_outputs)
        verify_receipt(
            args.receipt, args.trusted_keys, args.openssl, expected, required_output_paths
        )
    except ReceiptError as exc:
        print(f"receipt rejected: {exc}", file=sys.stderr)
        return 1
    print("external execution receipt verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
