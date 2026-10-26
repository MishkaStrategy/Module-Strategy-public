from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest
from threading import RLock
from time import monotonic
from typing import Self

from .errors import ApiAuthenticationError, ApiAuthorizationError
from .security import ApiPrincipal, ApiRole, role_allows, validate_api_token

_TOKEN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HEX_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def utc_now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("security timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    return _utc(datetime.fromisoformat(normalized))


@dataclass(frozen=True, slots=True)
class ApiTokenRecord:
    token_id: str
    digest_sha256: bytes
    role: ApiRole
    not_before: datetime
    expires_at: datetime | None = None
    revoked: bool = False

    def __post_init__(self) -> None:
        if not _TOKEN_ID_PATTERN.fullmatch(self.token_id):
            raise ValueError("token_id must be a bounded non-secret identifier")
        if len(self.digest_sha256) != 32:
            raise ValueError("token digest must contain exactly 32 bytes")
        not_before = _utc(self.not_before)
        object.__setattr__(self, "not_before", not_before)
        if self.expires_at is not None:
            expires_at = _utc(self.expires_at)
            if expires_at <= not_before:
                raise ValueError("token expiry must be after activation")
            object.__setattr__(self, "expires_at", expires_at)

    @classmethod
    def from_plaintext(
        cls,
        *,
        token_id: str,
        token: str,
        role: ApiRole | str,
        not_before: datetime,
        expires_at: datetime | None = None,
        revoked: bool = False,
    ) -> ApiTokenRecord:
        normalized = validate_api_token(token)
        return cls(
            token_id=token_id,
            digest_sha256=sha256(normalized.encode("utf-8")).digest(),
            role=role if isinstance(role, ApiRole) else ApiRole(role),
            not_before=not_before,
            expires_at=expires_at,
            revoked=revoked,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ApiTokenRecord:
        digest_hex = value.get("digest_sha256")
        if not isinstance(digest_hex, str) or not _HEX_DIGEST_PATTERN.fullmatch(digest_hex):
            raise ValueError("digest_sha256 must be a lowercase 64-character hex digest")
        token_id = value.get("token_id")
        role = value.get("role")
        not_before = value.get("not_before")
        expires_at = value.get("expires_at")
        revoked = value.get("revoked", False)
        if not isinstance(token_id, str):
            raise ValueError("token_id must be a string")
        if not isinstance(role, str):
            raise ValueError("token role must be a string")
        if not isinstance(not_before, str):
            raise ValueError("token not_before must be an ISO-8601 string")
        if expires_at is not None and not isinstance(expires_at, str):
            raise ValueError("token expires_at must be null or an ISO-8601 string")
        if not isinstance(revoked, bool):
            raise ValueError("token revoked must be a boolean")
        return cls(
            token_id=token_id,
            digest_sha256=bytes.fromhex(digest_hex),
            role=ApiRole(role),
            not_before=_parse_timestamp(not_before),
            expires_at=_parse_timestamp(expires_at) if expires_at is not None else None,
            revoked=revoked,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "token_id": self.token_id,
            "digest_sha256": self.digest_sha256.hex(),
            "role": self.role.value,
            "not_before": _timestamp(self.not_before),
            "expires_at": _timestamp(self.expires_at) if self.expires_at is not None else None,
            "revoked": self.revoked,
        }

    def active_at(self, now: datetime) -> bool:
        current = _utc(now)
        return (
            not self.revoked
            and current >= self.not_before
            and (self.expires_at is None or current < self.expires_at)
        )


@dataclass(frozen=True, slots=True)
class ApiTokenSnapshot:
    generation: int
    created_at: datetime
    records: tuple[ApiTokenRecord, ...]

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("token generation must be positive")
        object.__setattr__(self, "created_at", _utc(self.created_at))
        if not self.records:
            raise ValueError("token snapshot must contain at least one record")
        token_ids = [record.token_id for record in self.records]
        digests = [record.digest_sha256 for record in self.records]
        if len(token_ids) != len(set(token_ids)):
            raise ValueError("token IDs must be unique within a snapshot")
        if len(digests) != len(set(digests)):
            raise ValueError("token digests must be unique within a snapshot")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ApiTokenSnapshot:
        generation = value.get("generation")
        created_at = value.get("created_at")
        records = value.get("records")
        if not isinstance(generation, int) or isinstance(generation, bool):
            raise ValueError("snapshot generation must be an integer")
        if not isinstance(created_at, str):
            raise ValueError("snapshot created_at must be an ISO-8601 string")
        if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
            raise ValueError("snapshot records must be a list of objects")
        return cls(
            generation=generation,
            created_at=_parse_timestamp(created_at),
            records=tuple(ApiTokenRecord.from_mapping(item) for item in records),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "generation": self.generation,
            "created_at": _timestamp(self.created_at),
            "records": [record.to_mapping() for record in self.records],
        }

    def by_id(self) -> dict[str, ApiTokenRecord]:
        return {record.token_id: record for record in self.records}

    def with_revoked(self, token_id: str, *, generation: int, now: datetime) -> ApiTokenSnapshot:
        if token_id not in self.by_id():
            raise KeyError(token_id)
        records = tuple(
            ApiTokenRecord(
                token_id=record.token_id,
                digest_sha256=record.digest_sha256,
                role=record.role,
                not_before=record.not_before,
                expires_at=record.expires_at,
                revoked=record.revoked or record.token_id == token_id,
            )
            for record in self.records
        )
        return ApiTokenSnapshot(generation=generation, created_at=now, records=records)


@dataclass(frozen=True, slots=True)
class TokenSnapshotState:
    current: ApiTokenSnapshot
    previous: ApiTokenSnapshot | None = None
    previous_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if (self.previous is None) != (self.previous_expires_at is None):
            raise ValueError("previous snapshot and expiry must be configured together")
        if self.previous is not None:
            if self.previous.generation >= self.current.generation:
                raise ValueError("previous generation must be lower than current generation")
            assert self.previous_expires_at is not None
            object.__setattr__(self, "previous_expires_at", _utc(self.previous_expires_at))


class ReloadableApiTokenAuthenticator:
    """Thread-safe immutable token snapshots with bounded overlap and revocation."""

    def __init__(
        self,
        state: TokenSnapshotState,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._state = state
        self._clock = clock
        self._lock = RLock()

    @property
    def state(self) -> TokenSnapshotState:
        with self._lock:
            return self._state

    def authenticate(self, token: str, *, required_role: ApiRole) -> ApiPrincipal:
        normalized = validate_api_token(token)
        supplied = sha256(normalized.encode("utf-8")).digest()
        now = _utc(self._clock())
        with self._lock:
            state = self._state
        current_by_id = state.current.by_id()
        snapshots = [state.current]
        if (
            state.previous is not None
            and state.previous_expires_at is not None
            and now < state.previous_expires_at
        ):
            snapshots.append(state.previous)

        principal: ApiPrincipal | None = None
        for snapshot in snapshots:
            for record in snapshot.records:
                current_record = current_by_id.get(record.token_id)
                revoked_now = current_record is not None and current_record.revoked
                digest_matches = compare_digest(supplied, record.digest_sha256)
                if digest_matches and record.active_at(now) and not revoked_now:
                    principal = ApiPrincipal(
                        principal_id=record.token_id,
                        role=record.role,
                        token_id=record.token_id,
                        token_generation=snapshot.generation,
                    )

        if principal is None:
            raise ApiAuthenticationError()
        if not role_allows(principal.role, required_role):
            raise ApiAuthorizationError(
                principal_id=principal.principal_id,
                actual_role=principal.role.value,
                required_role=required_role.value,
            )
        return principal

    def reload(
        self,
        snapshot: ApiTokenSnapshot,
        *,
        overlap_seconds: int,
        now: datetime | None = None,
    ) -> TokenSnapshotState:
        if not 0 <= overlap_seconds <= 3600:
            raise ValueError("token overlap must be between 0 and 3600 seconds")
        current_time = _utc(now or self._clock())
        with self._lock:
            if snapshot.generation <= self._state.current.generation:
                raise ValueError("token generation must increase monotonically")
            previous = self._state.current if overlap_seconds else None
            previous_expires_at = (
                current_time + timedelta(seconds=overlap_seconds) if previous is not None else None
            )
            self._state = TokenSnapshotState(
                current=snapshot,
                previous=previous,
                previous_expires_at=previous_expires_at,
            )
            return self._state

    def replace_state(self, state: TokenSnapshotState) -> None:
        with self._lock:
            if state.current.generation < self._state.current.generation:
                raise ValueError("cannot replace token state with an older generation")
            self._state = state


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    remaining: float
    retry_after_seconds: int | None = None


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


class BoundedTokenBucketLimiter:
    """Deterministic bounded-memory token buckets with lazy refill and LRU eviction."""

    def __init__(
        self,
        *,
        capacity: int,
        refill_per_second: float,
        max_entries: int,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if capacity < 1:
            raise ValueError("rate capacity must be positive")
        if refill_per_second <= 0:
            raise ValueError("rate refill must be positive")
        if max_entries < 1:
            raise ValueError("rate limiter max_entries must be positive")
        self.capacity = float(capacity)
        self.refill_per_second = refill_per_second
        self.max_entries = max_entries
        self._clock = clock
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._lock = RLock()

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._buckets)

    def consume(self, key: str, *, amount: float = 1.0) -> RateLimitDecision:
        if not key or len(key) > 512:
            raise ValueError("rate-limit key must contain between 1 and 512 characters")
        if amount <= 0 or amount > self.capacity:
            raise ValueError("rate-limit amount must be positive and no larger than capacity")
        now = self._clock()
        with self._lock:
            bucket = self._buckets.pop(key, None)
            if bucket is None:
                if len(self._buckets) >= self.max_entries:
                    self._buckets.popitem(last=False)
                bucket = _Bucket(tokens=self.capacity, updated_at=now)
            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(
                self.capacity,
                bucket.tokens + elapsed * self.refill_per_second,
            )
            bucket.updated_at = now
            if bucket.tokens >= amount:
                bucket.tokens -= amount
                decision = RateLimitDecision(allowed=True, remaining=bucket.tokens)
            else:
                deficit = amount - bucket.tokens
                retry = max(1, int(deficit / self.refill_per_second + 0.999999))
                decision = RateLimitDecision(
                    allowed=False,
                    remaining=bucket.tokens,
                    retry_after_seconds=retry,
                )
            self._buckets[key] = bucket
            return decision


@dataclass(frozen=True, slots=True)
class ControlPlaneLimits:
    max_header_bytes: int = 32 * 1024
    max_body_bytes: int = 2 * 1024 * 1024
    max_sse_global: int = 32
    max_sse_per_principal: int = 4
    max_sse_per_peer: int = 8

    def __post_init__(self) -> None:
        for value in (
            self.max_header_bytes,
            self.max_body_bytes,
            self.max_sse_global,
            self.max_sse_per_principal,
            self.max_sse_per_peer,
        ):
            if value < 1:
                raise ValueError("control-plane limits must be positive")


class SseAdmissionLease(AbstractContextManager[None]):
    def __init__(self, limiter: SseAdmissionLimiter, principal_id: str, peer: str) -> None:
        self._limiter = limiter
        self._principal_id = principal_id
        self._peer = peer
        self._released = False

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.release()

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._limiter.release(self._principal_id, self._peer)


class SseAdmissionLimiter:
    def __init__(self, limits: ControlPlaneLimits) -> None:
        self._limits = limits
        self._global = 0
        self._principals: dict[str, int] = {}
        self._peers: dict[str, int] = {}
        self._lock = RLock()

    def acquire(self, principal_id: str, peer: str) -> SseAdmissionLease:
        with self._lock:
            principal_count = self._principals.get(principal_id, 0)
            peer_count = self._peers.get(peer, 0)
            if self._global >= self._limits.max_sse_global:
                raise RuntimeError("global SSE admission limit exceeded")
            if principal_count >= self._limits.max_sse_per_principal:
                raise RuntimeError("principal SSE admission limit exceeded")
            if peer_count >= self._limits.max_sse_per_peer:
                raise RuntimeError("peer SSE admission limit exceeded")
            self._global += 1
            self._principals[principal_id] = principal_count + 1
            self._peers[peer] = peer_count + 1
        return SseAdmissionLease(self, principal_id, peer)

    def release(self, principal_id: str, peer: str) -> None:
        with self._lock:
            if self._global <= 0:
                raise RuntimeError("SSE admission state underflow")
            principal_count = self._principals.get(principal_id, 0)
            peer_count = self._peers.get(peer, 0)
            if principal_count <= 0 or peer_count <= 0:
                raise RuntimeError("SSE admission key underflow")
            self._global -= 1
            if principal_count == 1:
                self._principals.pop(principal_id)
            else:
                self._principals[principal_id] = principal_count - 1
            if peer_count == 1:
                self._peers.pop(peer)
            else:
                self._peers[peer] = peer_count - 1

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None
