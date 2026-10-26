from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from hmac import compare_digest

from .errors import ApiAuthenticationError, ApiAuthorizationError


class ApiRole(StrEnum):
    READER = "reader"
    OPERATOR = "operator"
    ADMIN = "admin"


_ROLE_RANK = {
    ApiRole.READER: 1,
    ApiRole.OPERATOR: 2,
    ApiRole.ADMIN: 3,
}


@dataclass(frozen=True, slots=True)
class ApiPrincipal:
    principal_id: str
    role: ApiRole
    token_id: str | None = None
    token_generation: int | None = None


def role_allows(actual: ApiRole, required: ApiRole) -> bool:
    return _ROLE_RANK[actual] >= _ROLE_RANK[required]


def validate_api_token(token: str) -> str:
    if not isinstance(token, str):
        raise TypeError("API token must be a string")
    normalized = token.strip()
    if not 32 <= len(normalized) <= 512:
        raise ValueError("API token must contain between 32 and 512 characters")
    if any(ord(character) < 33 or ord(character) == 127 for character in normalized):
        raise ValueError("API token must not contain whitespace or control characters")
    return normalized


class ApiTokenAuthenticator:
    """Constant-time bearer-token authentication without retaining plaintext tokens."""

    def __init__(self, tokens: Mapping[str, ApiRole | str]) -> None:
        entries: list[tuple[bytes, ApiPrincipal]] = []
        for token, configured_role in tokens.items():
            normalized = validate_api_token(token)
            role = (
                configured_role
                if isinstance(configured_role, ApiRole)
                else ApiRole(configured_role)
            )
            digest = sha256(normalized.encode("utf-8")).digest()
            token_id = f"token_{digest.hex()[:16]}"
            principal = ApiPrincipal(
                principal_id=token_id,
                role=role,
                token_id=token_id,
                token_generation=1,
            )
            entries.append((digest, principal))
        if not entries:
            raise ValueError("at least one API token must be configured")
        digests = [digest for digest, _principal in entries]
        if len(set(digests)) != len(digests):
            raise ValueError("API tokens must be unique")
        self._entries = tuple(entries)

    def authenticate(self, token: str, *, required_role: ApiRole) -> ApiPrincipal:
        normalized = validate_api_token(token)
        supplied = sha256(normalized.encode("utf-8")).digest()
        principal: ApiPrincipal | None = None
        for digest, candidate in self._entries:
            if compare_digest(supplied, digest):
                principal = candidate
        if principal is None:
            raise ApiAuthenticationError()
        if not role_allows(principal.role, required_role):
            raise ApiAuthorizationError(
                principal_id=principal.principal_id,
                actual_role=principal.role.value,
                required_role=required_role.value,
            )
        return principal
