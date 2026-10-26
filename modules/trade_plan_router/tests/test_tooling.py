from __future__ import annotations

from pathlib import Path
import sys

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from check_path_collisions import find_collisions  # noqa: E402


def test_path_collision_check_detects_case_and_unicode_collisions(
    tmp_path,
    monkeypatch,
):
    synthetic_paths = (
        tmp_path / "Docs",
        tmp_path / "docs",
        tmp_path / "Docs" / "Cafe\u0301.md",
        tmp_path / "docs" / "CAF\u00c9.md",
    )

    def fake_rglob(root: Path, pattern: str):
        assert root == tmp_path
        assert pattern == "*"
        return iter(synthetic_paths)

    monkeypatch.setattr(Path, "rglob", fake_rglob)

    collisions = find_collisions(tmp_path)
    assert collisions
    flattened = {path for _, paths in collisions for path in paths}
    assert "Docs" in flattened
    assert "docs" in flattened
    assert "Docs/Cafe\u0301.md" in flattened
    assert "docs/CAF\u00c9.md" in flattened


def test_path_collision_check_ignores_generated_directories(tmp_path):
    for directory in ("build", "dist", "__pycache__", ".pytest_cache", ".venv-test"):
        first = tmp_path / directory / "A.py"
        second = tmp_path / directory / "a.py"
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_text("a", encoding="utf-8")
        second.write_text("b", encoding="utf-8")
    assert find_collisions(tmp_path) == ()


def test_documentation_links_and_config_example_are_release_complete():
    import re

    import yaml

    from modular_strategy_trade_plan_router.config import RouterConfiguration

    module_root = Path(__file__).resolve().parents[1]
    readme = (module_root / "README.md").read_text(encoding="utf-8")
    local_links = re.findall(r"\[[^\]]+\]\(([^)]+)\)", readme)
    assert local_links
    for relative in local_links:
        if "://" in relative or relative.startswith("#"):
            continue
        assert (module_root / relative).is_file(), relative

    payload = yaml.safe_load((module_root / "config.example.yaml").read_text(encoding="utf-8"))
    config = RouterConfiguration.from_mapping(payload)
    assert config.schema_version == "1"
    assert config.require_portfolio_snapshot is True
    assert config.max_entry_candidates == 1000


def test_type_surface_audit_detects_missing_annotations(tmp_path):
    from type_surface_audit import audit_file

    path = tmp_path / "sample.py"
    path.write_text(
        "def public(value):\n    return value\n\ndef _private(value):\n    return value\n",
        encoding="utf-8",
    )
    findings = audit_file(path)
    assert len(findings) == 2
    assert any("argument value" in item for item in findings)
    assert any("return annotation" in item for item in findings)


def test_text_file_audit_detects_trailing_whitespace_and_missing_newline(tmp_path):
    from check_text_files import audit_tree

    path = tmp_path / "bad.py"
    path.write_bytes(b"value = 1  \nlast = 2")
    findings = audit_tree(tmp_path)
    assert any("trailing whitespace" in item for item in findings)
    assert any("missing final newline" in item for item in findings)


def test_text_file_audit_ignores_binary_and_generated_trees(tmp_path):
    from check_text_files import audit_tree

    (tmp_path / "image.bin").write_bytes(b"\x00\xff")
    generated = tmp_path / "build" / "bad.py"
    generated.parent.mkdir()
    generated.write_text("bad  ", encoding="utf-8")
    assert audit_tree(tmp_path) == ()
