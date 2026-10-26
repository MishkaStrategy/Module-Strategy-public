from modular_core import (
    AtomicJsonStateRepository,
    BacktestEngine,
    CheckpointManager,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    StrategyModule,
    restore_module_states,
)


class StatefulCounter(StrategyModule):
    name = "stateful_counter"
    manifest = ModuleManifest(name=name, version="1.2.0", state_schema_version="2")

    def __init__(self) -> None:
        self.count = 0

    def on_bar(self, view: MarketView) -> ModuleDecision:
        self.count += 1
        return ModuleDecision()

    def snapshot_state(self):
        return {"count": self.count}

    def restore_state(self, state):
        self.count = int(state["count"])


def test_backtest_result_contains_restorable_module_checkpoint(bars_factory) -> None:
    source = StatefulCounter()
    result = BacktestEngine(source).run(bars_factory([100, 101, 102]))
    assert result.checkpoint.last_bar_index == 2
    assert result.checkpoint.module_states[0].payload["count"] == 3

    restored = StatefulCounter()
    restore_module_states((restored,), result.checkpoint.module_states)
    assert restored.count == 3


def test_checkpoint_manager_persists_checkpoint(tmp_path, bars_factory) -> None:
    result = BacktestEngine(StatefulCounter()).run(bars_factory([100, 101]))
    manager = CheckpointManager(AtomicJsonStateRepository(tmp_path))
    manager.save("latest", result.checkpoint)
    loaded = manager.load("latest")
    assert loaded == result.checkpoint
