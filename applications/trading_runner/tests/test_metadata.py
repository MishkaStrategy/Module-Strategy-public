from __future__ import annotations

from collections.abc import Mapping

import pytest

from modular_trading_runner.errors import CheckpointIntegrityError
from modular_trading_runner.metadata import (
    CompositeRuntimeMetadata,
    NamedRuntimeMetadata,
)


class Component:
    def __init__(self, value: int, *, fail_once: bool = False) -> None:
        self.value = value
        self.fail_once = fail_once

    def snapshot_runtime_metadata(self) -> Mapping[str, object]:
        return {"value": self.value}

    def restore_runtime_metadata(self, state: Mapping[str, object]) -> None:
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("restore failed")
        self.value = int(state["value"])


def test_composite_metadata_is_namespaced_and_deterministic() -> None:
    lifecycle = Component(1)
    cycle = Component(2)
    composite = CompositeRuntimeMetadata(
        (
            NamedRuntimeMetadata("market_cycle", cycle),
            NamedRuntimeMetadata("runtime", lifecycle),
        )
    )
    assert composite.namespaces == ("market_cycle", "runtime")
    assert composite.snapshot_runtime_metadata() == {
        "market_cycle": {"value": 2},
        "runtime": {"value": 1},
    }
    composite.restore_runtime_metadata(
        {
            "runtime": {"value": 10},
            "market_cycle": {"value": 20},
        }
    )
    assert lifecycle.value == 10
    assert cycle.value == 20


def test_composite_metadata_rejects_namespace_drift() -> None:
    composite = CompositeRuntimeMetadata((NamedRuntimeMetadata("runtime", Component(1)),))
    with pytest.raises(CheckpointIntegrityError, match="namespace mismatch"):
        composite.restore_runtime_metadata({"unexpected": {"value": 2}})


def test_composite_metadata_rolls_back_prior_components() -> None:
    first = Component(1)
    second = Component(2, fail_once=True)
    composite = CompositeRuntimeMetadata(
        (
            NamedRuntimeMetadata("first", first),
            NamedRuntimeMetadata("second", second),
        )
    )
    with pytest.raises(CheckpointIntegrityError, match="restore failed"):
        composite.restore_runtime_metadata(
            {
                "first": {"value": 10},
                "second": {"value": 20},
            }
        )
    assert first.value == 1
    assert second.value == 2


def test_duplicate_metadata_namespace_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicates"):
        CompositeRuntimeMetadata(
            (
                NamedRuntimeMetadata("runtime", Component(1)),
                NamedRuntimeMetadata("runtime", Component(2)),
            )
        )
