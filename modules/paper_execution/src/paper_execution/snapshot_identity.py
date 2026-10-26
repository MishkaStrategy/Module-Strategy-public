from __future__ import annotations

import hmac

from ._utils import sha256_text
from .enums import IntentRole

_SNAPSHOT_TAG_PREFIX = "pevr1"
_SCOPE_DIGEST_CHARS = 24


def build_snapshot_tag(
    *,
    portfolio_id: str,
    instrument: str,
    role: IntentRole,
) -> str:
    if not isinstance(portfolio_id, str) or not portfolio_id.strip():
        raise ValueError("portfolio_id must be non-blank")
    if not isinstance(instrument, str) or not instrument.strip():
        raise ValueError("instrument must be non-blank")
    normalized_role = role if isinstance(role, IntentRole) else IntentRole(role)
    if normalized_role is IntentRole.CANCEL:
        raise ValueError("cancel intents do not produce venue snapshots")
    digest = sha256_text(f"{portfolio_id}\x00{instrument}")[:_SCOPE_DIGEST_CHARS]
    return f"{_SNAPSHOT_TAG_PREFIX}:{normalized_role.value}:{digest}"


def parse_snapshot_tag(
    tag: object,
    *,
    portfolio_id: str,
    instrument: str,
) -> IntentRole | None:
    if not isinstance(tag, str):
        return None
    parts = tag.split(":")
    if len(parts) != 3 or parts[0] != _SNAPSHOT_TAG_PREFIX:
        return None
    try:
        role = IntentRole(parts[1])
        expected = build_snapshot_tag(
            portfolio_id=portfolio_id,
            instrument=instrument,
            role=role,
        )
    except (TypeError, ValueError):
        return None
    if not hmac.compare_digest(tag, expected):
        return None
    return role
