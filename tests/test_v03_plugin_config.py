from modular_core import (
    MarketView,
    ModuleDecision,
    ModuleManifest,
    ModuleRegistry,
    StrategyModule,
)


class PluginModule(StrategyModule):
    name = "plugin_module"
    manifest = ModuleManifest(
        name=name,
        parameters_schema={"threshold": {"type": "number", "minimum": 0}},
    )

    def __init__(self, threshold=0):
        self.parameters = {"threshold": threshold}

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision()


def test_yaml_style_plugin_config_loads_registered_module():
    registry = ModuleRegistry()
    registry.register("my_plugin", PluginModule)
    modules = registry.load_config(
        {
            "modules": [
                {
                    "plugin": "my_plugin",
                    "enabled": True,
                    "parameters": {"threshold": 2.5},
                }
            ]
        }
    )
    assert len(modules) == 1
    assert modules[0].parameters["threshold"] == 2.5
