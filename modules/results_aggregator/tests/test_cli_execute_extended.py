from __future__ import annotations

import json
import runpy
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import make_package
from modular_results_registry import cli
from modular_results_registry.canonical import canonical_json_bytes
from modular_results_registry.errors import ValidationError


def _write_json(path: Path, value) -> Path:
    path.write_bytes(canonical_json_bytes(value))
    return path


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_cli_registry_lifecycle_and_recovery(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "registry.sqlite"
    package_file = _write_json(tmp_path / "package.json", make_package())
    import_output = tmp_path / "import.json"

    assert cli.main(["init", str(database)]) == 0
    init_payload = json.loads(capsys.readouterr().out)
    assert init_payload["revision"] == 0

    assert (
        cli.main(["import", str(database), str(package_file), "--output", str(import_output)]) == 0
    )
    assert _read(import_output)["state"] == "REGISTERED"

    list_output = tmp_path / "list.json"
    assert (
        cli.main(
            [
                "list",
                str(database),
                "experiment",
                "--filter",
                "status=COMPLETED",
                "--limit",
                "10",
                "--offset",
                "0",
                "--output",
                str(list_output),
            ]
        )
        == 0
    )
    assert len(_read(list_output)["items"]) == 1

    experiment_id = make_package().experiments[0].experiment_id
    show_output = tmp_path / "show.json"
    assert (
        cli.main(["show", str(database), "experiment", experiment_id, "--output", str(show_output)])
        == 0
    )
    assert _read(show_output)["item"]["experiment_id"] == experiment_id
    assert (
        cli.main(["show", str(database), "experiment", "missing", "--output", str(show_output)])
        == 4
    )
    assert _read(show_output)["item"] is None

    annotation_input = _write_json(tmp_path / "annotation.json", {"text": "reviewed"})
    annotation_output = tmp_path / "annotation-output.json"
    assert (
        cli.main(
            [
                "annotate",
                str(database),
                "experiment",
                experiment_id,
                "auditor",
                "LIMITATION",
                str(annotation_input),
                "--output",
                str(annotation_output),
            ]
        )
        == 0
    )
    annotation_id = _read(annotation_output)["annotation_id"]
    annotations_output = tmp_path / "annotations.json"
    assert (
        cli.main(
            [
                "annotations",
                str(database),
                "--entity-type",
                "experiment",
                "--entity-id",
                experiment_id,
                "--output",
                str(annotations_output),
            ]
        )
        == 0
    )
    assert _read(annotations_output)["items"][0]["annotation_id"] == annotation_id

    verify_output = tmp_path / "verify.json"
    assert cli.main(["verify", str(database), "--output", str(verify_output)]) == 0
    assert _read(verify_output)["sqlite_integrity"] == "ok"

    export_output = tmp_path / "export.json"
    assert (
        cli.main(["export", str(database), "--output", str(export_output), "--without-quarantine"])
        == 0
    )
    assert _read(export_output)["registry_revision"] == 2

    bundle_output = tmp_path / "bundle.json"
    assert cli.main(["export-rebuild-bundle", str(database), "--output", str(bundle_output)]) == 0
    rebuilt = tmp_path / "rebuilt.sqlite"
    assert cli.main(["rebuild", str(bundle_output), str(rebuilt)]) == 0
    assert (
        cli.main(["doctor", str(rebuilt), "--output", str(tmp_path / "doctor-rebuilt.json")]) == 0
    )

    backup = tmp_path / "backup.sqlite"
    manifest = tmp_path / "backup-manifest.json"
    backup_output = tmp_path / "backup-output.json"
    assert (
        cli.main(
            ["backup", str(database), str(backup), str(manifest), "--output", str(backup_output)]
        )
        == 0
    )
    assert _read(backup_output)["registry_revision"] == 2
    restored = tmp_path / "restored.sqlite"
    assert cli.main(["restore", str(backup), str(manifest), str(restored)]) == 0
    assert (
        cli.main(["verify", str(restored), "--output", str(tmp_path / "verify-restored.json")]) == 0
    )

    migrate_output = tmp_path / "migrate.json"
    assert cli.main(["migrate", str(database), "--output", str(migrate_output)]) == 0
    assert _read(migrate_output)["schema_version"] >= 1
    doctor_output = tmp_path / "doctor.json"
    assert cli.main(["doctor", str(database), "--output", str(doctor_output)]) == 0
    assert _read(doctor_output)["status"] == "PASS"


def test_cli_import_conflict_returns_structured_error(tmp_path: Path) -> None:
    database = tmp_path / "registry.sqlite"
    first = make_package()
    first_file = _write_json(tmp_path / "first.json", first)
    assert (
        cli.main(["import", str(database), str(first_file), "--output", str(tmp_path / "one.json")])
        == 0
    )
    conflicting = replace(first, producer_module_version="different")
    conflict_file = _write_json(tmp_path / "conflict.json", conflicting)
    output = tmp_path / "conflict-output.json"
    assert cli.main(["import", str(database), str(conflict_file), "--output", str(output)]) == 2
    assert _read(output)["status"] == "ERROR"


def test_cli_legacy_validate_and_analytics_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "download_summary.csv").write_text("symbol,rows\nBTCUSDT,1\n", encoding="utf-8")
    output = tmp_path / "legacy.json"
    assert cli.main(["legacy-import", str(legacy), "--output", str(output)]) == 0
    assert _read(output)["profile"] == "LEGACY_UNVERIFIED_IMPORT"

    profile_file = _write_json(tmp_path / "profile.json", {})
    monkeypatch.setattr(cli, "_profile", lambda value: "profile")
    import modular_results_registry.cli_execute as execute_module

    monkeypatch.setattr(
        execute_module,
        "validate_artifact_package",
        lambda directory, profile: {"directory": str(directory), "profile": profile},
    )
    validate_output = tmp_path / "validate.json"
    assert (
        cli.main(["validate", str(tmp_path), str(profile_file), "--output", str(validate_output)])
        == 0
    )
    assert _read(validate_output)["profile"] == "profile"

    input_file = _write_json(tmp_path / "input.json", {})
    dispatch = {
        "compare": "_run_compare",
        "slices": "_run_slices",
        "pareto": "_run_pareto",
        "findings": "_run_findings",
    }
    for command, attribute in dispatch.items():
        monkeypatch.setattr(cli, attribute, lambda value, command=command: {"command": command})
        command_output = tmp_path / f"{command}.json"
        assert cli.main([command, str(input_file), "--output", str(command_output)]) == 0
        assert _read(command_output)["command"] == command
    monkeypatch.setattr(cli, "_build_audit_package", lambda value: {"package": "audit"})
    audit_output = tmp_path / "audit.json"
    assert cli.main(["build-audit-package", str(input_file), "--output", str(audit_output)]) == 0
    assert _read(audit_output) == {"package": "audit"}


def test_cli_logging_and_input_limit(tmp_path: Path) -> None:
    database = tmp_path / "registry.sqlite"
    log = tmp_path / "events.jsonl"
    assert cli.main(["--log-jsonl", str(log), "init", str(database)]) == 0
    event = json.loads(log.read_text(encoding="utf-8"))
    assert event["operation"] == "cli.init"
    assert event["status"] == "PASS"
    with pytest.raises(ValueError, match="positive"):
        cli.main(["--max-json-bytes", "0", "init", str(tmp_path / "bad.sqlite")])


def test_cli_json_helpers_and_filters_fail_closed(tmp_path: Path) -> None:
    good = _write_json(tmp_path / "good.json", {"a": 1})
    assert cli._load_json_object(good) == {"a": 1}
    for name, data in (
        ("list.json", b"[]"),
        ("invalid.json", b"{"),
        ("utf8.json", b"\xff"),
        ("nan.json", b'{"x":NaN}'),
    ):
        path = tmp_path / name
        path.write_bytes(data)
        with pytest.raises(ValidationError):
            cli._load_json_object(path)
    with pytest.raises(Exception):
        cli._load_json_object(good, max_bytes=1)

    assert cli._filters(["a=1", "b=null"]) == {"a": "1", "b": None}
    for values in (["broken"], ["=x"], ["a=1", "a=2"]):
        with pytest.raises(ValidationError):
            cli._filters(values)
    assert cli._string("", "x", allow_empty=True) == ""
    assert cli._optional_string(None, "x") is None
    assert cli._boolean(True, "x") is True
    assert cli._nonnegative_integer(0, "x") == 0
    assert str(cli._decimal("1.25", "x")) == "1.25"
    assert cli._datetime("2026-01-01T00:00:00Z", "x").tzinfo is not None
    for call in (
        lambda: cli._mapping([], "x"),
        lambda: cli._list({}, "x"),
        lambda: cli._string("", "x"),
        lambda: cli._optional_string(1, "x"),
        lambda: cli._boolean(1, "x"),
        lambda: cli._nonnegative_integer(-1, "x"),
        lambda: cli._decimal(float("inf"), "x"),
        lambda: cli._datetime("bad", "x"),
    ):
        with pytest.raises(ValidationError):
            call()


def test_module_main_delegates_to_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "registry.sqlite"
    monkeypatch.setattr(sys, "argv", ["modular_results_registry", "init", str(database)])
    with pytest.raises(SystemExit) as raised:
        runpy.run_module("modular_results_registry.__main__", run_name="__main__")
    assert raised.value.code == 0
