from __future__ import annotations

import copy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from modular_core import Candle, FeatureView, MarketView

import modular_session_range.module as module_impl
from modular_session_range import ACTIVE_FEATURE, COMPLETED_FEATURE, SessionRangeModule
from modular_session_range.config import (
    SessionBoundaryError,
    SessionRangeConfigurationError,
    SessionRangeSpec,
    _decimal,
    _parse_time,
    parse_specs,
)
from modular_session_range.state import BarCursor, RangeAccumulator, StoredFeature


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
        high=price + 1.0,
        low=price - 1.0,
        close=price + 0.25,
        volume=volume,
        symbol=symbol,
        timeframe=tf,
    )


def view(
    bars: list[Candle],
    *,
    gap: bool = False,
    features: FeatureView | None = None,
) -> MarketView:
    return MarketView(
        base_bars=bars,
        timeframe_bars={},
        index=len(bars) - 1,
        cash=1000,
        equity=1000,
        position=None,
        pending_orders=(),
        data_gap=gap,
        features=features,
    )


def make_module(**extra: object) -> SessionRangeModule:
    payload = {"name": "x", "timezone": "UTC", "start": "00:00", "end": "00:15"}
    payload.update(extra)
    return SessionRangeModule([payload])


def run(module: SessionRangeModule, bars: list[Candle]) -> list[object]:
    seen: list[Candle] = []
    decisions = []
    for bar in bars:
        seen.append(bar)
        decisions.append(module.on_bar(view(seen)))
    return decisions


@pytest.mark.parametrize("name", ["ny", "NY_00-04", "asia.range", "x1"])
def test_valid_session_names(name: str) -> None:
    spec = parse_specs([{"name": name, "timezone": "UTC", "start": "00:00", "end": "00:05"}])[0]
    assert spec.name == name


def test_unknown_timezone_rejected() -> None:
    with pytest.raises(SessionRangeConfigurationError, match="unknown IANA timezone"):
        make_module(timezone="Mars/Olympus")


def test_private_time_parser_rejects_non_string() -> None:
    with pytest.raises(SessionRangeConfigurationError, match="must be a string"):
        _parse_time(123, "start")  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", object()])
def test_decimal_parser_rejects_nonfinite_and_unparseable(value: object) -> None:
    with pytest.raises(SessionRangeConfigurationError):
        _decimal(value, "value")


@pytest.mark.parametrize(
    "payload",
    [
        {"weekdays": []},
        {"weekdays": [0, 0]},
        {"weekdays": [-1]},
        {"minimum_bars": 0},
        {"minimum_range": "-0.1"},
        {"minimum_range": "2", "maximum_range": "2"},
        {"dst_fold": 2},
        {"emit_active": 1},
        {"minimum_bars": True},
        {"dst_fold": True},
        {"weekdays": "01234"},
        {"weekdays": [False]},
        {"unknown": 1},
    ],
)
def test_invalid_optional_config_rejected(payload: dict[str, object]) -> None:
    base: dict[str, object] = {
        "name": "x",
        "timezone": "UTC",
        "start": "00:00",
        "end": "00:05",
    }
    base.update(payload)
    with pytest.raises(SessionRangeConfigurationError):
        SessionRangeModule([base])


def test_missing_required_config_rejected() -> None:
    with pytest.raises(SessionRangeConfigurationError, match="missing required"):
        SessionRangeSpec.from_mapping({"name": "x"})


@pytest.mark.parametrize("rows", [[], [1], "bad"])
def test_parse_specs_rejects_wrong_shapes(rows: object) -> None:
    with pytest.raises(SessionRangeConfigurationError):
        parse_specs(rows)  # type: ignore[arg-type]


def test_duplicate_session_names_rejected() -> None:
    row = {"name": "x", "timezone": "UTC", "start": "00:00", "end": "00:05"}
    with pytest.raises(SessionRangeConfigurationError, match="unique"):
        SessionRangeModule([row, row])


def test_weekdays_are_normalized_and_mapping_is_immutable() -> None:
    spec = SessionRangeSpec.from_mapping(
        {"name": "x", "timezone": "UTC", "start": "00:00", "end": "00:05", "weekdays": [4, 1, 2]}
    )
    assert spec.weekdays == (1, 2, 4)
    mapping = spec.to_mapping()
    assert mapping["weekdays"] == (1, 2, 4)
    with pytest.raises(TypeError):
        mapping["name"] = "changed"  # type: ignore[index]


def test_cross_midnight_and_full_day_bounds() -> None:
    cross = SessionRangeSpec.from_mapping(
        {"name": "x", "timezone": "UTC", "start": "23:55", "end": "00:05"}
    )
    start, end = cross.bounds_for(date(2026, 7, 20))
    assert end - start == timedelta(minutes=10)
    full = SessionRangeSpec.from_mapping(
        {"name": "all", "timezone": "UTC", "start": "00:00", "end": "00:00"}
    )
    start, end = full.bounds_for(date(2026, 7, 20))
    assert end - start == timedelta(days=1)


def test_disabled_weekday_bounds_rejected() -> None:
    spec = SessionRangeSpec.from_mapping(
        {"name": "x", "timezone": "UTC", "start": "00:00", "end": "00:05", "weekdays": [0]}
    )
    with pytest.raises(SessionBoundaryError, match="disabled"):
        spec.bounds_for(date(2026, 7, 21))


def test_nonexistent_dst_boundary_rejected() -> None:
    spec = SessionRangeSpec.from_mapping(
        {
            "name": "ny",
            "timezone": "America/New_York",
            "start": "02:30",
            "end": "03:30",
        }
    )
    with pytest.raises(SessionBoundaryError, match="nonexistent"):
        spec.bounds_for(date(2026, 3, 8))


def test_ambiguous_dst_fold_is_deterministic() -> None:
    rows = {
        "name": "ny",
        "timezone": "America/New_York",
        "start": "01:30",
        "end": "02:30",
    }
    first = SessionRangeSpec.from_mapping({**rows, "dst_fold": 0}).bounds_for(date(2026, 11, 1))[0]
    second = SessionRangeSpec.from_mapping({**rows, "dst_fold": 1}).bounds_for(date(2026, 11, 1))[0]
    assert second - first == timedelta(hours=1)


def test_constructor_rejects_non_boolean_strict_flag() -> None:
    with pytest.raises(SessionRangeConfigurationError, match="must be boolean"):
        SessionRangeModule(
            [{"name": "x", "timezone": "UTC", "start": "00:00", "end": "00:05"}],
            strict_data_validation=1,  # type: ignore[arg-type]
        )


def test_lifecycle_hooks_are_safe(caplog: pytest.LogCaptureFixture) -> None:
    m = make_module()
    with caplog.at_level("INFO"):
        m.on_start()
        m.on_finish()
    assert "module_started" in caplog.text
    assert "module_finished" in caplog.text


def test_first_bar_gap_flag_is_diagnosed_but_exact_start_can_build() -> None:
    m = make_module()
    bar = candle(datetime(2026, 7, 20, tzinfo=UTC))
    decision = m.on_bar(view([bar], gap=True))
    assert "session_range.data_gap" in decision.skips
    assert any(feature.key == ACTIVE_FEATURE for feature in decision.features)


def test_dst_nonexistent_boundary_is_safe_skip() -> None:
    m = SessionRangeModule(
        [{"name": "ny", "timezone": "America/New_York", "start": "02:30", "end": "03:30"}]
    )
    decision = m.on_bar(view([candle(datetime(2026, 3, 8, 7, 30, tzinfo=UTC), tf="1h")]))
    assert any(skip.startswith("session_range.invalid_dst_boundary") for skip in decision.skips)


def test_unknown_timeframe_safe_failure_and_strict_mode() -> None:
    bar = candle(datetime(2026, 7, 20, tzinfo=UTC), tf="unknown")
    decision = make_module().on_bar(view([bar]))
    assert decision.skips == ("session_range.unknown_or_incomplete_interval",)
    strict = SessionRangeModule(
        [{"name": "x", "timezone": "UTC", "start": "00:00", "end": "00:15"}],
        strict_data_validation=True,
    )
    with pytest.raises(ValueError, match="unknown_or_incomplete_interval"):
        strict.on_bar(view([bar]))


def test_missing_start_and_partial_boundary_are_rejected() -> None:
    missing = make_module().on_bar(view([candle(datetime(2026, 7, 20, 0, 5, tzinfo=UTC))]))
    assert "session_range.missing_session_start:x" in missing.skips
    partial_module = SessionRangeModule(
        [{"name": "x", "timezone": "UTC", "start": "00:02", "end": "00:12"}]
    )
    partial = partial_module.on_bar(view([candle(datetime(2026, 7, 20, 0, 0, tzinfo=UTC))]))
    assert "session_range.boundary_misaligned:x" in partial.skips


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"minimum_bars": 4}, "session_range.minimum_bars_not_met:x"),
        ({"minimum_range": "100"}, "session_range.minimum_range_not_met:x"),
        ({"maximum_range": "1.5"}, "session_range.maximum_range_exceeded:x"),
    ],
)
def test_completion_filters(extra: dict[str, object], expected: str) -> None:
    bars = [candle(datetime(2026, 7, 20, 0, i * 5, tzinfo=UTC), price=100 + i) for i in range(3)]
    decision = run(make_module(**extra), bars)[-1]
    assert expected in decision.skips
    assert not any(feature.key == COMPLETED_FEATURE for feature in decision.features)


def test_emit_active_can_be_disabled() -> None:
    decision = make_module(emit_active=False).on_bar(
        view([candle(datetime(2026, 7, 20, tzinfo=UTC))])
    )
    assert not any(feature.key == ACTIVE_FEATURE for feature in decision.features)


def test_multiple_sessions_complete_independently() -> None:
    m = SessionRangeModule(
        [
            {"name": "first", "timezone": "UTC", "start": "00:00", "end": "00:05"},
            {"name": "second", "timezone": "UTC", "start": "00:05", "end": "00:10"},
        ]
    )
    first = m.on_bar(view([candle(datetime(2026, 7, 20, 0, 0, tzinfo=UTC))]))
    second_bar = candle(datetime(2026, 7, 20, 0, 5, tzinfo=UTC))
    second = m.on_bar(view([candle(datetime(2026, 7, 20, 0, 0, tzinfo=UTC)), second_bar]))
    assert [f.value["session"] for f in first.features if f.key == COMPLETED_FEATURE] == ["first"]
    assert [f.value["session"] for f in second.features if f.key == COMPLETED_FEATURE] == ["second"]


def test_in_session_gap_reason_is_session_scoped() -> None:
    m = make_module()
    first = candle(datetime(2026, 7, 20, 0, 0, tzinfo=UTC), tf="1m")
    m.on_bar(view([first]))
    # Same stream cursor remains contiguous, but the session accumulator expects 00:01.
    # Direct state mutation simulates corrupted in-memory state and verifies fail-closed behavior.
    accumulator = next(iter(m._active.values()))  # noqa: SLF001
    accumulator.next_open = datetime(2026, 7, 20, 0, 2, tzinfo=UTC)
    second = candle(datetime(2026, 7, 20, 0, 1, tzinfo=UTC), tf="1m")
    decision = m.on_bar(view([first, second]))
    assert "session_range.data_gap:x" in decision.skips


def test_restore_empty_state_is_noop() -> None:
    m = make_module()
    before = m.snapshot_state()
    m.restore_state({})
    assert m.snapshot_state() == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("active", {}),
        ("active", [1]),
        ("last_bars", []),
        ("terminal_tokens", []),
        ("last_completed", []),
    ],
)
def test_restore_rejects_invalid_container_shapes(field: str, value: object) -> None:
    state = dict(make_module().snapshot_state())
    state[field] = value
    with pytest.raises(ValueError):
        make_module().restore_state(state)


def test_restore_rejects_bad_entries_and_contracts() -> None:
    state = dict(make_module().snapshot_state())
    state["last_bars"] = {1: {}}
    with pytest.raises(ValueError, match="last_bars entries"):
        make_module().restore_state(state)
    state = dict(make_module().snapshot_state())
    state["terminal_tokens"] = {"BTCUSDT|5m|x": "wrong"}
    with pytest.raises(ValueError, match="terminal token"):
        make_module().restore_state(state)


def test_restore_rejects_unknown_session_and_changed_boundaries() -> None:
    m = make_module()
    bar = candle(datetime(2026, 7, 20, tzinfo=UTC))
    m.on_bar(view([bar]))
    state = copy.deepcopy(m.snapshot_state())
    state["active"][0]["session_name"] = "unknown"
    with pytest.raises(ValueError, match="unknown session"):
        make_module().restore_state(state)
    changed = copy.deepcopy(m.snapshot_state())
    changed["schema_version"] = "1"
    changed.pop("config_fingerprint")
    changed.pop("module_version")
    changed["active"][0]["end_utc"] = "2026-07-20T00:10:00+00:00"
    with pytest.raises(ValueError, match="boundaries"):
        make_module().restore_state(changed)


def _completed_state() -> tuple[dict[str, object], object]:
    m = SessionRangeModule([{"name": "x", "timezone": "UTC", "start": "00:00", "end": "00:05"}])
    bar = candle(datetime(2026, 7, 20, tzinfo=UTC))
    decision = m.on_bar(view([bar]))
    feature = next(item for item in decision.features if item.key == COMPLETED_FEATURE)
    return dict(m.snapshot_state()), feature


def test_restore_reemits_missing_completed_feature() -> None:
    state, feature = _completed_state()
    restored = SessionRangeModule(
        [{"name": "x", "timezone": "UTC", "start": "00:00", "end": "00:05"}]
    )
    restored.restore_state(state)
    later = candle(datetime(2026, 7, 20, 0, 5, tzinfo=UTC))
    decision = restored.on_bar(view([later]))
    assert feature.feature_id in {item.feature_id for item in decision.features}


def test_restore_does_not_duplicate_existing_or_expired_feature() -> None:
    state, feature = _completed_state()
    restored = SessionRangeModule(
        [{"name": "x", "timezone": "UTC", "start": "00:00", "end": "00:05"}]
    )
    restored.restore_state(state)
    later = candle(datetime(2026, 7, 20, 0, 5, tzinfo=UTC))
    feature_view = FeatureView([feature], later.close_time)
    decision = restored.on_bar(view([later], features=feature_view))
    assert feature.feature_id not in {item.feature_id for item in decision.features}

    expired_state = copy.deepcopy(state)
    stored = next(iter(expired_state["last_completed"].values()))
    stored["valid_until"] = "2026-07-20T00:05:00+00:00"
    expired = SessionRangeModule(
        [{"name": "x", "timezone": "UTC", "start": "00:00", "end": "00:05"}]
    )
    expired.restore_state(expired_state)
    decision = expired.on_bar(view([candle(datetime(2026, 7, 20, 0, 10, tzinfo=UTC))]))
    assert feature.feature_id not in {item.feature_id for item in decision.features}


def test_restore_rejects_stored_feature_contract_mismatch() -> None:
    state, _ = _completed_state()
    broken = copy.deepcopy(state)
    key = next(iter(broken["last_completed"]))
    broken["last_completed"][key]["key"] = ACTIVE_FEATURE
    with pytest.raises(ValueError, match="contract mismatch"):
        SessionRangeModule(
            [{"name": "x", "timezone": "UTC", "start": "00:00", "end": "00:05"}]
        ).restore_state(broken)


def test_range_accumulator_invariants_and_round_trip() -> None:
    start = datetime(2026, 7, 20, tzinfo=UTC)
    end = start + timedelta(minutes=5)
    bar = candle(start)
    accumulator = RangeAccumulator.start("x", date(2026, 7, 20), start, end, bar)
    restored = RangeAccumulator.from_mapping(accumulator.to_mapping())
    assert restored.to_mapping() == accumulator.to_mapping()
    invalids = [
        {"start_utc": start.replace(tzinfo=None)},
        {"end_utc": start},
        {"session_name": ""},
        {"high": Decimal("1"), "low": Decimal("2")},
        {"bar_count": 0},
        {"next_open": start},
        {"volume": Decimal("-1")},
        {"high": Decimal("NaN")},
    ]
    values = accumulator.to_mapping()
    for changes in invalids:
        kwargs = {
            "session_name": accumulator.session_name,
            "symbol": accumulator.symbol,
            "timeframe": accumulator.timeframe,
            "anchor_date": accumulator.anchor_date,
            "start_utc": accumulator.start_utc,
            "end_utc": accumulator.end_utc,
            "first_open": accumulator.first_open,
            "high": accumulator.high,
            "low": accumulator.low,
            "last_close": accumulator.last_close,
            "volume": accumulator.volume,
            "bar_count": accumulator.bar_count,
            "next_open": accumulator.next_open,
        }
        kwargs.update(changes)
        with pytest.raises(ValueError):
            RangeAccumulator(**kwargs)
    assert values["bar_count"] == 1


def test_cursor_invariants_and_round_trip() -> None:
    bar = candle(datetime(2026, 7, 20, tzinfo=UTC))
    cursor = BarCursor.from_candle(bar)
    assert BarCursor.from_mapping(cursor.to_mapping()) == cursor
    with pytest.raises(ValueError, match="five values"):
        BarCursor.from_mapping(
            {
                "open_time": bar.timestamp.isoformat(),
                "close_time": bar.close_time.isoformat(),
                "fingerprint": [1],
            }
        )
    with pytest.raises(ValueError):
        BarCursor(bar.timestamp.replace(tzinfo=None), bar.close_time, cursor.fingerprint)
    with pytest.raises(ValueError):
        BarCursor(bar.timestamp, bar.close_time.replace(tzinfo=None), cursor.fingerprint)
    with pytest.raises(ValueError):
        BarCursor(bar.timestamp, bar.timestamp, cursor.fingerprint)


def test_stored_feature_invariants_and_round_trip() -> None:
    now = datetime(2026, 7, 20, tzinfo=UTC)
    stored = StoredFeature(
        key=COMPLETED_FEATURE,
        feature_id="id",
        symbol="BTCUSDT",
        timeframe="5m",
        created_at=now,
        valid_until=now,
        value={"x": 1},
        metadata={"session": "x"},
    )
    assert StoredFeature.from_mapping(stored.to_mapping()).to_mapping() == stored.to_mapping()
    with pytest.raises(ValueError):
        StoredFeature("", "id", "BTC", "5m", now, now, {}, {})
    with pytest.raises(ValueError):
        StoredFeature(COMPLETED_FEATURE, "id", "BTC", "5m", now.replace(tzinfo=None), now, {}, {})
    with pytest.raises(ValueError):
        StoredFeature(COMPLETED_FEATURE, "id", "BTC", "5m", now, now.replace(tzinfo=None), {}, {})
    with pytest.raises(ValueError):
        StoredFeature(COMPLETED_FEATURE, "id", "BTC", "5m", now, now - timedelta(seconds=1), {}, {})
    with pytest.raises(ValueError, match="must be mappings"):
        StoredFeature.from_mapping(
            {
                "key": COMPLETED_FEATURE,
                "feature_id": "id",
                "symbol": "BTC",
                "timeframe": "5m",
                "created_at": now.isoformat(),
                "valid_until": now.isoformat(),
                "value": [],
                "metadata": {},
            }
        )


def test_finite_float_guard() -> None:
    with pytest.raises(ValueError, match="finite float"):
        SessionRangeModule._finite_float(Decimal("1e999999"), "high")


def test_bounds_cache_eviction_and_pruning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module_impl, "_MAX_BOUNDS_CACHE", 1)
    m = make_module()
    spec = m._specs[0]  # noqa: SLF001
    old = date(2026, 1, 1)
    m._bounds_for(spec, old)  # noqa: SLF001
    m._bounds_for(spec, date(2026, 1, 2))  # noqa: SLF001
    assert len(m._bounds_cache) == 1  # noqa: SLF001

    m._terminal_token_by_scope["bad"] = "not-a-date"  # noqa: SLF001
    m._terminal_token_by_scope["BTC|5m|x"] = "BTC|5m|x|2020-01-01"  # noqa: SLF001
    m._bounds_cache[("x", old)] = spec.bounds_for(old)  # noqa: SLF001
    now = datetime(2026, 7, 20, tzinfo=UTC)
    m._last_completed["old"] = StoredFeature(  # noqa: SLF001
        COMPLETED_FEATURE,
        "old",
        "BTC",
        "5m",
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2020, 1, 1, tzinfo=UTC),
        {},
        {"session": "x"},
    )
    m._prune_terminal_state(now)  # noqa: SLF001
    assert "BTC|5m|x" not in m._terminal_token_by_scope  # noqa: SLF001
    assert "bad" in m._terminal_token_by_scope  # noqa: SLF001
    assert "old" not in m._last_completed  # noqa: SLF001
    assert ("x", old) not in m._bounds_cache  # noqa: SLF001


def test_pruning_runs_at_most_once_per_utc_day(monkeypatch: pytest.MonkeyPatch) -> None:
    m = make_module()
    calls = 0
    original_items = m._bounds_cache.items  # noqa: SLF001

    class CountingCache(dict[tuple[str, date], tuple[datetime, datetime]]):
        def __iter__(self):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            return super().__iter__()

    cache: CountingCache = CountingCache()
    cache.update(m._bounds_cache)  # noqa: SLF001
    m._bounds_cache = cache  # noqa: SLF001
    now = datetime(2026, 7, 20, tzinfo=UTC)
    m._prune_terminal_state(now)  # noqa: SLF001
    first_calls = calls
    m._prune_terminal_state(now + timedelta(hours=1))  # noqa: SLF001
    assert calls == first_calls
    m._prune_terminal_state(now + timedelta(days=1))  # noqa: SLF001
    assert calls > first_calls
    assert original_items is not None


def test_configuration_caps_session_definitions() -> None:
    rows = [
        {"name": f"s{index}", "timezone": "UTC", "start": "00:00", "end": "00:05"}
        for index in range(65)
    ]
    with pytest.raises(SessionRangeConfigurationError, match="more than 64"):
        SessionRangeModule(rows)


def test_stream_and_feature_identity_escape_delimiters() -> None:
    first = SessionRangeModule._stream_key("A|B", "5m")
    second = SessionRangeModule._stream_key("A", "B|5m")
    assert first != second
    assert "%7C" in first and "%7C" in second
    feature_id = SessionRangeModule._completed_feature_id(
        "x", "BTC:PERP", "5m|custom", date(2026, 7, 20)
    )
    assert "BTC%3APERP" in feature_id
    assert "5m%7Ccustom" in feature_id


def test_runtime_stream_limit_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module_impl, "_MAX_TRACKED_STREAMS", 1)
    m = make_module()
    timestamp = datetime(2026, 7, 20, tzinfo=UTC)
    first = candle(timestamp, symbol="BTCUSDT")
    m.on_bar(view([first]))
    second = candle(timestamp, symbol="ETHUSDT")
    decision = m.on_bar(view([second]))
    assert decision.skips == ("session_range.stream_limit_exceeded",)
    assert len(m.snapshot_state()["last_bars"]) == 1


def test_schema_v2_requires_fingerprint_and_rejects_unknown_keys() -> None:
    state = dict(make_module().snapshot_state())
    state.pop("config_fingerprint")
    with pytest.raises(ValueError, match="requires config_fingerprint"):
        make_module().restore_state(state)
    state = dict(make_module().snapshot_state())
    state["unexpected"] = True
    with pytest.raises(ValueError, match="unknown Session Range state keys"):
        make_module().restore_state(state)


def test_restore_rejects_oversized_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module_impl, "_MAX_TRACKED_STREAMS", 1)
    state = dict(make_module().snapshot_state())
    state["last_bars"] = {"a": {}, "b": {}}
    with pytest.raises(ValueError, match="tracked-stream limit"):
        make_module().restore_state(state)

    monkeypatch.setattr(module_impl, "_MAX_SCOPED_STATE", 1)
    for field, value in (
        ("active", [{}, {}]),
        ("terminal_tokens", {"a": "a|2026-01-01", "b": "b|2026-01-01"}),
        ("last_completed", {"a": {}, "b": {}}),
    ):
        state = dict(make_module().snapshot_state())
        state[field] = value
        with pytest.raises(ValueError, match="bounded-state limit"):
            make_module().restore_state(state)


def test_restore_rejects_duplicate_active_token_and_cursor_mismatch() -> None:
    m = make_module()
    first = candle(datetime(2026, 7, 20, tzinfo=UTC))
    m.on_bar(view([first]))
    state = copy.deepcopy(m.snapshot_state())
    state["active"] = [state["active"][0], copy.deepcopy(state["active"][0])]
    with pytest.raises(ValueError, match="duplicate active state token"):
        make_module().restore_state(state)

    state = copy.deepcopy(m.snapshot_state())
    cursor = next(iter(state["last_bars"].values()))
    cursor["close_time"] = "2026-07-20T00:10:00+00:00"
    with pytest.raises(ValueError, match="active state cursor mismatch"):
        make_module().restore_state(state)


def test_restore_rejects_terminal_unknown_session() -> None:
    state = dict(make_module().snapshot_state())
    state["terminal_tokens"] = {"BTCUSDT|5m|unknown": "BTCUSDT|5m|unknown|2026-07-20"}
    with pytest.raises(ValueError, match="unknown session"):
        make_module().restore_state(state)


def test_schema_v1_completed_feature_id_is_migrated() -> None:
    state, _ = _completed_state()
    state = copy.deepcopy(state)
    state["schema_version"] = "1"
    state.pop("config_fingerprint")
    state.pop("module_version")
    stored = next(iter(state["last_completed"].values()))
    stored["feature_id"] = "session-range-completed:x:BTCUSDT:2026-07-20"
    restored = SessionRangeModule(
        [{"name": "x", "timezone": "UTC", "start": "00:00", "end": "00:05"}]
    )
    restored.restore_state(state)
    migrated = next(iter(restored.snapshot_state()["last_completed"].values()))
    assert migrated["feature_id"] == "session-range-completed:x:BTCUSDT:5m:2026-07-20"


def test_schema_v1_completed_feature_requires_session_date() -> None:
    state, _ = _completed_state()
    state = copy.deepcopy(state)
    state["schema_version"] = "1"
    state.pop("config_fingerprint")
    state.pop("module_version")
    stored = next(iter(state["last_completed"].values()))
    stored["metadata"].pop("session_date", None)
    stored["value"].pop("session_date", None)
    with pytest.raises(ValueError, match="valid session_date"):
        SessionRangeModule(
            [{"name": "x", "timezone": "UTC", "start": "00:00", "end": "00:05"}]
        ).restore_state(state)


def test_decimal_results_ignore_ambient_context() -> None:
    from decimal import localcontext

    bars = [
        candle(
            datetime(2026, 7, 20, 0, index * 5, tzinfo=UTC),
            price=12345.678901 + index * 0.111111,
            volume=98765.432109,
        )
        for index in range(3)
    ]
    outputs = []
    snapshots = []
    for precision in (6, 50):
        with localcontext() as context:
            context.prec = precision
            m = make_module()
            decision = run(m, bars)[-1]
            outputs.append(
                next(
                    feature.value
                    for feature in decision.features
                    if feature.key == COMPLETED_FEATURE
                )
            )
            snapshots.append(m.snapshot_state())
    assert outputs[0] == outputs[1]
    assert snapshots[0] == snapshots[1]


def test_bar_cursor_rejects_nonfinite_fingerprint() -> None:
    timestamp = datetime(2026, 7, 20, tzinfo=UTC)
    with pytest.raises(ValueError, match="finite values"):
        BarCursor(
            open_time=timestamp,
            close_time=timestamp + timedelta(minutes=5),
            fingerprint=(100.0, 101.0, 99.0, 100.0, float("nan")),
        )


def test_daily_pruning_removes_inactive_stale_stream_cursor() -> None:
    m = make_module()
    old = datetime(2020, 1, 1, tzinfo=UTC)
    m._last_bar["old|5m"] = BarCursor(  # noqa: SLF001
        open_time=old,
        close_time=old + timedelta(minutes=5),
        fingerprint=(100.0, 101.0, 99.0, 100.0, 1.0),
    )
    m._prune_terminal_state(datetime(2026, 7, 20, tzinfo=UTC))  # noqa: SLF001
    assert "old|5m" not in m._last_bar  # noqa: SLF001


def test_real_registry_and_dependency_planner_contract() -> None:
    from modular_core import ModuleDecision, ModuleManifest, ModuleRegistry, StrategyModule

    class Consumer(StrategyModule):
        name = "consumer"
        parameters: dict[str, object] = {}
        manifest = ModuleManifest(
            name=name,
            version="1.0.0",
            api_version="1.0",
            required_features=(COMPLETED_FEATURE,),
            produced_features=("consumer.result",),
            skip_until_ready=False,
        )

        def on_bar(self, market_view: MarketView) -> ModuleDecision:
            del market_view
            return ModuleDecision()

    registry = ModuleRegistry()
    registry.register("session_range", SessionRangeModule)
    loaded = registry.load_config(
        {
            "modules": [
                {
                    "plugin": "session_range",
                    "parameters": {
                        "sessions": [
                            {
                                "name": "x",
                                "timezone": "UTC",
                                "start": "00:00",
                                "end": "00:05",
                            }
                        ]
                    },
                }
            ]
        }
    )
    producer = loaded[0]
    plan = registry.validate_and_plan((Consumer(), producer), base_timeframe="5m")
    assert [module.name for module in plan.modules] == ["session_range", "consumer"]
    assert plan.produced_by[COMPLETED_FEATURE] == "session_range"


def test_feature_store_exposes_completed_feature_on_same_closed_bar() -> None:
    from modular_core import FeatureStore

    module = SessionRangeModule(
        [{"name": "x", "timezone": "UTC", "start": "00:00", "end": "00:05"}]
    )
    bar = candle(datetime(2026, 7, 20, tzinfo=UTC))
    decision = module.on_bar(view([bar]))
    completed = next(feature for feature in decision.features if feature.key == COMPLETED_FEATURE)
    store = FeatureStore()
    stored = store.publish(completed)
    visible = store.snapshot(bar.close_time).latest(
        COMPLETED_FEATURE,
        symbol="BTCUSDT",
        timeframe="5m",
        source_module="session_range",
    )
    assert visible == stored
    assert visible is not None and visible.created_at == bar.close_time


def test_config_identity_fields_require_strings() -> None:
    base: dict[str, object] = {
        "name": "x",
        "timezone": "UTC",
        "start": "00:00",
        "end": "00:05",
    }
    for field, value in (("name", True), ("name", 1), ("timezone", 1), ("start", 0), ("end", [])):
        payload = dict(base)
        payload[field] = value
        with pytest.raises(SessionRangeConfigurationError):
            SessionRangeModule([payload])


def test_module_parameters_are_deeply_immutable() -> None:
    module = make_module()
    sessions = module.parameters["sessions"]
    assert isinstance(sessions, tuple)
    with pytest.raises(TypeError):
        sessions[0]["name"] = "changed"  # type: ignore[index]


def test_gap_after_restore_does_not_reemit_invalidated_completed_feature() -> None:
    bars = [candle(datetime(2026, 7, 20, 0, minute, tzinfo=UTC)) for minute in (0, 5, 10)]
    original = make_module()
    completed = run(original, bars)[-1].features
    assert any(feature.key == COMPLETED_FEATURE for feature in completed)

    restored = make_module()
    restored.restore_state(original.snapshot_state())
    gap_bar = candle(datetime(2026, 7, 20, 0, 15, tzinfo=UTC))
    decision = restored.on_bar(view([gap_bar], gap=True))

    assert not any(feature.key == COMPLETED_FEATURE for feature in decision.features)
    assert decision.skips == ("session_range.data_gap",)
    assert restored.snapshot_state()["last_completed"] == {}


def test_duplicate_with_gap_invalidates_active_and_reports_both_reasons() -> None:
    module = make_module()
    first = candle(datetime(2026, 7, 20, 0, 0, tzinfo=UTC))
    module.on_bar(view([first]))

    decision = module.on_bar(view([first, first], gap=True))

    assert decision.skips == (
        "session_range.data_gap",
        "session_range.duplicate_bar",
    )
    assert module.snapshot_state()["active"] == []


def test_inferred_gap_requests_completed_feature_delete() -> None:
    bars = [candle(datetime(2026, 7, 20, 0, minute, tzinfo=UTC)) for minute in (0, 5, 10)]
    module = make_module()
    completed = run(module, bars)[-1].features[-1]

    later = candle(datetime(2026, 7, 20, 0, 25, tzinfo=UTC))
    decision = module.on_bar(view([*bars, later]))

    assert decision.skips == ("session_range.data_gap",)
    assert len(decision.feature_deletes) == 1
    deletion = decision.feature_deletes[0]
    assert deletion.feature_id == completed.feature_id
    assert deletion.key == COMPLETED_FEATURE
    assert module.snapshot_state()["last_completed"] == {}


def test_extreme_finite_volume_fails_closed_without_active_state_leak() -> None:
    module = make_module()
    first = candle(datetime(2026, 7, 20, 0, 0, tzinfo=UTC), volume=1e308)
    second = candle(datetime(2026, 7, 20, 0, 5, tzinfo=UTC), volume=1e308)
    module.on_bar(view([first]))

    decision = module.on_bar(view([first, second]))

    assert decision.skips == ("session_range.numeric_overflow:x",)
    assert decision.features == ()
    assert module.snapshot_state()["active"] == []


def test_direct_spec_constructor_enforces_runtime_types() -> None:
    valid = {
        "name": "x",
        "timezone_name": "UTC",
        "start": datetime.strptime("00:00", "%H:%M").time(),
        "end": datetime.strptime("00:05", "%H:%M").time(),
    }
    invalid = (
        {**valid, "name": True},
        {**valid, "timezone_name": 1},
        {**valid, "weekdays": (True,)},
        {**valid, "minimum_bars": True},
        {**valid, "emit_active": 1},
        {**valid, "dst_fold": False},
    )
    for payload in invalid:
        with pytest.raises(SessionRangeConfigurationError):
            SessionRangeSpec(**payload)  # type: ignore[arg-type]


def test_schema_v2_requires_nonempty_module_version() -> None:
    module = make_module()
    state = dict(module.snapshot_state())
    state.pop("module_version")
    with pytest.raises(ValueError, match="module_version"):
        module.restore_state(state)
    state["module_version"] = []
    with pytest.raises(ValueError, match="module_version"):
        module.restore_state(state)


def test_restore_rejects_unknown_nested_state_keys_and_boolean_bar_count() -> None:
    module = make_module()
    first = candle(datetime(2026, 7, 20, 0, 0, tzinfo=UTC))
    module.on_bar(view([first]))
    state = copy.deepcopy(module.snapshot_state())

    state["active"][0]["unexpected"] = 1
    with pytest.raises(ValueError, match="unknown range accumulator keys"):
        make_module().restore_state(state)

    state = copy.deepcopy(module.snapshot_state())
    state["last_bars"]["BTCUSDT|5m"]["unexpected"] = 1
    with pytest.raises(ValueError, match="unknown bar cursor keys"):
        make_module().restore_state(state)

    state = copy.deepcopy(module.snapshot_state())
    state["active"][0]["bar_count"] = True
    with pytest.raises(ValueError, match="bar_count"):
        make_module().restore_state(state)


def test_restore_normalizes_equivalent_offsets_to_canonical_utc() -> None:
    module = make_module()
    first = candle(datetime(2026, 7, 20, 0, 0, tzinfo=UTC))
    module.on_bar(view([first]))
    state = copy.deepcopy(module.snapshot_state())
    active = state["active"][0]
    active["start_utc"] = "2026-07-20T02:00:00+02:00"
    active["end_utc"] = "2026-07-20T02:15:00+02:00"
    active["next_open"] = "2026-07-20T02:05:00+02:00"
    cursor = state["last_bars"]["BTCUSDT|5m"]
    cursor["open_time"] = "2026-07-20T02:00:00+02:00"
    cursor["close_time"] = "2026-07-20T02:05:00+02:00"

    restored = make_module()
    restored.restore_state(state)
    canonical = restored.snapshot_state()

    assert canonical["active"][0]["start_utc"] == "2026-07-20T00:00:00+00:00"
    assert canonical["last_bars"]["BTCUSDT|5m"]["close_time"] == "2026-07-20T00:05:00+00:00"


def test_restore_rejects_corrupted_ohlc_geometry() -> None:
    module = make_module()
    first = candle(datetime(2026, 7, 20, 0, 0, tzinfo=UTC))
    module.on_bar(view([first]))
    state = copy.deepcopy(module.snapshot_state())
    state["active"][0]["first_open"] = "1000"
    with pytest.raises(ValueError, match="OHLC geometry"):
        make_module().restore_state(state)

    state = copy.deepcopy(module.snapshot_state())
    state["last_bars"]["BTCUSDT|5m"]["fingerprint"] = [100, 90, 80, 85, 1]
    with pytest.raises(ValueError, match="fingerprint OHLC geometry"):
        make_module().restore_state(state)


def test_restore_rejects_invalid_terminal_date_and_completed_feature_id() -> None:
    bars = [candle(datetime(2026, 7, 20, 0, minute, tzinfo=UTC)) for minute in (0, 5, 10)]
    module = make_module()
    run(module, bars)

    state = copy.deepcopy(module.snapshot_state())
    scope = next(iter(state["terminal_tokens"]))
    state["terminal_tokens"][scope] = f"{scope}|not-a-date"
    with pytest.raises(ValueError, match="terminal token date"):
        make_module().restore_state(state)

    state = copy.deepcopy(module.snapshot_state())
    scope = next(iter(state["last_completed"]))
    state["last_completed"][scope]["feature_id"] = "corrupted"
    with pytest.raises(ValueError, match="feature_id"):
        make_module().restore_state(state)


def test_checkpoint_scalar_parsers_reject_noncanonical_values() -> None:
    start = datetime(2026, 7, 20, tzinfo=UTC)
    accumulator = RangeAccumulator.start(
        "x", date(2026, 7, 20), start, start + timedelta(minutes=5), candle(start)
    )
    mapping = accumulator.to_mapping()

    non_string_key = dict(mapping)
    non_string_key[1] = "bad"
    with pytest.raises(ValueError, match="keys must be strings"):
        RangeAccumulator.from_mapping(non_string_key)  # type: ignore[arg-type]

    missing_key = dict(mapping)
    missing_key.pop("volume")
    with pytest.raises(ValueError, match="missing range accumulator keys"):
        RangeAccumulator.from_mapping(missing_key)

    for field, value, message in (
        ("session_name", "", "non-empty string"),
        ("start_utc", "not-a-datetime", "ISO-8601 datetime"),
        ("start_utc", "2026-07-20T00:00:00", "timezone-aware"),
        ("anchor_date", "not-a-date", "ISO-8601 date"),
        ("first_open", 100, "decimal string"),
        ("first_open", "not-a-decimal", "decimal string"),
        ("first_open", "Infinity", "finite"),
    ):
        broken = dict(mapping)
        broken[field] = value
        with pytest.raises(ValueError, match=message):
            RangeAccumulator.from_mapping(broken)


def test_restore_rejects_non_mapping_and_non_string_top_level_keys() -> None:
    with pytest.raises(ValueError, match="state must be a mapping"):
        make_module().restore_state([])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="state keys must be strings"):
        make_module().restore_state({1: "bad"})  # type: ignore[arg-type]


def test_restore_rejects_completed_feature_semantic_mismatches() -> None:
    state, _ = _completed_state()
    for container, field, value, message in (
        ("metadata", "session", "other", "session mismatch"),
        ("value", "session", "other", "session mismatch"),
        ("metadata", "complete", False, "complete flag"),
        ("value", "complete", False, "complete flag"),
    ):
        broken = copy.deepcopy(state)
        stored = next(iter(broken["last_completed"].values()))
        stored[container][field] = value
        with pytest.raises(ValueError, match=message):
            SessionRangeModule(
                [{"name": "x", "timezone": "UTC", "start": "00:00", "end": "00:05"}]
            ).restore_state(broken)


def test_direct_spec_rejects_timezone_aware_clock_and_non_decimal_ranges() -> None:
    valid = {
        "name": "x",
        "timezone_name": "UTC",
        "start": datetime.strptime("00:00", "%H:%M").time(),
        "end": datetime.strptime("00:05", "%H:%M").time(),
    }
    with pytest.raises(SessionRangeConfigurationError, match="naive clock"):
        SessionRangeSpec(**{**valid, "start": valid["start"].replace(tzinfo=UTC)})
    with pytest.raises(SessionRangeConfigurationError, match="finite Decimal"):
        SessionRangeSpec(**{**valid, "minimum_range": 0})  # type: ignore[arg-type]
