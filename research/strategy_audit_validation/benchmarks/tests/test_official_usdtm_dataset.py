from __future__ import annotations

import http.client
import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "official_usdtm_dataset.py"
_SPEC = importlib.util.spec_from_file_location("official_usdtm_dataset", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
subject = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = subject
_SPEC.loader.exec_module(subject)


class _JsonResponse(io.BytesIO):
    status = 200

    def __enter__(self) -> _JsonResponse:
        return self

    def __exit__(self, *args: object) -> None:
        del args
        self.close()


def _response(payload: object) -> _JsonResponse:
    return _JsonResponse(json.dumps(payload).encode("utf-8"))


def _raw(open_time: int, close: str = "100.5") -> list[object]:
    return [
        open_time,
        "100.0",
        "101.0",
        "99.0",
        close,
        "12.0",
        open_time + subject.INTERVAL_MS - 1,
        "1200.0",
        10,
        "6.0",
        "600.0",
        "0",
    ]


def test_request_url_is_exact_public_usdtm_contract() -> None:
    url = subject.request_url(300_000, 900_000)
    parsed = urlparse(url)

    assert f"{parsed.scheme}://{parsed.netloc}" == subject.BASE_URL
    assert parsed.path == subject.ENDPOINT
    assert parse_qs(parsed.query) == {
        "endTime": ["900000"],
        "interval": ["5m"],
        "limit": ["1000"],
        "startTime": ["300000"],
        "symbol": ["BTCUSDT"],
    }


def test_default_fetch_retries_transient_url_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    def urlopen(request: object, timeout: int) -> _JsonResponse:
        nonlocal attempts
        del request
        assert timeout == 60
        attempts += 1
        if attempts < 3:
            raise urllib.error.URLError("Tunnel connection failed: 503 Service Unavailable")
        return _response({"ok": True})

    monkeypatch.setattr(subject.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(subject.time, "sleep", sleeps.append)

    assert subject._default_fetch(subject.request_url(0, 0)) == {"ok": True}
    assert attempts == 3
    assert sleeps == [1.0, 2.0]


def test_default_fetch_retries_incomplete_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    def urlopen(request: object, timeout: int) -> _JsonResponse:
        nonlocal attempts
        del request
        assert timeout == 60
        attempts += 1
        if attempts == 1:
            raise http.client.IncompleteRead(b"{", 10)
        return _response([1, 2, 3])

    monkeypatch.setattr(subject.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(subject.time, "sleep", sleeps.append)

    assert subject._default_fetch(subject.request_url(0, 0)) == [1, 2, 3]
    assert attempts == 2
    assert sleeps == [1.0]


def test_default_fetch_exhausts_bounded_transient_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    def urlopen(request: object, timeout: int) -> _JsonResponse:
        nonlocal attempts
        del request
        assert timeout == 60
        attempts += 1
        raise urllib.error.URLError("temporary tunnel failure")

    monkeypatch.setattr(subject.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(subject.time, "sleep", sleeps.append)

    with pytest.raises(RuntimeError, match=r"unavailable after 5 attempts: URLError"):
        subject._default_fetch(subject.request_url(0, 0))

    assert attempts == 5
    assert sleeps == [1.0, 2.0, 4.0, 8.0]


@pytest.mark.parametrize("status", [408, 503])
def test_default_fetch_retries_transient_http(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    def urlopen(request: object, timeout: int) -> _JsonResponse:
        nonlocal attempts
        del request
        assert timeout == 60
        attempts += 1
        if attempts == 1:
            raise urllib.error.HTTPError(
                subject.request_url(0, 0),
                status,
                "Transient HTTP failure",
                hdrs=None,
                fp=io.BytesIO(b"temporary"),
            )
        return _response({"ok": True})

    monkeypatch.setattr(subject.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(subject.time, "sleep", sleeps.append)

    assert subject._default_fetch(subject.request_url(0, 0)) == {"ok": True}
    assert attempts == 2
    assert sleeps == [1.0]


def test_default_fetch_preserves_http_451_archive_fallback_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    def urlopen(request: object, timeout: int) -> _JsonResponse:
        nonlocal attempts
        del request
        assert timeout == 60
        attempts += 1
        raise urllib.error.HTTPError(
            subject.request_url(0, 0),
            451,
            "Unavailable For Legal Reasons",
            hdrs=None,
            fp=io.BytesIO(b"restricted"),
        )

    monkeypatch.setattr(subject.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(subject.time, "sleep", sleeps.append)

    with pytest.raises(RuntimeError, match=r"Binance endpoint HTTP 451: restricted"):
        subject._default_fetch(subject.request_url(0, 0))

    assert attempts == 1
    assert sleeps == []


def test_download_paginates_without_gap_or_duplicate() -> None:
    calls: list[int] = []

    def fetch(url: str) -> object:
        start = int(parse_qs(urlparse(url).query)["startTime"][0])
        calls.append(start)
        if start == 0:
            return [_raw(0), _raw(300_000)]
        return [_raw(600_000)]

    rows, requests = subject.download_klines(
        start_ms=0,
        end_ms=600_000,
        fetch=fetch,
        throttle_seconds=0,
    )

    assert [row.open_time_ms for row in rows] == [0, 300_000, 600_000]
    assert calls == [0, 600_000]
    assert requests == 2
    assert subject.content_sha256(rows) == subject.content_sha256(list(rows))


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda row: row.__setitem__(6, 1), "close time"),
        (lambda row: row.__setitem__(2, "98"), "OHLC"),
        (lambda row: row.__setitem__(5, "-1"), "negative"),
        (lambda row: row.__setitem__(9, "13"), "exceeds"),
        (lambda row: row.__setitem__(1, "nan"), "non-finite"),
    ],
)
def test_parse_kline_fails_closed(mutation, match: str) -> None:
    row = _raw(0)
    mutation(row)

    with pytest.raises(ValueError, match=match):
        subject.parse_kline(row)


def test_download_rejects_boundary_and_endpoint_drift() -> None:
    with pytest.raises(ValueError, match="boundaries"):
        subject.download_klines(start_ms=1, end_ms=300_000, throttle_seconds=0)

    with pytest.raises(RuntimeError, match="non-array"):
        subject.download_klines(
            start_ms=0,
            end_ms=0,
            fetch=lambda _: {"error": True},
            throttle_seconds=0,
        )

    with pytest.raises(RuntimeError, match="ended before"):
        subject.download_klines(
            start_ms=0,
            end_ms=0,
            fetch=lambda _: [],
            throttle_seconds=0,
        )

    with pytest.raises(RuntimeError, match="pagination start"):
        subject.download_klines(
            start_ms=0,
            end_ms=0,
            fetch=lambda _: [_raw(300_000)],
            throttle_seconds=0,
        )


def test_expected_fixed_range_has_exact_row_count() -> None:
    start = subject._epoch_ms(subject.EXPECTED_START)
    end = subject._epoch_ms(subject.EXPECTED_END)

    assert (end - start) // subject.INTERVAL_MS + 1 == subject.EXPECTED_ROWS
