from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "config" / "test_suites.toml"
RUNNER = ROOT / "scripts" / "test_suites.py"
INVENTORY = ROOT / "scripts" / "test_inventory.py"
COLLECTION = ROOT / "scripts" / "test_collection_baseline.py"
COLLECTION_RECORDER = ROOT / "scripts" / "pytest_collect_ids.py"


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_suite_manifest_is_valid_and_covers_all_configured_paths() -> None:
    runner = _load_script("test_suites_contract", RUNNER)
    manifest = runner.load_manifest(MANIFEST)
    assert manifest.audited_base_sha == "30263f9e2bc4a972bc0027ffec40d23a0b3f2a37"
    assert "all" in manifest.groups
    assert set(manifest.groups["all"]) == set(manifest.suites)
    assert manifest.groups["without_optional"] == ("root-no-optional",)
    assert "module-template" in manifest.suites
    assert len(manifest.suites) >= 22
    assert set(manifest.profiles) == {"root-no-optional"}
    assert manifest.suites["root"].pytest_args == ("--import-mode=importlib",)
    assert all(not suite.pytest_args for name, suite in manifest.suites.items() if name != "root")

    profile = manifest.profiles["root-no-optional"]
    assert profile.required_imports == ("pytest", "yaml")
    assert profile.pytest_args == (
        "--import-mode=importlib",
        "--ignore=tests/backtest_engine/test_registered_data.py",
        "--ignore=tests/backtest_engine/test_registered_streaming_integration.py",
        "--ignore=tests/backtest_engine/test_registered_streaming_recovery.py",
        "--ignore=tests/backtest_engine/test_registered_streaming_runner.py",
        "--ignore=tests/backtest_engine/test_registered_streaming_source.py",
        "--ignore=tests/integration/test_system_acceptance_stage1_deployment.py",
        "--ignore=tests/integration/test_system_acceptance_stage1_host.py",
        "--ignore=tests/integration/test_system_acceptance_stage1_owner_reconnect.py",
        "--ignore=tests/integration/test_system_acceptance_stage1_service_manager.py",
        "--ignore=tests/integration/test_system_acceptance_stage1_launchd.py",
        "--ignore=tests/signal_bot/test_properties.py",
        "-k",
        "not chart_reference_scenarios",
    )


def _imports_pyarrow(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(
            alias.name == "pyarrow" or alias.name.startswith("pyarrow.") for alias in node.names
        )
    return (
        isinstance(node, ast.ImportFrom)
        and node.module is not None
        and (node.module == "pyarrow" or node.module.startswith("pyarrow."))
    )


def test_optional_free_profile_ignores_all_pyarrow_test_modules() -> None:
    runner = _load_script("test_suites_optional_contract", RUNNER)
    profile = runner.load_manifest(MANIFEST).profiles["root-no-optional"]
    ignored = {
        argument.removeprefix("--ignore=")
        for argument in profile.pytest_args
        if argument.startswith("--ignore=")
    }
    pyarrow_tests: set[str] = set()
    for path in (ROOT / "tests").rglob("test*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(_imports_pyarrow(node) for node in ast.walk(tree)):
            pyarrow_tests.add(path.relative_to(ROOT).as_posix())
    assert pyarrow_tests <= ignored


def test_static_inventory_maps_every_test_file(record_testsuite_property) -> None:
    _load_script("test_suites", RUNNER)
    inventory_module = _load_script("test_inventory_contract", INVENTORY)
    manifest = inventory_module.load_manifest(MANIFEST)
    inventory = inventory_module.build_inventory(manifest)
    summary = inventory["summary"]
    for key in (
        "discovered_test_named_python_files",
        "test_files",
        "mapped_test_files",
        "unmapped_test_files",
        "test_named_non_test_files",
        "fixture_files",
        "mapped_fixture_files",
        "unmapped_fixture_files",
        "duplicate_basenames",
        "static_test_functions",
        "fixture_definitions",
        "parametrized_decorators",
        "parse_errors",
    ):
        record_testsuite_property(f"test_architecture_{key}", summary[key])
    record_testsuite_property(
        "test_architecture_suite_file_counts",
        json.dumps(inventory["suite_file_counts"], sort_keys=True),
    )
    record_testsuite_property(
        "test_architecture_fixture_suite_file_counts",
        json.dumps(inventory["fixture_suite_file_counts"], sort_keys=True),
    )
    record_testsuite_property(
        "test_architecture_marker_file_counts",
        json.dumps(inventory["marker_file_counts"], sort_keys=True),
    )
    record_testsuite_property(
        "test_architecture_dependency_file_counts",
        json.dumps(inventory["dependency_file_counts"], sort_keys=True),
    )
    record_testsuite_property(
        "test_architecture_test_named_non_test_files",
        json.dumps(inventory["test_named_non_test_files"], sort_keys=True),
    )
    assert summary["test_files"] >= 549
    assert summary["unmapped_test_files"] == 0
    assert summary["unmapped_fixture_files"] == 0
    assert summary["fixture_definitions"] > 1
    assert summary["parse_errors"] == 0
    assert summary["duplicate_basenames"] > 0
    assert all(path.startswith("scripts/") for path in inventory["test_named_non_test_files"])


def test_missing_nested_dependency_is_reported_without_exception() -> None:
    runner = _load_script("test_suites_missing_dependency", RUNNER)
    suite = runner.Suite(
        name="missing",
        component="missing",
        working_directory=Path("."),
        paths=("tests",),
        required_imports=("definitely_missing_parent.definitely_missing_child",),
        install_hint="install missing dependency",
        runtime_class="fast",
        ci_frequency="manual",
        categories=("unit",),
        external_dependencies=(),
    )
    assert runner.missing_imports(suite) == ("definitely_missing_parent.definitely_missing_child",)


def test_cli_options_work_after_selection_and_pytest_passthrough() -> None:
    runner = _load_script("test_suites_cli_contract", RUNNER)
    parser_argv, passthrough = runner.split_pytest_args(
        ["collect", "all", "--keep-going", "--", "-ra"]
    )
    args = runner.build_parser().parse_args(parser_argv)
    assert args.command == "collect"
    assert args.selection == ["all"]
    assert args.keep_going is True
    assert passthrough == ("-ra",)

    _load_script("test_suites", RUNNER)
    collection = _load_script("test_collection_cli_contract", COLLECTION)
    parser_argv, passthrough = collection.split_pytest_args(
        [
            "collect",
            "all",
            "--keep-going",
            "--timeout-seconds",
            "1200",
            "--output",
            "collection.json",
            "--",
            "-ra",
        ]
    )
    args = collection.build_parser().parse_args(parser_argv)
    assert args.command == "collect"
    assert args.selection == ["all"]
    assert args.keep_going is True
    assert args.timeout_seconds == 1200.0
    assert args.output == Path("collection.json")
    assert passthrough == ("-ra",)

    args = collection.build_parser().parse_args(
        ["compare", "before.json", "after.json", "--require-exact"]
    )
    assert args.command == "compare"
    assert args.require_exact is True


def test_runner_list_command_is_dependency_free() -> None:
    completed = subprocess.run(
        [sys.executable, str(RUNNER), "--manifest", str(MANIFEST), "list"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "group all:" in completed.stdout
    assert "group without_optional: root-no-optional" in completed.stdout
    assert "root-no-optional: kind=profile" in completed.stdout
    assert "market-data:" in completed.stdout


def test_collection_baseline_extracts_and_compares_node_ids() -> None:
    _load_script("test_suites", RUNNER)
    collection = _load_script("test_collection_baseline_contract", COLLECTION)
    output = (
        "tests/test_example.py::test_one\n"
        "tests/test_example.py::TestGroup::test_two[value]\n"
        "2 tests collected in 0.01s\n"
    )
    assert collection.extract_node_ids(output) == (
        "tests/test_example.py::test_one",
        "tests/test_example.py::TestGroup::test_two[value]",
    )
    before = {
        "schema_version": 1,
        "suites": [{"suite": "root", "node_ids": ["tests/test_a.py::test_a"]}],
    }
    after = {
        "schema_version": 1,
        "suites": [{"suite": "root", "node_ids": ["tests/test_b.py::test_b"]}],
    }
    comparison = collection.compare_reports(before, after)
    assert comparison["status"] == "removed-test-ids"
    assert comparison["exact_match"] is False
    assert comparison["total_removed"] == 1
    assert comparison["total_added"] == 1

    exact = collection.compare_reports(before, before)
    assert exact["status"] == "exact-match"
    assert exact["exact_match"] is True
    assert exact["total_removed"] == 0
    assert exact["total_added"] == 0

    added = collection.compare_reports(
        {"schema_version": 1, "suites": [{"suite": "root", "node_ids": []}]}, before
    )
    assert added["status"] == "added-test-ids"
    assert added["exact_match"] is False
    assert added["total_removed"] == 0
    assert added["total_added"] == 1


def test_collection_recorder_preserves_cwd_and_exact_ids(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "-q"\n', encoding="utf-8"
    )
    (tmp_path / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "test_sample.py").write_text(
        "from helper import VALUE\n\n"
        "import pytest\n\n"
        "@pytest.mark.parametrize('value', [1, 2])\n"
        "def test_value(value):\n"
        "    assert value == VALUE or value == 2\n",
        encoding="utf-8",
    )
    report = tmp_path / "collection.json"
    _load_script("test_suites", RUNNER)
    collection = _load_script("test_collection_recorder_contract", COLLECTION)
    suite = collection.Suite(
        name="synthetic",
        component="Synthetic collection contract",
        working_directory=Path("."),
        paths=("test_sample.py",),
        required_imports=("pytest",),
        install_hint="python -m pip install pytest",
        runtime_class="fast",
        ci_frequency="manual",
        categories=("unit",),
        external_dependencies=(),
    )
    completed = subprocess.run(
        collection._suite_command(suite, report, ("-q",)),
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["collected"] == 2
    assert payload["node_ids"] == [
        "test_sample.py::test_value[1]",
        "test_sample.py::test_value[2]",
    ]
    assert payload["collection_errors"] == []


def test_inventory_cli_emits_json_without_importing_optional_packages() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(INVENTORY),
            "--manifest",
            str(MANIFEST),
            "--compact",
            "--fail-on-unmapped",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["summary"]["mapped_test_files"] == payload["summary"]["test_files"]
    assert payload["summary"]["mapped_fixture_files"] == payload["summary"]["fixture_files"]
