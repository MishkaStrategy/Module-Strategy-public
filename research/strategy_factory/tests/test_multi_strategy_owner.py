from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from strategy_factory import multi_strategy_owner as owner_runtime
from strategy_factory.dataset import DatasetValidationPolicy
from strategy_factory.errors import CompatibilityError, ExternalComponentUnavailableError

_START = owner_runtime.MS5A_TESTED_PERIOD_START - timedelta(days=1)
_FUNDING_START = _START + timedelta(days=1)
_END = _START + timedelta(days=3)
_FUNDING_HASH = owner_runtime.MS5A_FUNDING_HASH
_MARK_PRICE_TYPE = "mark_price"


@pytest.fixture(autouse=True)
def _approved_required_gap_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = SimpleNamespace(
        canonical_hash=owner_runtime.MS5A_REQUIRED_GAP_POLICY_HASH,
        exceptions=(),
    )
    monkeypatch.setattr(
        owner_runtime,
        "_required_gap_api",
        lambda: (
            lambda adapter_version: owner_runtime.MS5A_REQUIRED_GAP_POLICY_HASH,
            lambda storage_root, digest: policy,
        ),
    )


def _manifest() -> SimpleNamespace:
    return SimpleNamespace(
        instrument=SimpleNamespace(symbol="BTCUSDT"),
        exchange=SimpleNamespace(
            id="binance",
            adapter_version=(
                "binance-test+required-gap-policy." + owner_runtime.MS5A_REQUIRED_GAP_POLICY_HASH
            ),
        ),
        market_type=SimpleNamespace(value="usdt_m_perpetual"),
        created_at=_START,
    )


def _owner_selections(
    tmp_path: Path,
    *,
    funding_mark_prices: tuple[str | None, str | None] = ("10000", "10100"),
    price_times: tuple[datetime, datetime] | None = None,
    price_opens: tuple[str, str] = ("9999", "10150"),
    mark_dataset_hash: str = _FUNDING_HASH,
) -> tuple[Any, Any, Any, Any, Any, Any, Any, Any]:
    partition_path = tmp_path / "part.parquet"
    partition_path.write_bytes(b"owner-parquet-bytes")
    partition = SimpleNamespace(
        storage_uri="normalized/part.parquet",
        start=_START,
        end=_END,
    )
    klines = SimpleNamespace(
        descriptor=SimpleNamespace(
            dataset_id=owner_runtime.MS5A_DATASET_ID,
            version=1,
            dataset_hash=owner_runtime.MS5A_DATASET_HASH,
        ),
        manifest=_manifest(),
        partitions=(partition,),
        row_count=30,
        storage_root=tmp_path,
    )
    artifact = SimpleNamespace(
        artifact_id="funding-raw-1",
        source=SimpleNamespace(value="official_rest"),
        checksum_sha256="a" * 64,
        record_count=2,
        start=_FUNDING_START,
        end=_END,
    )
    funding = SimpleNamespace(
        descriptor=SimpleNamespace(
            dataset_id=owner_runtime.MS5A_DATASET_ID,
            version=owner_runtime.MS5A_FUNDING_VERSION,
            dataset_hash=_FUNDING_HASH,
        ),
        manifest=_manifest(),
        coverage_start_inclusive=_FUNDING_START,
        coverage_end_exclusive=_END,
        event_span_start_inclusive=_FUNDING_START,
        event_span_end_exclusive=_END,
        event_count=2,
        source_record_count=2,
        partitions=(partition,),
        source_artifacts=(artifact,),
    )
    funding_records = (
        SimpleNamespace(
            funding_time=_FUNDING_START,
            funding_rate="0.0001",
            mark_price=funding_mark_prices[0],
            symbol="BTCUSDT",
            exchange_id="binance",
            market_type="usdt_m_perpetual",
        ),
        SimpleNamespace(
            funding_time=_FUNDING_START + timedelta(hours=8),
            funding_rate="-0.0002",
            mark_price=funding_mark_prices[1],
            symbol="BTCUSDT",
            exchange_id="binance",
            market_type="usdt_m_perpetual",
        ),
    )
    marks = SimpleNamespace(
        descriptor=SimpleNamespace(
            dataset_id=owner_runtime.MS5A_DATASET_ID,
            version=owner_runtime.MS5A_FUNDING_VERSION,
            dataset_hash=mark_dataset_hash,
        ),
        manifest=_manifest(),
        storage_root=tmp_path,
        partitions=(partition,),
        partition_start_inclusive=_FUNDING_START,
        partition_end_exclusive=_END,
        row_count=2,
    )
    timestamps = price_times or (
        _FUNDING_START,
        _FUNDING_START + timedelta(hours=8),
    )
    mark_records = tuple(
        SimpleNamespace(open_time=timestamp, open=value)
        for timestamp, value in zip(timestamps, price_opens, strict=True)
    )

    def resolve_klines(*args: Any, **kwargs: Any) -> Any:
        return klines

    def iter_klines(selection: Any, *, start: datetime, end: datetime) -> Any:
        del selection
        assert start == _FUNDING_START
        assert end == _END
        return iter(range(20))

    def resolve_funding(*args: Any, **kwargs: Any) -> Any:
        assert args[2] == owner_runtime.MS5A_FUNDING_VERSION
        return funding

    def iter_funding(selection: Any) -> Any:
        assert selection is funding
        return iter(funding_records)

    def resolve_prices(*args: Any, **kwargs: Any) -> Any:
        assert args[2] == owner_runtime.MS5A_FUNDING_VERSION
        assert kwargs["price_type"] == _MARK_PRICE_TYPE
        assert kwargs["timeframe"] == "5m"
        return marks

    def iter_prices(
        selection: Any,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Any:
        assert selection is marks
        assert start == _FUNDING_START
        assert end == _END
        return iter(mark_records)

    def resolve_path(root: Path, uri: str, *, kind: str) -> Path:
        del root, uri, kind
        return partition_path

    return (
        resolve_klines,
        iter_klines,
        resolve_funding,
        iter_funding,
        resolve_prices,
        iter_prices,
        _MARK_PRICE_TYPE,
        resolve_path,
    )


def test_owner_state_uses_real_ohlcv_funding_overlap(monkeypatch, tmp_path: Path) -> None:
    api = _owner_selections(tmp_path)
    monkeypatch.setattr(owner_runtime, "_market_data_api", lambda: api)

    state = owner_runtime.build_ms5a_owner_state(tmp_path, created_at=_END)

    assert state.request.exchange == owner_runtime.MS5A_EXECUTION_EXCHANGE
    assert state.request.dataset.exchange == owner_runtime.MS5A_EXECUTION_EXCHANGE
    assert state.request.dataset.period.start == _FUNDING_START
    assert state.request.dataset.period.end == _END
    assert state.request.dataset.row_count == 20
    assert state.owner_row_count == 30
    assert state.dataset_file_size == len(b"owner-parquet-bytes")
    assert state.funding_source.dataset_hash == _FUNDING_HASH
    assert state.funding_source.dataset_version == owner_runtime.MS5A_FUNDING_VERSION
    assert state.funding_source.event_count == 2
    assert state.funding_source.schema_version == 2
    assert state.funding_source.source_record_count == 2
    assert state.funding_source.coverage_start_inclusive == _FUNDING_START
    assert state.funding_source.coverage_end_exclusive == _END
    assert state.funding_source.scope_start_inclusive == _FUNDING_START
    assert state.funding_source.scope_end_exclusive == _END
    assert [event.mark_price for event in state.funding_source.events] == [
        Decimal("10000"),
        Decimal("10100"),
    ]
    assert state.request.request_id == "ms5a-multi-strategy-search-v2"
    assert state.request.dataset.funding_sha256 == owner_runtime.MS5A_FUNDING_HASH
    assert state.request.dataset.funding_uri == (
        f"market-data-registry:{owner_runtime.MS5A_DATASET_ID}:"
        f"v{owner_runtime.MS5A_FUNDING_VERSION}:funding"
    )
    assert state.request.random_seed == owner_runtime.MS5A_RANDOM_SEED
    assert state.request.allowed_strategy_families == owner_runtime.MS5A_ALLOWED_FAMILIES
    assert state.request.allowed_timeframes == (
        "5m",
        *owner_runtime.MS5A_REQUIRED_TIMEFRAMES,
    )
    assert state.request.budget.maximum_experiments == 132
    assert state.request.budget.maximum_architectures == 12
    assert state.request.budget.maximum_finalists == 3
    assert state.request.budget.maximum_parallel_tasks == 1
    assert state.request.budget.maximum_cpu_workers == 1
    assert state.request.budget.stage_experiment_limits == {
        "structural_screening": 96,
        "coarse_search": 24,
        "local_refinement": 12,
    }
    assert "walk_forward" not in state.request.budget.stage_experiment_limits
    assert "holdout" not in state.request.budget.stage_experiment_limits


def test_owner_resolution_diagnostics_are_allowlisted_and_semantics_neutral(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    api = _owner_selections(tmp_path)
    deep_calls: list[bool | None] = []
    for index in (0, 2, 4):
        resolver = api[index]

        def track_deep(*args: Any, _resolver: Any = resolver, **kwargs: Any) -> Any:
            deep_calls.append(kwargs.get("deep"))
            return _resolver(*args, **kwargs)

        api = (*api[:index], track_deep, *api[index + 1 :])
    monkeypatch.setattr(owner_runtime, "_market_data_api", lambda: api)
    baseline = owner_runtime.build_ms5a_owner_state(tmp_path, created_at=_END)
    events: list[tuple[str, str | None, int | None]] = []
    diagnostics = owner_runtime.OwnerResolutionDiagnostics(
        deadline_monotonic=100.0,
        record=lambda *event: events.append(event),
        monotonic=lambda: 1.0,
    )

    observed = owner_runtime.build_ms5a_owner_state(
        tmp_path, created_at=_END, diagnostics=diagnostics
    )

    assert observed.request == baseline.request
    assert observed.owner_row_count == baseline.owner_row_count == 30
    assert observed.request.dataset.row_count == baseline.request.dataset.row_count == 20
    assert observed.dataset_file_size == baseline.dataset_file_size
    assert observed.funding_source == baseline.funding_source
    expected = [
        ("owner_kline_resolver_started", None, None),
        ("owner_kline_resolver_returned", "partitions_processed", 1),
        ("owner_funding_resolver_started", None, None),
        ("owner_funding_resolver_returned", "partitions_processed", 1),
        ("owner_mark_price_resolver_started", None, None),
        ("owner_mark_price_resolver_returned", "partitions_processed", 1),
        ("owner_kline_overlap_count_started", None, None),
        ("owner_kline_overlap_count_returned", "rows_processed", 20),
        ("owner_registered_file_size_started", None, None),
        ("owner_registered_file_size_returned", "files_processed", 1),
        ("owner_funding_mark_merge_started", None, None),
        ("owner_funding_mark_merge_returned", "events_built", 2),
    ]
    assert events == expected
    assert deep_calls == [True] * 6
    assert "/" not in str(events)


def test_owner_default_path_avoids_per_record_deadline_wrappers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(owner_runtime, "_market_data_api", lambda: _owner_selections(tmp_path))
    monkeypatch.setattr(
        owner_runtime,
        "_deadline_checked",
        lambda *_args, **_kwargs: pytest.fail("default owner path must not wrap iterators"),
    )

    state = owner_runtime.build_ms5a_owner_state(tmp_path, created_at=_END)

    assert state.owner_row_count == 30
    assert state.funding_source.event_count == 2


def test_owner_deadline_stops_before_first_resolver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    api = list(_owner_selections(tmp_path))
    calls: list[str] = []
    api[0] = lambda *_args, **_kwargs: calls.append("kline")
    monkeypatch.setattr(owner_runtime, "_market_data_api", lambda: tuple(api))
    events: list[tuple[str, str | None, int | None]] = []
    diagnostics = owner_runtime.OwnerResolutionDiagnostics(
        deadline_monotonic=4.0,
        record=lambda *event: events.append(event),
        monotonic=lambda: 4.0,
    )

    with pytest.raises(TimeoutError, match="registered owner setup exhausted native deadline"):
        owner_runtime.build_ms5a_owner_state(tmp_path, created_at=_END, diagnostics=diagnostics)

    assert calls == []
    assert events == []


def test_resolver_timeout_retains_default_unavailable_error(monkeypatch, tmp_path: Path) -> None:
    api = list(_owner_selections(tmp_path))

    def resolver_timeout(*_args: Any, **_kwargs: Any) -> Any:
        raise TimeoutError("synthetic resolver timeout")

    api[0] = resolver_timeout
    monkeypatch.setattr(owner_runtime, "_market_data_api", lambda: tuple(api))

    with pytest.raises(
        ExternalComponentUnavailableError,
        match="MS5A registered Market Data owner cannot be deep-verified",
    ) as raised:
        owner_runtime.build_ms5a_owner_state(tmp_path, created_at=_END)

    assert isinstance(raised.value.__cause__, TimeoutError)


def test_owner_deadline_after_single_resolver_does_not_start_next_phase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    api = list(_owner_selections(tmp_path))
    now = [1.0]
    calls: list[str] = []
    resolve_klines = api[0]

    def late_kline_resolver(*args: Any, **kwargs: Any) -> Any:
        calls.append("kline")
        result = resolve_klines(*args, **kwargs)
        now[0] = 5.0
        return result

    api[0] = late_kline_resolver
    api[2] = lambda *_args, **_kwargs: calls.append("funding")
    monkeypatch.setattr(owner_runtime, "_market_data_api", lambda: tuple(api))
    events: list[tuple[str, str | None, int | None]] = []
    diagnostics = owner_runtime.OwnerResolutionDiagnostics(
        deadline_monotonic=5.0,
        record=lambda *event: events.append(event),
        monotonic=lambda: now[0],
    )

    with pytest.raises(TimeoutError, match="registered owner setup exhausted native deadline"):
        owner_runtime.build_ms5a_owner_state(tmp_path, created_at=_END, diagnostics=diagnostics)

    assert calls == ["kline"]
    assert events == [
        ("owner_kline_resolver_started", None, None),
        ("owner_kline_resolver_returned", "partitions_processed", 1),
    ]
    # The injected resolver returned after the absolute deadline; cooperative
    # checks stop the next phase, but cannot preempt that single call.


def test_owner_diagnostics_reject_counter_for_wrong_phase() -> None:
    events: list[tuple[str, str | None, int | None]] = []
    diagnostics = owner_runtime.OwnerResolutionDiagnostics(
        deadline_monotonic=100.0,
        record=lambda *event: events.append(event),
        monotonic=lambda: 1.0,
    )

    with pytest.raises(CompatibilityError, match="counter does not match phase"):
        diagnostics.finish("kline_resolver", counter_name="events_built", counter_value=1)

    assert events == []


def test_owner_deadline_is_checked_between_kline_rows() -> None:
    now = [1.0]
    seen: list[int] = []

    def records() -> Any:
        seen.append(1)
        now[0] = 2.0
        yield 1
        seen.append(2)
        yield 2

    diagnostics = owner_runtime.OwnerResolutionDiagnostics(
        deadline_monotonic=2.0,
        record=lambda *_event: None,
        monotonic=lambda: now[0],
    )

    with pytest.raises(TimeoutError, match="registered owner setup exhausted native deadline"):
        owner_runtime._count_owner_rows(records(), diagnostics)

    assert seen == [1]


def test_owner_deadline_is_checked_between_registered_file_stats(tmp_path: Path) -> None:
    file_path = tmp_path / "synthetic.parquet"
    file_path.write_bytes(b"synthetic")
    partition = SimpleNamespace(storage_uri="synthetic.parquet")
    selection = SimpleNamespace(partitions=(partition, partition), storage_root=tmp_path)
    resolved: list[str] = []
    checks = 0

    def check_deadline() -> None:
        nonlocal checks
        checks += 1
        if checks == 3:
            raise TimeoutError("synthetic deadline")

    def resolve(_root: Path, uri: str, *, kind: str) -> Path:
        assert kind == "canonical kline partition"
        resolved.append(uri)
        return file_path

    with pytest.raises(TimeoutError, match="synthetic deadline"):
        owner_runtime._registered_dataset_file_size(
            selection, resolve, deadline_check=check_deadline
        )

    assert resolved == ["synthetic.parquet"]


def test_owner_deadline_is_checked_inside_funding_mark_merge(tmp_path: Path) -> None:
    api = _owner_selections(tmp_path)
    resolve_funding, iter_funding = api[2], api[3]
    resolve_prices, iter_prices = api[4], api[5]
    funding = resolve_funding(
        tmp_path,
        owner_runtime.MS5A_DATASET_ID,
        owner_runtime.MS5A_FUNDING_VERSION,
        consumer="strategy_factory",
        deep=True,
    )
    marks = resolve_prices(
        tmp_path,
        owner_runtime.MS5A_DATASET_ID,
        owner_runtime.MS5A_FUNDING_VERSION,
        price_type=api[6],
        timeframe="5m",
        consumer="strategy_factory",
        deep=True,
    )
    price_records = iter_prices(marks, start=_FUNDING_START, end=_END)
    checks = 0

    def check_deadline() -> None:
        nonlocal checks
        checks += 1
        if checks == 6:
            raise TimeoutError("synthetic deadline")

    with pytest.raises(TimeoutError, match="synthetic deadline"):
        owner_runtime._registered_funding_source(
            funding,
            iter_funding(funding),
            marks,
            price_records,
            scope_start=_FUNDING_START,
            scope_end=_END,
            deadline_check=check_deadline,
        )

    assert checks == 6


def test_owner_state_uses_exact_registered_mark_price_fallback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    api = _owner_selections(
        tmp_path,
        funding_mark_prices=(None, "10100"),
        price_opens=("9999", "10150"),
    )
    monkeypatch.setattr(owner_runtime, "_market_data_api", lambda: api)

    state = owner_runtime.build_ms5a_owner_state(tmp_path, created_at=_END)

    assert [event.mark_price for event in state.funding_source.events] == [
        Decimal("9999"),
        Decimal("10100"),
    ]


def test_owner_state_rejects_missing_exact_mark_price_timestamp(
    monkeypatch,
    tmp_path: Path,
) -> None:
    api = _owner_selections(
        tmp_path,
        funding_mark_prices=(None, "10100"),
        price_times=(
            _FUNDING_START + timedelta(minutes=5),
            _FUNDING_START + timedelta(hours=8),
        ),
    )
    monkeypatch.setattr(owner_runtime, "_market_data_api", lambda: api)

    with pytest.raises(CompatibilityError, match="exact funding timestamp"):
        owner_runtime.build_ms5a_owner_state(tmp_path, created_at=_END)


def test_funding_source_excludes_pre_boundary_record_before_valuation() -> None:
    selection = SimpleNamespace(
        descriptor=SimpleNamespace(dataset_id="funding", version=4, dataset_hash=_FUNDING_HASH),
        manifest=_manifest(),
        coverage_start_inclusive=_START,
        coverage_end_exclusive=_END,
        source_record_count=2,
        source_artifacts=(
            SimpleNamespace(
                artifact_id="raw",
                source=SimpleNamespace(value="official_rest"),
                checksum_sha256="a" * 64,
                record_count=2,
                start=_START,
                end=_END,
            ),
        ),
    )
    records = (
        SimpleNamespace(
            funding_time=_START,
            funding_rate="0.1",
            mark_price=None,
            symbol="BTCUSDT",
            exchange_id="binance",
            market_type="usdt_m_perpetual",
        ),
        SimpleNamespace(
            funding_time=_FUNDING_START,
            funding_rate="0.2",
            mark_price=None,
            symbol="BTCUSDT",
            exchange_id="binance",
            market_type="usdt_m_perpetual",
        ),
    )
    prices = SimpleNamespace(descriptor=selection.descriptor)
    source = owner_runtime._registered_funding_source(
        selection,
        records,
        prices,
        (SimpleNamespace(open_time=_FUNDING_START, open="100"),),
        scope_start=_FUNDING_START,
        scope_end=_END,
    )

    assert source.event_count == 1
    assert source.source_record_count == 2
    assert source.events[0].timestamp == _FUNDING_START
    assert source.identity()["scope_start_inclusive"] == _FUNDING_START.isoformat()
    with pytest.raises(ValueError, match="does not cover"):
        source.assert_covers(_START, _END)


def test_funding_source_preserves_full_owner_and_scopes_at_read_time() -> None:
    selection = SimpleNamespace(
        descriptor=SimpleNamespace(dataset_id="funding", version=4, dataset_hash=_FUNDING_HASH),
        manifest=_manifest(),
        coverage_start_inclusive=_START,
        coverage_end_exclusive=_END,
        source_record_count=2,
        source_artifacts=(
            SimpleNamespace(
                artifact_id="raw",
                source=SimpleNamespace(value="official_rest"),
                checksum_sha256="a" * 64,
                record_count=2,
                start=_START,
                end=_END,
            ),
        ),
    )
    records = (
        SimpleNamespace(
            funding_time=_START,
            funding_rate="0.1",
            mark_price=None,
            symbol="BTCUSDT",
            exchange_id="binance",
            market_type="usdt_m_perpetual",
        ),
        SimpleNamespace(
            funding_time=_FUNDING_START,
            funding_rate="0.2",
            mark_price=None,
            symbol="BTCUSDT",
            exchange_id="binance",
            market_type="usdt_m_perpetual",
        ),
    )
    prices = SimpleNamespace(descriptor=selection.descriptor)
    source = owner_runtime._registered_funding_source(
        selection,
        records,
        prices,
        (
            SimpleNamespace(open_time=_START, open="99"),
            SimpleNamespace(open_time=_FUNDING_START, open="100"),
        ),
        scope_start=_START,
        scope_end=_END,
    )

    assert source.event_count == 2
    assert source.source_record_count == 2
    assert tuple(item.timestamp for item in source.events) == (_START, _FUNDING_START)
    assert tuple(item.timestamp for item in source.events_between(_FUNDING_START, _END)) == (
        _FUNDING_START,
    )
    source.assert_covers(_START, _END)


def test_owner_state_rejects_mark_price_dataset_hash_drift(monkeypatch, tmp_path: Path) -> None:
    api = _owner_selections(tmp_path, mark_dataset_hash="e" * 64)
    monkeypatch.setattr(owner_runtime, "_market_data_api", lambda: api)

    with pytest.raises(
        CompatibilityError,
        match="mark-price owner hash differs from frozen v6 identity",
    ):
        owner_runtime.build_ms5a_owner_state(tmp_path, created_at=_END)


def test_owner_state_rejects_exact_funding_v6_hash_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = list(_owner_selections(tmp_path))
    _, funding, _ = _resolved_owner_objects(api)
    funding.descriptor.dataset_hash = "e" * 64
    monkeypatch.setattr(owner_runtime, "_market_data_api", lambda: tuple(api))

    with pytest.raises(
        CompatibilityError,
        match="funding owner hash differs from frozen v6 identity",
    ):
        owner_runtime.build_ms5a_owner_state(tmp_path, created_at=_END)


def test_owner_state_rejects_required_gap_policy_hash_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = _owner_selections(tmp_path)
    monkeypatch.setattr(owner_runtime, "_market_data_api", lambda: api)
    monkeypatch.setattr(
        owner_runtime,
        "_required_gap_api",
        lambda: (lambda adapter_version: "0" * 64, lambda storage_root, digest: None),
    )

    with pytest.raises(CompatibilityError, match="policy hash differs"):
        owner_runtime.build_ms5a_owner_state(tmp_path, created_at=_END)


def test_sparse_mark_iterator_splits_only_approved_gaps() -> None:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    gap_start = start + timedelta(minutes=10)
    gap_end = start + timedelta(minutes=20)
    end = start + timedelta(minutes=30)
    selection = SimpleNamespace(
        partition_start_inclusive=start,
        partition_end_exclusive=end,
        row_count=4,
    )
    policy = SimpleNamespace(
        exceptions=(SimpleNamespace(start=gap_start, end=gap_end),),
    )
    calls: list[tuple[datetime, datetime]] = []

    def iter_prices(selection_arg: Any, *, start: datetime, end: datetime) -> Any:
        assert selection_arg is selection
        calls.append((start, end))
        cursor = start
        records = []
        while cursor < end:
            records.append(SimpleNamespace(open_time=cursor, open="100"))
            cursor += timedelta(minutes=5)
        return iter(records)

    records = list(owner_runtime._iter_ms5a_mark_prices(selection, iter_prices, policy))

    assert len(records) == 4
    assert calls == [(start, gap_start), (gap_end, end)]


def test_sparse_mark_iterator_rejects_rows_inside_approved_gap() -> None:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    gap_start = start + timedelta(minutes=10)
    gap_end = start + timedelta(minutes=20)
    end = start + timedelta(minutes=30)
    selection = SimpleNamespace(
        partition_start_inclusive=start,
        partition_end_exclusive=end,
        row_count=5,
    )
    policy = SimpleNamespace(
        exceptions=(SimpleNamespace(start=gap_start, end=gap_end),),
    )

    def iter_prices(selection_arg: Any, *, start: datetime, end: datetime) -> Any:
        assert selection_arg is selection
        cursor = start
        records = []
        while cursor < end:
            records.append(SimpleNamespace(open_time=cursor, open="100"))
            cursor += timedelta(minutes=5)
        return iter(records)

    with pytest.raises(CompatibilityError, match="approved sparse policy"):
        list(owner_runtime._iter_ms5a_mark_prices(selection, iter_prices, policy))


def test_mark_gap_policy_scan_checks_deadline_before_remaining_gaps() -> None:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    end = start + timedelta(minutes=30_000)
    selection = SimpleNamespace(
        partition_start_inclusive=start,
        partition_end_exclusive=end,
        row_count=0,
    )
    consumed: list[int] = []

    def exceptions() -> Any:
        cursor = start
        for index in range(1_000):
            consumed.append(index)
            gap_end = cursor + timedelta(minutes=5)
            yield SimpleNamespace(start=cursor, end=gap_end)
            cursor = gap_end

    checks = 0

    def check_deadline() -> None:
        nonlocal checks
        checks += 1
        if checks == 3:
            raise TimeoutError("synthetic deadline")

    policy = SimpleNamespace(exceptions=exceptions())
    with pytest.raises(TimeoutError, match="synthetic deadline"):
        list(
            owner_runtime._iter_ms5a_mark_prices(
                selection,
                lambda *_args, **_kwargs: (),
                policy,
                deadline_check=check_deadline,
            )
        )

    assert consumed == [0]


def test_mark_record_iteration_checks_deadline_between_rows() -> None:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    end = start + timedelta(minutes=5_000)
    selection = SimpleNamespace(
        partition_start_inclusive=start,
        partition_end_exclusive=end,
        row_count=1_000,
    )
    visited: list[int] = []

    def iter_prices(*_args: Any, **_kwargs: Any) -> Any:
        for index in range(1_000):
            visited.append(index)
            yield SimpleNamespace(open_time=start + timedelta(minutes=5 * index), open="100")

    checks = 0

    def check_deadline() -> None:
        nonlocal checks
        checks += 1
        if checks == 5:
            raise TimeoutError("synthetic deadline")

    with pytest.raises(TimeoutError, match="synthetic deadline"):
        tuple(
            owner_runtime._iter_ms5a_mark_prices(
                selection,
                iter_prices,
                SimpleNamespace(exceptions=()),
                deadline_check=check_deadline,
            )
        )

    assert visited == [0, 1]


def test_sparse_owner_rejects_funding_event_inside_approved_gap() -> None:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    policy = SimpleNamespace(
        exceptions=(
            SimpleNamespace(
                start=start + timedelta(minutes=10),
                end=start + timedelta(minutes=20),
            ),
        ),
    )
    records = (SimpleNamespace(funding_time=start + timedelta(minutes=15)),)

    with pytest.raises(CompatibilityError, match="funding event intersects"):
        list(owner_runtime._funding_records_outside_required_gaps(records, policy))


def test_funding_gap_filter_checks_deadline_and_yields_clear_record() -> None:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    policy = SimpleNamespace(
        exceptions=(
            SimpleNamespace(
                start=start + timedelta(minutes=10),
                end=start + timedelta(minutes=20),
            ),
        ),
    )
    record = SimpleNamespace(funding_time=start)
    checks = 0

    def check_deadline() -> None:
        nonlocal checks
        checks += 1

    result = tuple(
        owner_runtime._funding_records_outside_required_gaps(
            (record,), policy, deadline_check=check_deadline
        )
    )

    assert result == (record,)
    assert checks == 4


def test_funding_gap_deadline_stops_before_advancing_owner_records() -> None:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    policy = SimpleNamespace(
        exceptions=(
            SimpleNamespace(
                start=start + timedelta(minutes=10),
                end=start + timedelta(minutes=20),
            ),
        ),
    )
    visited: list[int] = []

    def records() -> Any:
        for index in range(100):
            visited.append(index)
            yield SimpleNamespace(funding_time=start)

    checks = 0

    def check_deadline() -> None:
        nonlocal checks
        checks += 1
        if checks == 4:
            raise TimeoutError("synthetic deadline")

    with pytest.raises(TimeoutError, match="synthetic deadline"):
        tuple(
            owner_runtime._funding_records_outside_required_gaps(
                records(), policy, deadline_check=check_deadline
            )
        )

    assert visited == [0]


def test_canonical_owner_analyzer_accepts_verified_subset(monkeypatch, tmp_path: Path) -> None:
    api = _owner_selections(tmp_path)
    monkeypatch.setattr(owner_runtime, "_market_data_api", lambda: api)
    owner = owner_runtime.build_ms5a_owner_state(tmp_path, created_at=_END)
    analyzer = owner_runtime.MS5ARegisteredDatasetAnalyzer(
        DatasetValidationPolicy(interval_seconds=300),
        storage_root=tmp_path,
        dataset_id=owner_runtime.MS5A_DATASET_ID,
        dataset_version=1,
        base_timeframe="5m",
        derived_timeframes=owner_runtime.MS5A_REQUIRED_TIMEFRAMES,
        funding_dataset_id=owner_runtime.MS5A_DATASET_ID,
        funding_dataset_version=owner_runtime.MS5A_FUNDING_VERSION,
    )

    selection = analyzer._resolve_selection(owner.request.dataset)
    assert selection.row_count == 30

    drifted = replace(owner.request.dataset, exchange="binance")
    with pytest.raises(CompatibilityError, match="execution exchange alias"):
        analyzer._resolve_selection(drifted)


def test_ms5a_owner_request_fingerprint_is_deterministic(monkeypatch, tmp_path: Path) -> None:
    api = _owner_selections(tmp_path)
    monkeypatch.setattr(owner_runtime, "_market_data_api", lambda: api)

    first = owner_runtime.build_ms5a_owner_state(tmp_path, created_at=_END)
    second = owner_runtime.build_ms5a_owner_state(tmp_path, created_at=_END)

    assert first.request == second.request
    assert first.request.fingerprint == second.request.fingerprint
    assert len(first.request.fingerprint) == 64


_ORIGINAL_REQUIRED_GAP_API = owner_runtime._required_gap_api


def test_owner_optional_api_loaders_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str):
        raise ImportError(name)

    monkeypatch.setattr(owner_runtime.importlib, "import_module", missing)
    with pytest.raises(Exception, match="Market Data owner package is unavailable"):
        owner_runtime._market_data_api()

    monkeypatch.setattr(owner_runtime, "_required_gap_api", _ORIGINAL_REQUIRED_GAP_API)
    with pytest.raises(Exception, match="Market Data gap policy is unavailable"):
        owner_runtime._required_gap_api()


def test_required_gap_policy_rejects_identity_sidecar_and_hash_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    selection = SimpleNamespace(
        storage_root=tmp_path,
        manifest=SimpleNamespace(exchange=SimpleNamespace(adapter_version="adapter")),
    )

    def invalid_identity(value):
        del value
        raise ValueError("invalid")

    monkeypatch.setattr(
        owner_runtime,
        "_required_gap_api",
        lambda: (invalid_identity, lambda root, digest: None),
    )
    with pytest.raises(CompatibilityError, match="policy identity is invalid"):
        owner_runtime._ms5a_required_gap_policy(selection)

    monkeypatch.setattr(
        owner_runtime,
        "_required_gap_api",
        lambda: (
            lambda value: owner_runtime.MS5A_REQUIRED_GAP_POLICY_HASH,
            lambda root, digest: (_ for _ in ()).throw(OSError("missing")),
        ),
    )
    with pytest.raises(CompatibilityError, match="sidecar cannot be verified"):
        owner_runtime._ms5a_required_gap_policy(selection)

    monkeypatch.setattr(
        owner_runtime,
        "_required_gap_api",
        lambda: (
            lambda value: owner_runtime.MS5A_REQUIRED_GAP_POLICY_HASH,
            lambda root, digest: SimpleNamespace(canonical_hash="0" * 64),
        ),
    )
    with pytest.raises(CompatibilityError, match="sidecar hash differs"):
        owner_runtime._ms5a_required_gap_policy(selection)


def test_registered_dataset_file_size_rejects_missing_empty_and_no_partitions(
    tmp_path: Path,
) -> None:
    partition = SimpleNamespace(storage_uri="part.parquet")
    selection = SimpleNamespace(storage_root=tmp_path, partitions=(partition,))

    with pytest.raises(CompatibilityError, match="missing or unsafe"):
        owner_runtime._registered_dataset_file_size(
            selection,
            lambda root, uri, *, kind: tmp_path / "missing.parquet",
        )

    empty = tmp_path / "empty.parquet"
    empty.write_bytes(b"")
    with pytest.raises(CompatibilityError, match="partition is empty"):
        owner_runtime._registered_dataset_file_size(
            selection,
            lambda root, uri, *, kind: empty,
        )

    with pytest.raises(CompatibilityError, match="no physical bytes"):
        owner_runtime._registered_dataset_file_size(
            SimpleNamespace(storage_root=tmp_path, partitions=()),
            lambda root, uri, *, kind: tmp_path / uri,
        )


def test_funding_event_rejects_identity_invalid_and_nonpositive_mark_price() -> None:
    manifest = _manifest()
    base = SimpleNamespace(
        funding_time=_FUNDING_START,
        funding_rate="0.0001",
        symbol="BTCUSDT",
        exchange_id="binance",
        market_type="usdt_m_perpetual",
    )

    with pytest.raises(CompatibilityError, match="identity differs"):
        owner_runtime._funding_event(
            SimpleNamespace(**{**vars(base), "symbol": "ETHUSDT"}),
            manifest=manifest,
            ordinal=0,
            mark_price_text="100",
            funding_event_type=lambda **kwargs: kwargs,
        )
    with pytest.raises(CompatibilityError, match="mark price is invalid"):
        owner_runtime._funding_event(
            base,
            manifest=manifest,
            ordinal=0,
            mark_price_text="not-a-number",
            funding_event_type=lambda **kwargs: kwargs,
        )
    with pytest.raises(CompatibilityError, match="must be positive"):
        owner_runtime._funding_event(
            base,
            manifest=manifest,
            ordinal=0,
            mark_price_text="0",
            funding_event_type=lambda **kwargs: kwargs,
        )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    (
        ("dataset_hash", "b" * 64, "owner hashes differ"),
        ("dataset_id", "other", "owner identities differ"),
        ("version", 5, "owner versions differ"),
    ),
)
def test_registered_funding_source_rejects_price_owner_identity_drift(
    field: str,
    value,
    match: str,
) -> None:
    descriptor = SimpleNamespace(dataset_hash="a" * 64, dataset_id="funding", version=4)
    selection = SimpleNamespace(descriptor=descriptor)
    price_descriptor = SimpleNamespace(dataset_hash="a" * 64, dataset_id="funding", version=4)
    setattr(price_descriptor, field, value)

    with pytest.raises(CompatibilityError, match=match):
        owner_runtime._registered_funding_source(
            selection,
            (),
            SimpleNamespace(descriptor=price_descriptor),
            (),
            scope_start=_FUNDING_START,
            scope_end=_END,
        )


def test_owner_analyzer_rejects_descriptor_contract_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = _owner_selections(tmp_path)
    monkeypatch.setattr(owner_runtime, "_market_data_api", lambda: api)
    owner = owner_runtime.build_ms5a_owner_state(tmp_path, created_at=_END)
    analyzer = owner_runtime.MS5ARegisteredDatasetAnalyzer(
        DatasetValidationPolicy(interval_seconds=300),
        storage_root=tmp_path,
        dataset_id=owner_runtime.MS5A_DATASET_ID,
        dataset_version=1,
        base_timeframe="5m",
        derived_timeframes=owner_runtime.MS5A_REQUIRED_TIMEFRAMES,
        funding_dataset_id=owner_runtime.MS5A_DATASET_ID,
        funding_dataset_version=owner_runtime.MS5A_FUNDING_VERSION,
    )
    descriptor = owner.request.dataset

    cases = (
        (replace(descriptor, dataset_id="other"), "Registry identity"),
        (
            replace(
                descriptor,
                base_timeframe="1m",
                available_timeframes=("1m", *descriptor.available_timeframes),
            ),
            "base timeframe differs",
        ),
        (
            replace(
                descriptor,
                available_timeframes=(*descriptor.available_timeframes, "1m"),
            ),
            "timeframes exceed",
        ),
        (replace(descriptor, file_format="csv"), "requires a Parquet"),
        (
            replace(descriptor, funding_uri=None, funding_sha256=None),
            "requires funding lineage",
        ),
    )
    for drifted, match in cases:
        with pytest.raises(CompatibilityError, match=match):
            analyzer._resolve_selection(drifted)


def test_owner_state_rejects_symlink_root_and_wraps_registered_owner_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(CompatibilityError, match="root must not be a symlink"):
        owner_runtime.build_ms5a_owner_state(link, created_at=_END)

    def fail(*args, **kwargs):
        raise OSError("owner unavailable")

    monkeypatch.setattr(
        owner_runtime,
        "_market_data_api",
        lambda: (
            fail,
            lambda *args, **kwargs: (),
            fail,
            lambda *args, **kwargs: (),
            fail,
            lambda *args, **kwargs: (),
            _MARK_PRICE_TYPE,
            lambda *args, **kwargs: tmp_path / "part.parquet",
        ),
    )
    with pytest.raises(Exception, match="cannot be deep-verified"):
        owner_runtime.build_ms5a_owner_state(tmp_path, created_at=_END)


@pytest.mark.parametrize(
    "exception",
    (
        SimpleNamespace(
            start=_FUNDING_START - timedelta(minutes=5),
            end=_FUNDING_START + timedelta(minutes=5),
        ),
        SimpleNamespace(
            start=_END - timedelta(minutes=5),
            end=_END + timedelta(minutes=5),
        ),
    ),
)
def test_sparse_mark_iterator_rejects_gap_outside_registered_coverage(exception) -> None:
    selection = SimpleNamespace(
        partition_start_inclusive=_FUNDING_START,
        partition_end_exclusive=_END,
        row_count=0,
    )
    policy = SimpleNamespace(exceptions=(exception,))

    with pytest.raises(CompatibilityError, match="gap escapes registered coverage"):
        list(
            owner_runtime._iter_ms5a_mark_prices(
                selection,
                lambda *args, **kwargs: (),
                policy,
            )
        )


def test_funding_period_filter_is_half_open() -> None:
    records = (
        SimpleNamespace(funding_time=_FUNDING_START - timedelta(seconds=1)),
        SimpleNamespace(funding_time=_FUNDING_START),
        SimpleNamespace(funding_time=_END - timedelta(seconds=1)),
        SimpleNamespace(funding_time=_END),
    )

    selected = tuple(owner_runtime._funding_records_in_period(records, _FUNDING_START, _END))

    assert tuple(item.funding_time for item in selected) == (
        _FUNDING_START,
        _END - timedelta(seconds=1),
    )


@pytest.mark.parametrize("excluded_side", ["prefix", "suffix"])
def test_funding_filter_checks_deadline_on_raw_excluded_rows(
    excluded_side: str,
) -> None:
    if excluded_side == "prefix":
        times = [_FUNDING_START - timedelta(seconds=1)] * 1_000
    else:
        times = [_FUNDING_START, *([_END] * 1_000)]
    visited: list[int] = []

    def records() -> Any:
        for index, funding_time in enumerate(times):
            visited.append(index)
            yield SimpleNamespace(funding_time=funding_time)

    checks = 0

    def check_deadline() -> None:
        nonlocal checks
        checks += 1
        if checks == 6:
            raise TimeoutError("synthetic deadline")

    with pytest.raises(TimeoutError, match="synthetic deadline"):
        tuple(
            owner_runtime._funding_records_in_period(
                records(),
                _FUNDING_START,
                _END,
                deadline_check=check_deadline,
            )
        )

    assert len(visited) < len(times)
    assert len(visited) == 3


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("symbol", "ETHUSDT"),
        ("exchange_id", "other"),
        ("market_type", "spot"),
    ),
)
def test_funding_event_rejects_each_identity_dimension(field: str, value: str) -> None:
    payload = {
        "funding_time": _FUNDING_START,
        "funding_rate": "0.0001",
        "symbol": "BTCUSDT",
        "exchange_id": "binance",
        "market_type": "usdt_m_perpetual",
    }
    payload[field] = value

    with pytest.raises(CompatibilityError, match="identity differs"):
        owner_runtime._funding_event(
            SimpleNamespace(**payload),
            manifest=_manifest(),
            ordinal=0,
            mark_price_text="100",
            funding_event_type=lambda **kwargs: kwargs,
        )


def test_registered_funding_source_rejects_trailing_event_without_exact_mark() -> None:
    descriptor = SimpleNamespace(dataset_id="funding", version=4, dataset_hash=_FUNDING_HASH)
    selection = SimpleNamespace(
        descriptor=descriptor,
        manifest=_manifest(),
        coverage_start_inclusive=_FUNDING_START,
        coverage_end_exclusive=_END,
        source_record_count=1,
        source_artifacts=(
            SimpleNamespace(
                artifact_id="raw",
                source="official_rest",
                checksum_sha256="a" * 64,
                record_count=1,
                start=_FUNDING_START,
                end=_END,
            ),
        ),
    )
    record = SimpleNamespace(
        funding_time=_FUNDING_START + timedelta(hours=8),
        funding_rate="0.0001",
        mark_price=None,
        symbol="BTCUSDT",
        exchange_id="binance",
        market_type="usdt_m_perpetual",
    )

    with pytest.raises(CompatibilityError, match="lacks exact funding timestamp"):
        owner_runtime._registered_funding_source(
            selection,
            (record,),
            SimpleNamespace(descriptor=descriptor),
            (),
            scope_start=_FUNDING_START,
            scope_end=_END,
        )


def test_registered_dataset_file_size_rejects_symlink_partition(tmp_path: Path) -> None:
    target = tmp_path / "target.parquet"
    target.write_bytes(b"data")
    link = tmp_path / "link.parquet"
    link.symlink_to(target)
    selection = SimpleNamespace(
        storage_root=tmp_path,
        partitions=(SimpleNamespace(storage_uri="link.parquet"),),
    )

    with pytest.raises(CompatibilityError, match="missing or unsafe"):
        owner_runtime._registered_dataset_file_size(
            selection,
            lambda root, uri, *, kind: link,
        )


def _resolved_owner_objects(api):
    klines = api[0]()
    funding = api[2](None, None, owner_runtime.MS5A_FUNDING_VERSION)
    marks = api[4](
        None,
        None,
        owner_runtime.MS5A_FUNDING_VERSION,
        price_type=_MARK_PRICE_TYPE,
        timeframe="5m",
    )
    return klines, funding, marks


def test_owner_state_rejects_each_registered_identity_dimension(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cases = (
        ("OHLCV", 0, "symbol", "ETHUSDT", "symbol mismatch"),
        ("OHLCV", 0, "exchange", "other", "exchange mismatch"),
        ("OHLCV", 0, "market", "spot", "market mismatch"),
        ("funding", 1, "symbol", "ETHUSDT", "symbol mismatch"),
        ("funding", 1, "exchange", "other", "exchange mismatch"),
        ("funding", 1, "market", "spot", "market mismatch"),
        ("mark-price", 2, "symbol", "ETHUSDT", "symbol mismatch"),
        ("mark-price", 2, "exchange", "other", "exchange mismatch"),
        ("mark-price", 2, "market", "spot", "market mismatch"),
    )
    for label, index, field, value, suffix in cases:
        api = list(_owner_selections(tmp_path))
        objects = _resolved_owner_objects(api)
        selection = objects[index]
        if field == "symbol":
            selection.manifest.instrument.symbol = value
        elif field == "exchange":
            selection.manifest.exchange.id = value
        else:
            selection.manifest.market_type.value = value
        monkeypatch.setattr(owner_runtime, "_market_data_api", lambda api=tuple(api): api)

        with pytest.raises(CompatibilityError, match=f"canonical {label} owner {suffix}"):
            owner_runtime.build_ms5a_owner_state(tmp_path, created_at=_END)


def test_owner_state_rejects_funding_owner_and_coverage_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = list(_owner_selections(tmp_path))
    _, _, marks = _resolved_owner_objects(api)
    marks.descriptor.dataset_id = "other"
    monkeypatch.setattr(owner_runtime, "_market_data_api", lambda: tuple(api))
    with pytest.raises(
        CompatibilityError,
        match="mark-price owner dataset identity differs from frozen v6 identity",
    ):
        owner_runtime.build_ms5a_owner_state(tmp_path, created_at=_END)

    api = list(_owner_selections(tmp_path))
    _, _, marks = _resolved_owner_objects(api)
    marks.descriptor.version = owner_runtime.MS5A_FUNDING_VERSION + 1
    monkeypatch.setattr(owner_runtime, "_market_data_api", lambda: tuple(api))
    with pytest.raises(
        CompatibilityError,
        match="mark-price owner version differs from frozen v6 identity",
    ):
        owner_runtime.build_ms5a_owner_state(tmp_path, created_at=_END)

    api = list(_owner_selections(tmp_path))
    _, funding, marks = _resolved_owner_objects(api)
    marks.partition_start_inclusive = funding.event_span_start_inclusive + timedelta(minutes=5)
    monkeypatch.setattr(owner_runtime, "_market_data_api", lambda: tuple(api))
    with pytest.raises(CompatibilityError, match="starts after funding events"):
        owner_runtime.build_ms5a_owner_state(tmp_path, created_at=_END)

    api = list(_owner_selections(tmp_path))
    _, funding, marks = _resolved_owner_objects(api)
    marks.partition_end_exclusive = funding.event_span_end_exclusive - timedelta(minutes=5)
    monkeypatch.setattr(owner_runtime, "_market_data_api", lambda: tuple(api))
    with pytest.raises(CompatibilityError, match="ends before funding events"):
        owner_runtime.build_ms5a_owner_state(tmp_path, created_at=_END)

    api = list(_owner_selections(tmp_path))
    _, funding, _ = _resolved_owner_objects(api)
    funding.coverage_end_exclusive = _END - timedelta(minutes=5)
    monkeypatch.setattr(owner_runtime, "_market_data_api", lambda: tuple(api))
    with pytest.raises(CompatibilityError, match="funding owner ends before execution coverage"):
        owner_runtime.build_ms5a_owner_state(tmp_path, created_at=_END)


def test_owner_state_rejects_empty_overlap_and_empty_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = list(_owner_selections(tmp_path))
    klines, _, _ = _resolved_owner_objects(api)
    klines.partitions[0].start = _END
    monkeypatch.setattr(owner_runtime, "_market_data_api", lambda: tuple(api))
    with pytest.raises(CompatibilityError, match="coverage do not overlap"):
        owner_runtime.build_ms5a_owner_state(tmp_path, created_at=_END)

    api = list(_owner_selections(tmp_path))
    api[1] = lambda selection, *, start, end: iter(())
    monkeypatch.setattr(owner_runtime, "_market_data_api", lambda: tuple(api))
    with pytest.raises(CompatibilityError, match="contains no OHLCV rows"):
        owner_runtime.build_ms5a_owner_state(tmp_path, created_at=_END)


def test_owner_state_rejects_non_directory_root(tmp_path: Path) -> None:
    path = tmp_path / "not-a-directory"
    path.write_text("file", encoding="utf-8")

    with pytest.raises(CompatibilityError, match="must be a real directory"):
        owner_runtime.build_ms5a_owner_state(path, created_at=_END)
