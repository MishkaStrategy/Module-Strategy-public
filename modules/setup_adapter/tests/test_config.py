from __future__ import annotations

import pytest

from modular_setup_adapter import CanonicalSetupAdapterModule, SetupAdapterConfig


def test_default_config_and_dynamic_manifest() -> None:
    module = CanonicalSetupAdapterModule()
    assert module.manifest.required_features == ("breakout_return.confirmed",)
    assert module.manifest.produced_features == ("setup.confirmed", "setup_adapter.rejection")
    assert module.manifest.version == "0.1.0"
    assert module.manifest.state_schema_version == "1"
    assert module.config.max_processed_sources == 10_000


def test_custom_sources_are_validated_and_order_preserved() -> None:
    config = SetupAdapterConfig.from_parameters(
        sources=[
            {
                "feature_key": "alpha.confirmed",
                "profile": "breakout_return_v1",
                "source_module": "alpha",
            },
            {
                "feature_key": "beta.confirmed",
                "profile": "breakout_return_v1",
                "source_module": "beta",
            },
        ],
        max_processed_sources=4,
    )
    assert [source.feature_key for source in config.sources] == [
        "alpha.confirmed",
        "beta.confirmed",
    ]
    assert len(config.fingerprint) == 64


@pytest.mark.parametrize(
    "sources,match",
    [
        ([], "sources cannot be empty"),
        (
            [
                {"feature_key": "alpha.confirmed", "profile": "breakout_return_v1"},
                {"feature_key": "alpha.confirmed", "profile": "breakout_return_v1"},
            ],
            "unique",
        ),
        ([{"feature_key": "not_namespaced", "profile": "breakout_return_v1"}], "namespaced"),
        ([{"feature_key": "alpha.confirmed", "profile": "unknown"}], "unsupported"),
    ],
)
def test_invalid_sources_fail_closed(sources, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        SetupAdapterConfig.from_parameters(sources=sources)


@pytest.mark.parametrize("value", [0, 1_000_001, True])
def test_invalid_capacity_rejected(value: object) -> None:
    with pytest.raises(ValueError, match="max_processed_sources"):
        SetupAdapterConfig.from_parameters(max_processed_sources=value)  # type: ignore[arg-type]


def test_source_spec_rejects_non_mapping_unknown_and_blank_module() -> None:
    with pytest.raises(ValueError, match="mapping"):
        SetupAdapterConfig.from_parameters(sources=["bad"])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="unknown source fields"):
        SetupAdapterConfig.from_parameters(
            sources=[
                {
                    "feature_key": "alpha.confirmed",
                    "profile": "breakout_return_v1",
                    "unexpected": True,
                }
            ]
        )
    with pytest.raises(ValueError, match="source_module"):
        SetupAdapterConfig.from_parameters(
            sources=[
                {
                    "feature_key": "alpha.confirmed",
                    "profile": "breakout_return_v1",
                    "source_module": "   ",
                }
            ]
        )
