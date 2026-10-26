"""SQLite table and index DDL for the immutable results registry."""

_SCHEMA_TABLES_SQL = r"""
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS registry_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                revision INTEGER NOT NULL CHECK(revision >= 0)
            );
            INSERT OR IGNORE INTO registry_state(singleton, revision) VALUES(1, 0);

            CREATE TABLE IF NOT EXISTS packages (
                package_id TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                payload_json BLOB NOT NULL,
                registered_revision INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                package_id TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                payload_json BLOB NOT NULL,
                status TEXT NOT NULL,
                registered_revision INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_artifacts_package ON artifacts(package_id);
            CREATE INDEX IF NOT EXISTS ix_artifacts_sha256 ON artifacts(sha256);

            CREATE TABLE IF NOT EXISTS datasets (
                dataset_id TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                payload_json BLOB NOT NULL,
                registered_revision INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS configurations (
                configuration_id TEXT PRIMARY KEY,
                strategy_id TEXT NOT NULL,
                configuration_hash TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                payload_json BLOB NOT NULL,
                registered_revision INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS experiments (
                experiment_id TEXT PRIMARY KEY,
                research_id TEXT NOT NULL,
                parent_experiment_id TEXT REFERENCES experiments(experiment_id) DEFERRABLE INITIALLY DEFERRED,
                strategy_id TEXT NOT NULL,
                status TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                payload_json BLOB NOT NULL,
                registered_revision INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
                configuration_id TEXT NOT NULL REFERENCES configurations(configuration_id),
                status TEXT NOT NULL,
                result_hash TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                payload_json BLOB NOT NULL,
                registered_revision INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_datasets (
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
                dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id) ON DELETE RESTRICT,
                PRIMARY KEY(run_id, dataset_id)
            );
            CREATE TABLE IF NOT EXISTS run_artifacts (
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
                artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
                PRIMARY KEY(run_id, artifact_id)
            );
            CREATE TABLE IF NOT EXISTS metric_definitions (
                definition_id TEXT PRIMARY KEY,
                metric_name TEXT NOT NULL,
                definition_version TEXT NOT NULL,
                owner_module TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                payload_json BLOB NOT NULL,
                registered_revision INTEGER NOT NULL,
                UNIQUE(metric_name, definition_version)
            );
            CREATE TABLE IF NOT EXISTS metrics (
                metric_key TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
                definition_id TEXT NOT NULL REFERENCES metric_definitions(definition_id) ON DELETE RESTRICT,
                metric_name TEXT NOT NULL,
                definition_version TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                payload_json BLOB NOT NULL,
                registered_revision INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_metrics_run_name ON metrics(run_id, metric_name);

            CREATE TABLE IF NOT EXISTS evidence (
                evidence_id TEXT PRIMARY KEY,
                experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
                evidence_type TEXT NOT NULL,
                status TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                payload_json BLOB NOT NULL,
                registered_revision INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evidence_runs (
                evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id) ON DELETE RESTRICT,
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
                PRIMARY KEY(evidence_id, run_id)
            );
            CREATE TABLE IF NOT EXISTS evidence_artifacts (
                evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id) ON DELETE RESTRICT,
                artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
                PRIMARY KEY(evidence_id, artifact_id)
            );
            CREATE TABLE IF NOT EXISTS experiment_roles (
                decision_id TEXT PRIMARY KEY,
                experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
                configuration_id TEXT NOT NULL REFERENCES configurations(configuration_id),
                role TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                payload_json BLOB NOT NULL,
                registered_revision INTEGER NOT NULL,
                UNIQUE(experiment_id, configuration_id)
            );

            CREATE TABLE IF NOT EXISTS schema_migrations (
                migration_id TEXT PRIMARY KEY,
                source_version INTEGER NOT NULL,
                target_version INTEGER NOT NULL,
                applied_at_utc TEXT NOT NULL,
                report_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS registry_annotations (
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
            );

            CREATE TABLE IF NOT EXISTS quarantine (
                quarantine_id TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                existing_content_hash TEXT NOT NULL,
                incoming_content_hash TEXT NOT NULL,
                payload_json BLOB NOT NULL,
                reason_code TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                created_at_utc TEXT NOT NULL,
                UNIQUE(entity_type, entity_id, incoming_content_hash)
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                timestamp_utc TEXT NOT NULL,
                actor TEXT NOT NULL,
                operation TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                previous_state TEXT,
                new_state TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                revision INTEGER NOT NULL
            );

"""
