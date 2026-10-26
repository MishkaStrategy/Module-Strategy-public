from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from modular_strategy_fvg import FvgModule


class _SignatureAdapter:
    symbol = "BTCUSDT"
    timeframe = "15m"
    open = 95.0
    high = 100.0
    low = 90.0
    close = 95.0
    volume = 1.0

    def __init__(self, timestamp: datetime) -> None:
        self.timestamp = timestamp
        self.close_time = timestamp + timedelta(minutes=15)


class _BrokenSignatureAdapter(_SignatureAdapter):
    @property
    def high(self) -> float:
        raise RuntimeError("unstable high property")


def test_duplicate_signature_rejects_naive_adapter_timestamp() -> None:
    adapter = _SignatureAdapter(datetime(2024, 1, 1))

    with pytest.raises(ValueError, match="timezone-aware"):
        FvgModule._raw_signature(  # noqa: SLF001
            adapter,  # type: ignore[arg-type]
            datetime(2024, 1, 1, 0, 15, tzinfo=timezone.utc),
        )


def test_duplicate_signature_canonicalizes_adapter_property_failure() -> None:
    adapter = _BrokenSignatureAdapter(datetime(2024, 1, 1, tzinfo=timezone.utc))

    with pytest.raises(
        ValueError,
        match="signature candle attribute high cannot be read",
    ):
        FvgModule._raw_signature(  # noqa: SLF001
            adapter,  # type: ignore[arg-type]
            adapter.close_time,
        )
