from datetime import datetime, timedelta, timezone

from modular_core import (
    BacktestEngine,
    Candle,
    EngineConfig,
    Feature,
    ModuleDecision,
    ModuleManifest,
    StrategyModule,
)

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bars() -> tuple[Candle, ...]:
    return tuple(
        Candle(
            START + timedelta(minutes=5 * index),
            100 + index,
            102 + index,
            99 + index,
            101 + index,
            1,
            "X",
            "5m",
        )
        for index in range(3)
    )


class Producer(StrategyModule):
    name = "producer"
    manifest = ModuleManifest(name=name, produced_features=("producer.value",))

    def __init__(self) -> None:
        self.calls = 0
        self.finished = False

    def on_bar(self, view):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("producer failed")
        return ModuleDecision(
            features=(Feature("producer.value", self.name, view.current.close_time, self.calls),)
        )

    def on_finish(self) -> None:
        self.finished = True


class Dependent(StrategyModule):
    name = "dependent"
    manifest = ModuleManifest(name=name, required_features=("producer.value",))

    def __init__(self) -> None:
        self.calls = 0
        self.finished = False

    def on_bar(self, view):  # type: ignore[no-untyped-def]
        self.calls += 1
        return ModuleDecision()

    def on_finish(self) -> None:
        self.finished = True


class Healthy(StrategyModule):
    name = "healthy"
    manifest = ModuleManifest(name=name)

    def __init__(self) -> None:
        self.calls = 0
        self.finished = False

    def on_bar(self, view):  # type: ignore[no-untyped-def]
        self.calls += 1
        return ModuleDecision()

    def on_finish(self) -> None:
        self.finished = True


def test_runtime_failure_quarantines_module_and_dependents_only() -> None:
    producer = Producer()
    dependent = Dependent()
    healthy = Healthy()
    result = BacktestEngine(
        (dependent, healthy, producer),
        EngineConfig(strict_module_errors=False),
    ).run(_bars())

    assert producer.calls == 2
    assert dependent.calls == 1
    assert healthy.calls == 3
    assert producer.finished and dependent.finished and healthy.finished

    quarantine_skips = [
        row for row in result.journal.skips if row["reason_code"] == "module_quarantined"
    ]
    assert {(row["module"], row["reason_detail"]) for row in quarantine_skips} >= {
        ("producer", "on_bar:RuntimeError"),
        ("dependent", "dependency:producer"),
    }
    assert any(
        row["action"] == "module_quarantined" and row["feature"].source_module == "producer"
        for row in result.journal.features
    )
