from __future__ import annotations

import json
from pathlib import Path

import pytest

from market_data_application.control_plane_runtime import control_plane_from_environment
from market_data_application.control_plane_store import ControlPlaneStateError
from market_data_application.security import ApiRole
from market_data_application.settings import ApplicationSettings

ADMIN_TOKEN = "runtime-admin-token-000000000000000000000000"
AUDIT_KEY = b"runtime-control-plane-audit-key-at-least-32-bytes"


def _settings(tmp_path: Path) -> ApplicationSettings:
    return ApplicationSettings(storage_root=tmp_path)


def _bootstrap_json(*, overlap_seconds: int = 0) -> str:
    return json.dumps(
        {
            "generation": 1,
            "overlap_seconds": overlap_seconds,
            "tokens": [
                {
                    "token_id": "runtime-admin",
                    "token": ADMIN_TOKEN,
                    "role": "admin",
                }
            ],
        }
    )


def test_runtime_is_disabled_without_audit_key(tmp_path: Path) -> None:
    assert control_plane_from_environment(_settings(tmp_path), {}) is None
    with pytest.raises(ValueError, match="AUDIT_KEY_HEX"):
        control_plane_from_environment(
            _settings(tmp_path),
            {"MDI_CONTROL_PLANE_BOOTSTRAP_JSON": _bootstrap_json()},
        )


def test_runtime_bootstraps_persists_digest_and_restarts_without_plaintext(
    tmp_path: Path,
) -> None:
    environment = {
        "MDI_CONTROL_PLANE_AUDIT_KEY_HEX": AUDIT_KEY.hex(),
        "MDI_CONTROL_PLANE_BOOTSTRAP_JSON": _bootstrap_json(),
    }
    controller = control_plane_from_environment(_settings(tmp_path), environment)
    assert controller is not None
    principal = controller.authenticator.authenticate(
        ADMIN_TOKEN,
        required_role=ApiRole.ADMIN,
    )
    assert principal.token_id == "runtime-admin"
    assert controller.state.current.generation == 1

    persisted = b"".join(
        candidate.read_bytes() for candidate in sorted(tmp_path.iterdir()) if candidate.is_file()
    )
    assert ADMIN_TOKEN.encode() not in persisted

    restarted = control_plane_from_environment(
        _settings(tmp_path),
        {"MDI_CONTROL_PLANE_AUDIT_KEY_HEX": AUDIT_KEY.hex()},
    )
    assert restarted is not None
    assert restarted.state == controller.state
    assert (
        restarted.authenticator.authenticate(
            ADMIN_TOKEN,
            required_role=ApiRole.ADMIN,
        ).token_generation
        == 1
    )


def test_runtime_requires_bootstrap_for_uninitialized_store(tmp_path: Path) -> None:
    with pytest.raises(ControlPlaneStateError, match="not initialized"):
        control_plane_from_environment(
            _settings(tmp_path),
            {"MDI_CONTROL_PLANE_AUDIT_KEY_HEX": AUDIT_KEY.hex()},
        )


def test_runtime_rejects_invalid_secret_filename_bootstrap_and_limits(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with pytest.raises(ValueError, match="even number"):
        control_plane_from_environment(
            settings,
            {"MDI_CONTROL_PLANE_AUDIT_KEY_HEX": "abc"},
        )
    with pytest.raises(ValueError, match="hexadecimal"):
        control_plane_from_environment(
            settings,
            {"MDI_CONTROL_PLANE_AUDIT_KEY_HEX": "zz" * 32},
        )
    with pytest.raises(ValueError, match="at least 32"):
        control_plane_from_environment(
            settings,
            {"MDI_CONTROL_PLANE_AUDIT_KEY_HEX": b"short".hex()},
        )
    with pytest.raises(ValueError, match="safe local"):
        control_plane_from_environment(
            settings,
            {
                "MDI_CONTROL_PLANE_AUDIT_KEY_HEX": AUDIT_KEY.hex(),
                "MDI_CONTROL_PLANE_FILENAME": "../unsafe.sqlite3",
            },
        )
    with pytest.raises(ValueError, match="valid JSON"):
        control_plane_from_environment(
            settings,
            {
                "MDI_CONTROL_PLANE_AUDIT_KEY_HEX": AUDIT_KEY.hex(),
                "MDI_CONTROL_PLANE_BOOTSTRAP_JSON": "{",
            },
        )
    with pytest.raises(ValueError, match="overlap must be zero"):
        control_plane_from_environment(
            settings,
            {
                "MDI_CONTROL_PLANE_AUDIT_KEY_HEX": AUDIT_KEY.hex(),
                "MDI_CONTROL_PLANE_BOOTSTRAP_JSON": _bootstrap_json(overlap_seconds=1),
            },
        )
    with pytest.raises(ValueError, match="MAX_HEADER_BYTES"):
        control_plane_from_environment(
            settings,
            {
                "MDI_CONTROL_PLANE_AUDIT_KEY_HEX": AUDIT_KEY.hex(),
                "MDI_CONTROL_PLANE_BOOTSTRAP_JSON": _bootstrap_json(),
                "MDI_CONTROL_PLANE_MAX_HEADER_BYTES": "100",
            },
        )


def test_runtime_rejects_non_numeric_rate_configuration(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="PEER_RATE_REFILL"):
        control_plane_from_environment(
            _settings(tmp_path),
            {
                "MDI_CONTROL_PLANE_AUDIT_KEY_HEX": AUDIT_KEY.hex(),
                "MDI_CONTROL_PLANE_BOOTSTRAP_JSON": _bootstrap_json(),
                "MDI_CONTROL_PLANE_PEER_RATE_REFILL": "not-a-number",
            },
        )
