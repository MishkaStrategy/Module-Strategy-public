from __future__ import annotations

import pytest

from paper_execution.ids import ClientOrderIdFactory


def test_factory_rejects_non_string_or_unsafe_namespace() -> None:
    with pytest.raises(TypeError, match="namespace must be a string"):
        ClientOrderIdFactory(None)
    for namespace in ("", "unsafe namespace", "ü", "/tmp"):
        with pytest.raises(ValueError, match="safe characters"):
            ClientOrderIdFactory(namespace)


def test_factory_length_and_revision_types_are_strict() -> None:
    with pytest.raises(TypeError, match="max_length must be int"):
        ClientOrderIdFactory(max_length=True)
    with pytest.raises(ValueError, match=r"\[20, 128\]"):
        ClientOrderIdFactory(max_length=129)

    factory = ClientOrderIdFactory()
    with pytest.raises(TypeError, match="revision must be int"):
        factory.create(
            plan_id="plan",
            intent_id="entry",
            role="entry",
            revision=True,
        )


def test_huge_revision_fails_instead_of_overflowing_output() -> None:
    factory = ClientOrderIdFactory("pevr", 20)
    with pytest.raises(ValueError, match="insufficient room"):
        factory.create(
            plan_id="plan",
            intent_id="entry",
            role="entry",
            revision=10**20,
        )


def test_factory_inputs_are_bounded(monkeypatch) -> None:
    import paper_execution.ids as ids_module

    monkeypatch.setattr(ids_module, "MAX_FACTORY_INPUT_LENGTH", 4)
    factory = ClientOrderIdFactory()
    with pytest.raises(ValueError, match="plan_id has invalid length"):
        factory.create(
            plan_id="12345",
            intent_id="i",
            role="entry",
        )


def test_output_postconditions_hold_at_minimum_length() -> None:
    factory = ClientOrderIdFactory("long_namespace", 20)
    result = factory.create(
        plan_id="plan",
        intent_id="entry",
        role="entry",
        revision=0,
    )
    assert len(result) == 20
    assert result.startswith("long-0-")
    assert len(result.rsplit("-", 1)[-1]) >= 12
