from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any

from typer.main import get_command

from market_data_application.cli import create_cli
from market_data_application.migration_cli import _parser as migration_parser
from market_data_ingestion.cli import _parser as legacy_parser

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MATRIX_PATH = REPOSITORY_ROOT / "docs/integration/MARKET_DATA_CLI_PARITY_MATRIX.json"
LEGACY_SOURCE = REPOSITORY_ROOT / "modules/market_data_ingestion/src/market_data_ingestion/cli.py"
V1_SOURCE = REPOSITORY_ROOT / "modules/market_data_ingestion/src/market_data_application/cli.py"
MIGRATION_SOURCE = (
    REPOSITORY_ROOT / "modules/market_data_ingestion/src/market_data_application/migration_cli.py"
)
FRAMEWORK_OPTIONS = {
    "--help",
    "-h",
    "--install-completion",
    "--show-completion",
}
ALLOWED_CLASSIFICATIONS = {
    "equivalent",
    "safely_replaced",
    "intentionally_incompatible",
    "not_yet_implemented",
}


def _matrix() -> dict[str, Any]:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def _normalized_options(options: list[str] | tuple[str, ...]) -> list[str]:
    return sorted(set(options) - FRAMEWORK_OPTIONS)


def _argparse_surface(parser: argparse.ArgumentParser) -> dict[str, Any]:
    subparsers = [
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    ]
    commands: dict[str, list[str]] = {}
    if subparsers:
        assert len(subparsers) == 1
        for name, command_parser in subparsers[0].choices.items():
            commands[name] = _normalized_options(
                [option for action in command_parser._actions for option in action.option_strings]
            )
    return {
        "root_options": _normalized_options(
            [option for action in parser._actions for option in action.option_strings]
        ),
        "commands": dict(sorted(commands.items())),
    }


def _legacy_surface() -> dict[str, Any]:
    return _argparse_surface(legacy_parser())


def _migration_surface() -> dict[str, Any]:
    return _argparse_surface(migration_parser())


def _command_options(command: Any) -> list[str]:
    options: list[str] = []
    for parameter in command.params:
        options.extend(getattr(parameter, "opts", ()))
        options.extend(getattr(parameter, "secondary_opts", ()))
    return _normalized_options(options)


def _v1_surface() -> dict[str, Any]:
    root = get_command(create_cli())
    assert isinstance(getattr(root, "commands", None), dict)
    commands: dict[str, list[str]] = {}

    def visit(group: Any, prefix: tuple[str, ...]) -> None:
        group_commands = getattr(group, "commands", None)
        assert isinstance(group_commands, dict)
        for name, command in sorted(group_commands.items()):
            path = (*prefix, name)
            if isinstance(getattr(command, "commands", None), dict):
                visit(command, path)
            else:
                commands[" ".join(path)] = _command_options(command)

    visit(root, ())
    return {
        "root_options": _command_options(root),
        "commands": commands,
    }


def _integer_constants(node: ast.AST | None) -> set[int]:
    if node is None:
        return set()
    return {
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant)
        and isinstance(child.value, int)
        and not isinstance(child.value, bool)
    }


def _function(source: Path, name: str) -> ast.FunctionDef:
    module = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    return next(
        node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _legacy_exit_codes() -> list[int]:
    main = _function(LEGACY_SOURCE, "main")
    codes: set[int] = set()
    for node in ast.walk(main):
        if isinstance(node, ast.Return):
            codes.update(_integer_constants(node.value))
    return sorted(codes)


def _v1_exit_codes() -> list[int]:
    module = ast.parse(V1_SOURCE.read_text(encoding="utf-8"), filename=str(V1_SOURCE))
    codes = {0}
    exit_mapper = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_exit_code"
    )
    for node in ast.walk(exit_mapper):
        if isinstance(node, ast.Return):
            codes.update(_integer_constants(node.value))
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        if not (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "typer"
            and node.func.attr == "Exit"
        ):
            continue
        for keyword in node.keywords:
            if keyword.arg == "code":
                codes.update(_integer_constants(keyword.value))
    return sorted(codes)


def _migration_exit_codes() -> list[int]:
    module = ast.parse(
        MIGRATION_SOURCE.read_text(encoding="utf-8"),
        filename=str(MIGRATION_SOURCE),
    )
    codes = {0}
    exit_mapper = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_exit_code"
    )
    for node in ast.walk(exit_mapper):
        if isinstance(node, ast.Return):
            codes.update(_integer_constants(node.value))
    return sorted(codes)


def test_matrix_matches_actual_cli_surfaces() -> None:
    matrix = _matrix()
    assert matrix["observed_surfaces"]["legacy"] == _legacy_surface()
    assert matrix["observed_surfaces"]["v1"] == _v1_surface()
    assert matrix["observed_surfaces"]["migration"] == _migration_surface()


def test_matrix_covers_every_legacy_command_once() -> None:
    matrix = _matrix()
    legacy_commands = set(matrix["observed_surfaces"]["legacy"]["commands"])
    classifications = matrix["classifications"]
    classified_commands = [item["legacy_command"] for item in classifications]

    assert len(classified_commands) == len(set(classified_commands))
    assert set(classified_commands) == legacy_commands
    assert {item["classification"] for item in classifications} <= ALLOWED_CLASSIFICATIONS


def test_every_declared_v1_path_exists() -> None:
    matrix = _matrix()
    v1_commands = set(matrix["observed_surfaces"]["v1"]["commands"])
    entry_points = set(matrix["entry_points"])
    for item in matrix["classifications"]:
        assert set(item["v1_paths"]) <= v1_commands
        assert set(item.get("migration_entry_points", ())) <= entry_points


def test_unresolved_or_incompatible_migrations_remain_release_gates() -> None:
    matrix = _matrix()
    for item in matrix["classifications"]:
        if item["classification"] in {
            "not_yet_implemented",
            "intentionally_incompatible",
        }:
            assert item["release_gate"] is True


def test_documented_exit_code_sets_match_source() -> None:
    matrix = _matrix()
    assert matrix["exit_codes"]["legacy"] == _legacy_exit_codes()
    assert matrix["exit_codes"]["v1"] == _v1_exit_codes()
    assert matrix["exit_codes"]["migration"] == _migration_exit_codes()


def test_audit_does_not_authorize_product_or_release_changes() -> None:
    policy = _matrix()["policy"]
    assert policy == {
        "legacy_entry_point_removal_authorized": False,
        "production_cli_changes_allowed": False,
        "automatic_mutation_aliases_allowed": False,
        "integration_merge_authorized": False,
    }
