from pathlib import Path

from scripts.integration_smoke import run_smoke


CONFIG = Path(__file__).resolve().parents[2] / "configs" / "integration.example.yaml"


def test_all_modules_resolve_and_run_without_look_ahead() -> None:
    report = run_smoke(CONFIG)

    assert report["status"] == "ok"
    assert report["entry_points"] == ["fvg", "market_regime", "session_range"]
    assert report["modules"].index("session_range") < report["modules"].index("breakout_return")
    assert report["required_timeframes"] == ["15m"]
    assert report["breakout_sell_signals"] == 1
    assert report["events"] > 0
    assert report["features"] > 0
