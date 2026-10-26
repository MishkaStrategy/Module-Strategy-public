from modular_core import ModuleManifest

MANIFEST = ModuleManifest(
    name="replace_me",
    version="0.1.0",
    api_version="1.0",
    required_timeframes=(),
    required_features=(),
    produced_features=("replace_me.output",),
    warmup_bars={},
    parameters_schema={},
)
