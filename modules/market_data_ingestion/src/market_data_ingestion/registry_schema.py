from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from .errors import RegistryError
from .registry import SQLiteDatasetRegistry

REGISTRY_SCHEMA_VERSION = 1
_DATASET_COLUMNS = (
    "dataset_id",
    "version",
    "dataset_hash",
    "descriptor_json",
    "registered_at",
)
_REQUEST_COLUMNS = (
    "request_id",
    "request_hash",
    "descriptor_json",
    "updated_at",
)


class VersionedSQLiteDatasetRegistry(SQLiteDatasetRegistry):
    """SQLite registry entrypoint with an explicit, fail-closed schema contract."""

    schema_version = REGISTRY_SCHEMA_VERSION

    def __init__(self, path: Path) -> None:
        candidate = path.expanduser().absolute()
        self._reject_symlink_path(candidate)
        resolved = candidate.resolve()
        self._preflight_existing_database(resolved)
        super().__init__(resolved)
        self._adopt_or_validate_schema_version()

    @staticmethod
    def _reject_symlink_path(path: Path) -> None:
        cursor = Path(path.anchor)
        for segment in path.parts[1:]:
            cursor /= segment
            if cursor.exists() and cursor.is_symlink():
                raise RegistryError(
                    "REGISTRY_PATH_INVALID",
                    "registry path must not contain symbolic links",
                    False,
                    "initializing_registry",
                    {"path": str(cursor)},
                )

    @classmethod
    def _preflight_existing_database(cls, path: Path) -> None:
        if not path.exists():
            return
        if path.is_symlink() or not path.is_file():
            raise RegistryError(
                "REGISTRY_PATH_INVALID",
                "registry path must be a regular non-symlink file",
                False,
                "initializing_registry",
            )
        try:
            with closing(sqlite3.connect(path, timeout=10)) as connection:
                version = cls._user_version(connection)
                cls._reject_unsupported_version(version)
                cls._validate_known_tables(connection, allow_absent=True)
        except RegistryError:
            raise
        except sqlite3.Error as exc:
            raise RegistryError(
                "REGISTRY_SCHEMA_IO",
                "registry schema could not be inspected",
                True,
                "initializing_registry",
            ) from exc

    def _adopt_or_validate_schema_version(self) -> None:
        try:
            with closing(sqlite3.connect(self.path, timeout=10)) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                version = self._user_version(connection)
                self._reject_unsupported_version(version)
                self._validate_known_tables(connection, allow_absent=False)
                if version == 0:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(f"PRAGMA user_version={REGISTRY_SCHEMA_VERSION}")
                    connection.commit()
                final_version = self._user_version(connection)
                if final_version != REGISTRY_SCHEMA_VERSION:
                    raise RegistryError(
                        "REGISTRY_SCHEMA_VERSION_INVALID",
                        "registry schema version was not committed",
                        False,
                        "initializing_registry",
                        {
                            "expected": REGISTRY_SCHEMA_VERSION,
                            "actual": final_version,
                        },
                    )
        except RegistryError:
            raise
        except sqlite3.Error as exc:
            raise RegistryError(
                "REGISTRY_SCHEMA_IO",
                "registry schema version could not be committed",
                True,
                "initializing_registry",
            ) from exc

    @staticmethod
    def _user_version(connection: sqlite3.Connection) -> int:
        row = connection.execute("PRAGMA user_version").fetchone()
        if row is None:
            raise RegistryError(
                "REGISTRY_SCHEMA_INVALID",
                "registry schema version is unavailable",
                False,
                "initializing_registry",
            )
        return int(row[0])

    @staticmethod
    def _reject_unsupported_version(version: int) -> None:
        if version > REGISTRY_SCHEMA_VERSION:
            raise RegistryError(
                "REGISTRY_SCHEMA_DOWNGRADE_REJECTED",
                "registry was created by a newer schema version",
                False,
                "initializing_registry",
                {
                    "supported": REGISTRY_SCHEMA_VERSION,
                    "actual": version,
                },
            )
        if version not in {0, REGISTRY_SCHEMA_VERSION}:
            raise RegistryError(
                "REGISTRY_SCHEMA_VERSION_UNSUPPORTED",
                "registry schema version is unsupported",
                False,
                "initializing_registry",
                {
                    "supported": REGISTRY_SCHEMA_VERSION,
                    "actual": version,
                },
            )

    @classmethod
    def _validate_known_tables(
        cls,
        connection: sqlite3.Connection,
        *,
        allow_absent: bool,
    ) -> None:
        cls._validate_columns(
            connection,
            table="datasets",
            expected=_DATASET_COLUMNS,
            allow_absent=allow_absent,
        )
        cls._validate_columns(
            connection,
            table="ingestion_requests",
            expected=_REQUEST_COLUMNS,
            allow_absent=allow_absent,
        )
        dataset_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='datasets'"
        ).fetchone()
        if dataset_exists is None:
            return
        index_rows = connection.execute("PRAGMA index_list(datasets)").fetchall()
        index_names = {str(row[1]) for row in index_rows}
        if "idx_datasets_hash" not in index_names and not allow_absent:
            raise RegistryError(
                "REGISTRY_SCHEMA_INVALID",
                "registry dataset hash index is missing",
                False,
                "initializing_registry",
            )
        if "idx_datasets_hash" in index_names:
            columns = tuple(
                str(row[2])
                for row in connection.execute("PRAGMA index_info(idx_datasets_hash)").fetchall()
            )
            if columns != ("dataset_hash",):
                raise RegistryError(
                    "REGISTRY_SCHEMA_INVALID",
                    "registry dataset hash index has unexpected columns",
                    False,
                    "initializing_registry",
                    {"actual": columns},
                )

    @staticmethod
    def _validate_columns(
        connection: sqlite3.Connection,
        *,
        table: str,
        expected: tuple[str, ...],
        allow_absent: bool,
    ) -> None:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        if not rows:
            if allow_absent:
                return
            raise RegistryError(
                "REGISTRY_SCHEMA_INVALID",
                f"registry table is missing: {table}",
                False,
                "initializing_registry",
            )
        actual = tuple(str(row[1]) for row in rows)
        if actual != expected:
            raise RegistryError(
                "REGISTRY_SCHEMA_INVALID",
                f"registry table has unexpected columns: {table}",
                False,
                "initializing_registry",
                {"expected": expected, "actual": actual},
            )


def open_versioned_registry(path: Path) -> VersionedSQLiteDatasetRegistry:
    """Open the versioned registry contract used by Wave 2 control-plane consumers."""
    return VersionedSQLiteDatasetRegistry(path)
