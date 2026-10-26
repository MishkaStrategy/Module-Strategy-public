from __future__ import annotations

import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from modular_backtest import open_registered_mark_price_source
from modular_backtest.valuation import MarkValuationError

START = datetime(2026, 1, 1, tzinfo=UTC)
END = START + timedelta(minutes=5)
_SAFE_MESSAGE = "registered mark validation failed"
_SENSITIVE_OWNER_TEXT = (
    "owner failure at /private/sentinel/runner/work/mark.parquet "
    "file:///private/sentinel/dataset/manifest.json "
    "hostname=sentinel-private-host.internal "
    "token=SENTINEL_TOKEN_DO_NOT_LEAK "
    "endpoint=https://private-exchange.invalid/api"
)
_SENSITIVE_FRAGMENTS = (
    "/private/sentinel/runner/work/mark.parquet",
    "file:///private/sentinel/dataset/manifest.json",
    "sentinel-private-host.internal",
    "SENTINEL_TOKEN_DO_NOT_LEAK",
    "private-exchange.invalid",
)


@pytest.mark.parametrize("failure_phase", ["resolve", "iterate"])
def test_registered_mark_owner_failure_has_no_sensitive_exception_context(
    failure_phase: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    price_reader = pytest.importorskip("market_data_ingestion.price_dataset_reader")

    def resolve(*_args: object, **_kwargs: object) -> object:
        if failure_phase == "resolve":
            raise OSError(_SENSITIVE_OWNER_TEXT)
        return object()

    def iterate(*_args: object, **_kwargs: object):
        raise OSError(_SENSITIVE_OWNER_TEXT)

    monkeypatch.setattr(price_reader, "resolve_registered_price_dataset", resolve)
    monkeypatch.setattr(price_reader, "iter_registered_prices", iterate)

    with pytest.raises(MarkValuationError) as caught:
        open_registered_mark_price_source(
            tmp_path,
            "registered-mark",
            1,
            symbol="BTCUSDT",
            timeframe="5m",
            start=START,
            end=END,
        )

    error = caught.value
    assert str(error) == _SAFE_MESSAGE
    assert error.__cause__ is None
    assert error.__context__ is None

    public_traceback = "".join(traceback.format_exception(error))
    assert _SAFE_MESSAGE in public_traceback
    for fragment in _SENSITIVE_FRAGMENTS:
        assert fragment not in public_traceback
