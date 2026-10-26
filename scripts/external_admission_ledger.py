"""Local reference ledger for immutable external-execution admissions.

This SQLite adapter is for tests and local reference use only. It proves neither
provider authentication, production isolation, nor signing custody. Production
storage and its claim interface must be protected from candidate access, and a
production controller must authenticate provider observations independently.
Claim compares a caller-supplied grant digest with the issued record; it does not
authenticate a bearer grant. A production controller must authenticate and
derive that digest outside this adapter.
The admission record is immutable; claim state lives in a separate ledger row.
This local ledger also enforces one admission ID per grant digest, but that database
constraint does not authenticate the caller or establish a protected controller,
global/durable trust, or publication authorization boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, NoReturn
from uuid import UUID

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_GIT_SHA = re.compile(r"^[a-f0-9]{40}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OUTPUT_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RESERVED_WINDOWS = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?$", re.IGNORECASE
)
_UTC_TIMESTAMP = re.compile(
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{1,6})?Z$"
)
_NETWORK_DESTINATION = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,252}[A-Za-z0-9])?$")
_MAX_OUTPUTS = 256
_MAX_ISSUANCE_RESPONSE_BYTES = 8_000_000
_UTC_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
CANONICAL_REPOSITORY = "https://github.com/MishkaStrategy/Module-Strategy.git"


class AdmissionError(ValueError):
    """Admission record or ledger operation is invalid."""


class AdmissionConflict(AdmissionError):
    """An admission ID conflicts or a grant digest was already issued."""


class AdmissionExpired(AdmissionError):
    """An admission can no longer be claimed."""


class AdmissionAlreadyClaimed(AdmissionError):
    """An admission has already been claimed."""


class GrantMismatch(AdmissionError):
    """The supplied grant digest does not match the issued admission."""


def _exact_object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AdmissionError(f"{label} must be an object")
    missing = keys - value.keys()
    extra = value.keys() - keys
    if missing or extra:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if extra:
            details.append("unknown " + ", ".join(sorted(extra)))
        raise AdmissionError(f"{label}: " + "; ".join(details))
    return value


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise AdmissionError(f"duplicate JSON key: {key}")
        output[key] = value
    return output


def _reject_constant(value: str) -> NoReturn:
    raise AdmissionError(f"nonfinite JSON value is forbidden: {value}")


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise AdmissionError(f"admission cannot be canonicalized: {exc}") from exc


def admission_digest(admission: Admission) -> str:
    """Return SHA-256 of the complete immutable canonical admission mapping."""
    if not isinstance(admission, Admission):
        raise AdmissionError("admission_digest requires a closed Admission record")
    return hashlib.sha256(_canonical_json(admission.to_mapping())).hexdigest()


def _timestamp(value: str, label: str) -> datetime:
    if not isinstance(value, str) or not _UTC_TIMESTAMP.fullmatch(value):
        raise AdmissionError(f"{label} must be canonical UTC RFC 3339 ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise AdmissionError(f"{label} is not a valid UTC timestamp") from exc
    return parsed


def _time_text(value: datetime | str) -> str:
    if isinstance(value, str):
        _timestamp(value, "now")
        return value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise AdmissionError("now must be a timezone-aware UTC datetime or canonical UTC timestamp")
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _epoch_microseconds(value: str) -> int:
    delta = _timestamp(value, "timestamp") - _UTC_EPOCH
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _validate_identifier(value: Any, label: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise AdmissionError(f"{label} must be a bounded identifier")


def _validate_output_path(path: Any) -> None:
    if not isinstance(path, str) or not path or len(path) > 512:
        raise AdmissionError("output path must be a nonempty bounded string")
    segments = path.split("/")
    for segment in segments:
        if (
            not _OUTPUT_SEGMENT.fullmatch(segment)
            or segment.endswith(".")
            or _RESERVED_WINDOWS.fullmatch(segment)
        ):
            raise AdmissionError(
                f"output path is outside the portable relative-path dialect: {path}"
            )


def _validate_output_set(paths: tuple[str, ...]) -> None:
    if len(paths) > _MAX_OUTPUTS:
        raise AdmissionError(f"required_output_paths may contain at most {_MAX_OUTPUTS} entries")
    seen: set[str] = set()
    for path in paths:
        _validate_output_path(path)
        normalized = path.casefold()
        if normalized in seen:
            raise AdmissionError(f"required outputs contain a case-insensitive collision: {path}")
        parts = normalized.split("/")
        parents = {"/".join(parts[:index]) for index in range(1, len(parts))}
        if parents & seen or any(existing.startswith(normalized + "/") for existing in seen):
            raise AdmissionError(
                f"required outputs contain a file/directory prefix conflict: {path}"
            )
        seen.add(normalized)


@dataclass(frozen=True)
class ResourcePolicy:
    cpu_millis: int
    memory_bytes: int
    storage_bytes: int
    process_limit: int
    execution_timeout_seconds: int

    def __post_init__(self) -> None:
        for name in (
            "cpu_millis",
            "memory_bytes",
            "storage_bytes",
            "process_limit",
            "execution_timeout_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise AdmissionError(f"resource_policy.{name} must be a positive integer")

    def to_mapping(self) -> dict[str, int]:
        return {
            "cpu_millis": self.cpu_millis,
            "memory_bytes": self.memory_bytes,
            "storage_bytes": self.storage_bytes,
            "process_limit": self.process_limit,
            "execution_timeout_seconds": self.execution_timeout_seconds,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> ResourcePolicy:
        data = _exact_object(
            value,
            {
                "cpu_millis",
                "memory_bytes",
                "storage_bytes",
                "process_limit",
                "execution_timeout_seconds",
            },
            "resource_policy",
        )
        return cls(**data)


@dataclass(frozen=True)
class NetworkPolicy:
    mode: str
    allowed_destinations: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.mode, str) or self.mode not in {"disabled", "allowlist"}:
            raise AdmissionError("network_policy.mode must be disabled or allowlist")
        if not isinstance(self.allowed_destinations, tuple):
            raise AdmissionError("network_policy.allowed_destinations must be a tuple")
        if self.mode == "disabled" and self.allowed_destinations:
            raise AdmissionError("disabled network policy cannot list destinations")
        if self.mode == "allowlist" and not self.allowed_destinations:
            raise AdmissionError("allowlist network policy must list at least one destination")
        seen: set[str] = set()
        for destination in self.allowed_destinations:
            if (
                not isinstance(destination, str)
                or not _NETWORK_DESTINATION.fullmatch(destination)
                or "*" in destination
            ):
                raise AdmissionError("network destination is invalid")
            normalized = destination.lower()
            if normalized in seen:
                raise AdmissionError("network destinations must be unique ignoring case")
            seen.add(normalized)

    def to_mapping(self) -> dict[str, Any]:
        return {"mode": self.mode, "allowed_destinations": list(self.allowed_destinations)}

    @classmethod
    def from_mapping(cls, value: Any) -> NetworkPolicy:
        data = _exact_object(value, {"mode", "allowed_destinations"}, "network_policy")
        destinations = data["allowed_destinations"]
        if not isinstance(destinations, list):
            raise AdmissionError("network_policy.allowed_destinations must be an array")
        return cls(mode=data["mode"], allowed_destinations=tuple(destinations))


@dataclass(frozen=True)
class Admission:
    admission_id: str
    grant_digest: str
    repository: str
    source_sha: str
    workflow_digest: str
    runtime_id: str
    workload_id: str
    workload_digest: str
    policy_id: str
    policy_digest: str
    environment_id: str
    image_digest: str
    image_os: str
    image_arch: str
    provider_id: str
    provider_run_id: str
    provider_attempt: int
    candidate_id: str
    candidate_digest: str
    profile_id: str
    profile_digest: str
    material_id: str
    material_digest: str
    required_output_paths: tuple[str, ...]
    resource_policy: ResourcePolicy
    network_policy: NetworkPolicy
    issued_at: str
    expires_at: str
    execution_deadline: str
    publication_deadline: str

    def __post_init__(self) -> None:
        try:
            parsed_id = str(UUID(self.admission_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise AdmissionError("admission_id must be a UUID") from exc
        if parsed_id != self.admission_id:
            raise AdmissionError("admission_id must be a canonical UUID")
        if not _SHA256.fullmatch(self.grant_digest):
            raise AdmissionError("grant_digest must be a lowercase SHA-256 digest")
        if self.repository != CANONICAL_REPOSITORY:
            raise AdmissionError("repository must be the canonical Module-Strategy URI")
        if not isinstance(self.source_sha, str) or not _GIT_SHA.fullmatch(self.source_sha):
            raise AdmissionError("source_sha must be a lowercase 40-character Git SHA")
        for field in (
            "workflow_digest",
            "workload_digest",
            "policy_digest",
            "image_digest",
            "candidate_digest",
            "profile_digest",
            "material_digest",
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise AdmissionError(f"{field} must be a lowercase SHA-256 digest")
        for field in (
            "runtime_id",
            "workload_id",
            "policy_id",
            "environment_id",
            "provider_id",
            "provider_run_id",
            "candidate_id",
            "profile_id",
            "material_id",
        ):
            _validate_identifier(getattr(self, field), field)
        if (
            isinstance(self.provider_attempt, bool)
            or not isinstance(self.provider_attempt, int)
            or self.provider_attempt < 1
        ):
            raise AdmissionError("provider_attempt must be a positive integer")
        if not isinstance(self.image_os, str) or self.image_os not in {
            "linux",
            "darwin",
            "windows",
        }:
            raise AdmissionError("image_os is invalid")
        if not isinstance(self.image_arch, str) or self.image_arch not in {
            "x86_64",
            "aarch64",
            "arm64",
            "AMD64",
        }:
            raise AdmissionError("image_arch is invalid")
        arch = {"arm64": "aarch64", "AMD64": "x86_64"}.get(self.image_arch, self.image_arch)
        if (self.image_os, arch) not in {
            ("linux", "x86_64"),
            ("linux", "aarch64"),
            ("darwin", "x86_64"),
            ("darwin", "aarch64"),
            ("windows", "x86_64"),
        }:
            raise AdmissionError("image OS/architecture mapping is unsupported")
        if not isinstance(self.required_output_paths, tuple) or any(
            not isinstance(path, str) for path in self.required_output_paths
        ):
            raise AdmissionError("required_output_paths must be a tuple of paths")
        _validate_output_set(self.required_output_paths)
        if not isinstance(self.resource_policy, ResourcePolicy) or not isinstance(
            self.network_policy, NetworkPolicy
        ):
            raise AdmissionError(
                "resource_policy and network_policy must use their closed record types"
            )
        issued = _timestamp(self.issued_at, "issued_at")
        expires = _timestamp(self.expires_at, "expires_at")
        deadline = _timestamp(self.execution_deadline, "execution_deadline")
        publication = _timestamp(self.publication_deadline, "publication_deadline")
        if not issued < expires <= deadline <= publication:
            raise AdmissionError(
                "timestamps must satisfy issued_at < expires_at <= execution_deadline <= publication_deadline"
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "admission_id": self.admission_id,
            "grant_digest": self.grant_digest,
            "repository": self.repository,
            "source_sha": self.source_sha,
            "workflow_digest": self.workflow_digest,
            "runtime_id": self.runtime_id,
            "workload_id": self.workload_id,
            "workload_digest": self.workload_digest,
            "policy_id": self.policy_id,
            "policy_digest": self.policy_digest,
            "environment_id": self.environment_id,
            "image_digest": self.image_digest,
            "image_os": self.image_os,
            "image_arch": self.image_arch,
            "provider_id": self.provider_id,
            "provider_run_id": self.provider_run_id,
            "provider_attempt": self.provider_attempt,
            "candidate_id": self.candidate_id,
            "candidate_digest": self.candidate_digest,
            "profile_id": self.profile_id,
            "profile_digest": self.profile_digest,
            "material_id": self.material_id,
            "material_digest": self.material_digest,
            "required_output_paths": list(self.required_output_paths),
            "resource_policy": self.resource_policy.to_mapping(),
            "network_policy": self.network_policy.to_mapping(),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "execution_deadline": self.execution_deadline,
            "publication_deadline": self.publication_deadline,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> Admission:
        fields = {
            "admission_id",
            "grant_digest",
            "repository",
            "source_sha",
            "workflow_digest",
            "runtime_id",
            "workload_id",
            "workload_digest",
            "policy_id",
            "policy_digest",
            "environment_id",
            "image_digest",
            "image_os",
            "image_arch",
            "provider_id",
            "provider_run_id",
            "provider_attempt",
            "candidate_id",
            "candidate_digest",
            "profile_id",
            "profile_digest",
            "material_id",
            "material_digest",
            "required_output_paths",
            "resource_policy",
            "network_policy",
            "issued_at",
            "expires_at",
            "execution_deadline",
            "publication_deadline",
        }
        data = _exact_object(value, fields, "admission")
        output_paths = data["required_output_paths"]
        if not isinstance(output_paths, list):
            raise AdmissionError("required_output_paths must be an array")
        converted = dict(data)
        converted["required_output_paths"] = tuple(output_paths)
        converted["resource_policy"] = ResourcePolicy.from_mapping(data["resource_policy"])
        converted["network_policy"] = NetworkPolicy.from_mapping(data["network_policy"])
        return cls(**converted)


@dataclass(frozen=True)
class Claim:
    admission_id: str
    executor_id: str
    claimed_at: str
    execution_deadline: str

    def to_mapping(self) -> dict[str, str]:
        return {
            "admission_id": self.admission_id,
            "executor_id": self.executor_id,
            "claimed_at": self.claimed_at,
            "execution_deadline": self.execution_deadline,
        }


class AdmissionLedger:
    """SQLite reference adapter with immutable issue and atomic one-use claim."""

    def __init__(self, database: str | Path, *, timeout_seconds: float = 10.0) -> None:
        self.database = str(database)
        self.timeout_seconds = timeout_seconds
        if self.database == ":memory:":
            raise AdmissionError("use a local file-backed SQLite database for the reference ledger")
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS admissions (
                    admission_id TEXT PRIMARY KEY,
                    record_json BLOB NOT NULL,
                    record_sha256 TEXT NOT NULL,
                    grant_digest TEXT NOT NULL,
                    issued_at_utc_us INTEGER NOT NULL,
                    expires_at_utc_us INTEGER NOT NULL,
                    execution_deadline TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('ISSUED', 'CLAIMED')),
                    executor_id TEXT,
                    claimed_at TEXT,
                    CHECK ((status = 'ISSUED' AND executor_id IS NULL AND claimed_at IS NULL)
                        OR (status = 'CLAIMED' AND executor_id IS NOT NULL AND claimed_at IS NOT NULL))
                )"""
            )
            try:
                connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS admissions_grant_digest_uq "
                    "ON admissions(grant_digest)"
                )
            except sqlite3.IntegrityError as exc:
                raise AdmissionError(
                    "existing admissions reuse grant_digest; cannot enable one-admission-per-grant"
                ) from exc
            connection.execute(
                """CREATE TABLE IF NOT EXISTS protected_issuance (
                    issuance_key TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    content_digest TEXT,
                    response_json BLOB,
                    response_sha256 TEXT
                )"""
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(protected_issuance)").fetchall()
            }
            if "content_digest" not in columns:
                connection.execute("ALTER TABLE protected_issuance ADD COLUMN content_digest TEXT")
            if "response_json" not in columns:
                connection.execute("ALTER TABLE protected_issuance ADD COLUMN response_json BLOB")
            if "response_sha256" not in columns:
                connection.execute("ALTER TABLE protected_issuance ADD COLUMN response_sha256 TEXT")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database, timeout=self.timeout_seconds, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={int(self.timeout_seconds * 1000)}")
        return connection

    def read_snapshot(self, admission_id: str) -> tuple[Admission | None, Claim | None]:
        """Read admission and claim through a SQLite connection that cannot create the DB."""
        try:
            database_uri = Path(self.database).resolve(strict=True).as_uri() + "?mode=ro"
        except OSError as exc:
            raise AdmissionError(f"read-only admission database is unavailable: {exc}") from exc
        connection = sqlite3.connect(
            database_uri, uri=True, timeout=self.timeout_seconds, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(f"PRAGMA busy_timeout={int(self.timeout_seconds * 1000)}")
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT record_json, record_sha256, status, executor_id, claimed_at, execution_deadline "
                "FROM admissions WHERE admission_id = ?",
                (admission_id,),
            ).fetchone()
            if row is None:
                return None, None
            raw = bytes(row["record_json"])
            if hashlib.sha256(raw).hexdigest() != row["record_sha256"]:
                raise AdmissionError("stored immutable admission digest does not match")
            try:
                mapping = json.loads(
                    raw.decode("ascii"),
                    object_pairs_hook=_pairs_no_duplicates,
                    parse_constant=_reject_constant,
                )
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise AdmissionError(f"stored admission cannot be decoded: {exc}") from exc
            admission = Admission.from_mapping(mapping)
            if admission.admission_id != admission_id:
                raise AdmissionError(
                    "stored immutable admission id does not match its database key"
                )
            if row["status"] != "CLAIMED":
                return admission, None
            claim = Claim(
                admission_id,
                row["executor_id"],
                row["claimed_at"],
                row["execution_deadline"],
            )
            return admission, claim
        finally:
            connection.close()

    def issue(self, admission: Admission) -> bool:
        """Insert an admission; return False only for a byte-identical retry."""
        return self.issue_many((admission,))[0]

    def issue_many(
        self,
        admissions: tuple[Admission, ...],
        *,
        issuance_key: str | None = None,
        content_digest: str | None = None,
        response: Any = None,
    ) -> tuple[bool, ...]:
        """Atomically insert a complete batch and optional unique issuance reservation."""
        if not isinstance(admissions, tuple) or not admissions:
            raise AdmissionError("issue_many requires a nonempty tuple of closed admissions")
        if len(admissions) > _MAX_OUTPUTS:
            raise AdmissionError(f"issue_many may contain at most {_MAX_OUTPUTS} admissions")
        if issuance_key is not None and not _SHA256.fullmatch(issuance_key):
            raise AdmissionError("issuance_key must be a lowercase SHA-256 digest")
        response_bytes: bytes | None = None
        if issuance_key is not None:
            if not isinstance(content_digest, str) or not _SHA256.fullmatch(content_digest):
                raise AdmissionError("protected issuance requires a lowercase content digest")
            if response is None:
                raise AdmissionError("protected issuance requires an immutable response")
            response_bytes = _canonical_json(response)
            if len(response_bytes) > _MAX_ISSUANCE_RESPONSE_BYTES:
                raise AdmissionError("protected issuance response exceeds the maximum size")
        elif content_digest is not None or response is not None:
            raise AdmissionError("content_digest and response require an issuance_key")
        prepared: list[tuple[Admission, bytes, str]] = []
        ids: set[str] = set()
        grant_digests: set[str] = set()
        for admission in admissions:
            if not isinstance(admission, Admission):
                raise AdmissionError("issue_many requires only closed Admission records")
            if admission.admission_id in ids or admission.grant_digest in grant_digests:
                raise AdmissionConflict("batch reuses an admission ID or grant digest")
            ids.add(admission.admission_id)
            grant_digests.add(admission.grant_digest)
            data = _canonical_json(admission.to_mapping())
            prepared.append((admission, data, hashlib.sha256(data).hexdigest()))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if issuance_key is not None:
                reserved = connection.execute(
                    "SELECT content_digest, response_json, response_sha256 "
                    "FROM protected_issuance WHERE issuance_key = ?",
                    (issuance_key,),
                ).fetchone()
                if reserved is not None:
                    if reserved["content_digest"] != content_digest:
                        raise AdmissionConflict(
                            "issuance key is already bound to different protected content"
                        )
                    if reserved["response_json"] is None or reserved["response_sha256"] is None:
                        raise AdmissionConflict(
                            "prior issuance has no replayable immutable response"
                        )
                    if (
                        hashlib.sha256(bytes(reserved["response_json"])).hexdigest()
                        != reserved["response_sha256"]
                    ):
                        raise AdmissionError("stored issuance response digest does not match")
                    connection.commit()
                    return tuple(False for _ in admissions)
            inserted: list[bool] = []
            for admission, data, _ in prepared:
                existing = connection.execute(
                    "SELECT record_json FROM admissions WHERE admission_id = ?",
                    (admission.admission_id,),
                ).fetchone()
                if existing is not None:
                    if bytes(existing["record_json"]) != data:
                        raise AdmissionConflict(
                            "admission_id already exists with different immutable record bytes"
                        )
                    inserted.append(False)
                    continue
                grant_owner = connection.execute(
                    "SELECT admission_id FROM admissions WHERE grant_digest = ?",
                    (admission.grant_digest,),
                ).fetchone()
                if grant_owner is not None:
                    raise AdmissionConflict(
                        "grant_digest already issued for a different admission_id"
                    )
                inserted.append(True)
            if issuance_key is not None:
                connection.execute(
                    """INSERT INTO protected_issuance
                       (issuance_key, created_at, content_digest, response_json, response_sha256)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        issuance_key,
                        datetime.now(timezone.utc).isoformat(),
                        content_digest,
                        response_bytes,
                        hashlib.sha256(response_bytes or b"").hexdigest(),
                    ),
                )
            for (admission, data, digest), should_insert in zip(prepared, inserted, strict=True):
                if not should_insert:
                    continue
                connection.execute(
                    """INSERT INTO admissions
                        (admission_id, record_json, record_sha256, grant_digest, issued_at_utc_us,
                         expires_at_utc_us, execution_deadline, status, executor_id, claimed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'ISSUED', NULL, NULL)""",
                    (
                        admission.admission_id,
                        data,
                        digest,
                        admission.grant_digest,
                        _epoch_microseconds(admission.issued_at),
                        _epoch_microseconds(admission.expires_at),
                        admission.execution_deadline,
                    ),
                )
            connection.commit()
            return tuple(inserted)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def read_issuance(self, issuance_key: str) -> tuple[str, Any] | None:
        """Read a protected immutable issuance response without changing ledger state."""
        if not _SHA256.fullmatch(issuance_key if isinstance(issuance_key, str) else ""):
            raise AdmissionError("issuance_key must be a lowercase SHA-256 digest")
        try:
            database_uri = Path(self.database).resolve(strict=True).as_uri() + "?mode=ro"
        except OSError as exc:
            raise AdmissionError(f"admission database is unavailable: {exc}") from exc
        connection = sqlite3.connect(database_uri, uri=True, timeout=self.timeout_seconds)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT content_digest, response_json, response_sha256 "
                "FROM protected_issuance WHERE issuance_key = ?",
                (issuance_key,),
            ).fetchone()
            if row is None or row["response_json"] is None:
                return None
            response_bytes = bytes(row["response_json"])
            if len(response_bytes) > _MAX_ISSUANCE_RESPONSE_BYTES:
                raise AdmissionError("stored issuance response exceeds the maximum size")
            if (
                row["response_sha256"] is None
                or hashlib.sha256(response_bytes).hexdigest() != row["response_sha256"]
            ):
                raise AdmissionError("stored issuance response digest does not match")
            try:
                response = json.loads(
                    response_bytes.decode("ascii"),
                    object_pairs_hook=_pairs_no_duplicates,
                    parse_constant=_reject_constant,
                )
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise AdmissionError(f"stored issuance response cannot be decoded: {exc}") from exc
            return row["content_digest"], response
        finally:
            connection.close()

    def get(self, admission_id: str) -> Admission | None:
        """Return the immutable admission record regardless of claim state."""
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT record_json, record_sha256 FROM admissions WHERE admission_id = ?",
                (admission_id,),
            ).fetchone()
            if row is None:
                return None
            raw = bytes(row["record_json"])
            if hashlib.sha256(raw).hexdigest() != row["record_sha256"]:
                raise AdmissionError("stored immutable admission digest does not match")
            try:
                mapping = json.loads(
                    raw.decode("ascii"),
                    object_pairs_hook=_pairs_no_duplicates,
                    parse_constant=_reject_constant,
                )
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise AdmissionError(f"stored admission cannot be decoded: {exc}") from exc
            return Admission.from_mapping(mapping)
        finally:
            connection.close()

    def get_claim(self, admission_id: str) -> Claim | None:
        """Return the persisted executor, claim time, and deadline, if claimed."""
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT status, executor_id, claimed_at, execution_deadline FROM admissions WHERE admission_id = ?",
                (admission_id,),
            ).fetchone()
            if row is None or row["status"] != "CLAIMED":
                return None
            return Claim(
                admission_id, row["executor_id"], row["claimed_at"], row["execution_deadline"]
            )
        finally:
            connection.close()

    def claim(
        self,
        admission_id: str,
        grant_digest: str,
        executor_id: str,
        now: datetime | str | Callable[[], datetime | str],
    ) -> Claim:
        """Atomically transition ISSUED to CLAIMED; only one concurrent caller wins."""
        if not _SHA256.fullmatch(grant_digest if isinstance(grant_digest, str) else ""):
            raise AdmissionError("grant_digest must be a lowercase SHA-256 digest")
        _validate_identifier(executor_id, "executor_id")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT grant_digest, issued_at_utc_us, expires_at_utc_us, execution_deadline, status FROM admissions WHERE admission_id = ?",
                (admission_id,),
            ).fetchone()
            if row is None:
                raise AdmissionError("unknown admission_id")
            if grant_digest != row["grant_digest"]:
                raise GrantMismatch("grant digest does not match issued admission")
            if row["status"] != "ISSUED":
                raise AdmissionAlreadyClaimed(
                    "admission has already been claimed; retries require a new admission"
                )
            # Resolve callable clocks only after BEGIN IMMEDIATE has acquired the
            # write lock, so waiting for the lock cannot make an old timestamp valid.
            claimed_at = _time_text(now() if callable(now) else now)
            claimed_at_us = _epoch_microseconds(claimed_at)
            if claimed_at_us < row["issued_at_utc_us"]:
                raise AdmissionError("claim time is earlier than admission issued_at")
            if claimed_at_us >= row["expires_at_utc_us"]:
                raise AdmissionExpired(
                    "admission expired before claim; retry requires a new admission"
                )
            cursor = connection.execute(
                """UPDATE admissions
                   SET status = 'CLAIMED', executor_id = ?, claimed_at = ?
                   WHERE admission_id = ? AND status = 'ISSUED' AND grant_digest = ?
                     AND issued_at_utc_us <= ? AND expires_at_utc_us > ?""",
                (executor_id, claimed_at, admission_id, grant_digest, claimed_at_us, claimed_at_us),
            )
            if cursor.rowcount != 1:
                raise AdmissionAlreadyClaimed("admission claim lost the one-use race")
            claim = Claim(admission_id, executor_id, claimed_at, row["execution_deadline"])
            connection.commit()
            return claim
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
