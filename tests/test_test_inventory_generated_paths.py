from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INVENTORY = REPOSITORY_ROOT / "scripts" / "test_inventory.py"
SUITES = REPOSITORY_ROOT / "scripts" / "test_suites.py"
MANIFEST = REPOSITORY_ROOT / "config" / "test_suites.toml"


def _load_inventory():
    suites_spec = importlib.util.spec_from_file_location("test_suites", SUITES)
    assert suites_spec is not None and suites_spec.loader is not None
    suites = importlib.util.module_from_spec(suites_spec)
    sys.modules["test_suites"] = suites
    suites_spec.loader.exec_module(suites)

    spec = importlib.util.spec_from_file_location(
        "test_inventory_generated_paths_contract", INVENTORY
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _set_root(inventory, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inventory, "ROOT", root)
    inventory._has_pyvenv_marker.cache_clear()


def _write_python(path: Path, source: str = "def test_ok():\n    assert True\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _manifest(inventory, *paths: str):
    suites = {}
    for index, path in enumerate(paths):
        suite = inventory.Suite(
            name=f"suite-{index}",
            component="Synthetic inventory contract",
            working_directory=Path("."),
            paths=(path,),
            required_imports=("pytest",),
            install_hint="synthetic test dependency",
            runtime_class="fast",
            ci_frequency="manual",
            categories=("unit",),
            external_dependencies=(),
        )
        suites[suite.name] = suite
    return inventory.Manifest(
        schema_version=1,
        audited_base_sha="0" * 40,
        timezone="UTC",
        pythonhashseed="0",
        groups={"all": tuple(suites)},
        suites=suites,
        profiles={},
    )


@pytest.mark.parametrize(
    "generated_directory",
    [
        ".uv-cache-123/archive/package",
        ".pip-cache-random/wheels/package",
        ".cache/pip/wheels/package",
        "pip-build-env-random/overlay/lib/python3.13/site-packages/package",
        ".wheel-venv/lib/python3.13/site-packages",
        ".sdist-venv/lib/python3.13/site-packages",
        "build/lib/package",
        "dist/package",
        "reports/coverage/package",
        "artifacts/evidence/package",
        "wheelhouse/package",
    ],
)
def test_generated_cache_environment_and_artifact_tests_are_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    generated_directory: str,
) -> None:
    inventory = _load_inventory()
    _set_root(inventory, tmp_path, monkeypatch)
    _write_python(tmp_path / generated_directory / "test_dependency.py")

    assert inventory.discover_test_files() == ()


def test_arbitrarily_named_virtualenv_is_detected_by_pyvenv_cfg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _load_inventory()
    _set_root(inventory, tmp_path, monkeypatch)
    environment = tmp_path / "custom-python-environment"
    (environment / "lib/python3.13/site-packages/package").mkdir(parents=True)
    (environment / "pyvenv.cfg").write_text("home = /python\n", encoding="utf-8")
    _write_python(environment / "lib/python3.13/site-packages/package/test_dependency.py")

    assert inventory.discover_test_files() == ()


def test_real_repository_test_fixture_and_hidden_tree_remain_discoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _load_inventory()
    _set_root(inventory, tmp_path, monkeypatch)
    real_test = tmp_path / "tests/test_real_repository_file.py"
    fixture = tmp_path / "tests/conftest.py"
    hidden_test = tmp_path / ".repository-tests/test_hidden_repository_file.py"
    _write_python(real_test)
    _write_python(fixture, "import pytest\n")
    _write_python(hidden_test)

    assert inventory.discover_test_files() == (
        hidden_test.resolve(),
        real_test.resolve(),
    )
    assert inventory.discover_fixture_files() == (fixture.resolve(),)


def test_build_inventory_keeps_mapped_files_and_reports_real_unmapped_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _load_inventory()
    _set_root(inventory, tmp_path, monkeypatch)
    mapped_test = tmp_path / "tests/mapped/test_mapped.py"
    mapped_fixture = tmp_path / "tests/mapped/conftest.py"
    _write_python(mapped_test)
    _write_python(
        mapped_fixture,
        "import pytest\n\n@pytest.fixture\ndef mapped_fixture():\n    return 1\n",
    )
    _write_python(tmp_path / "tests/test_real_repository_file.py")
    _write_python(
        tmp_path / "tests/conftest.py",
        "import pytest\n\n@pytest.fixture\ndef real_fixture():\n    return 1\n",
    )

    result = inventory.build_inventory(_manifest(inventory, "tests/mapped"))

    assert [item["path"] for item in result["files"]] == ["tests/mapped/test_mapped.py"]
    assert [item["path"] for item in result["fixture_files"]] == ["tests/mapped/conftest.py"]
    assert result["unmapped_test_files"] == ["tests/test_real_repository_file.py"]
    assert result["unmapped_fixture_files"] == ["tests/conftest.py"]


def test_parse_errors_and_ambiguous_suite_mapping_remain_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _load_inventory()
    _set_root(inventory, tmp_path, monkeypatch)
    _write_python(tmp_path / "tests/mapped/test_bad.py", "def test_bad(:\n")

    result = inventory.build_inventory(_manifest(inventory, "tests/mapped"))
    assert result["summary"]["parse_errors"] == 1
    assert result["parse_error_files"] == ["tests/mapped/test_bad.py"]

    with pytest.raises(RuntimeError, match="maps to multiple suites"):
        inventory.build_inventory(_manifest(inventory, "tests", "tests/mapped"))


def test_git_aware_discovery_does_not_hide_tracked_repository_test(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _load_inventory()
    _set_root(inventory, tmp_path, monkeypatch)
    tracked = tmp_path / "reports/test_tracked_repository_file.py"
    _write_python(tracked)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", tracked.relative_to(tmp_path).as_posix()],
        check=True,
    )

    assert inventory.discover_test_files() == (tracked.resolve(),)


def test_current_inventory_is_clean_and_deterministic() -> None:
    inventory = _load_inventory()
    manifest = inventory.load_manifest(MANIFEST)

    first = inventory.build_inventory(manifest)
    second = inventory.build_inventory(manifest)

    assert first == second
    assert first["summary"]["unmapped_test_files"] == 0
    assert first["summary"]["unmapped_fixture_files"] == 0
    assert first["summary"]["parse_errors"] == 0
