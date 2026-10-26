from __future__ import annotations

from collections.abc import Mapping

from modular_core import Feature, MarketView

from .config import SourceSpec
from .enums import AdapterReason
from .primitives import required_text, sha256_hex

MODULE_VERSION = "0.1.0"
SETUP_FEATURE_KEY = "setup.confirmed"
REJECTION_FEATURE_KEY = "setup_adapter.rejection"


def canonical_feature(
    *,
    module_name: str,
    source: SourceSpec,
    source_feature: Feature,
    view: MarketView,
    canonical: Mapping[str, object],
    source_id: str,
    source_fingerprint: str,
) -> Feature:
    current = view.current
    return Feature(
        key=SETUP_FEATURE_KEY,
        source_module=module_name,
        created_at=current.close_time,
        value=canonical,
        symbol=required_text(canonical["symbol"], "symbol"),
        timeframe=current.timeframe,
        valid_until=current.close_time,
        schema_version="1",
        feature_id="setup-confirmed-"
        + sha256_hex(
            {
                "source_id": source_id,
                "source_fingerprint": source_fingerprint,
                "canonical": canonical,
            }
        )[:40],
        invalidate_on_gap=True,
        metadata={
            "source_feature_key": source.feature_key,
            "source_feature_id": source_id,
            "profile": source.profile.value,
            "source_module": source_feature.source_module,
        },
    )


def rejection_feature(
    *,
    module_name: str,
    view: MarketView,
    source_id: str,
    source_fingerprint: str,
    reason: AdapterReason,
    details: str,
) -> Feature:
    created_at = view.current.close_time
    payload = {
        "schema_version": "1",
        "module_version": MODULE_VERSION,
        "reason": reason.value,
        "details": details[:500],
        "source_id": source_id,
        "source_fingerprint": source_fingerprint,
        "created_at": created_at.isoformat(),
        "symbol": view.current.symbol,
    }
    return Feature(
        key=REJECTION_FEATURE_KEY,
        source_module=module_name,
        created_at=created_at,
        value=payload,
        symbol=view.current.symbol,
        timeframe=view.current.timeframe,
        valid_until=created_at,
        schema_version="1",
        feature_id="setup-adapter-rejection-" + sha256_hex(payload)[:40],
        invalidate_on_gap=True,
        metadata={"reason": reason.value},
    )
