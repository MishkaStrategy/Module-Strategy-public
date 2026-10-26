from __future__ import annotations

import os
import subprocess
import sys
from copy import deepcopy
from decimal import Decimal, localcontext

from hypothesis import given, settings
from hypothesis import strategies as st

from modular_modules.entry_model import EntryModelModule, EntryPlan

from .support import payload, setup_feature, view_at


@settings(max_examples=60, deadline=None)
@given(
    confirmation=st.decimals(
        min_value="0.000001",
        max_value="1000000000",
        allow_nan=False,
        allow_infinity=False,
        places=12,
    ),
    distance=st.decimals(
        min_value="0.000001",
        max_value="1000000",
        allow_nan=False,
        allow_infinity=False,
        places=12,
    ),
    fraction=st.decimals(
        min_value="0.000001",
        max_value="0.999999",
        allow_nan=False,
        allow_infinity=False,
        places=12,
    ),
)
def test_buy_sell_symmetry(
    confirmation: Decimal,
    distance: Decimal,
    fraction: Decimal,
) -> None:
    if distance >= confirmation:
        distance = confirmation / Decimal("2")
    with localcontext() as context:
        context.prec = 80
        buy_extreme = confirmation - distance
        sell_extreme = confirmation + distance
    buy_value = payload(
        setup_id="buy",
        side="buy",
        confirmation_price=str(confirmation),
        extreme_price=str(buy_extreme),
    )
    sell_value = payload(
        setup_id="sell",
        side="sell",
        confirmation_price=str(confirmation),
        extreme_price=str(sell_extreme),
    )
    buy = EntryPlan.from_feature_value(
        EntryModelModule(retracement_fraction=fraction)
        .on_bar(view_at(features=(setup_feature(value=buy_value, feature_id="buy"),)))
        .features[0]
        .value
    )
    sell = EntryPlan.from_feature_value(
        EntryModelModule(retracement_fraction=fraction)
        .on_bar(view_at(features=(setup_feature(value=sell_value, feature_id="sell"),)))
        .features[0]
        .value
    )
    with localcontext() as context:
        context.prec = 80
        assert confirmation - buy.price == sell.price - confirmation


@settings(max_examples=40, deadline=None)
@given(setup_id=st.text(min_size=1, max_size=40).filter(lambda text: bool(text.strip())))
def test_unicode_setup_identity_is_deterministic(setup_id: str) -> None:
    value = payload(setup_id=setup_id)
    left = EntryModelModule().on_bar(view_at(features=(setup_feature(value=deepcopy(value)),)))
    right = EntryModelModule().on_bar(view_at(features=(setup_feature(value=deepcopy(value)),)))
    assert left.features[0].feature_id == right.features[0].feature_id


def test_python_hash_seed_does_not_change_plan_id() -> None:
    code = r"""
from datetime import datetime, timezone
from modular_core import Candle, Feature, FeatureView, MarketView
from modular_modules.entry_model import EntryModelModule
bar=Candle(datetime(2026,7,21,12,0,tzinfo=timezone.utc),100,101,99,100,1,"BTCUSDT","5m")
f=Feature(key="setup.confirmed",source_module="x",created_at=bar.close_time,value={"setup_id":"s","status":"confirmed","side":"buy","confirmed_at":bar.close_time.isoformat(),"confirmation_price":"100","extreme_price":"90","metadata":{"b":2,"a":1}},symbol="BTCUSDT",timeframe="5m",valid_until=bar.close_time,feature_id="f")
v=MarketView((bar,),{},0,10000,10000,None,(),features=FeatureView((f,),bar.close_time))
print(EntryModelModule().on_bar(v).features[0].feature_id)
"""
    outputs = []
    for seed in ("1", "42", "999"):
        outputs.append(
            subprocess.check_output(
                [sys.executable, "-c", code],
                text=True,
                env={**os.environ, "PYTHONHASHSEED": seed},
            ).strip()
        )
    assert len(set(outputs)) == 1
