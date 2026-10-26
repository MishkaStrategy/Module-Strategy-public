"""SQLite append-only trigger DDL for the immutable results registry."""

_SCHEMA_TRIGGERS_SQL = r"""            CREATE TRIGGER IF NOT EXISTS prevent_packages_update
            BEFORE UPDATE ON packages BEGIN SELECT RAISE(ABORT, 'packages is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_packages_delete
            BEFORE DELETE ON packages BEGIN SELECT RAISE(ABORT, 'packages is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_artifacts_update
            BEFORE UPDATE ON artifacts BEGIN SELECT RAISE(ABORT, 'artifacts is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_artifacts_delete
            BEFORE DELETE ON artifacts BEGIN SELECT RAISE(ABORT, 'artifacts is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_datasets_update
            BEFORE UPDATE ON datasets BEGIN SELECT RAISE(ABORT, 'datasets is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_datasets_delete
            BEFORE DELETE ON datasets BEGIN SELECT RAISE(ABORT, 'datasets is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_configurations_update
            BEFORE UPDATE ON configurations BEGIN SELECT RAISE(ABORT, 'configurations is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_configurations_delete
            BEFORE DELETE ON configurations BEGIN SELECT RAISE(ABORT, 'configurations is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_experiments_update
            BEFORE UPDATE ON experiments BEGIN SELECT RAISE(ABORT, 'experiments is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_experiments_delete
            BEFORE DELETE ON experiments BEGIN SELECT RAISE(ABORT, 'experiments is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_runs_update
            BEFORE UPDATE ON runs BEGIN SELECT RAISE(ABORT, 'runs is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_runs_delete
            BEFORE DELETE ON runs BEGIN SELECT RAISE(ABORT, 'runs is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_metric_definitions_update
            BEFORE UPDATE ON metric_definitions BEGIN SELECT RAISE(ABORT, 'metric_definitions is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_metric_definitions_delete
            BEFORE DELETE ON metric_definitions BEGIN SELECT RAISE(ABORT, 'metric_definitions is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_metrics_update
            BEFORE UPDATE ON metrics BEGIN SELECT RAISE(ABORT, 'metrics is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_metrics_delete
            BEFORE DELETE ON metrics BEGIN SELECT RAISE(ABORT, 'metrics is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_evidence_update
            BEFORE UPDATE ON evidence BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_evidence_delete
            BEFORE DELETE ON evidence BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_roles_update
            BEFORE UPDATE ON experiment_roles BEGIN SELECT RAISE(ABORT, 'experiment_roles is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_roles_delete
            BEFORE DELETE ON experiment_roles BEGIN SELECT RAISE(ABORT, 'experiment_roles is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_run_datasets_update
            BEFORE UPDATE ON run_datasets BEGIN SELECT RAISE(ABORT, 'run_datasets is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_run_datasets_delete
            BEFORE DELETE ON run_datasets BEGIN SELECT RAISE(ABORT, 'run_datasets is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_run_artifacts_update
            BEFORE UPDATE ON run_artifacts BEGIN SELECT RAISE(ABORT, 'run_artifacts is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_run_artifacts_delete
            BEFORE DELETE ON run_artifacts BEGIN SELECT RAISE(ABORT, 'run_artifacts is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_evidence_runs_update
            BEFORE UPDATE ON evidence_runs BEGIN SELECT RAISE(ABORT, 'evidence_runs is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_evidence_runs_delete
            BEFORE DELETE ON evidence_runs BEGIN SELECT RAISE(ABORT, 'evidence_runs is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_evidence_artifacts_update
            BEFORE UPDATE ON evidence_artifacts BEGIN SELECT RAISE(ABORT, 'evidence_artifacts is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_evidence_artifacts_delete
            BEFORE DELETE ON evidence_artifacts BEGIN SELECT RAISE(ABORT, 'evidence_artifacts is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_schema_migrations_update
            BEFORE UPDATE ON schema_migrations BEGIN SELECT RAISE(ABORT, 'schema_migrations is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_schema_migrations_delete
            BEFORE DELETE ON schema_migrations BEGIN SELECT RAISE(ABORT, 'schema_migrations is append-only'); END;

            CREATE TRIGGER IF NOT EXISTS prevent_audit_update
            BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_audit_delete
            BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_annotation_update
            BEFORE UPDATE ON registry_annotations BEGIN SELECT RAISE(ABORT, 'registry_annotations is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_annotation_delete
            BEFORE DELETE ON registry_annotations BEGIN SELECT RAISE(ABORT, 'registry_annotations is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_quarantine_update
            BEFORE UPDATE ON quarantine BEGIN SELECT RAISE(ABORT, 'quarantine is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS prevent_quarantine_delete
            BEFORE DELETE ON quarantine BEGIN SELECT RAISE(ABORT, 'quarantine is append-only'); END;
"""
