"""Closed local external-publication receipt contract and Ed25519 verification.

This reference contract verifies bytes under an explicitly supplied local signer
directory. It does not establish key custody, authorization authenticity, a protected
trust root, global replay protection, or immutable remote destinations.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

DOMAIN = b"MODULAR-STRATEGY:EXTERNAL-PUBLICATION-RECEIPT:V1\0"
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OBJECT_KEY = re.compile(r"^objects/sha256/[a-f0-9]{64}$")
_UTC_TIMESTAMP = re.compile(
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{1,6})?Z$"
)
MAX_RECEIPT_BYTES = 1_048_576
MAX_ARTIFACT_BYTES = 2_147_483_648


class PublicationReceiptError(ValueError):
    """A publication receipt failed closed validation or signature verification."""


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PublicationReceiptError(f"value cannot be canonicalized: {exc}") from exc


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PublicationReceiptError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise PublicationReceiptError(f"nonfinite JSON value is forbidden: {value}")


def _decode_json(data: bytes) -> dict[str, Any]:
    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_RECEIPT_BYTES:
        raise PublicationReceiptError("receipt size is invalid")
    try:
        value = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constant,
        )
    except PublicationReceiptError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PublicationReceiptError(f"receipt JSON is invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise PublicationReceiptError("receipt must be an object")
    return value


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PublicationReceiptError(f"{label} must be an object")
    missing = keys - value.keys()
    extra = value.keys() - keys
    if missing or extra:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if extra:
            details.append("unknown " + ", ".join(sorted(extra)))
        raise PublicationReceiptError(f"{label}: " + "; ".join(details))
    return value


def _string(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise PublicationReceiptError(f"{label} is invalid")
    return value


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not _UTC_TIMESTAMP.fullmatch(value):
        raise PublicationReceiptError(f"{label} must be canonical UTC RFC 3339")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise PublicationReceiptError(f"{label} is not a valid timestamp") from exc


def validate_payload(value: Any) -> dict[str, Any]:
    payload = _exact(
        value,
        {
            "publication_id",
            "authorization_claim",
            "admission",
            "execution",
            "publisher",
            "destination",
            "artifact",
            "timestamps",
            "outcome",
        },
        "payload",
    )
    _string(payload["publication_id"], _UUID, "publication_id")
    authorization = _exact(
        payload["authorization_claim"],
        {"authorization_id", "authorization_digest"},
        "authorization_claim",
    )
    _string(authorization["authorization_id"], _UUID, "authorization_id")
    _string(authorization["authorization_digest"], _SHA256, "authorization_digest")
    admission = _exact(payload["admission"], {"admission_id", "admission_digest"}, "admission")
    _string(admission["admission_id"], _UUID, "admission_id")
    _string(admission["admission_digest"], _SHA256, "admission_digest")
    execution = _exact(
        payload["execution"],
        {"publication_id", "receipt_digest", "bundle_digest", "bundle_size_bytes"},
        "execution",
    )
    for field in ("publication_id", "receipt_digest", "bundle_digest"):
        _string(execution[field], _SHA256, f"execution.{field}")
    _size(execution["bundle_size_bytes"], "execution.bundle_size_bytes")
    publisher = _exact(payload["publisher"], {"publisher_id", "policy_digest"}, "publisher")
    _string(publisher["publisher_id"], _IDENTIFIER, "publisher.publisher_id")
    _string(publisher["policy_digest"], _SHA256, "publisher.policy_digest")
    destination = _exact(
        payload["destination"],
        {
            "kind",
            "store_id",
            "store_root",
            "namespace",
            "object_key",
            "object_id",
            "object_version",
        },
        "destination",
    )
    if destination["kind"] != "local_staging":
        raise PublicationReceiptError("unsupported publication destination kind")
    _string(destination["store_id"], _IDENTIFIER, "destination.store_id")
    store_root = destination["store_root"]
    if (
        not isinstance(store_root, str)
        or not store_root.startswith("/")
        or "\x00" in store_root
        or len(store_root) > 4096
    ):
        raise PublicationReceiptError("destination.store_root must be an absolute local path")
    _string(destination["namespace"], _IDENTIFIER, "destination.namespace")
    object_key = _string(destination["object_key"], _OBJECT_KEY, "destination.object_key")
    object_digest = object_key.rsplit("/", 1)[-1]
    if destination["object_id"] != f"sha256:{object_digest}":
        raise PublicationReceiptError(
            "destination.object_id does not match its content-addressed key"
        )
    _string(destination["object_version"], _SHA256, "destination.object_version")
    if destination["object_version"] != object_digest:
        raise PublicationReceiptError(
            "destination.object_version does not match its content-addressed key"
        )
    artifact = _exact(payload["artifact"], {"sha256", "size_bytes"}, "artifact")
    artifact_digest = _string(artifact["sha256"], _SHA256, "artifact.sha256")
    _size(artifact["size_bytes"], "artifact.size_bytes")
    if object_digest != artifact_digest:
        raise PublicationReceiptError("destination object does not identify the artifact digest")
    if (
        execution["bundle_digest"] != artifact_digest
        or execution["bundle_size_bytes"] != artifact["size_bytes"]
    ):
        raise PublicationReceiptError("execution bundle and staged artifact digest/size must match")
    timestamps = _exact(
        payload["timestamps"],
        {"reserved_at", "in_flight_at", "verified_at", "committed_at"},
        "timestamps",
    )
    parsed = [
        _timestamp(timestamps[name], f"timestamps.{name}")
        for name in ("reserved_at", "in_flight_at", "verified_at", "committed_at")
    ]
    if parsed != sorted(parsed):
        raise PublicationReceiptError("publication timestamps are out of order")
    if payload["outcome"] != "committed":
        raise PublicationReceiptError("publication receipt outcome must be committed")
    return payload


def _size(value: Any, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_ARTIFACT_BYTES
    ):
        raise PublicationReceiptError(f"{label} is invalid")
    return value


def _key_path(trusted_signers: str | Path, signer_id: str) -> Path:
    _string(signer_id, _IDENTIFIER, "key_id")
    root = Path(trusted_signers)
    if root.is_symlink() or not root.is_dir():
        raise PublicationReceiptError(
            "trusted signer directory is required and must be a real directory"
        )
    path = root / f"{signer_id}.pub.pem"
    if path.is_symlink() or not path.is_file():
        raise PublicationReceiptError(
            "receipt signer is not present in the trusted signer directory"
        )
    try:
        pem = path.read_bytes()
    except OSError as exc:
        raise PublicationReceiptError(f"trusted signer key cannot be read: {exc}") from exc
    if not pem.startswith(b"-----BEGIN PUBLIC KEY-----"):
        raise PublicationReceiptError("trusted signer must use SubjectPublicKeyInfo PEM encoding")
    return path


def _require_ed25519(public_key: Path, openssl: str) -> None:
    try:
        result = subprocess.run(
            [openssl, "pkey", "-pubin", "-in", str(public_key), "-text", "-noout"],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError as exc:
        raise PublicationReceiptError(f"cannot run OpenSSL: {exc}") from exc
    if result.returncode != 0 or not result.stdout.startswith("ED25519 Public-Key:"):
        raise PublicationReceiptError("trusted receipt signer must be an Ed25519 public key")


def _signed_bytes(schema_version: int, key_id: str, payload: dict[str, Any]) -> bytes:
    return DOMAIN + canonical_json_bytes(
        {"schema_version": schema_version, "key_id": key_id, "payload": payload}
    )


def sign_receipt(
    payload: dict[str, Any],
    *,
    key_id: str,
    private_key: str | Path,
    trusted_signers: str | Path,
    openssl: str = "openssl",
) -> bytes:
    """Sign a local receipt only when the private key matches the required pinned key."""
    validate_payload(payload)
    public_key = _key_path(trusted_signers, key_id)
    _require_ed25519(public_key, openssl)
    private_path = Path(private_key)
    if private_path.is_symlink() or not private_path.is_file():
        raise PublicationReceiptError("receipt signing key must be a regular non-symlink file")
    try:
        with tempfile.TemporaryDirectory(prefix="external-publication-sign-") as directory:
            derived_path = Path(directory) / "derived.pub.der"
            trusted_path = Path(directory) / "trusted.pub.der"
            input_path = Path(directory) / "receipt.bin"
            signature_path = Path(directory) / "signature.bin"
            derived = subprocess.run(
                [
                    openssl,
                    "pkey",
                    "-in",
                    str(private_path),
                    "-pubout",
                    "-outform",
                    "DER",
                    "-out",
                    str(derived_path),
                ],
                capture_output=True,
                check=False,
            )
            trusted = subprocess.run(
                [
                    openssl,
                    "pkey",
                    "-pubin",
                    "-in",
                    str(public_key),
                    "-pubout",
                    "-outform",
                    "DER",
                    "-out",
                    str(trusted_path),
                ],
                capture_output=True,
                check=False,
            )
            if (
                derived.returncode != 0
                or trusted.returncode != 0
                or derived_path.read_bytes() != trusted_path.read_bytes()
            ):
                raise PublicationReceiptError(
                    "receipt signing key does not match the trusted signer input"
                )
            input_path.write_bytes(_signed_bytes(1, key_id, payload))
            result = subprocess.run(
                [
                    openssl,
                    "pkeyutl",
                    "-sign",
                    "-inkey",
                    str(private_path),
                    "-rawin",
                    "-in",
                    str(input_path),
                    "-out",
                    str(signature_path),
                ],
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                raise PublicationReceiptError("OpenSSL could not sign the publication receipt")
            signature = signature_path.read_bytes()
    except OSError as exc:
        raise PublicationReceiptError(f"cannot run OpenSSL for publication signing: {exc}") from exc
    if len(signature) != 64:
        raise PublicationReceiptError("Ed25519 signature must be exactly 64 bytes")
    return canonical_json_bytes(
        {
            "schema_version": 1,
            "key_id": key_id,
            "payload": payload,
            "signature": base64.b64encode(signature).decode("ascii"),
        }
    )


def verify_receipt(
    receipt_bytes: bytes,
    *,
    trusted_signers: str | Path,
    expected_binding: dict[str, Any],
    trusted_publisher_keys: dict[str, str],
    openssl: str = "openssl",
    expected_destination_kind: str = "local_staging",
    expected_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify signature plus a closed caller-supplied publication identity binding."""
    envelope = _exact(
        _decode_json(receipt_bytes), {"schema_version", "key_id", "payload", "signature"}, "receipt"
    )
    if type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1:
        raise PublicationReceiptError("unsupported publication receipt schema_version")
    key_id = _string(envelope["key_id"], _IDENTIFIER, "key_id")
    payload = validate_payload(envelope["payload"])
    if expected_payload is not None and payload != expected_payload:
        raise PublicationReceiptError(
            "receipt payload does not match the expected publication identity"
        )
    binding = _exact(
        expected_binding,
        {
            "publication_id",
            "authorization_claim",
            "admission",
            "execution",
            "publisher",
            "destination",
            "artifact",
        },
        "expected_binding",
    )
    for name, keys in {
        "authorization_claim": {"authorization_id", "authorization_digest"},
        "admission": {"admission_id", "admission_digest"},
        "execution": {"publication_id", "receipt_digest", "bundle_digest", "bundle_size_bytes"},
        "publisher": {"publisher_id", "policy_digest"},
        "destination": {
            "kind",
            "store_id",
            "store_root",
            "namespace",
            "object_key",
            "object_id",
            "object_version",
        },
        "artifact": {"sha256", "size_bytes"},
    }.items():
        _exact(binding[name], keys, f"expected_binding.{name}")
    if any(payload.get(name) != value for name, value in binding.items()):
        raise PublicationReceiptError("receipt does not match the expected publication binding")
    if trusted_publisher_keys.get(payload["publisher"]["publisher_id"]) != key_id:
        raise PublicationReceiptError("receipt signer is not mapped to the declared publisher")
    if canonical_json_bytes(envelope) != receipt_bytes:
        raise PublicationReceiptError("receipt JSON is not canonical")
    if payload["destination"]["kind"] != expected_destination_kind:
        raise PublicationReceiptError(
            "receipt destination kind does not match the requested publication"
        )
    signature_text = envelope["signature"]
    if not isinstance(signature_text, str):
        raise PublicationReceiptError("signature must be base64 text")
    try:
        signature = base64.b64decode(signature_text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PublicationReceiptError("signature is not valid base64") from exc
    if len(signature) != 64 or base64.b64encode(signature).decode("ascii") != signature_text:
        raise PublicationReceiptError("signature must be canonical 64-byte Ed25519 base64")
    public_key = _key_path(trusted_signers, key_id)
    _require_ed25519(public_key, openssl)
    try:
        with tempfile.TemporaryDirectory(prefix="external-publication-verify-") as directory:
            input_path = Path(directory) / "receipt.bin"
            signature_path = Path(directory) / "signature.bin"
            input_path.write_bytes(_signed_bytes(1, key_id, payload))
            signature_path.write_bytes(signature)
            result = subprocess.run(
                [
                    openssl,
                    "pkeyutl",
                    "-verify",
                    "-pubin",
                    "-inkey",
                    str(public_key),
                    "-rawin",
                    "-sigfile",
                    str(signature_path),
                    "-in",
                    str(input_path),
                ],
                capture_output=True,
                check=False,
            )
    except OSError as exc:
        raise PublicationReceiptError(
            f"cannot run OpenSSL for publication verification: {exc}"
        ) from exc
    if result.returncode != 0:
        raise PublicationReceiptError("Ed25519 publication receipt signature verification failed")
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a signed local external-publication receipt."
    )
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--trusted-signers", type=Path, required=True)
    parser.add_argument(
        "--trusted-publisher", action="append", required=True, metavar="PUBLISHER=KEY_ID"
    )
    parser.add_argument("--expected-binding", type=Path, required=True)
    parser.add_argument("--destination-kind", default="local_staging")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        publisher_keys = dict(item.split("=", 1) for item in args.trusted_publisher)
        binding = _decode_json(args.expected_binding.read_bytes())
        payload = verify_receipt(
            args.receipt.read_bytes(),
            trusted_signers=args.trusted_signers,
            expected_binding=binding,
            trusted_publisher_keys=publisher_keys,
            expected_destination_kind=args.destination_kind,
        )
    except (OSError, PublicationReceiptError) as exc:
        raise SystemExit(f"publication receipt rejected: {exc}") from exc
    print(json.dumps(payload, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
