from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import pytest

from modular_backtest import provenance as provenance_module
from modular_backtest._version import APPLICATION_VERSION
from modular_backtest.provenance import (
    ProvenanceError,
    build_source_provenance,
    resolve_source_commit_sha,
    source_tree_sha256,
)


def test_source_tree_digest_is_stable_and_content_sensitive(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "b.py").write_text("B = 2\n", encoding="utf-8")
    (package / "a.py").write_text("A = 1\n", encoding="utf-8")
    (package / "ignored.json").write_text("{}", encoding="utf-8")

    first = source_tree_sha256(package, "example")
    second = source_tree_sha256(package, "example")
    (package / "a.py").write_text("A = 3\n", encoding="utf-8")
    changed = source_tree_sha256(package, "example")

    assert len(first) == 64
    assert first == second
    assert first != changed


def test_source_tree_digest_includes_py_typed_and_ignores_symlinks(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    source = package / "module.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    before = source_tree_sha256(package, "example")
    (package / "py.typed").write_text("", encoding="utf-8")
    with_marker = source_tree_sha256(package, "example")
    (package / "linked.py").symlink_to(source)

    assert before != with_marker
    assert source_tree_sha256(package, "example") == with_marker


def test_source_tree_digest_rejects_empty_or_invalid_roots(tmp_path: Path) -> None:
    with pytest.raises(ProvenanceError, match="existing directory"):
        source_tree_sha256(tmp_path / "missing", "example")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ProvenanceError, match="no package source"):
        source_tree_sha256(empty, "example")
    (empty / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    with pytest.raises(ProvenanceError, match="package_name"):
        source_tree_sha256(empty, "")


def test_explicit_source_commit_is_validated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MODULAR_STRATEGY_SOURCE_SHA", "A" * 40)
    assert resolve_source_commit_sha(tmp_path) == "a" * 40

    monkeypatch.setenv("MODULAR_STRATEGY_SOURCE_SHA", "not-a-sha")
    with pytest.raises(ProvenanceError, match="40 hexadecimal"):
        resolve_source_commit_sha(tmp_path)


def test_source_commit_is_optional_when_git_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MODULAR_STRATEGY_SOURCE_SHA", raising=False)

    def missing_git(*args: object, **kwargs: object) -> NoReturn:
        raise FileNotFoundError("git not installed")

    monkeypatch.setattr(provenance_module.subprocess, "run", missing_git)

    assert resolve_source_commit_sha(tmp_path) is None


def test_package_provenance_contains_exact_source_hashes() -> None:
    provenance = build_source_provenance()

    assert provenance.application_version == APPLICATION_VERSION
    assert len(provenance.application_source_sha256) == 64
    assert len(provenance.core_source_sha256) == 64
    assert len(provenance.application_identity_sha256) == 64
    if provenance.source_commit_sha is not None:
        assert len(provenance.source_commit_sha) == 40
    assert provenance.to_dict()["application_version"] == APPLICATION_VERSION
