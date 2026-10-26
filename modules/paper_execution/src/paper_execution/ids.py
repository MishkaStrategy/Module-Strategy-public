from __future__ import annotations

import re

from ._utils import sha256_text

_SAFE = re.compile(r"[^A-Za-z0-9_-]+")
_NAMESPACE = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_FACTORY_NAMESPACE_LENGTH = 64
MAX_FACTORY_INPUT_LENGTH = 4_096
MAX_FACTORY_OUTPUT_LENGTH = 128
MIN_DIGEST_LENGTH = 12


class ClientOrderIdFactory:
    def __init__(
        self,
        namespace: str = "pevr",
        max_length: int = 36,
    ) -> None:
        if not isinstance(max_length, int) or isinstance(max_length, bool):
            raise TypeError("max_length must be int")
        if not 20 <= max_length <= MAX_FACTORY_OUTPUT_LENGTH:
            raise ValueError("max_length must be in [20, 128]")
        if not isinstance(namespace, str):
            raise TypeError("namespace must be a string")
        if (
            not namespace
            or len(namespace) > MAX_FACTORY_NAMESPACE_LENGTH
            or _NAMESPACE.fullmatch(namespace) is None
        ):
            raise ValueError("namespace must contain only bounded safe characters")
        self.namespace = namespace
        self.max_length = max_length

    def create(
        self,
        *,
        plan_id: str,
        intent_id: str,
        role: str,
        revision: int = 0,
    ) -> str:
        for value, name in (
            (plan_id, "plan_id"),
            (intent_id, "intent_id"),
            (role, "role"),
        ):
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a string")
            if not value or len(value) > MAX_FACTORY_INPUT_LENGTH:
                raise ValueError(f"{name} has invalid length")
        if not isinstance(revision, int) or isinstance(revision, bool):
            raise TypeError("revision must be int")
        if revision < 0:
            raise ValueError("revision cannot be negative")

        role_token = _SAFE.sub("-", role).strip("-")[:5] or "order"
        digest = sha256_text(f"{self.namespace}|{plan_id}|{intent_id}|{role}|{revision}")
        prefix = f"{self.namespace}-{role_token}-{revision}-"
        available = self.max_length - len(prefix)
        if available < MIN_DIGEST_LENGTH:
            prefix = f"{self.namespace[:4]}-{revision}-"
            available = self.max_length - len(prefix)
        if available < MIN_DIGEST_LENGTH:
            raise ValueError("revision leaves insufficient room for collision-resistant ID")
        result = f"{prefix}{digest[:available]}"
        if len(result) > self.max_length:
            raise RuntimeError("client order ID exceeded configured limit")
        return result
