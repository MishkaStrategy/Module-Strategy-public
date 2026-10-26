from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest

from modular_core.extensions import (
    AtomicJsonStateRepository,
    InMemoryStateRepository,
)


@pytest.mark.parametrize(
    "key",
    ["", "   ", ".", "..", "../escape", "nested/state", "nested\\state"],
)
def test_in_memory_state_load_validates_key_like_save_and_delete(key: str) -> None:
    repository = InMemoryStateRepository()

    with pytest.raises(ValueError, match="safe relative name"):
        repository.load(key)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ('{"value":1,"value":2}', "duplicate JSON key"),
        ('{"value":NaN}', "non-finite JSON constant"),
        ('{"value":Infinity}', "non-finite JSON constant"),
        ('{"value":-Infinity}', "non-finite JSON constant"),
    ],
)
def test_atomic_state_load_rejects_ambiguous_json(
    tmp_path: Path,
    payload: str,
    message: str,
) -> None:
    repository = AtomicJsonStateRepository(tmp_path)
    (tmp_path / "state.json").write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        repository.load("state")


def test_atomic_state_load_rejects_non_object_top_level(tmp_path: Path) -> None:
    repository = AtomicJsonStateRepository(tmp_path)
    (tmp_path / "state.json").write_text("[1,2,3]", encoding="utf-8")

    with pytest.raises(ValueError, match="stored state must be a JSON object"):
        repository.load("state")


@pytest.mark.parametrize("repository_kind", ["memory", "disk"])
def test_state_repositories_return_detached_immutable_values(
    tmp_path: Path,
    repository_kind: str,
) -> None:
    repository = (
        InMemoryStateRepository()
        if repository_kind == "memory"
        else AtomicJsonStateRepository(tmp_path)
    )
    source = {"nested": {"values": [1, 2]}}

    repository.save("state", source)
    loaded = repository.load("state")
    assert isinstance(loaded, MappingProxyType)
    assert loaded["nested"]["values"] == (1, 2)  # type: ignore[index]
    source["nested"]["values"].append(3)  # type: ignore[index,union-attr]
    assert loaded["nested"]["values"] == (1, 2)  # type: ignore[index]
