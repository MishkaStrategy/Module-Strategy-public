from __future__ import annotations

import json
from pathlib import Path
from typing import Any

COMPOSITION_NAME = "FVG-RT Final 15m + Frozen 1H Short + Frozen 4H Short"


def _compositions() -> Path:
    return Path(__file__).resolve().parents[1] / "compositions"


def _load(name: str) -> dict[str, Any]:
    value = json.loads((_compositions() / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_recovery_registry_is_explicitly_fail_closed() -> None:
    registry = _load("recovered-contracts-v1.json")

    assert registry["composition_name"] == COMPOSITION_NAME
    assert registry["status"] == "PARTIAL_RECOVERY_FAIL_CLOSED"
    assert registry["canonical"] is False
    assert registry["executable"] is False
    assert registry["approvable"] is False
    assert registry["original_gap_asset"].endswith(".gap.json")
    assert registry["control_inventory"]["expected_combined_champion_trades"] == 395
    assert registry["control_inventory"]["expected_component_trades"] == {
        "frozen_1h_short": 91,
        "frozen_4h_short": 21,
        "fvg_rt_final_15m": 283,
    }

    contracts = {item["module_id"]: item for item in registry["contracts"]}
    assert set(contracts) == {
        "fvg_rt_final_15m",
        "frozen_1h_short",
        "frozen_4h_short",
    }
    assert contracts["fvg_rt_final_15m"]["contract_status"] == ("RECOVERED_SPEC_NOT_IMPLEMENTED")
    assert contracts["frozen_1h_short"]["contract_status"] == "RECOVERED_PARTIAL"
    assert contracts["frozen_4h_short"]["contract_status"] == ("MISSING_AUTHORITATIVE_CONTRACT")
    assert all(item["required_decisions"] for item in contracts.values())


def test_final_15m_recovery_preserves_frozen_rules_and_final_additions() -> None:
    contract = _load("fvg-rt-final-15m.recovered.json")

    assert contract["contract_status"] == "RECOVERED_SPEC_NOT_IMPLEMENTED"
    assert contract["executable"] is False
    assert contract["quality_filter"]["minimum"] == 0.45
    assert contract["long"]["filters"]["ema200_regime"] == (
        "last completed 4h close > completed 4h EMA200"
    )
    assert contract["short_A"]["filters"]["ema200_regime"] == (
        "last completed 1d close < completed 1d EMA200"
    )
    assert contract["short_B"]["filters"]["ema200_regime"] == (
        "last completed 1d close < completed 1d EMA200"
    )

    assert contract["long"]["execution"]["target_R"] == 4.5
    assert contract["long"]["execution"]["limit_ttl_5m_bars"] == 36
    assert contract["short_A"]["execution"]["target_R"] == 5.0
    assert contract["short_A"]["execution"]["limit_ttl_5m_bars"] == 48
    assert contract["short_B"]["execution"]["target_R"] == 6.0
    assert contract["short_B"]["execution"]["max_hold_5m_bars"] == 576

    assert contract["intrabar"]["stop_on_fill_bar"] is True
    assert contract["intrabar"]["full_target_on_fill_bar"] is False
    assert contract["intrabar"]["stop_target_same_bar"] == "stop first"
    assert contract["missing_for_executable_contract"]


def test_frozen_1h_recovery_preserves_entry_but_stays_partial() -> None:
    contract = _load("frozen-1h-short.recovered-gap.json")

    assert contract["contract_status"] == "RECOVERED_PARTIAL"
    assert contract["executable"] is False
    assert contract["identity"]["signal_timeframe"] == "1h"
    assert contract["filters"]["fvg_size_atr"] == {
        "maximum": 1.25,
        "minimum": 0.08,
    }
    assert contract["filters"]["impulse_efficiency_minimum"] == 0.45
    assert contract["filters"]["short_location"] == {
        "lookback_completed_1h_candles": 8,
        "minimum": 0.75,
    }
    assert contract["execution"]["entry"] == {
        "depth_fraction": 0.75,
        "formula": "C.high + 0.75 * (A.low - C.high)",
        "reference_zone": "bearish FVG [C.high, A.low]",
        "status": "RECOVERED",
    }
    assert contract["control_inventory"]["expected_component_trades_in_champion"] == 91
    assert contract["missing_for_complete_contract"]


def test_frozen_4h_gap_rejects_pr_298_substitution() -> None:
    contract = _load("frozen-4h-short.gap.json")

    assert contract["contract_status"] == "MISSING_AUTHORITATIVE_CONTRACT"
    assert contract["executable"] is False
    assert contract["identity"]["signal_timeframe"] == "4h"
    assert contract["recovered_facts"]["distinct_from_pr_298_first_retest_baseline"] is True
    assert contract["non_substitution"]["draft_pr"] == 298
    assert contract["missing_for_complete_contract"]
