from __future__ import annotations

import os
import secrets
import shutil
import socket
from collections.abc import Mapping
from pathlib import Path

from .adapters.base import ExchangeAdapter
from .registry import SQLiteDatasetRegistry

_MIN_FREE_BYTES = 1_000_000_000


def _write_probe(storage_root: Path) -> bool:
    probe = storage_root / f".doctor-write-probe-{secrets.token_hex(8)}"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(probe, flags, 0o600)
        os.write(file_descriptor, b"ok")
        os.fsync(file_descriptor)
        return True
    except OSError:
        return False
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        try:
            if probe.exists() and not probe.is_symlink():
                probe.unlink()
        except OSError:
            pass


def doctor(
    *,
    storage_root: Path,
    registry: SQLiteDatasetRegistry,
    adapters: Mapping[str, ExchangeAdapter],
) -> dict[str, object]:
    storage_root.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(storage_root)
    writable = _write_probe(storage_root)
    dns = True
    try:
        socket.getaddrinfo("data.binance.vision", 443)
    except OSError:
        dns = False
    adapter_health = {
        name: {
            "version": adapter.adapter_version,
            "maturity": adapter.get_capabilities().maturity.value,
            "markets": [item.value for item in adapter.get_capabilities().markets],
        }
        for name, adapter in adapters.items()
    }
    registry_health = registry.health()
    healthy = writable and usage.free > _MIN_FREE_BYTES and registry_health.get("status") == "ok"
    return {
        "status": "ok" if healthy else "error",
        "storage": {
            "root": str(storage_root.resolve()),
            "writable": writable,
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "minimum_free_bytes": _MIN_FREE_BYTES,
        },
        "network": {"dns_data_binance_vision": dns},
        "registry": registry_health,
        "adapters": adapter_health,
    }
