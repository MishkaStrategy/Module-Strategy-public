from __future__ import annotations

import re
from dataclasses import dataclass

from .contracts import AcceptanceErrorCode, AcceptanceOwnerError
from .owner import PhaseExecutionContext

_GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class TrustedAcceptanceIdentity:
    """Server-owned immutable identity expected by production phase adapters."""

    full_rc_sha: str
    dataset_id: str
    dataset_digest: str
    strategy_id: str
    strategy_digest: str
    config_digest: str

    def __post_init__(self) -> None:
        _match(self.full_rc_sha, _GIT_SHA, "full_rc_sha")
        _match(self.dataset_id, _IDENTIFIER, "dataset_id")
        _match(self.dataset_digest, _SHA256, "dataset_digest")
        _match(self.strategy_id, _IDENTIFIER, "strategy_id")
        _match(self.strategy_digest, _SHA256, "strategy_digest")
        _match(self.config_digest, _SHA256, "config_digest")

    def require_context(self, context: PhaseExecutionContext) -> None:
        observed = (
            context.full_rc_sha,
            context.dataset_id,
            context.dataset_digest,
            context.strategy_id,
            context.strategy_digest,
            context.config_digest,
        )
        expected = (
            self.full_rc_sha,
            self.dataset_id,
            self.dataset_digest,
            self.strategy_id,
            self.strategy_digest,
            self.config_digest,
        )
        if observed != expected:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_BLOCKED,
                "trusted acceptance candidate identity does not match the durable run",
            )


def require_sha256(value: object, name: str) -> str:
    return _match(value, _SHA256, name)


def require_identifier(value: object, name: str) -> str:
    return _match(value, _IDENTIFIER, name)


def _match(value: object, pattern: re.Pattern[str], name: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.INVALID_REQUEST,
            f"trusted {name} is invalid",
        )
    return value


__all__ = ["TrustedAcceptanceIdentity", "require_identifier", "require_sha256"]
