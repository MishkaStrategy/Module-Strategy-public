from __future__ import annotations

import argparse

from . import cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="results-registry")
    parser.add_argument("--no-color", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--max-json-bytes",
        type=int,
        default=cli._JSON_INPUT_LIMITS.max_file_bytes,
        help="maximum bytes accepted for any JSON input",
    )
    parser.add_argument("--log-jsonl", help="append structured operational events to JSONL")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("database")

    register = sub.add_parser("import")
    register.add_argument("database")
    register.add_argument("package_json")
    register.add_argument("--output")

    validate = sub.add_parser("validate")
    validate.add_argument("directory")
    validate.add_argument("profile_json")
    validate.add_argument("--output")

    legacy = sub.add_parser("legacy-import")
    legacy.add_argument("directory")
    legacy.add_argument("--output")

    listing = sub.add_parser("list")
    listing.add_argument("database")
    listing.add_argument("entity_type")
    listing.add_argument("--filter", action="append", default=[])
    listing.add_argument("--limit", type=int, default=100)
    listing.add_argument("--offset", type=int, default=0)
    listing.add_argument("--output")

    show = sub.add_parser("show")
    show.add_argument("database")
    show.add_argument("entity_type")
    show.add_argument("entity_id")
    show.add_argument("--output")

    annotate = sub.add_parser("annotate")
    annotate.add_argument("database")
    annotate.add_argument("entity_type")
    annotate.add_argument("entity_id")
    annotate.add_argument("actor")
    annotate.add_argument("reason")
    annotate.add_argument("payload_json")
    annotate.add_argument("--output")

    annotations = sub.add_parser("annotations")
    annotations.add_argument("database")
    annotations.add_argument("--entity-type")
    annotations.add_argument("--entity-id")
    annotations.add_argument("--limit", type=int, default=1000)
    annotations.add_argument("--offset", type=int, default=0)
    annotations.add_argument("--output")

    compare = sub.add_parser("compare")
    compare.add_argument("input_json")
    compare.add_argument("--output")

    slices = sub.add_parser("slices")
    slices.add_argument("input_json")
    slices.add_argument("--output")

    pareto = sub.add_parser("pareto")
    pareto.add_argument("input_json")
    pareto.add_argument("--output")

    findings = sub.add_parser("findings")
    findings.add_argument("input_json")
    findings.add_argument("--output")

    audit_package = sub.add_parser("build-audit-package")
    audit_package.add_argument("input_json")
    audit_package.add_argument("--output", required=True)

    verify = sub.add_parser("verify")
    verify.add_argument("database")
    verify.add_argument("--output")

    export = sub.add_parser("export")
    export.add_argument("database")
    export.add_argument("--output", required=True)
    export.add_argument("--without-quarantine", action="store_true")

    bundle = sub.add_parser("export-rebuild-bundle")
    bundle.add_argument("database")
    bundle.add_argument("--output", required=True)

    backup = sub.add_parser("backup")
    backup.add_argument("database")
    backup.add_argument("backup_database")
    backup.add_argument("manifest")
    backup.add_argument("--output")

    restore = sub.add_parser("restore")
    restore.add_argument("backup_database")
    restore.add_argument("manifest")
    restore.add_argument("destination")

    rebuild = sub.add_parser("rebuild")
    rebuild.add_argument("bundle")
    rebuild.add_argument("destination")

    migrate = sub.add_parser("migrate")
    migrate.add_argument("database")
    migrate.add_argument("--output")

    doctor = sub.add_parser("doctor")
    doctor.add_argument("database")
    doctor.add_argument("--output")
    return parser
