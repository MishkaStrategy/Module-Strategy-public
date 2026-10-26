from importlib.metadata import entry_points
from importlib.resources import files

from strategy_modules.breakout_return import BreakoutReturnModule


def test_breakout_return_entry_point_loads_public_module() -> None:
    selected = {
        entry_point.name: entry_point
        for entry_point in entry_points().select(group="modular_strategy.modules")
    }
    assert "breakout_return" in selected
    assert selected["breakout_return"].load() is BreakoutReturnModule


def test_strategy_modules_package_declares_typing_marker() -> None:
    assert files("strategy_modules").joinpath("py.typed").is_file()
