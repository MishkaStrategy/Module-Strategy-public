from __future__ import annotations

import importlib
import queue
import threading
from pathlib import Path

import pytest

import modular_results_registry as package
from modular_results_registry.errors import (
    IntegrityError,
    RegistryError,
    UnsupportedPlatformError,
)
from modular_results_registry.runtime_capabilities import (
    _replace_supports_directory_descriptors,
    missing_posix_capabilities,
    require_posix_storage_capabilities,
)
from modular_results_registry.storage import SQLiteRegistry
from modular_results_registry.storage_scalars import (
    strict_schema_version_text,
    strict_sqlite_integer,
)


def test_registry_instance_rejects_cross_thread_reuse(tmp_path: Path) -> None:
    registry = SQLiteRegistry(tmp_path / "registry.sqlite3")
    outcomes: queue.Queue[BaseException | None] = queue.Queue()

    def worker() -> None:
        try:
            registry.revision()
        except BaseException as exc:  # captured for assertion in owner thread
            outcomes.put(exc)
        else:
            outcomes.put(None)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    error = outcomes.get_nowait()
    assert isinstance(error, RegistryError)
    assert "single-thread owned" in str(error)
    registry.close()


def test_registry_instances_have_independent_deadlines_per_worker(
    tmp_path: Path,
) -> None:
    path = tmp_path / "shared.sqlite3"
    with SQLiteRegistry(path):
        pass

    barrier = threading.Barrier(2)
    outcomes: queue.Queue[BaseException | int] = queue.Queue()

    def worker() -> None:
        try:
            with SQLiteRegistry(path) as registry:
                with registry.read_deadline(2.0):
                    barrier.wait(timeout=2)
                    outcomes.put(registry.revision())
        except BaseException as exc:
            outcomes.put(exc)

    threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    values = [outcomes.get_nowait(), outcomes.get_nowait()]
    assert values == [0, 0]


def test_replace_capability_uses_real_keyword_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _replace_supports_directory_descriptors()
    assert "os.replace(src_dir_fd=..., dst_dir_fd=...)" not in missing_posix_capabilities()

    monkeypatch.setattr(
        "modular_results_registry.runtime_capabilities.os.replace",
        lambda source, destination: None,
    )
    assert not _replace_supports_directory_descriptors()
    assert "os.replace(src_dir_fd=..., dst_dir_fd=...)" in missing_posix_capabilities()


def test_unsupported_platform_is_explicit_and_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "modular_results_registry.runtime_capabilities.missing_posix_capabilities",
        lambda: ("os.open(dir_fd=...)", "os.O_NOFOLLOW"),
    )

    with pytest.raises(UnsupportedPlatformError, match="requires audited POSIX"):
        require_posix_storage_capabilities()


def test_package_reload_remains_fail_closed_on_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as scoped:
        scoped.setattr(
            "modular_results_registry.runtime_capabilities.missing_posix_capabilities",
            lambda: ("os.open(dir_fd=...)",),
        )
        with pytest.raises(UnsupportedPlatformError, match="requires audited POSIX"):
            importlib.reload(package)

    assert importlib.reload(package).__version__ == "0.1.0.dev0"


def test_scalar_decoders_reject_bool_float_bytes_and_noncanonical_text() -> None:
    for value in (True, 1.0, "1", b"1"):
        with pytest.raises(IntegrityError, match="SQLite INTEGER"):
            strict_sqlite_integer(value, "revision", minimum=0)
    for value in (4, True, "04", "4.0", b"4", "-1"):
        with pytest.raises(RegistryError, match="canonical decimal TEXT"):
            strict_schema_version_text(value)
    assert strict_sqlite_integer(0, "revision", minimum=0) == 0
    assert strict_schema_version_text("4") == 4


def test_corrupt_database_scalar_types_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.sqlite3"
    with SQLiteRegistry(path) as registry:
        registry._connection.execute("UPDATE schema_meta SET value='04' WHERE key='schema_version'")
        with pytest.raises(RegistryError, match="canonical decimal TEXT"):
            registry.schema_version()
        registry._connection.execute("UPDATE schema_meta SET value='4' WHERE key='schema_version'")

        registry._connection.execute(
            "UPDATE registry_state SET revision=CAST(1.5 AS REAL) WHERE singleton=1"
        )
        with pytest.raises(IntegrityError, match="SQLite INTEGER"):
            registry.revision()
        registry._connection.execute("UPDATE registry_state SET revision=0 WHERE singleton=1")

        registry._connection.execute("DROP TRIGGER prevent_schema_migrations_update")
        registry._connection.execute(
            "UPDATE schema_migrations SET source_version=CAST(0.5 AS REAL) "
            "WHERE rowid=(SELECT MIN(rowid) FROM schema_migrations)"
        )
        with pytest.raises(IntegrityError, match="SQLite INTEGER"):
            registry.migration_history()
