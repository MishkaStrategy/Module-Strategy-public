from __future__ import annotations

from datetime import UTC, datetime

import pytest

from market_data_ingestion.canonical import canonical_json
from market_data_ingestion.timeframes import (
    floor_timeframe,
    is_timeframe_aligned,
)

hypothesis = pytest.importorskip("hypothesis")
given = hypothesis.given
st = pytest.importorskip("hypothesis.strategies")


_AWARE_DATETIMES = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2035, 12, 31, 23, 59, 59, 999999),
    timezones=st.just(UTC),
)
_TIMEFRAMES = st.sampled_from(("1m", "5m", "15m", "1h", "4h", "1d"))


@given(value=_AWARE_DATETIMES, timeframe=_TIMEFRAMES)
def test_floor_timeframe_is_aligned_and_idempotent(
    value: datetime,
    timeframe: str,
) -> None:
    floored = floor_timeframe(value, timeframe)

    assert floored <= value
    assert is_timeframe_aligned(floored, timeframe)
    assert floor_timeframe(floored, timeframe) == floored


@given(
    keys=st.lists(
        st.text(
            alphabet=st.characters(
                blacklist_categories=("Cs",),
                blacklist_characters=("\x00",),
            ),
            min_size=1,
            max_size=12,
        ),
        min_size=1,
        max_size=12,
        unique=True,
    )
)
def test_canonical_json_is_independent_of_mapping_insertion_order(
    keys: list[str],
) -> None:
    forward = {key: index for index, key in enumerate(keys)}
    reverse = {key: forward[key] for key in reversed(keys)}

    assert canonical_json(forward) == canonical_json(reverse)
