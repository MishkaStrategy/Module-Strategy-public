from __future__ import annotations

from pathlib import Path

import pytest

from market_data_ingestion.config import ConfigurationError, load_config
from market_data_ingestion.network import HttpClient, HttpResponse


def test_config_rejects_quoted_boolean(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
schema_version: 1
request_id: request-001
exchange: {id: fake, market_type: usdt_m_perpetual}
instrument: {symbol: BTCUSDT}
range: {mode: explicit, start: '2026-01-01T00:00:00Z', end: '2026-01-02T00:00:00Z'}
data:
  base_timeframe: 5m
  derived_timeframes: [15m]
  required_types: [klines]
  optional_types: []
download: {concurrency: 1, retries: 1, timeout_seconds: 5, resume: 'true'}
validation: {deep: true, repair_gaps: true, reject_unresolved_required_gaps: true, allow_partial_optional_data: true}
storage: {root: ./data, format: csv, compression: zstd, partition: monthly}
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="YAML boolean"):
        load_config(path)


def test_http_429_uses_bounded_retry() -> None:
    statuses = iter([429, 200])
    sleeps: list[float] = []

    def transport(url: str, timeout: float, headers: object) -> HttpResponse:
        status = next(statuses)
        return HttpResponse(
            status=status,
            headers={"retry-after": "0"} if status == 429 else {},
            body=b"[]",
            url=url,
        )

    client = HttpClient(retries=1, transport=transport, sleep=sleeps.append)
    response = client.get("https://example.invalid", "/data")
    assert response.status == 200
    assert sleeps == [0.0]


def test_http_5xx_exhaustion_is_bounded_and_redacts_query() -> None:
    calls: list[str] = []
    sleeps: list[float] = []

    def transport(url: str, timeout: float, headers: object) -> HttpResponse:
        calls.append(url)
        return HttpResponse(status=503, headers={}, body=b"busy", url=url)

    from market_data_ingestion.errors import AdapterError

    client = HttpClient(
        retries=2,
        transport=transport,
        sleep=sleeps.append,
        random_source=__import__("random").Random(0),
    )
    with pytest.raises(AdapterError) as captured:
        client.get("https://example.invalid", "/data", {"apiKey": "secret"})
    assert captured.value.code == "HTTP_RETRY_EXHAUSTED"
    assert len(calls) == 3
    assert len(sleeps) == 2
    assert "secret" not in str(captured.value.details)


def test_network_timeout_exhaustion_is_structured() -> None:
    calls = 0

    def transport(url: str, timeout: float, headers: object) -> HttpResponse:
        nonlocal calls
        calls += 1
        raise TimeoutError("injected timeout")

    from market_data_ingestion.errors import AdapterError

    client = HttpClient(retries=1, transport=transport, sleep=lambda _: None)
    with pytest.raises(AdapterError) as captured:
        client.get("https://example.invalid", "/data")
    assert captured.value.code == "NETWORK_RETRY_EXHAUSTED"
    assert captured.value.retryable is True
    assert calls == 2


def test_circuit_breaker_opens_and_resets(monkeypatch: pytest.MonkeyPatch) -> None:
    from market_data_ingestion import network
    from market_data_ingestion.errors import AdapterError
    from market_data_ingestion.network import CircuitBreaker

    clock = [100.0]
    monkeypatch.setattr(network.time, "monotonic", lambda: clock[0])
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=0.001)
    breaker.failure()
    with pytest.raises(AdapterError) as captured:
        breaker.before_request()
    assert captured.value.code == "CIRCUIT_OPEN"
    clock[0] += 0.002
    breaker.before_request()


def test_http_response_size_limit_fails_closed_without_retry() -> None:
    calls = 0

    def transport(url: str, timeout: float, headers: object) -> HttpResponse:
        nonlocal calls
        del timeout, headers
        calls += 1
        return HttpResponse(status=200, headers={}, body=b"x" * 17, url=url)

    from market_data_ingestion.errors import AdapterError

    client = HttpClient(
        retries=5,
        transport=transport,
        max_response_bytes=16,
        sleep=lambda _: None,
    )
    with pytest.raises(AdapterError) as captured:
        client.get("https://example.invalid", "/oversized")
    assert captured.value.code == "RESPONSE_SIZE_LIMIT"
    assert captured.value.retryable is False
    assert calls == 1


def test_config_accepts_explicit_record_memory_bound(tmp_path: Path) -> None:
    path = tmp_path / "bounded.yaml"
    path.write_text(
        """
schema_version: 1
request_id: bounded-memory
exchange: {id: fake, market_type: usdt_m_perpetual}
instrument: {symbol: BTCUSDT}
range: {mode: explicit, start: '2026-01-01T00:00:00Z', end: '2026-01-01T01:00:00Z'}
data:
  base_timeframe: 5m
  derived_timeframes: []
  required_types: [klines]
  optional_types: []
download:
  concurrency: 1
  retries: 1
  timeout_seconds: 5
  resume: true
  max_records_in_memory: 12345
validation:
  deep: true
  repair_gaps: true
  reject_unresolved_required_gaps: true
  allow_partial_optional_data: true
storage: {root: ./data, format: csv, compression: zstd, partition: monthly}
""",
        encoding="utf-8",
    )
    assert load_config(path).download.max_records_in_memory == 12345
