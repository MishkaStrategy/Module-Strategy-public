from __future__ import annotations

from .errors import RegistryError
from .storage_common import _SCHEMA_VERSION
from .storage_mixin_base import StorageMixinBase
from .storage_scalars import strict_schema_version_text, strict_sqlite_integer
from .storage_schema_sql import _SCHEMA_SQL


class StorageSchemaMixin(StorageMixinBase):
    def _initialize(self) -> None:
        self._preflight_existing_schema()
        schema_sql = _SCHEMA_SQL
        with self.transaction() as connection:
            self._execute_schema_script(connection, schema_sql)
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                    (str(_SCHEMA_VERSION),),
                )
                self._record_migration(connection, 0, _SCHEMA_VERSION, "bootstrap-v4")
            else:
                current = strict_schema_version_text(row[0])
                if current == 1:
                    self._record_migration(connection, 1, 2, "v1-to-v2-annotations")
                    connection.execute(
                        "UPDATE schema_meta SET value='2' WHERE key='schema_version'"
                    )
                    current = 2
                if current == 2:
                    metric_row = connection.execute("SELECT COUNT(*) FROM metrics").fetchone()
                    if metric_row is None:
                        raise RegistryError("legacy metrics table count is missing")
                    metric_count = strict_sqlite_integer(
                        metric_row[0], "legacy metrics count", minimum=0
                    )
                    if metric_count:
                        raise RegistryError(
                            "registry schema v2 contains metrics without immutable metric definitions; "
                            "automatic inference is forbidden"
                        )
                    columns = {
                        str(item[1])
                        for item in connection.execute("PRAGMA table_info(metrics)").fetchall()
                    }
                    if "definition_id" not in columns:
                        connection.execute(
                            "ALTER TABLE metrics ADD COLUMN definition_id TEXT "
                            "REFERENCES metric_definitions(definition_id)"
                        )
                    self._record_migration(connection, 2, 3, "v2-to-v3-metric-definitions")
                    connection.execute(
                        "UPDATE schema_meta SET value='3' WHERE key='schema_version'"
                    )
                    current = 3
                if current == 3:
                    connection.execute("DROP TRIGGER IF EXISTS prevent_annotation_update")
                    connection.execute("DROP TRIGGER IF EXISTS prevent_annotation_delete")
                    connection.execute(
                        """
                        CREATE TABLE registry_annotations_v4 (
                            annotation_id TEXT PRIMARY KEY,
                            entity_type TEXT NOT NULL,
                            entity_id TEXT NOT NULL,
                            actor TEXT NOT NULL,
                            reason TEXT NOT NULL,
                            previous_revision INTEGER NOT NULL,
                            new_revision INTEGER NOT NULL,
                            entity_hash TEXT NOT NULL,
                            payload_hash TEXT NOT NULL,
                            payload_json BLOB NOT NULL,
                            created_at_utc TEXT NOT NULL,
                            UNIQUE(entity_type, entity_id, actor, reason, payload_hash)
                        )
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO registry_annotations_v4(
                            annotation_id, entity_type, entity_id, actor, reason,
                            previous_revision, new_revision, entity_hash, payload_hash,
                            payload_json, created_at_utc
                        )
                        SELECT
                            annotation_id, entity_type, entity_id, actor, reason,
                            previous_revision, new_revision, entity_hash, payload_hash,
                            payload_json, created_at_utc
                        FROM registry_annotations
                        """
                    )
                    connection.execute("DROP TABLE registry_annotations")
                    connection.execute(
                        "ALTER TABLE registry_annotations_v4 RENAME TO registry_annotations"
                    )
                    connection.execute(
                        """
                        CREATE TRIGGER prevent_annotation_update
                        BEFORE UPDATE ON registry_annotations
                        BEGIN SELECT RAISE(ABORT, 'registry_annotations is append-only'); END
                        """
                    )
                    connection.execute(
                        """
                        CREATE TRIGGER prevent_annotation_delete
                        BEFORE DELETE ON registry_annotations
                        BEGIN SELECT RAISE(ABORT, 'registry_annotations is append-only'); END
                        """
                    )
                    self._record_migration(connection, 3, 4, "v3-to-v4-actor-scoped-annotations")
                    connection.execute(
                        "UPDATE schema_meta SET value='4' WHERE key='schema_version'"
                    )
                    current = 4
                if current != _SCHEMA_VERSION:
                    raise RegistryError(
                        f"unsupported registry schema version {row[0]!r}; expected {_SCHEMA_VERSION}"
                    )
