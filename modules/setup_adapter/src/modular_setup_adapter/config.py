from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .enums import SourceProfile
from .primitives import required_text, sha256_hex


@dataclass(frozen=True, slots=True)
class SourceSpec:
    feature_key: str
    profile: SourceProfile
    source_module: str | None = None

    def __post_init__(self) -> None:
        if "." not in self.feature_key or not self.feature_key.strip():
            raise ValueError("source feature_key must be namespaced")
        if self.source_module is not None and not self.source_module.strip():
            raise ValueError("source_module cannot be blank")

    @classmethod
    def from_mapping(cls, value: object) -> SourceSpec:
        if not isinstance(value, Mapping):
            raise ValueError("every source must be a mapping")
        unknown = sorted(set(value) - {"feature_key", "profile", "source_module"})
        if unknown:
            raise ValueError(f"unknown source fields: {', '.join(str(item) for item in unknown)}")
        try:
            profile = SourceProfile(required_text(value.get("profile"), "profile"))
        except ValueError as exc:
            raise ValueError(f"unsupported source profile: {value.get('profile')!r}") from exc
        source_module_raw = value.get("source_module")
        return cls(
            feature_key=required_text(value.get("feature_key"), "feature_key"),
            profile=profile,
            source_module=(
                required_text(source_module_raw, "source_module")
                if source_module_raw is not None
                else None
            ),
        )

    def as_mapping(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "feature_key": self.feature_key,
                "profile": self.profile.value,
                "source_module": self.source_module,
            }
        )


_DEFAULT_SOURCE = SourceSpec(
    feature_key="breakout_return.confirmed",
    profile=SourceProfile.BREAKOUT_RETURN_V1,
    source_module="breakout_return",
)


@dataclass(frozen=True, slots=True)
class SetupAdapterConfig:
    sources: tuple[SourceSpec, ...] = (_DEFAULT_SOURCE,)
    max_processed_sources: int = 10_000
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if isinstance(self.max_processed_sources, bool) or not isinstance(
            self.max_processed_sources, int
        ):
            raise ValueError("max_processed_sources must be an integer")
        if not 1 <= self.max_processed_sources <= 1_000_000:
            raise ValueError("max_processed_sources must be in [1, 1000000]")
        if not self.sources:
            raise ValueError("sources cannot be empty")
        keys = [source.feature_key for source in self.sources]
        if len(keys) != len(set(keys)):
            raise ValueError("source feature keys must be unique")
        object.__setattr__(self, "fingerprint", sha256_hex(self.canonical_payload()))

    @classmethod
    def from_parameters(
        cls,
        *,
        sources: Iterable[Mapping[str, object]] | None = None,
        max_processed_sources: int = 10_000,
    ) -> SetupAdapterConfig:
        parsed = (
            tuple(SourceSpec.from_mapping(value) for value in sources)
            if sources is not None
            else (_DEFAULT_SOURCE,)
        )
        return cls(sources=parsed, max_processed_sources=max_processed_sources)

    def canonical_payload(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "sources": tuple(dict(source.as_mapping()) for source in self.sources),
                "max_processed_sources": self.max_processed_sources,
            }
        )

    def as_parameters(self) -> Mapping[str, object]:
        return self.canonical_payload()
