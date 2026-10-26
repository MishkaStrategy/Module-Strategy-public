from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from strategy_audit_validation.canonical_runtime import MODULE_RULES

_ROOT = Path(__file__).resolve().parents[1]
_COMPOSITIONS = _ROOT / "compositions"
_EVIDENCE = _ROOT / "evidence"


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_owner_authorization_resolves_variant_two_decisions() -> None:
    decision = _load(_COMPOSITIONS / "canonical-strategy-owner-decision-v2.json")
    assert decision["decision_id"] == "STRATEGY-312-VARIANT-2"
    assert decision["approved_by"] == "mishkacher"
    assert decision["authorization"] == {
        "historical_counts_are_non_binding_references": True,
        "live_trading_authorized": False,
        "new_canonical_candidate_authorized": True,
        "private_exchange_access_authorized": False,
        "research_and_paper_only": True,
    }
    assert set(decision["decisions"]) == {
        "STRATEGY-312-01",
        "STRATEGY-312-02",
        "STRATEGY-312-03",
        "STRATEGY-312-04",
        "STRATEGY-312-05",
        "STRATEGY-312-06",
    }
    controls = decision["decisions"]["STRATEGY-312-05"]
    assert controls["historical_counts_mandatory"] is False
    assert controls["new_ledgers_required"] is True
    assert controls["dataset_generation"]["base_url"] == "https://fapi.binance.com"
    assert controls["dataset_generation"]["endpoint"] == "GET /fapi/v1/klines"


def test_component_assets_match_runtime_identities_and_setups() -> None:
    assets = {
        "fvg_rt_final_15m": _load(_COMPOSITIONS / "fvg-rt-final-15m.v2.json"),
        "frozen_1h_short": _load(_COMPOSITIONS / "frozen-1h-short.v1.json"),
        "frozen_4h_short": _load(_COMPOSITIONS / "frozen-4h-short.v1.json"),
    }
    for module_id, asset in assets.items():
        runtime = MODULE_RULES[module_id]
        assert asset["status"] == "OWNER_APPROVED_EXECUTABLE_CONTRACT"
        assert asset["identity"]["module_id"] == module_id
        assert asset["identity"]["module_version"] == runtime.module_version
        assert asset["adapter"]["output_contract"] == "canonical-strategy-order-plan-v1"
        assert set(asset["setups"]) == set(runtime.setups)
        for setup_id, setup in runtime.setups.items():
            stored = asset["setups"][setup_id]
            assert stored["side"] == setup.side
            assert stored["target_r"] == setup.target_r
            assert stored["ttl_bars_5m"] == setup.ttl_bars_5m
            assert stored["max_hold_bars_5m"] == setup.max_hold_bars_5m
            assert stored["no_progress_bars_5m"] == setup.no_progress_bars_5m
            assert stored["no_progress_min_mfe_r"] == setup.no_progress_min_mfe_r
            assert set(asset["required_conditions_by_setup"][setup_id]) == set(
                setup.required_conditions
            )


def test_frozen_4h_asset_preserves_approved_v1_parameters() -> None:
    asset = _load(_COMPOSITIONS / "frozen-4h-short.v1.json")
    assert asset["filters"] == {
        "atr_fraction_of_close": [0.0, 0.025],
        "ema_regime": None,
        "fvg": "C.high < A.low; strict equality exclusion",
        "fvg_size_atr": [0.05, 1.25],
        "impulse_efficiency_min": 0.55,
        "middle_body_atr_min": 0.15,
        "middle_direction": "bearish",
        "rsi14_max": 40.0,
        "short_location": (
            "(high_8_completed_4h - C.close) / (high_8_completed_4h - "
            "low_8_completed_4h) >= 0.35; zero range rejects"
        ),
        "volume_ratio_min": 1.15,
    }
    assert asset["entry"] == "C.high + 0.50 * (A.low - C.high)"
    assert "last two completed 4h candles" in asset["stop"]
    assert asset["setups"]["short"] == {
        "max_hold_bars_5m": 576,
        "no_progress_bars_5m": 432,
        "no_progress_min_mfe_r": 0.35,
        "side": "short",
        "target_r": 6.0,
        "ttl_bars_5m": 576,
    }


def test_endpoint_evidence_is_forward_only_and_public() -> None:
    evidence = _load(_EVIDENCE / "binance-usdtm-kline-endpoint-v1.json")
    assert evidence["base_url"] == "https://fapi.binance.com"
    assert evidence["endpoint"] == {
        "method": "GET",
        "path": "/fapi/v1/klines",
        "security": "NONE",
    }
    assert evidence["parameters"]["symbol"] == "BTCUSDT"
    assert evidence["parameters"]["interval"] == "5m"
    assert evidence["provenance_status"] == "APPROVED_FOR_NEW_DATASET_GENERATION_ONLY"
    assert "does not retroactively prove" in evidence["trust_boundary"]
