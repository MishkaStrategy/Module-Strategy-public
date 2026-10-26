from __future__ import annotations

import json

from .contracts import (
    AcceptanceMutation,
    AcceptanceMutationRequest,
    AcceptanceRunSpec,
)
from .http import SystemAcceptanceHttpServer

_START_SPEC_FIELDS = frozenset(
    {
        "dataset_id",
        "dataset_digest",
        "strategy_id",
        "strategy_digest",
        "config_digest",
        "enable_soak_24h",
        "enable_soak_72h",
    }
)


class PinnedSystemAcceptanceHttpServer(SystemAcceptanceHttpServer):
    """Canonical Control Plane transport pinned to a trusted full-RC candidate."""

    def __init__(self, *args: object, full_rc_sha: str, **kwargs: object) -> None:
        _git_sha(full_rc_sha)
        self._full_rc_sha = full_rc_sha
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def _request(self, mutation: AcceptanceMutation, body: bytes) -> AcceptanceMutationRequest:
        if mutation is not AcceptanceMutation.START:
            return super()._request(mutation, body)  # noqa: SLF001 - intentional owner subclass
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("request body must be object")
        if set(payload) != {"request_id", "spec"} or not isinstance(payload["spec"], dict):
            raise ValueError("START body is invalid")
        request_id = payload["request_id"]
        spec_raw = payload["spec"]
        if not isinstance(request_id, str):
            raise ValueError("request_id must be string")
        if set(spec_raw) - _START_SPEC_FIELDS:
            raise ValueError("START spec has unknown fields")
        run_spec = AcceptanceRunSpec(
            full_rc_sha=self._full_rc_sha,
            dataset_id=_string(spec_raw, "dataset_id"),
            dataset_digest=_string(spec_raw, "dataset_digest"),
            strategy_id=_string(spec_raw, "strategy_id"),
            strategy_digest=_string(spec_raw, "strategy_digest"),
            config_digest=_string(spec_raw, "config_digest"),
            enable_soak_24h=_boolean(spec_raw.get("enable_soak_24h", False)),
            enable_soak_72h=_boolean(spec_raw.get("enable_soak_72h", False)),
            mode="PAPER",
        )
        return AcceptanceMutationRequest(
            request_id=request_id,
            operator=self._operator_subject,  # noqa: SLF001 - canonical trusted owner identity
            mutation=mutation,
            spec=run_spec,
        )


def _git_sha(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("full_rc_sha must be a lowercase 40-character Git SHA")


def _string(mapping: dict[str, object], key: str) -> str:
    value = mapping[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be string")
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("value must be boolean")
    return value
