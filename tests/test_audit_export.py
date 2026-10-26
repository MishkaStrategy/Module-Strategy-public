import json

from modular_core import BacktestEngine, MarketView, ModuleDecision, StrategyModule


class DiagnosticModule(StrategyModule):
    name = "diagnostic"

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision(diagnostics={"index": view.index})


def test_journal_exports_json_and_csv(bars_factory, tmp_path):
    result = BacktestEngine(DiagnosticModule()).run(bars_factory([100, 101]))
    output = result.export_directory(tmp_path / "audit")
    assert (output / "journal.json").exists()
    assert (output / "trades.csv").exists()
    assert (output / "equity.csv").exists()
    assert (output / "metrics.json").exists()
    assert (output / "run.json").exists()
    payload = json.loads((output / "journal.json").read_text())
    assert payload["diagnostics"][0]["values"] == {"index": 0}
