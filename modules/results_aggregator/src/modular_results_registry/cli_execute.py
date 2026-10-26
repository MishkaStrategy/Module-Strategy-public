from __future__ import annotations

import argparse

from . import cli
from .errors import RegistryError
from .intake import inspect_legacy_directory, validate_artifact_package
from .recovery import create_backup, rebuild_from_bundle, restore_backup
from .serialization import registration_package_from_mapping
from .storage import SQLiteRegistry


def execute(args: argparse.Namespace) -> int:
    global _JSON_INPUT_LIMITS
    if args.max_json_bytes < 1:
        raise ValueError("--max-json-bytes must be positive")
    cli._JSON_INPUT_LIMITS = cli.FileLimits(max_file_bytes=args.max_json_bytes)
    if args.command == "init":
        with SQLiteRegistry(args.database) as registry:
            cli._write_json(
                {"schema_version": registry.schema_version(), "revision": registry.revision()},
                None,
            )
        return 0
    if args.command == "import":
        package = registration_package_from_mapping(cli._load_json_object(args.package_json))
        try:
            with SQLiteRegistry(args.database) as registry:
                outcome = registry.register_package(package)
        except RegistryError as exc:
            cli._write_json({"status": "ERROR", "error": str(exc)}, args.output)
            return 2
        cli._write_json(outcome, args.output)
        return 0
    if args.command == "validate":
        profile = cli._profile(cli._load_json_object(args.profile_json))
        validation_result = validate_artifact_package(args.directory, profile)
        cli._write_json(validation_result, args.output)
        return 0
    if args.command == "legacy-import":
        legacy_report = inspect_legacy_directory(args.directory)
        cli._write_json(legacy_report, args.output)
        return 0
    if args.command == "list":
        with SQLiteRegistry(args.database) as registry:
            rows = registry.query_payloads(
                args.entity_type,
                filters=cli._filters(args.filter),
                limit=args.limit,
                offset=args.offset,
            )
        cli._write_json({"items": rows, "offset": args.offset, "limit": args.limit}, args.output)
        return 0
    if args.command == "show":
        with SQLiteRegistry(args.database) as registry:
            item = registry.get_payload(args.entity_type, args.entity_id)
        cli._write_json({"item": item}, args.output)
        return 0 if item is not None else 4
    if args.command == "annotate":
        payload = cli._load_json_object(args.payload_json)
        with SQLiteRegistry(args.database) as registry:
            annotation_id, annotation_state, revision = registry.add_annotation(
                entity_type=args.entity_type,
                entity_id=args.entity_id,
                actor=args.actor,
                reason=args.reason,
                payload=payload,
            )
        cli._write_json(
            {
                "annotation_id": annotation_id,
                "state": annotation_state,
                "revision": revision,
            },
            args.output,
        )
        return 0
    if args.command == "annotations":
        with SQLiteRegistry(args.database) as registry:
            rows = registry.annotations(
                entity_type=args.entity_type,
                entity_id=args.entity_id,
                limit=args.limit,
                offset=args.offset,
            )
        cli._write_json({"items": rows, "offset": args.offset, "limit": args.limit}, args.output)
        return 0
    if args.command == "compare":
        cli._write_json(cli._run_compare(cli._load_json_object(args.input_json)), args.output)
        return 0
    if args.command == "slices":
        cli._write_json(cli._run_slices(cli._load_json_object(args.input_json)), args.output)
        return 0
    if args.command == "pareto":
        cli._write_json(cli._run_pareto(cli._load_json_object(args.input_json)), args.output)
        return 0
    if args.command == "findings":
        cli._write_json(cli._run_findings(cli._load_json_object(args.input_json)), args.output)
        return 0
    if args.command == "build-audit-package":
        audit_package = cli._build_audit_package(cli._load_json_object(args.input_json))
        cli._write_json(audit_package, args.output)
        return 0
    if args.command == "verify":
        with SQLiteRegistry(args.database) as registry:
            integrity_report = registry.verify_integrity()
        cli._write_json(integrity_report, args.output)
        return 0 if cli._integrity_pass(integrity_report) else 3
    if args.command == "export":
        with SQLiteRegistry(args.database) as registry:
            exported_state = registry.export_state(include_quarantine=not args.without_quarantine)
        cli._write_json(exported_state, args.output)
        return 0
    if args.command == "export-rebuild-bundle":
        with SQLiteRegistry(args.database) as registry:
            rebuild_bundle = registry.export_rebuild_bundle()
        cli._write_json(rebuild_bundle, args.output)
        return 0
    if args.command == "backup":
        with SQLiteRegistry(args.database) as registry:
            backup_result = create_backup(registry, args.backup_database, args.manifest)
        cli._write_json(backup_result, args.output)
        return 0
    if args.command == "restore":
        restore_backup(args.backup_database, args.manifest, args.destination)
        return 0
    if args.command == "rebuild":
        rebuild_from_bundle(cli._load_json_object(args.bundle), args.destination)
        return 0
    if args.command == "migrate":
        with SQLiteRegistry(args.database) as registry:
            cli._write_json(
                {
                    "schema_version": registry.schema_version(),
                    "migrations": registry.migration_history(),
                },
                args.output,
            )
        return 0
    if args.command == "doctor":
        with SQLiteRegistry(args.database) as registry:
            doctor_report = registry.verify_integrity()
            doctor_payload = {
                "status": ("PASS" if cli._integrity_pass(doctor_report) else "FAIL"),
                "schema_version": registry.schema_version(),
                "revision": registry.revision(),
                "quarantine_count": registry.quarantine_count(),
                "integrity": doctor_report,
            }
        cli._write_json(doctor_payload, args.output)
        return 0 if doctor_payload["status"] == "PASS" else 3
    return 1
