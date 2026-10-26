from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from modular_modules.entry_model import EntryModelModule, EntryPlan
from modular_modules.entry_model.primitives import decimal_text

from .support import bar_at, next_feature, payload, setup_feature, view_at


@settings(max_examples=75, deadline=None)
@given(
    confirmation=st.integers(min_value=1_001, max_value=1_000_000),
    distance=st.integers(min_value=1, max_value=1_000),
    fraction_int=st.integers(min_value=1, max_value=9_999),
)
def test_retracement_buy_sell_symmetry_property(
    confirmation: int,
    distance: int,
    fraction_int: int,
) -> None:
    fraction = Decimal(fraction_int) / Decimal("10000")
    buy_feature = setup_feature(
        value=payload(
            setup_id="buy",
            side="buy",
            confirmed_at=bar_at(0).close_time,
            confirmation_price=str(confirmation),
            extreme_price=str(confirmation - distance),
        ),
        feature_id="buy-feature",
    )
    sell_feature = setup_feature(
        value=payload(
            setup_id="sell",
            side="sell",
            confirmed_at=bar_at(0).close_time,
            confirmation_price=str(confirmation),
            extreme_price=str(confirmation + distance),
        ),
        feature_id="sell-feature",
    )
    module = EntryModelModule(retracement_fraction=fraction)
    decision = module.on_bar(view_at(0, features=(buy_feature, sell_feature)))
    plans = {
        plan.side.value: plan
        for plan in (EntryPlan.from_feature_value(feature.value) for feature in decision.features)
    }
    expected_distance = fraction * Decimal(distance)
    assert Decimal(confirmation) - plans["buy"].price == expected_distance
    assert plans["sell"].price - Decimal(confirmation) == expected_distance


@settings(max_examples=50, deadline=None)
@given(
    fraction=st.decimals(
        min_value=Decimal("0.000000001"),
        max_value=Decimal("0.999999999"),
        allow_nan=False,
        allow_infinity=False,
        places=9,
    )
)
def test_decimal_config_and_plan_roundtrip_property(fraction: Decimal) -> None:
    module = EntryModelModule(retracement_fraction=fraction)
    decision = module.on_bar(view_at(0, features=(next_feature(0),)))
    plan = EntryPlan.from_feature_value(decision.features[0].value)
    roundtrip = EntryPlan.from_feature_value(json.loads(json.dumps(plan.to_feature_value())))
    assert roundtrip == plan
    assert module.parameters["retracement_fraction"] == decimal_text(fraction)


@settings(max_examples=50, deadline=None)
@given(
    keys=st.lists(
        st.text(
            alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=8
        ),
        min_size=1,
        max_size=8,
        unique=True,
    )
)
def test_metadata_order_does_not_change_plan_identity(keys: list[str]) -> None:
    values = {key: index for index, key in enumerate(keys)}
    reversed_values = dict(reversed(list(values.items())))
    feature_a = setup_feature(
        value=payload(setup_id="same", metadata=values),
        feature_id="a",
    )
    feature_b = setup_feature(
        value=payload(setup_id="same", metadata=reversed_values),
        feature_id="b",
    )
    plan_a = EntryPlan.from_feature_value(
        EntryModelModule().on_bar(view_at(0, features=(feature_a,))).features[0].value
    )
    plan_b = EntryPlan.from_feature_value(
        EntryModelModule().on_bar(view_at(0, features=(feature_b,))).features[0].value
    )
    assert plan_a.plan_id == plan_b.plan_id
    assert plan_a.setup_fingerprint == plan_b.setup_fingerprint


def test_hash_seed_does_not_change_plan_id() -> None:
    code = r"""
from modular_modules.entry_model import EntryModelModule
from tests.support import next_feature, view_at
m = EntryModelModule(allowed_entry_kinds=("market", "limit_retracement", "stop_market"))
d = m.on_bar(view_at(0, features=(next_feature(0),)))
print(d.features[0].feature_id)
"""
    root = Path(__file__).resolve().parents[1]
    ids = []
    for seed in ("1", "2", "123", "random"):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        env["PYTHONPATH"] = os.pathsep.join([str(root), str(root / "src")])
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        ids.append(result.stdout.strip())
    assert len(set(ids)) == 1


def test_deterministic_replay_digest_before_and_after_restore() -> None:
    first = EntryModelModule(max_setup_age_seconds=600)
    outputs: list[str] = []
    for index in range(5):
        decision = first.on_bar(view_at(index, features=(next_feature(index),)))
        outputs.extend(feature.feature_id or "" for feature in decision.features)
    state = json.loads(json.dumps(first.snapshot_state()))
    restored = EntryModelModule(max_setup_age_seconds=600)
    restored.restore_state(state)
    for index in range(5, 10):
        decision = restored.on_bar(view_at(index, features=(next_feature(index),)))
        outputs.extend(feature.feature_id or "" for feature in decision.features)

    uninterrupted = EntryModelModule(max_setup_age_seconds=600)
    expected: list[str] = []
    for index in range(10):
        decision = uninterrupted.on_bar(view_at(index, features=(next_feature(index),)))
        expected.extend(feature.feature_id or "" for feature in decision.features)
    assert outputs == expected


def test_subsecond_staleness_boundary() -> None:
    bar = bar_at(0)
    exact = setup_feature(
        value=payload(confirmed_at=bar.close_time - timedelta(microseconds=500_000)),
        created_at=bar.close_time,
        valid_until=bar.close_time,
    )
    accepted = EntryModelModule(max_setup_age_seconds="0.5").on_bar(view_at(0, features=(exact,)))
    assert accepted.features[0].key == "entry_model.plan"

    stale = setup_feature(
        value=payload(confirmed_at=bar.close_time - timedelta(microseconds=500_001)),
        created_at=bar.close_time,
        feature_id="stale",
        valid_until=bar.close_time,
    )
    rejected = EntryModelModule(max_setup_age_seconds="0.5").on_bar(view_at(0, features=(stale,)))
    assert rejected.features[0].value["reason"] == "entry_model.stale_setup"
