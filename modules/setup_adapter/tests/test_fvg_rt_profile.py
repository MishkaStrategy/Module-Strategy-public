from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from modular_core import Feature
from modular_modules.entry_model import EntryKind, EntryModelModule
from strategy_modules.fvg_rt import FEATURE_KEY, FvgRtFinal15mModule

from modular_setup_adapter import CanonicalSetupAdapterModule

from .conftest import bar, view


def _adapter() -> CanonicalSetupAdapterModule:
    return CanonicalSetupAdapterModule(
        sources=(
            {
                "feature_key": FEATURE_KEY,
                "profile": "fvg_rt_final_15m_v1",
                "source_module": "fvg_rt_final_15m",
            },
        )
    )


def _snapshot(current, setup_id: str = "long") -> dict[str, object]:
    entries = {"long": 100.0, "short_a": 101.0, "short_b": 101.0}
    stops = {"long": 95.0, "short_a": 102.0, "short_b": 103.0}
    raw = FvgRtFinal15mModule()._snapshot(
        setup_id,
        current.symbol,
        current.close_time,
        entry=entries[setup_id],
        stop=stops[setup_id],
    )
    assert raw is not None
    return dict(raw)


def _feature(current, value: dict[str, object] | None = None) -> Feature:
    snapshot = value or _snapshot(current)
    return FvgRtFinal15mModule()._feature(snapshot, current.close_time, current.symbol)


def test_fvg_rt_snapshot_maps_to_explicit_limit_entry_plan() -> None:
    current = bar()
    adapted = _adapter().on_bar(view(current, (_feature(current),)))

    assert len(adapted.features) == 1
    setup = adapted.features[0]
    assert setup.key == "setup.confirmed"
    assert setup.value["entry_kind"] == "limit_explicit"
    assert setup.value["entry_price"] == "100"
    assert setup.value["metadata"]["priority"] == -10
    assert setup.value["metadata"]["historical_priority"] == 1
    assert setup.value["metadata"]["historical_setup_priority"] == 0
    assert setup.value["metadata"]["strategy_id"] == "fvg_rt"
    assert setup.value["metadata"]["module_id"] == "fvg_rt_final_15m"
    assert setup.value["metadata"]["invalidation_price"] == "95"
    assert setup.value["metadata"]["target_price"] == "122.5"
    assert setup.value["metadata"]["target_r"] == "4.5"
    assert setup.value["metadata"]["entry_ttl_bars"] == 36
    assert setup.value["metadata"]["max_holding_seconds"] == 86_400
    assert setup.value["metadata"]["no_progress_rule"] == {
        "after_seconds": 14_400,
        "minimum_mfe_r": "0.14999999999999999",
    }

    entry = EntryModelModule(
        default_entry_kind=EntryKind.LIMIT_EXPLICIT,
        ttl_bars=48,
        allowed_entry_kinds=(EntryKind.LIMIT_EXPLICIT,),
    ).on_bar(view(current, (setup,)))
    assert len(entry.features) == 1
    plan = entry.features[0].value
    assert plan["entry_kind"] == "limit_explicit"
    assert plan["price"] == "100"
    assert plan["expires_at"] == (current.close_time + timedelta(hours=3)).isoformat()


def test_fvg_rt_profile_preserves_historical_binary64_target_price() -> None:
    current = bar(close=100.9)
    raw = FvgRtFinal15mModule()._snapshot(
        "long",
        current.symbol,
        current.close_time,
        entry=100.1,
        stop=99.8,
    )
    assert raw is not None

    adapted = _adapter().on_bar(view(current, (_feature(current, dict(raw)),)))

    metadata = adapted.features[0].value["metadata"]
    historical_target = 100.1 + 4.5 * (100.1 - 99.8)
    assert metadata["target_price"] == format(historical_target, ".17g")
    assert float(metadata["target_price"]) == historical_target
    assert metadata["target_r"] == "4.5"


def test_fvg_rt_profile_preserves_historical_setup_tie_break_order() -> None:
    current = bar()
    observed: dict[str, int] = {}

    for setup_id in ("long", "short_a", "short_b"):
        adapted = _adapter().on_bar(
            view(current, (_feature(current, _snapshot(current, setup_id)),))
        )
        metadata = adapted.features[0].value["metadata"]
        observed[setup_id] = metadata["priority"]

    assert observed == {"long": -10, "short_a": -11, "short_b": -12}


def test_fvg_rt_profile_accepts_every_owner_bound_15m_setup_contract() -> None:
    current = bar()

    for setup_id in ("long", "short_a", "short_b"):
        adapted = _adapter().on_bar(
            view(current, (_feature(current, _snapshot(current, setup_id)),))
        )
        assert adapted.features[0].key == "setup.confirmed"


def test_fvg_rt_profile_rejects_drifted_condition_identity() -> None:
    current = bar()
    value = _snapshot(current)
    value["conditions"] = {"unexpected": True}

    rejected = _adapter().on_bar(view(current, (_feature(current, value),)))

    assert rejected.features[0].key == "setup_adapter.rejection"
    assert "setup_adapter.invalid_source_payload" in rejected.skips


def test_fvg_rt_profile_rejects_a_tampered_semantic_digest() -> None:
    current = bar()
    value = _snapshot(current)
    value["semantic_input_digest"] = "0" * 64

    rejected = _adapter().on_bar(view(current, (_feature(current, value),)))

    assert rejected.features[0].key == "setup_adapter.rejection"
    assert "setup_adapter.invalid_source_payload" in rejected.skips


def test_fvg_rt_profile_rejects_drifted_completed_timeframes() -> None:
    current = bar()
    value = _snapshot(current)
    value["completed_timeframes"] = ("5m", "15m", "4h", "1d")

    rejected = _adapter().on_bar(view(current, (_feature(current, value),)))

    assert rejected.features[0].key == "setup_adapter.rejection"
    assert "setup_adapter.timeframe_mismatch" in rejected.skips


def test_fvg_rt_profile_rejects_drifted_execution_contract() -> None:
    current = bar()
    value = _snapshot(current)
    value["ttl_bars_5m"] = 48

    rejected = _adapter().on_bar(view(current, (_feature(current, value),)))

    assert rejected.features[0].key == "setup_adapter.rejection"
    assert "setup_adapter.invalid_source_payload" in rejected.skips


def test_fvg_rt_profile_accepts_composite_emitter_and_preserves_child_identity() -> None:
    current = bar()
    source = replace(_feature(current), source_module="fvg_rt_composite")
    adapted = CanonicalSetupAdapterModule(
        sources=(
            {
                "feature_key": FEATURE_KEY,
                "profile": "fvg_rt_final_15m_v1",
                "source_module": "fvg_rt_composite",
            },
        )
    ).on_bar(view(current, (source,)))

    assert len(adapted.features) == 1
    metadata = adapted.features[0].value["metadata"]
    assert metadata["module_id"] == "fvg_rt_final_15m"
    assert metadata["source_module"] == "fvg_rt_composite"
    assert metadata["source"]["module_id"] == "fvg_rt_final_15m"
