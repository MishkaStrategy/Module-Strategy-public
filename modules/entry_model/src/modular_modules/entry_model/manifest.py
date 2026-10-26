from modular_core import ModuleManifest

SETUP_FEATURE_KEY = "setup.confirmed"
ENTRY_PLAN_FEATURE_KEY = "entry_model.plan"
ENTRY_REJECTION_FEATURE_KEY = "entry_model.rejection"

MANIFEST = ModuleManifest(
    name="entry_model",
    version="0.2.0",
    api_version="1.0",
    required_features=(SETUP_FEATURE_KEY,),
    produced_features=(ENTRY_PLAN_FEATURE_KEY, ENTRY_REJECTION_FEATURE_KEY),
    parameters_schema={
        "default_entry_kind": {
            "type": "string",
            "enum": (
                "market",
                "limit_confirmation",
                "limit_retracement",
                "limit_explicit",
                "stop_market",
                "stop_limit",
            ),
        },
        "retracement_fraction": {"type": "string"},
        "ttl_bars": {"type": "integer", "minimum": 1, "maximum": 1_000_000},
        "honor_setup_hint": {"type": "boolean"},
        "post_only_limits": {"type": "boolean"},
        "stop_limit_offset_bps": {"type": "string"},
        "max_setup_age_seconds": {"type": "string"},
        "max_processed_setups": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1_000_000,
        },
        "allowed_entry_kinds": {"type": "array"},
    },
    skip_until_ready=False,
    description=(
        "Consumes canonical confirmed setup features and publishes deterministic "
        "entry plans without sizing, protection or order submission."
    ),
    state_schema_version="2",
)
