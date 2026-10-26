from modular_core import ModuleDecision, StrategyModule

from .manifest import MANIFEST


class ReplaceMeModule(StrategyModule):
    name = MANIFEST.name
    manifest = MANIFEST

    def on_bar(self, view):
        return ModuleDecision()
