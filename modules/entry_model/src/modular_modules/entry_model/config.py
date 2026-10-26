from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType

from .enums import EntryKind
from .primitives import decimal_text, parse_decimal, parse_entry_kind, sha256_hex

DEFAULT_ALLOWED_ENTRY_KINDS = tuple(EntryKind)


@dataclass(frozen=True, slots=True)
class EntryModelConfig:
    default_entry_kind: EntryKind = EntryKind.LIMIT_RETRACEMENT
    retracement_fraction: Decimal = Decimal("0.382")
    ttl_bars: int = 36
    honor_setup_hint: bool = True
    post_only_limits: bool = True
    stop_limit_offset_bps: Decimal = Decimal("0")
    max_setup_age_seconds: Decimal = Decimal("0")
    max_processed_setups: int = 10_000
    allowed_entry_kinds: tuple[EntryKind, ...] = DEFAULT_ALLOWED_ENTRY_KINDS
    allowed_entry_kind_set: frozenset[EntryKind] = field(init=False, repr=False)
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if isinstance(self.ttl_bars, bool) or not isinstance(self.ttl_bars, int):
            raise ValueError("ttl_bars must be an integer")
        if isinstance(self.max_processed_setups, bool) or not isinstance(
            self.max_processed_setups, int
        ):
            raise ValueError("max_processed_setups must be an integer")
        if not isinstance(self.honor_setup_hint, bool):
            raise ValueError("honor_setup_hint must be boolean")
        if not isinstance(self.post_only_limits, bool):
            raise ValueError("post_only_limits must be boolean")
        fraction = parse_decimal(self.retracement_fraction, "retracement_fraction", positive=False)
        offset = parse_decimal(self.stop_limit_offset_bps, "stop_limit_offset_bps", positive=False)
        max_age = parse_decimal(self.max_setup_age_seconds, "max_setup_age_seconds", positive=False)
        if not Decimal("0") < fraction < Decimal("1"):
            raise ValueError("retracement_fraction must be in (0, 1)")
        if not Decimal("0") <= offset < Decimal("10000"):
            raise ValueError("stop_limit_offset_bps must be in [0, 10000)")
        if max_age < 0:
            raise ValueError("max_setup_age_seconds must be >= 0")
        if not 1 <= self.ttl_bars <= 1_000_000:
            raise ValueError("ttl_bars must be in [1, 1000000]")
        if not 1 <= self.max_processed_setups <= 1_000_000:
            raise ValueError("max_processed_setups must be in [1, 1000000]")
        allowed = tuple(dict.fromkeys(self.allowed_entry_kinds))
        if not allowed:
            raise ValueError("allowed_entry_kinds cannot be empty")
        if self.default_entry_kind not in allowed:
            raise ValueError("default_entry_kind must be included in allowed_entry_kinds")
        object.__setattr__(self, "retracement_fraction", fraction)
        object.__setattr__(self, "stop_limit_offset_bps", offset)
        object.__setattr__(self, "max_setup_age_seconds", max_age)
        object.__setattr__(self, "allowed_entry_kinds", allowed)
        object.__setattr__(self, "allowed_entry_kind_set", frozenset(allowed))
        object.__setattr__(self, "fingerprint", sha256_hex(self.canonical_payload()))

    @classmethod
    def from_parameters(
        cls,
        *,
        default_entry_kind: str | EntryKind = EntryKind.LIMIT_RETRACEMENT,
        retracement_fraction: int | float | str | Decimal = Decimal("0.382"),
        ttl_bars: int = 36,
        honor_setup_hint: bool = True,
        post_only_limits: bool = True,
        stop_limit_offset_bps: int | float | str | Decimal = Decimal("0"),
        max_setup_age_seconds: int | float | str | Decimal = Decimal("0"),
        max_processed_setups: int = 10_000,
        allowed_entry_kinds: Iterable[str | EntryKind] = DEFAULT_ALLOWED_ENTRY_KINDS,
    ) -> EntryModelConfig:
        return cls(
            default_entry_kind=parse_entry_kind(default_entry_kind),
            retracement_fraction=parse_decimal(
                retracement_fraction, "retracement_fraction", positive=False
            ),
            ttl_bars=ttl_bars,
            honor_setup_hint=honor_setup_hint,
            post_only_limits=post_only_limits,
            stop_limit_offset_bps=parse_decimal(
                stop_limit_offset_bps, "stop_limit_offset_bps", positive=False
            ),
            max_setup_age_seconds=parse_decimal(
                max_setup_age_seconds, "max_setup_age_seconds", positive=False
            ),
            max_processed_setups=max_processed_setups,
            allowed_entry_kinds=tuple(parse_entry_kind(value) for value in allowed_entry_kinds),
        )

    def canonical_payload(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "default_entry_kind": self.default_entry_kind.value,
                "retracement_fraction": decimal_text(self.retracement_fraction),
                "ttl_bars": self.ttl_bars,
                "honor_setup_hint": self.honor_setup_hint,
                "post_only_limits": self.post_only_limits,
                "stop_limit_offset_bps": decimal_text(self.stop_limit_offset_bps),
                "max_setup_age_seconds": decimal_text(self.max_setup_age_seconds),
                "max_processed_setups": self.max_processed_setups,
                "allowed_entry_kinds": tuple(
                    sorted(kind.value for kind in self.allowed_entry_kinds)
                ),
            }
        )

    def as_parameters(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "default_entry_kind": self.default_entry_kind.value,
                "retracement_fraction": decimal_text(self.retracement_fraction),
                "ttl_bars": self.ttl_bars,
                "honor_setup_hint": self.honor_setup_hint,
                "post_only_limits": self.post_only_limits,
                "stop_limit_offset_bps": decimal_text(self.stop_limit_offset_bps),
                "max_setup_age_seconds": decimal_text(self.max_setup_age_seconds),
                "max_processed_setups": self.max_processed_setups,
                "allowed_entry_kinds": tuple(kind.value for kind in self.allowed_entry_kinds),
            }
        )
