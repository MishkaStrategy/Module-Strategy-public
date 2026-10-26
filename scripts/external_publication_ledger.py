"""Local-only publication staging ledger for verified execution publications.

This is a reference implementation. Authorization authenticity is delegated to
an explicitly injected verifier; this module supplies no authorization trust root
or production verifier. The SQLite ledger, signer custody, and staging store are
not protected or globally durable trust boundaries. The publisher-to-key mapping
is local configuration, not a deployment-pinned trust root. GitHub publication
is not supported, and receipts produced by this local staging path are not
acceptance-eligible.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from scripts.external_execution_publisher import LocalExecutionPublisher
from scripts.external_publication_receipt import (
    PublicationReceiptError,
    sign_receipt,
    verify_receipt,
)
from scripts.external_admission_ledger import admission_digest

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_UTC_TIMESTAMP = re.compile(
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]{1,6})?Z$"
)


class PublicationLedgerError(ValueError):
    """A local publication ledger operation failed closed."""


@dataclass(frozen=True, slots=True)
class VerifiedPublicationAuthorization:
    """Claims returned only after an injected verifier authenticates an authorization.

    The verifier must establish the authorization's trusted issuer/signature and
    derive actor, publisher policy digest, and nonce from that authenticated
    authorization, never from values supplied by the publication caller. The
    ledger independently checks the exact operation binding and expiry before
    reserving or writing.
    """

    authorization_id: str
    authorization_digest: str
    actor: str
    publisher_policy_digest: str
    audience: str
    nonce: str
    admission_id: str
    admission_digest: str
    execution_publication_id: str
    execution_receipt_digest: str
    output_inventory: tuple[tuple[str, str, int], ...]
    output_inventory_digest: str
    destination_kind: str
    store_id: str
    store_root: str
    namespace: str
    object_key: str
    object_id: str
    object_version: str
    expires_at: str


class PublicationAuthorizationVerifier(Protocol):
    """Authenticate an opaque owner authorization and return its verified facts."""

    def verify(self, authorization: object) -> VerifiedPublicationAuthorization: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _not_before(value: str, lower_bound: str) -> str:
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    floor = datetime.fromisoformat(lower_bound[:-1] + "+00:00")
    return value if parsed >= floor else lower_bound


def _inventory_digest(inventory: tuple[tuple[str, str, int], ...]) -> str:
    encoded = json.dumps(
        [{"path": path, "sha256": digest, "size_bytes": size} for path, digest, size in inventory],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _verify_authorization_binding(
    verifier: PublicationAuthorizationVerifier | None,
    authorization: object,
    *,
    expected: dict[str, object],
    now: str,
) -> VerifiedPublicationAuthorization:
    if verifier is None or not callable(getattr(verifier, "verify", None)):
        raise PublicationLedgerError("publication authorization verifier is not configured")
    try:
        verified = verifier.verify(authorization)
    except Exception as exc:
        raise PublicationLedgerError("publication authorization verification failed") from exc
    if not isinstance(verified, VerifiedPublicationAuthorization):
        raise PublicationLedgerError("authorization verifier returned invalid verified claims")
    if (
        not isinstance(verified.authorization_id, str)
        or not _UUID.fullmatch(verified.authorization_id)
        or not isinstance(verified.authorization_digest, str)
        or not _SHA256.fullmatch(verified.authorization_digest)
    ):
        raise PublicationLedgerError("verified authorization identity or expiry is invalid")
    publisher_policy_digest = getattr(verified, "publisher_policy_digest", None)
    if not isinstance(publisher_policy_digest, str) or not _SHA256.fullmatch(
        publisher_policy_digest
    ):
        raise PublicationLedgerError("verified publisher policy digest is invalid")
    if (
        not isinstance(verified.nonce, str)
        or not verified.nonce
        or len(verified.nonce) > 256
        or any(character.isspace() for character in verified.nonce)
        or not isinstance(verified.expires_at, str)
        or not _UTC_TIMESTAMP.fullmatch(verified.expires_at)
    ):
        raise PublicationLedgerError("verified authorization identity or expiry is invalid")
    _ensure_authorization_not_expired(verified, now=now)
    for field, expected_value in expected.items():
        if getattr(verified, field, object()) != expected_value:
            raise PublicationLedgerError(
                f"publication authorization does not match verified {field} binding"
            )
    return verified


def _ensure_authorization_not_expired(
    verified: VerifiedPublicationAuthorization, *, now: str
) -> None:
    """Recheck expiry at the serialized reservation boundary."""
    try:
        expires_at = datetime.fromisoformat(verified.expires_at[:-1] + "+00:00")
        current_time = datetime.fromisoformat(now[:-1] + "+00:00")
    except (AttributeError, ValueError) as exc:
        raise PublicationLedgerError("verified authorization expiry is invalid") from exc
    if expires_at <= current_time:
        raise PublicationLedgerError("publication authorization has expired")


def _open_directory_chain(
    root: Path, relative: tuple[str, ...] = (), *, create: bool = False
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(root.anchor, flags)
    try:
        for part in (*root.parts[1:], *relative):
            created = False
            if create:
                try:
                    os.mkdir(part, dir_fd=fd)
                    os.fsync(fd)
                    created = True
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=fd)
            if created:
                os.fsync(child)
            os.close(fd)
            fd = child
        return fd
    except Exception:
        os.close(fd)
        raise


def _read_store_object(root: Path, key: str) -> bytes:
    parts = key.split("/")
    directory = _open_directory_chain(root, tuple(parts[:-1]))
    try:
        fd = os.open(parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise PublicationLedgerError("staging object is not a regular file")
            chunks = []
            while chunk := os.read(fd, 1024 * 1024):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def _write_store_object(root: Path, key: str, content: bytes) -> None:
    parts = key.split("/")
    directory = _open_directory_chain(root, tuple(parts[:-1]), create=True)
    temp_name = f".staging-{secrets.token_hex(16)}.tmp"
    try:
        fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        try:
            view = memoryview(content)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write while staging publication object")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(
                temp_name,
                parts[-1],
                src_dir_fd=directory,
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
        except FileExistsError:
            raise
        os.fsync(directory)
    finally:
        try:
            os.unlink(temp_name, dir_fd=directory)
            os.fsync(directory)
        except FileNotFoundError:
            pass
        os.close(directory)


class LocalPublicationLedger:
    """Stage the exact execution bundle as the artifact under a local content key.

    An injected verifier must authenticate the authorization and the ledger
    matches its claims to the exact committed execution and configured local
    destination before reservation. Local receipts remain non-acceptance-eligible:
    the verifier, ledger, signer custody, and staging store are not production
    trust boundaries.
    """

    def __init__(
        self,
        database: str | Path,
        store_root: str | Path,
        *,
        store_id: str,
        namespace: str,
        trusted_publisher_keys: dict[str, str],
        publisher_audience: str,
        authorization_verifier: PublicationAuthorizationVerifier | None = None,
    ):
        if authorization_verifier is None or not callable(
            getattr(authorization_verifier, "verify", None)
        ):
            raise PublicationLedgerError("publication authorization verifier is required")
        if not isinstance(publisher_audience, str) or not publisher_audience.strip():
            raise PublicationLedgerError("publisher audience must be explicitly configured")
        self.database = str(database)
        self.store_root = Path(store_root).absolute()
        self.store_id = store_id
        self.namespace = namespace
        self.publisher_audience = publisher_audience
        self._authorization_verifier = authorization_verifier
        self.trusted_publisher_keys = dict(trusted_publisher_keys)
        if self.database == ":memory:":
            raise PublicationLedgerError("a file-backed local ledger is required")
        try:
            descriptor = _open_directory_chain(self.store_root, create=True)
            os.close(descriptor)
        except OSError as exc:
            raise PublicationLedgerError(f"cannot establish safe staging root: {exc}") from exc
        self.store_root = self.store_root.resolve(strict=True)
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS publications (
                publication_id TEXT PRIMARY KEY,
                authorization_id TEXT NOT NULL UNIQUE,
                authorization_nonce TEXT,
                admission_id TEXT NOT NULL UNIQUE,
                input_digest TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('RESERVED','IN_FLIGHT','VERIFIED','COMMITTED','UNCERTAIN')),
                receipt BLOB,
                object_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                owner_token TEXT,
                lease_until TEXT
            )""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(publications)")}
            if "owner_token" not in columns:
                db.execute("ALTER TABLE publications ADD COLUMN owner_token TEXT")
            if "lease_until" not in columns:
                db.execute("ALTER TABLE publications ADD COLUMN lease_until TEXT")
            if "authorization_nonce" not in columns:
                db.execute("ALTER TABLE publications ADD COLUMN authorization_nonce TEXT")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS publications_authorization_nonce_unique "
                "ON publications(authorization_nonce)"
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def publish(
        self,
        execution_publisher: LocalExecutionPublisher,
        execution_publication_id: str,
        *,
        authorization: object,
        publisher_id: str,
        key_id: str,
        private_key: str | Path,
        trusted_signers: str | Path,
        destination_kind: str = "local_staging",
    ) -> bytes:
        """Stage a committed bundle after authenticating and binding its authorization."""
        if destination_kind != "local_staging":
            raise PublicationLedgerError("only local_staging destinations are supported")
        if self._authorization_verifier is None:
            raise PublicationLedgerError("publication authorization verifier is not configured")
        if self.trusted_publisher_keys.get(publisher_id) != key_id:
            raise PublicationLedgerError(
                "publisher is not mapped to this signer by configured local policy"
            )
        snapshot = execution_publisher.read_committed(execution_publication_id)
        bundle = snapshot.bundle_bytes
        digest = hashlib.sha256(bundle).hexdigest()
        now = _now()
        owner_token = secrets.token_hex(32)
        lease_until = (
            (datetime.now(timezone.utc) + timedelta(minutes=5))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        inventory = tuple(
            (item.path, item.sha256, item.size_bytes)
            for item in sorted(snapshot.outputs, key=lambda item: item.path)
        )
        key = f"objects/sha256/{digest}"
        expected_authorization = {
            "actor": publisher_id,
            "audience": self.publisher_audience,
            "admission_id": snapshot.admission.admission_id,
            "admission_digest": admission_digest(snapshot.admission),
            "execution_publication_id": snapshot.publication_id,
            "execution_receipt_digest": hashlib.sha256(snapshot.receipt_bytes).hexdigest(),
            "output_inventory": inventory,
            "output_inventory_digest": _inventory_digest(inventory),
            "destination_kind": "local_staging",
            "store_id": self.store_id,
            "store_root": str(self.store_root),
            "namespace": self.namespace,
            "object_key": key,
            "object_id": f"sha256:{digest}",
            "object_version": digest,
        }
        verified_authorization = _verify_authorization_binding(
            self._authorization_verifier,
            authorization,
            expected=expected_authorization,
            now=now,
        )
        authorization_id = verified_authorization.authorization_id
        authorization_digest = verified_authorization.authorization_digest
        private_path = Path(private_key)
        trusted_path = Path(trusted_signers) / f"{key_id}.pub.pem"
        try:
            private_digest = hashlib.sha256(private_path.read_bytes()).hexdigest()
            trusted_digest = hashlib.sha256(trusted_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise PublicationLedgerError(f"signer key inputs are unavailable: {exc}") from exc
        authorization = {
            "authorization_id": authorization_id,
            "authorization_digest": authorization_digest,
        }
        input_digest = hashlib.sha256(
            json.dumps(
                {
                    "authorization_id": authorization_id,
                    "authorization_digest": authorization_digest,
                    "authorization_actor": verified_authorization.actor,
                    "authorization_audience": verified_authorization.audience,
                    "authorization_nonce": verified_authorization.nonce,
                    "authorization_expires_at": verified_authorization.expires_at,
                    "admission_id": snapshot.admission.admission_id,
                    "execution_publication_id": snapshot.publication_id,
                    "execution_receipt_digest": hashlib.sha256(snapshot.receipt_bytes).hexdigest(),
                    "output_inventory_digest": verified_authorization.output_inventory_digest,
                    "bundle_digest": digest,
                    "publisher_id": publisher_id,
                    "publisher_policy_digest": verified_authorization.publisher_policy_digest,
                    "destination_kind": destination_kind,
                    "store_id": self.store_id,
                    "store_root": str(self.store_root),
                    "namespace": self.namespace,
                    "key_id": key_id,
                    "private_key_digest": private_digest,
                    "trusted_key_digest": trusted_digest,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        receipt_id = str(uuid.uuid4())
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM publications WHERE authorization_id=? OR admission_id=? "
                "OR authorization_nonce=?",
                (authorization_id, snapshot.admission.admission_id, verified_authorization.nonce),
            ).fetchall()
            if len(existing) > 1:
                raise PublicationLedgerError(
                    "authorization, nonce, or admission conflicts with multiple reservations"
                )
            existing = existing[0] if existing else None
            if existing:
                if existing["input_digest"] != input_digest:
                    raise PublicationLedgerError(
                        "authorization/admission already reserved with conflicting input"
                    )
                if existing["status"] == "COMMITTED" and existing["receipt"] is not None:
                    try:
                        stored_payload = verify_receipt(
                            bytes(existing["receipt"]),
                            trusted_signers=trusted_signers,
                            trusted_publisher_keys=self.trusted_publisher_keys,
                            expected_binding={
                                "publication_id": existing["publication_id"],
                                "authorization_claim": authorization,
                                "admission": {
                                    "admission_id": snapshot.admission.admission_id,
                                    "admission_digest": admission_digest(snapshot.admission),
                                },
                                "execution": {
                                    "publication_id": snapshot.publication_id,
                                    "receipt_digest": hashlib.sha256(
                                        snapshot.receipt_bytes
                                    ).hexdigest(),
                                    "bundle_digest": digest,
                                    "bundle_size_bytes": len(bundle),
                                },
                                "publisher": {
                                    "publisher_id": publisher_id,
                                    "policy_digest": verified_authorization.publisher_policy_digest,
                                },
                                "destination": {
                                    "kind": "local_staging",
                                    "store_id": self.store_id,
                                    "store_root": str(self.store_root),
                                    "namespace": self.namespace,
                                    "object_key": key,
                                    "object_id": f"sha256:{digest}",
                                    "object_version": digest,
                                },
                                "artifact": {"sha256": digest, "size_bytes": len(bundle)},
                            },
                        )
                        object_bytes = _read_store_object(self.store_root, key)
                    except (OSError, PublicationReceiptError, PublicationLedgerError) as exc:
                        raise PublicationLedgerError(
                            f"committed publication failed retry verification: {exc}"
                        ) from exc
                    expected = {
                        "publication_id": existing["publication_id"],
                        "authorization_claim": authorization,
                        "admission": {
                            "admission_id": snapshot.admission.admission_id,
                            "admission_digest": admission_digest(snapshot.admission),
                        },
                        "execution": {
                            "publication_id": snapshot.publication_id,
                            "receipt_digest": hashlib.sha256(snapshot.receipt_bytes).hexdigest(),
                            "bundle_digest": digest,
                            "bundle_size_bytes": len(bundle),
                        },
                        "publisher": {
                            "publisher_id": publisher_id,
                            "policy_digest": verified_authorization.publisher_policy_digest,
                        },
                        "destination": {
                            "kind": "local_staging",
                            "store_id": self.store_id,
                            "store_root": str(self.store_root),
                            "namespace": self.namespace,
                            "object_key": key,
                            "object_id": f"sha256:{digest}",
                            "object_version": digest,
                        },
                        "artifact": {"sha256": digest, "size_bytes": len(bundle)},
                        "outcome": "committed",
                    }
                    if object_bytes != bundle or any(
                        stored_payload.get(name) != value for name, value in expected.items()
                    ):
                        raise PublicationLedgerError(
                            "committed publication does not match current bytes or identity"
                        )
                    db.execute("COMMIT")
                    return bytes(existing["receipt"])
                if existing["status"] == "COMMITTED":
                    raise PublicationLedgerError(
                        "committed publication is missing its immutable receipt"
                    )
                if existing["status"] != "COMMITTED":
                    expiry = existing["lease_until"]
                    if existing["status"] in {"IN_FLIGHT", "VERIFIED"} and expiry and expiry > now:
                        raise PublicationLedgerError("publication has an active writer lease")
                    try:
                        existing_object = _read_store_object(self.store_root, key)
                    except FileNotFoundError:
                        existing_object = None
                    except OSError as exc:
                        raise PublicationLedgerError(
                            f"cannot reconcile staging object safely: {exc}"
                        ) from exc
                    if existing_object == bundle:
                        db.execute(
                            "UPDATE publications SET status='VERIFIED' WHERE publication_id=?",
                            (existing["publication_id"],),
                        )
                    elif existing_object is not None:
                        db.execute(
                            "UPDATE publications SET status='UNCERTAIN' WHERE publication_id=?",
                            (existing["publication_id"],),
                        )
                        db.execute("COMMIT")
                        raise PublicationLedgerError(
                            "staging object conflicts with reserved identity"
                        )
                publication_id = existing["publication_id"]
                reserved_at = existing["created_at"]
            else:
                publication_id = receipt_id
                reserved_at = now
                db.execute(
                    """INSERT INTO publications (
                        publication_id, authorization_id, authorization_nonce, admission_id, input_digest,
                        status, receipt, object_key, created_at, owner_token, lease_until
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        publication_id,
                        authorization_id,
                        verified_authorization.nonce,
                        snapshot.admission.admission_id,
                        input_digest,
                        "RESERVED",
                        None,
                        key,
                        reserved_at,
                        owner_token,
                        lease_until,
                    ),
                )
            _ensure_authorization_not_expired(verified_authorization, now=_now())
            db.execute(
                "UPDATE publications SET status='IN_FLIGHT', owner_token=?, lease_until=? WHERE publication_id=?",
                (owner_token, lease_until, publication_id),
            )
            db.execute("COMMIT")
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

        try:
            try:
                _write_store_object(self.store_root, key, bundle)
            except FileExistsError:
                if _read_store_object(self.store_root, key) != bundle:
                    raise PublicationLedgerError(
                        "content-addressed destination exists with different bytes"
                    )
            if _read_store_object(self.store_root, key) != bundle:
                raise PublicationLedgerError(
                    "staging readback differs from verified execution bundle"
                )
        except Exception as exc:
            with self._connect() as db:
                db.execute(
                    "UPDATE publications SET status='UNCERTAIN' WHERE publication_id=? AND owner_token=?",
                    (publication_id, owner_token),
                )
            if isinstance(exc, PublicationLedgerError):
                raise
            raise PublicationLedgerError(f"staging write/readback failed: {exc}") from exc

        with self._connect() as db:
            db.execute(
                "UPDATE publications SET status='VERIFIED' WHERE publication_id=? AND status='IN_FLIGHT' AND owner_token=?",
                (publication_id, owner_token),
            )

        in_flight_at = _not_before(now, reserved_at)
        verified_at = _not_before(_now(), in_flight_at)
        committed_at = _not_before(_now(), verified_at)
        payload: dict[str, Any] = {
            "publication_id": publication_id,
            "authorization_claim": authorization,
            "admission": {
                "admission_id": snapshot.admission.admission_id,
                "admission_digest": admission_digest(snapshot.admission),
            },
            "execution": {
                "publication_id": snapshot.publication_id,
                "receipt_digest": hashlib.sha256(snapshot.receipt_bytes).hexdigest(),
                "bundle_digest": hashlib.sha256(bundle).hexdigest(),
                "bundle_size_bytes": len(bundle),
            },
            "publisher": {
                "publisher_id": publisher_id,
                "policy_digest": verified_authorization.publisher_policy_digest,
            },
            "destination": {
                "kind": "local_staging",
                "store_id": self.store_id,
                "store_root": str(self.store_root),
                "namespace": self.namespace,
                "object_key": key,
                "object_id": f"sha256:{digest}",
                "object_version": digest,
            },
            "artifact": {"sha256": digest, "size_bytes": len(bundle)},
            "timestamps": {
                "reserved_at": reserved_at,
                "in_flight_at": in_flight_at,
                "verified_at": verified_at,
                "committed_at": committed_at,
            },
            "outcome": "committed",
        }
        try:
            receipt = sign_receipt(
                payload, key_id=key_id, private_key=private_key, trusted_signers=trusted_signers
            )
            verify_receipt(
                receipt,
                trusted_signers=trusted_signers,
                trusted_publisher_keys=self.trusted_publisher_keys,
                expected_binding={
                    "publication_id": payload["publication_id"],
                    **{
                        name: payload[name]
                        for name in (
                            "authorization_claim",
                            "admission",
                            "execution",
                            "publisher",
                            "destination",
                            "artifact",
                        )
                    },
                },
            )
        except (OSError, PublicationReceiptError) as exc:
            with self._connect() as db:
                db.execute(
                    "UPDATE publications SET status='UNCERTAIN' WHERE publication_id=? AND owner_token=?",
                    (publication_id, owner_token),
                )
            raise PublicationLedgerError(
                f"publication receipt could not be verified: {exc}"
            ) from exc
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                "UPDATE publications SET status='COMMITTED', receipt=?, owner_token=NULL, lease_until=NULL "
                "WHERE publication_id=? AND status='VERIFIED' AND owner_token=?",
                (receipt, publication_id, owner_token),
            )
            if cursor.rowcount != 1:
                db.execute("ROLLBACK")
                raise PublicationLedgerError("publication writer fence was lost before commit")
            db.execute("COMMIT")
        return receipt
