from __future__ import annotations

import ast
import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from modular_core import Candle, Feature, MarketView

from modular_session_range import (
    ACTIVE_FEATURE,
    COMPLETED_FEATURE,
    MODULE_VERSION,
    STATE_SCHEMA_VERSION,
    SessionRangeConfigurationError,
    SessionRangeModule,
)

UTC = UTC


def candle(
    ts: datetime,
    *,
    price: float = 100.0,
    tf: str = "5m",
    symbol: str = "BTCUSDT",
    volume: float = 1.0,
) -> Candle:
    return Candle(
        timestamp=ts,
        open=price,
        high=price + 1,
        low=price - 1,
        close=price + 0.25,
        volume=volume,
        symbol=symbol,
        timeframe=tf,
    )


def view(bars: list[Candle], *, gap: bool = False) -> MarketView:
    return MarketView(
        base_bars=bars,
        timeframe_bars={},
        index=len(bars) - 1,
        cash=1000,
        equity=1000,
        position=None,
        pending_orders=(),
        data_gap=gap,
    )


def module(**extra: object) -> SessionRangeModule:
    row = {"name": "ny", "timezone": "UTC", "start": "00:00", "end": "00:15", **extra}
    return SessionRangeModule([row])


def run(m: SessionRangeModule, bars: list[Candle], gap_at: set[int] | None = None):
    out = []
    seen = []
    for i, b in enumerate(bars):
        seen.append(b)
        out.append(m.on_bar(view(seen, gap=i in (gap_at or set()))))
    return out


def test_version_and_manifest() -> None:
    m = module()
    assert MODULE_VERSION == "0.2.0"
    assert STATE_SCHEMA_VERSION == "2"
    assert m.manifest is not None
    assert m.manifest.version == MODULE_VERSION
    assert m.manifest.produced_features == (ACTIVE_FEATURE, COMPLETED_FEATURE)


@pytest.mark.parametrize("bad", [None, "x", 1, [], {}])
def test_sessions_shape_rejected(bad: object) -> None:
    with pytest.raises(SessionRangeConfigurationError):
        SessionRangeModule(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["", " bad", "a/b", "x" * 65])
def test_session_names_rejected(bad: str) -> None:
    with pytest.raises(SessionRangeConfigurationError):
        SessionRangeModule([{"name": bad, "timezone": "UTC", "start": "00:00", "end": "00:15"}])


@pytest.mark.parametrize("time_value", ["1", "24:00", "12:60", "xx:yy"])
def test_time_rejected(time_value: str) -> None:
    with pytest.raises(SessionRangeConfigurationError):
        SessionRangeModule([{"name": "x", "timezone": "UTC", "start": time_value, "end": "00:15"}])


def test_completed_feature_and_no_lookahead() -> None:
    bars = [candle(datetime(2026, 7, 20, 0, i * 5, tzinfo=UTC), price=100 + i) for i in range(3)]
    partial = run(module(), bars[:2])[-1]
    assert not any(f.key == COMPLETED_FEATURE for f in partial.features)
    final = run(module(), bars)[-1]
    completed = [f for f in final.features if f.key == COMPLETED_FEATURE]
    assert len(completed) == 1
    assert completed[0].created_at == bars[-1].close_time


def test_duplicate_is_idempotent() -> None:
    m = module()
    b = candle(datetime(2026, 7, 20, tzinfo=UTC))
    first = m.on_bar(view([b]))
    second = m.on_bar(view([b, b]))
    assert first.features and not second.features
    assert "session_range.duplicate_bar" in second.skips


def test_conflicting_duplicate_invalidates() -> None:
    m = module()
    ts = datetime(2026, 7, 20, tzinfo=UTC)
    b1 = candle(ts)
    b2 = candle(ts, price=101)
    m.on_bar(view([b1]))
    d = m.on_bar(view([b1, b2]))
    assert "session_range.conflicting_duplicate" in d.skips
    assert m.snapshot_state()["active"] == []


def test_out_of_order_no_mutation() -> None:
    m = module()
    b1 = candle(datetime(2026, 7, 20, 0, 5, tzinfo=UTC))
    m.on_bar(view([b1]))
    before = copy.deepcopy(m.snapshot_state())
    b0 = candle(datetime(2026, 7, 20, 0, 0, tzinfo=UTC))
    d = m.on_bar(view([b1, b0]))
    assert "session_range.out_of_order_bar" in d.skips
    assert m.snapshot_state() == before


def test_gap_invalidates() -> None:
    bars = [
        candle(datetime(2026, 7, 20, 0, 0, tzinfo=UTC)),
        candle(datetime(2026, 7, 20, 0, 10, tzinfo=UTC)),
    ]
    d = run(module(), bars)[-1]
    assert "session_range.data_gap" in d.skips
    assert not any(f.key == COMPLETED_FEATURE for f in d.features)


def test_checkpoint_restore_matches_uninterrupted() -> None:
    bars = [candle(datetime(2026, 7, 20, 0, i * 5, tzinfo=UTC), price=100 + i) for i in range(3)]
    uninterrupted = run(module(), bars)[-1].features
    m = module()
    m.on_bar(view([bars[0]]))
    snap = json.loads(json.dumps(m.snapshot_state()))
    restored = module()
    restored.restore_state(snap)
    restored.on_bar(view([bars[0], bars[1]]))
    got = restored.on_bar(view(bars)).features
    assert [f.value for f in got if f.key == COMPLETED_FEATURE] == [
        f.value for f in uninterrupted if f.key == COMPLETED_FEATURE
    ]


def test_restore_is_atomic_on_failure() -> None:
    m = module()
    b = candle(datetime(2026, 7, 20, tzinfo=UTC))
    m.on_bar(view([b]))
    before = copy.deepcopy(m.snapshot_state())
    bad = copy.deepcopy(before)
    bad["active"][0]["end_utc"] = "2026-07-20T00:10:00+00:00"
    with pytest.raises(ValueError):
        m.restore_state(bad)
    assert m.snapshot_state() == before


def test_config_fingerprint_rejected() -> None:
    m = module()
    snap = m.snapshot_state()
    other = module(end="00:20")
    with pytest.raises(ValueError, match="fingerprint"):
        other.restore_state(snap)


def test_schema_v1_migration_supported() -> None:
    m = module()
    b = candle(datetime(2026, 7, 20, tzinfo=UTC))
    m.on_bar(view([b]))
    snap = dict(m.snapshot_state())
    snap["schema_version"] = "1"
    snap.pop("config_fingerprint", None)
    snap.pop("module_version", None)
    restored = module()
    restored.restore_state(snap)
    assert restored.snapshot_state()["active"]


def test_feature_ids_include_timeframe() -> None:
    ts = datetime(2026, 7, 20, tzinfo=UTC)
    a = run(module(), [candle(ts, tf="5m")])[0].features[0].feature_id
    b = run(module(), [candle(ts, tf="15m")])[0].features[-1].feature_id
    assert a != b and ":5m:" in a and ":15m:" in b


@pytest.mark.parametrize("weekday", range(7))
def test_weekday_configuration(weekday: int) -> None:
    m = SessionRangeModule(
        [{"name": "x", "timezone": "UTC", "start": "00:00", "end": "00:05", "weekdays": [weekday]}]
    )
    day = datetime(2026, 7, 20 + weekday, tzinfo=UTC)
    d = m.on_bar(view([candle(day)]))
    assert isinstance(d.features, tuple)


@pytest.mark.parametrize("minimum", ["0", "1", "2.0000"])
def test_decimal_thresholds(minimum: str) -> None:
    m = module(minimum_range=minimum)
    assert str(m.parameters["sessions"][0]["minimum_range"]) == minimum


def test_multi_instance_isolation() -> None:
    a = module()
    b = module()
    bar = candle(datetime(2026, 7, 20, tzinfo=UTC))
    a.on_bar(view([bar]))
    assert a.snapshot_state()["active"] and not b.snapshot_state()["active"]


def test_parallel_symbols_are_independent() -> None:
    m = module()
    ts = datetime(2026, 7, 20, tzinfo=UTC)
    m.on_bar(view([candle(ts, symbol="BTCUSDT")]))
    m.on_bar(view([candle(ts, symbol="ETHUSDT")]))
    assert len(m.snapshot_state()["active"]) == 2


def test_cross_midnight_session() -> None:
    m = SessionRangeModule([{"name": "x", "timezone": "UTC", "start": "23:55", "end": "00:05"}])
    bars = [
        candle(datetime(2026, 7, 20, 23, 55, tzinfo=UTC)),
        candle(datetime(2026, 7, 21, 0, 0, tzinfo=UTC)),
    ]
    assert any(f.key == COMPLETED_FEATURE for f in run(m, bars)[-1].features)


def test_dst_summer_and_winter() -> None:
    spec = {"name": "ny", "timezone": "America/New_York", "start": "00:00", "end": "00:05"}
    for ts in [datetime(2026, 7, 20, 4, 0, tzinfo=UTC), datetime(2026, 1, 20, 5, 0, tzinfo=UTC)]:
        assert SessionRangeModule([spec]).on_bar(view([candle(ts)])).features


def test_snapshot_json_compatible_and_bounded() -> None:
    m = module()
    run(m, [candle(datetime(2026, 7, 20, 0, i * 5, tzinfo=UTC)) for i in range(3)])
    encoded = json.dumps(m.snapshot_state(), sort_keys=True)
    assert len(encoded) < 10000


def test_deterministic_replay_digest() -> None:
    bars = [candle(datetime(2026, 7, 20, 0, i * 5, tzinfo=UTC), price=100 + i) for i in range(3)]
    digests = []
    for _ in range(3):
        m = module()
        run(m, bars)
        digests.append(json.dumps(m.snapshot_state(), sort_keys=True))
    assert len(set(digests)) == 1


def test_entry_point_declared_after_install() -> None:
    # Source-tree test validates metadata declaration; clean-wheel smoke validates discovery.
    root = Path(__file__).parents[1]
    candidate = root / "pyproject.toml"
    if not candidate.exists():
        candidate = root / "module" / "pyproject.toml"
    text = candidate.read_text()
    assert 'session_range = "modular_session_range:SessionRangeModule"' in text


def test_full_day_session_completed_feature_validity() -> None:
    m = SessionRangeModule([{"name": "all", "timezone": "UTC", "start": "00:00", "end": "00:00"}])
    bars = [candle(datetime(2026, 7, 20, 0, 0, tzinfo=UTC), tf="1d")]
    completed = [
        feature for feature in m.on_bar(view(bars)).features if feature.key == COMPLETED_FEATURE
    ]
    assert len(completed) == 1
    assert completed[0].valid_until >= completed[0].created_at


def test_runtime_source_contains_no_assert_statements() -> None:
    root = Path(__file__).parents[1]
    component = root if (root / "src").is_dir() else root / "module"
    source = (component / "src/modular_session_range/module.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert not any(isinstance(node, ast.Assert) for node in ast.walk(tree))


@pytest.mark.parametrize(
    ("feature_id", "valid_until", "message"),
    [
        (None, datetime(2026, 7, 20, 0, 5, tzinfo=UTC), "feature_id"),
        ("deterministic-id", None, "valid_until"),
    ],
)
def test_stored_feature_invariants_survive_python_optimized_mode(
    feature_id: str | None, valid_until: datetime | None, message: str
) -> None:
    feature = Feature(
        key=COMPLETED_FEATURE,
        source_module="session_range",
        created_at=datetime(2026, 7, 20, tzinfo=UTC),
        valid_until=valid_until,
        value={"complete": True},
        symbol="BTCUSDT",
        timeframe="5m",
        feature_id=feature_id,
    )
    with pytest.raises(ValueError, match=message):
        SessionRangeModule._store_feature(feature)  # noqa: SLF001
