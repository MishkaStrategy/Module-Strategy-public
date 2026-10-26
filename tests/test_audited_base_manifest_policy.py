from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "test-architecture-validation.yml"
TEST_SUITES = ROOT / "scripts" / "test_suites.py"
TEST_SUITES_MANIFEST = ROOT / "config" / "test_suites.toml"
COLLECTION = ROOT / "scripts" / "test_collection_baseline.py"
PROJECTOR = ROOT / "scripts" / "materialize_audited_suite_manifest.py"
AUDITED_BASE = "30263f9e2bc4a972bc0027ffec40d23a0b3f2a37"


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _collection():
    _load_script("test_suites", TEST_SUITES)
    return _load_script("test_collection_baseline_relocation_policy", COLLECTION)


def _report(*node_ids: str, sha: str = AUDITED_BASE) -> dict[str, object]:
    return {
        "schema_version": 1,
        "audited_base_sha": sha,
        "suites": [{"suite": "root", "node_ids": list(node_ids)}],
    }


def _write_relocation_manifest(
    tmp_path: Path,
    relocations: dict[str, str],
    *,
    parameter_migrations: dict[str, str] | None = None,
    sha: str = AUDITED_BASE,
) -> Path:
    path = tmp_path / "test_suites.toml"
    lines = [
        "schema_version = 1",
        f"audited_base_sha = {json.dumps(sha)}",
        "",
        "[node_id_relocations]",
    ]
    for source, target in sorted(relocations.items()):
        lines.append(f"{json.dumps(source)} = {json.dumps(target)}")
    lines.extend(["", "[node_id_parameter_migrations]"])
    for source, target in sorted((parameter_migrations or {}).items()):
        lines.append(f"{json.dumps(source)} = {json.dumps(target)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_audited_base_collection_materializes_compatible_manifest() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "materialize_audited_suite_manifest.py" in workflow
    assert '--base-root "$base_worktree"' in workflow
    assert '--output "$base_worktree/config/test_suites.toml"' in workflow
    assert 'cp scripts/test_suites.py "$base_worktree/scripts/test_suites.py"' in workflow
    assert (
        'cp scripts/pytest_collect_ids.py "$base_worktree/scripts/pytest_collect_ids.py"'
        in workflow
    )
    assert (
        'cp scripts/test_collection_baseline.py "$base_worktree/scripts/test_collection_baseline.py"'
        in workflow
    )


def test_audited_base_projection_drops_only_absent_paths_and_entries(tmp_path: Path) -> None:
    projector = _load_script("audited_suite_projector", PROJECTOR)
    base_root = tmp_path / "base"
    (base_root / "existing" / "tests").mkdir(parents=True)
    raw = {
        "schema_version": 1,
        "audited_base_sha": "a" * 40,
        "environment": {"timezone": "UTC", "pythonhashseed": "0"},
        "groups": {"all": ["existing", "head-only"]},
        "suites": [
            {
                "name": "existing",
                "component": "Existing",
                "working_directory": "existing",
                "paths": ["tests", "benchmarks/tests"],
                "required_imports": ["pytest"],
                "install_hint": "install existing",
                "runtime_class": "fast",
                "ci_frequency": "pull_request",
                "categories": ["unit"],
                "external_dependencies": [],
            },
            {
                "name": "head-only",
                "component": "Head only",
                "working_directory": "head-only",
                "paths": ["tests"],
                "required_imports": ["pytest"],
                "install_hint": "install head-only",
                "runtime_class": "fast",
                "ci_frequency": "pull_request",
                "categories": ["unit"],
                "external_dependencies": [],
            },
        ],
        "profiles": [],
    }

    projected = projector.project_manifest(raw, base_root)
    assert [entry["name"] for entry in projected["suites"]] == ["existing"]
    assert projected["suites"][0]["paths"] == ["tests"]
    assert projected["groups"]["all"] == ["existing"]

    output = tmp_path / "projected.toml"
    projector.write_manifest(projected, output)
    with output.open("rb") as handle:
        encoded = tomllib.load(handle)
    assert encoded["suites"][0]["paths"] == ["tests"]
    assert encoded["groups"]["all"] == ["existing"]


def test_base_to_head_compare_allows_additive_head_only_suite() -> None:
    _load_script("test_suites", TEST_SUITES)
    collection = _load_script("test_collection_baseline_policy", COLLECTION)

    before = {
        "schema_version": 1,
        "audited_base_sha": "base",
        "suites": [{"suite": "existing", "node_ids": ["tests/test_existing.py::test_existing"]}],
    }
    after = {
        "schema_version": 1,
        "audited_base_sha": "base",
        "suites": [
            {"suite": "existing", "node_ids": ["tests/test_existing.py::test_existing"]},
            {"suite": "new-component", "node_ids": ["tests/test_new.py::test_new"]},
        ],
    }

    comparison = collection.compare_reports(before, after)
    assert comparison["status"] == "added-test-ids"
    assert comparison["total_removed"] == 0
    assert comparison["total_added"] == 1
    new_suite = next(item for item in comparison["suites"] if item["suite"] == "new-component")
    assert new_suite["before"] == 0
    assert new_suite["after"] == 1
    assert new_suite["removed"] == []
    assert new_suite["added"] == ["tests/test_new.py::test_new"]


def test_repository_relocation_manifest_is_exact_and_audited() -> None:
    collection = _collection()
    manifest = collection.load_relocation_manifest(TEST_SUITES_MANIFEST)
    assert manifest.audited_base_sha == AUDITED_BASE
    assert dict(manifest.relocations) == {
        "tests/test_coverage_policy.py": "tests/ci_policy/test_coverage_policy.py",
        "tests/test_runner_routing_policy.py": "tests/ci_policy/test_runner_routing_policy.py",
    }
    assert dict(manifest.parameter_id_migrations) == {
        "tests/test_edges.py::test_store_detects_additional_tampering[UPDATE audit_metadata SET value = '2'-params0-metadata]": "tests/test_edges.py::test_store_detects_additional_tampering[UPDATE audit_metadata SET value = '99'-params0-metadata]"
    }


def test_verified_file_relocation_preserves_identical_test_suffix(tmp_path: Path) -> None:
    collection = _collection()
    manifest = collection.load_relocation_manifest(
        _write_relocation_manifest(
            tmp_path,
            {"tests/test_old.py": "tests/ci_policy/test_old.py"},
        )
    )
    before = _report(
        "tests/test_old.py::test_one",
        "tests/test_old.py::TestGroup::test_two[value]",
    )
    after = _report(
        "tests/ci_policy/test_old.py::test_one",
        "tests/ci_policy/test_old.py::TestGroup::test_two[value]",
    )

    comparison = collection.compare_reports(before, after, relocation_manifest=manifest)

    assert comparison["status"] == "relocated-test-ids"
    assert comparison["exact_match"] is False
    assert comparison["total_removed"] == 0
    assert comparison["total_added"] == 0
    assert comparison["total_relocated"] == 2
    assert comparison["suites"][0]["relocated"] == [
        {
            "from": "tests/test_old.py::TestGroup::test_two[value]",
            "to": "tests/ci_policy/test_old.py::TestGroup::test_two[value]",
        },
        {
            "from": "tests/test_old.py::test_one",
            "to": "tests/ci_policy/test_old.py::test_one",
        },
    ]


def test_unmapped_raw_removal_remains_fatal_without_relocation() -> None:
    collection = _collection()
    comparison = collection.compare_reports(
        _report("tests/test_old.py::test_one"),
        _report("tests/test_new.py::test_one"),
    )
    assert comparison["status"] == "removed-test-ids"
    assert comparison["total_removed"] == 1
    assert comparison["total_added"] == 1
    assert comparison["total_relocated"] == 0


@pytest.mark.parametrize(
    "after_id",
    [
        "tests/ci_policy/test_old.py::test_renamed",
        "tests/ci_policy/test_other.py::test_one",
    ],
)
def test_mapping_cannot_hide_missing_or_renamed_test(
    tmp_path: Path,
    after_id: str,
) -> None:
    collection = _collection()
    manifest = collection.load_relocation_manifest(
        _write_relocation_manifest(
            tmp_path,
            {"tests/test_old.py": "tests/ci_policy/test_old.py"},
        )
    )
    with pytest.raises(ValueError, match="did not preserve"):
        collection.compare_reports(
            _report("tests/test_old.py::test_one"),
            _report(after_id),
            relocation_manifest=manifest,
        )


@pytest.mark.parametrize(
    "relocations",
    [
        {"../tests/test_old.py": "tests/test_new.py"},
        {"/tests/test_old.py": "tests/test_new.py"},
        {"tests/test_old.py": "tests/../test_new.py"},
        {"tests/test_old.txt": "tests/test_new.py"},
        {"tests/test_old.py": "tests/test_old.py"},
        {
            "tests/test_one.py": "tests/ci_policy/test_target.py",
            "tests/test_two.py": "tests/ci_policy/test_target.py",
        },
    ],
)
def test_relocation_manifest_rejects_unsafe_or_ambiguous_paths(
    tmp_path: Path,
    relocations: dict[str, str],
) -> None:
    collection = _collection()
    with pytest.raises(ValueError):
        collection.load_relocation_manifest(_write_relocation_manifest(tmp_path, relocations))


def test_relocation_manifest_is_bound_to_audited_base(tmp_path: Path) -> None:
    collection = _collection()
    manifest = collection.load_relocation_manifest(
        _write_relocation_manifest(
            tmp_path,
            {"tests/test_old.py": "tests/ci_policy/test_old.py"},
        )
    )
    with pytest.raises(ValueError, match="audited base does not match"):
        collection.compare_reports(
            _report("tests/test_old.py::test_one", sha="1" * 40),
            _report("tests/ci_policy/test_old.py::test_one", sha="1" * 40),
            relocation_manifest=manifest,
        )


OLD_METADATA_SCHEMA_NODE = (
    "tests/test_edges.py::test_store_detects_additional_tampering["
    "UPDATE audit_metadata SET value = '2'-params0-metadata]"
)
CURRENT_METADATA_SCHEMA_NODE = (
    "tests/test_edges.py::test_store_detects_additional_tampering["
    "UPDATE audit_metadata SET value = '99'-params0-metadata]"
)


def _parameter_migration_manifest(tmp_path: Path) -> Path:
    return _write_relocation_manifest(
        tmp_path,
        {},
        parameter_migrations={OLD_METADATA_SCHEMA_NODE: CURRENT_METADATA_SCHEMA_NODE},
    )


def test_verified_parameter_id_migration_preserves_same_test_and_reports_separately(
    tmp_path: Path,
) -> None:
    collection = _collection()
    manifest = collection.load_relocation_manifest(_parameter_migration_manifest(tmp_path))
    comparison = collection.compare_reports(
        _report(OLD_METADATA_SCHEMA_NODE),
        _report(CURRENT_METADATA_SCHEMA_NODE),
        relocation_manifest=manifest,
    )

    assert comparison["status"] == "parameter-id-migrations"
    assert comparison["exact_match"] is False
    assert comparison["total_removed"] == 0
    assert comparison["total_added"] == 0
    assert comparison["total_relocated"] == 0
    assert comparison["total_parameter_id_migrations"] == 1
    assert comparison["suites"][0]["parameter_id_migrations"] == [
        {"from": OLD_METADATA_SCHEMA_NODE, "to": CURRENT_METADATA_SCHEMA_NODE}
    ]


def test_parameter_id_migration_requires_exact_audited_base(tmp_path: Path) -> None:
    collection = _collection()
    manifest = collection.load_relocation_manifest(_parameter_migration_manifest(tmp_path))
    with pytest.raises(ValueError, match="audited base does not match"):
        collection.compare_reports(
            _report(OLD_METADATA_SCHEMA_NODE, sha="1" * 40),
            _report(CURRENT_METADATA_SCHEMA_NODE, sha="1" * 40),
            relocation_manifest=manifest,
        )


@pytest.mark.parametrize(
    ("before_ids", "after_ids", "match"),
    [
        ((OLD_METADATA_SCHEMA_NODE,), (), "target must be additive"),
        ((OLD_METADATA_SCHEMA_NODE,), (OLD_METADATA_SCHEMA_NODE,), "source remains"),
        (
            (OLD_METADATA_SCHEMA_NODE, CURRENT_METADATA_SCHEMA_NODE),
            (CURRENT_METADATA_SCHEMA_NODE,),
            "target already existed",
        ),
    ],
)
def test_parameter_id_migration_rejects_missing_retained_or_preexisting_target(
    tmp_path: Path,
    before_ids: tuple[str, ...],
    after_ids: tuple[str, ...],
    match: str,
) -> None:
    collection = _collection()
    manifest = collection.load_relocation_manifest(_parameter_migration_manifest(tmp_path))
    with pytest.raises(ValueError, match=match):
        collection.compare_reports(
            _report(*before_ids), _report(*after_ids), relocation_manifest=manifest
        )


def test_parameter_id_migration_must_remain_in_same_suite(tmp_path: Path) -> None:
    collection = _collection()
    manifest = collection.load_relocation_manifest(_parameter_migration_manifest(tmp_path))
    before = {
        "schema_version": 1,
        "audited_base_sha": AUDITED_BASE,
        "suites": [{"suite": "root", "node_ids": [OLD_METADATA_SCHEMA_NODE]}],
    }
    after = {
        "schema_version": 1,
        "audited_base_sha": AUDITED_BASE,
        "suites": [{"suite": "other", "node_ids": [CURRENT_METADATA_SCHEMA_NODE]}],
    }
    with pytest.raises(ValueError, match="same suite"):
        collection.compare_reports(before, after, relocation_manifest=manifest)


def test_parameter_id_migration_requires_source_removed_from_all_suites(tmp_path: Path) -> None:
    collection = _collection()
    manifest = collection.load_relocation_manifest(_parameter_migration_manifest(tmp_path))
    before = {
        "schema_version": 1,
        "audited_base_sha": AUDITED_BASE,
        "suites": [{"suite": "root", "node_ids": [OLD_METADATA_SCHEMA_NODE]}],
    }
    after = {
        "schema_version": 1,
        "audited_base_sha": AUDITED_BASE,
        "suites": [
            {"suite": "root", "node_ids": [CURRENT_METADATA_SCHEMA_NODE]},
            {"suite": "other", "node_ids": [OLD_METADATA_SCHEMA_NODE]},
        ],
    }
    with pytest.raises(ValueError, match="source remains"):
        collection.compare_reports(before, after, relocation_manifest=manifest)


@pytest.mark.parametrize("ambiguous_side", ["source", "target"])
def test_parameter_id_migration_rejects_cross_suite_ambiguity(
    tmp_path: Path,
    ambiguous_side: str,
) -> None:
    collection = _collection()
    manifest = collection.load_relocation_manifest(_parameter_migration_manifest(tmp_path))
    before_suites = [{"suite": "root", "node_ids": [OLD_METADATA_SCHEMA_NODE]}]
    after_suites = [{"suite": "root", "node_ids": [CURRENT_METADATA_SCHEMA_NODE]}]
    if ambiguous_side == "source":
        before_suites.append({"suite": "other", "node_ids": [OLD_METADATA_SCHEMA_NODE]})
    else:
        after_suites.append({"suite": "other", "node_ids": [CURRENT_METADATA_SCHEMA_NODE]})
    with pytest.raises(ValueError, match="ambiguous"):
        collection.compare_reports(
            {"schema_version": 1, "audited_base_sha": AUDITED_BASE, "suites": before_suites},
            {"schema_version": 1, "audited_base_sha": AUDITED_BASE, "suites": after_suites},
            relocation_manifest=manifest,
        )


def test_parameter_migration_fails_when_historical_source_is_missing(
    tmp_path: Path,
) -> None:
    collection = _collection()
    manifest = collection.load_relocation_manifest(_parameter_migration_manifest(tmp_path))
    with pytest.raises(ValueError, match="source is missing from the before report"):
        collection.compare_reports(
            _report(), _report(CURRENT_METADATA_SCHEMA_NODE), relocation_manifest=manifest
        )


def test_require_exact_allows_inert_migrations_for_identical_suite_node_sets(
    tmp_path: Path,
) -> None:
    collection = _collection()
    manifest_path = _parameter_migration_manifest(tmp_path)
    before_path = tmp_path / "before.json"
    after_path = tmp_path / "after.json"
    unchanged = _report(CURRENT_METADATA_SCHEMA_NODE)
    before_path.write_text(json.dumps(unchanged), encoding="utf-8")
    after_path.write_text(json.dumps(unchanged), encoding="utf-8")
    assert (
        collection.main(
            [
                "compare",
                str(before_path),
                str(after_path),
                "--relocations",
                str(manifest_path),
                "--require-exact",
            ]
        )
        == 0
    )


@pytest.mark.parametrize(
    "suites",
    [
        [{"suite": "root", "node_ids": [OLD_METADATA_SCHEMA_NODE, OLD_METADATA_SCHEMA_NODE]}],
        [
            {"suite": "root", "node_ids": [OLD_METADATA_SCHEMA_NODE]},
            {"suite": "root", "node_ids": [OLD_METADATA_SCHEMA_NODE]},
        ],
    ],
)
def test_parameter_id_migration_rejects_duplicate_collection_identity(
    tmp_path: Path,
    suites: list[dict[str, object]],
) -> None:
    collection = _collection()
    manifest = collection.load_relocation_manifest(_parameter_migration_manifest(tmp_path))
    before = {"schema_version": 1, "audited_base_sha": AUDITED_BASE, "suites": suites}
    with pytest.raises(ValueError, match="duplicate"):
        collection.compare_reports(
            before,
            _report(CURRENT_METADATA_SCHEMA_NODE),
            relocation_manifest=manifest,
        )


@pytest.mark.parametrize(
    "migrations",
    [
        {"../tests/test.py::test[x]": "../tests/test.py::test[y]"},
        {"tests/test.py::test[x]": "tests/other.py::test[y]"},
        {"tests/test.py::test[x]": "tests/test.py::other[y]"},
        {"tests/test.py::test[x]": "tests/test.py::test[x]"},
        {"tests/test.py::test": "tests/test.py::test[y]"},
        {
            "tests/test.py::test[x]": "tests/test.py::test[z]",
            "tests/test.py::other[y]": "tests/test.py::test[z]",
        },
        {
            "tests/test.py::test[x]": "tests/test.py::test[y]",
            "tests/test.py::test[y]": "tests/test.py::test[z]",
        },
    ],
)
def test_parameter_id_manifest_rejects_out_of_scope_or_ambiguous_migrations(
    tmp_path: Path,
    migrations: dict[str, str],
) -> None:
    collection = _collection()
    with pytest.raises(ValueError):
        collection.load_relocation_manifest(
            _write_relocation_manifest(tmp_path, {}, parameter_migrations=migrations)
        )


def test_require_exact_rejects_parameter_id_migrations(tmp_path: Path) -> None:
    collection = _collection()
    manifest_path = _parameter_migration_manifest(tmp_path)
    before_path = tmp_path / "before.json"
    after_path = tmp_path / "after.json"
    before_path.write_text(json.dumps(_report(OLD_METADATA_SCHEMA_NODE)), encoding="utf-8")
    after_path.write_text(json.dumps(_report(CURRENT_METADATA_SCHEMA_NODE)), encoding="utf-8")
    assert (
        collection.main(
            [
                "compare",
                str(before_path),
                str(after_path),
                "--relocations",
                str(manifest_path),
                "--require-exact",
            ]
        )
        == 1
    )
