from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from strategy_factory import owner_runtime
from strategy_factory.contracts import DatasetDescriptor, DatasetGateStatus, MarketType, TimeRange
from strategy_factory.dataset import DatasetValidationPolicy
from strategy_factory.errors import CompatibilityError, ExternalComponentUnavailableError
from strategy_factory.owner_runtime import PublishingResultsAggregator, RegisteredDatasetAnalyzer


def _registered_descriptor() -> DatasetDescriptor:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return DatasetDescriptor(
        dataset_id="dataset-btcusdt-5m",
        source="registered-market-data",
        symbol="BTCUSDT",
        exchange="binance_usdtm",
        market_type=MarketType.LINEAR_FUTURES,
        base_timeframe="5m",
        available_timeframes=("5m", "15m"),
        period=TimeRange(start, start + timedelta(minutes=15)),
        timezone="UTC",
        file_format="parquet",
        uri="/private/materialized/BTCUSDT_5m.parquet",
        sha256="1" * 64,
        row_count=3,
        received_at=datetime(2026, 1, 2, tzinfo=UTC),
        funding_sha256="2" * 64,
    )


def _owner_selection(descriptor: DatasetDescriptor):
    manifest = SimpleNamespace(
        instrument=SimpleNamespace(symbol=descriptor.symbol),
        exchange=SimpleNamespace(id=descriptor.exchange),
        market_type=SimpleNamespace(value="usdt_m_perpetual"),
    )
    return SimpleNamespace(
        descriptor=SimpleNamespace(dataset_hash=descriptor.sha256, version=1),
        manifest=manifest,
        row_count=descriptor.row_count,
        partitions=(SimpleNamespace(start=descriptor.period.start, end=descriptor.period.end),),
    )


def _funding_selection(descriptor: DatasetDescriptor):
    return SimpleNamespace(
        descriptor=SimpleNamespace(dataset_hash=descriptor.funding_sha256),
        manifest=SimpleNamespace(
            instrument=SimpleNamespace(symbol=descriptor.symbol),
            exchange=SimpleNamespace(id=descriptor.exchange),
            market_type=SimpleNamespace(value="usdt_m_perpetual"),
        ),
    )


def _analyzer(**overrides) -> RegisteredDatasetAnalyzer:
    kwargs = {
        "storage_root": "/not-exposed",
        "dataset_id": "dataset-btcusdt-5m",
        "dataset_version": 1,
        "derived_timeframes": ("15m",),
    }
    kwargs.update(overrides)
    return RegisteredDatasetAnalyzer(
        DatasetValidationPolicy(
            interval_seconds=300,
            minimum_rows=1,
            minimum_history_days=Decimal("0"),
        ),
        **kwargs,
    )


def _records(descriptor: DatasetDescriptor):
    return tuple(
        SimpleNamespace(
            open_time=descriptor.period.start + timedelta(minutes=5 * index),
            open=str(100 + index),
            high=str(101 + index),
            low=str(99 + index),
            close=str(100.5 + index),
            volume=str(1_000 + index),
        )
        for index in range(3)
    )


def _owner_api(monkeypatch, descriptor: DatasetDescriptor, records=None) -> None:
    selection = _owner_selection(descriptor)
    stream = _records(descriptor) if records is None else records

    def resolve_klines(*args, **kwargs):
        del args, kwargs
        return selection

    def iter_klines(resolved, *, start, end):
        assert resolved is selection
        assert start == descriptor.period.start
        assert end == descriptor.period.end
        yield from stream

    def resolve_funding(*args, **kwargs):
        del args, kwargs
        return _funding_selection(descriptor)

    monkeypatch.setattr(
        owner_runtime,
        "_market_data_api",
        lambda: (resolve_klines, iter_klines, resolve_funding),
    )


def test_registered_dataset_analyzer_uses_owner_identity_and_stream(monkeypatch) -> None:
    descriptor = _registered_descriptor()
    _owner_api(monkeypatch, descriptor)
    analyzer = _analyzer()

    analysis = analyzer.analyze(descriptor, now=datetime(2026, 1, 3, tzinfo=UTC))

    assert analysis.validation.status is DatasetGateStatus.ACCEPTED
    assert analysis.validation.computed_sha256 == descriptor.sha256
    assert analysis.validation.rows_scanned == descriptor.row_count
    assert analysis.validation.evidence["source"] == "registered_market_data_owner"
    assert "storage_root" not in analysis.validation.evidence
    assert analysis.profile is not None
    assert analysis.profile.observations == descriptor.row_count


def test_registered_dataset_analyzer_rejects_owner_hash_drift(monkeypatch) -> None:
    descriptor = _registered_descriptor()
    selection = _owner_selection(descriptor)

    def resolve_klines(*args, **kwargs):
        del args, kwargs
        return selection

    monkeypatch.setattr(
        owner_runtime,
        "_market_data_api",
        lambda: (resolve_klines, lambda *args, **kwargs: (), lambda *args, **kwargs: None),
    )
    analyzer = _analyzer()

    analysis = analyzer.analyze(replace(descriptor, sha256="3" * 64))

    assert analysis.validation.status is DatasetGateStatus.REJECTED
    assert analysis.profile is None
    assert analysis.validation.reason_codes == ("REGISTERED_OWNER_VERIFICATION_FAILED",)
    assert analysis.validation.evidence["failure_type"] == "CompatibilityError"


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"dataset_id": " "}, "dataset_id"),
        ({"dataset_version": 0}, "dataset_version"),
        ({"dataset_version": True}, "dataset_version"),
        ({"base_timeframe": " "}, "base_timeframe"),
        ({"derived_timeframes": ("15m", "15m")}, "derived_timeframes"),
        ({"derived_timeframes": ("15m", " ")}, "derived_timeframes"),
        ({"funding_dataset_id": "funding-btc", "funding_dataset_version": None}, "identity"),
        ({"funding_dataset_id": None, "funding_dataset_version": 1}, "identity"),
        ({"funding_dataset_id": " ", "funding_dataset_version": 1}, "funding_dataset_id"),
        ({"funding_dataset_id": "funding-btc", "funding_dataset_version": 0}, "version"),
        ({"funding_dataset_id": "funding-btc", "funding_dataset_version": True}, "version"),
    ),
)
def test_registered_dataset_analyzer_validates_config(overrides, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _analyzer(**overrides)


def test_market_data_api_fails_closed_when_owner_package_is_missing(monkeypatch) -> None:
    def missing_package(name: str):
        raise ImportError(name)

    monkeypatch.setattr(owner_runtime.importlib, "import_module", missing_package)

    with pytest.raises(ExternalComponentUnavailableError, match="owner package is unavailable"):
        owner_runtime._market_data_api()


def test_market_data_api_returns_only_owner_resolvers(monkeypatch) -> None:
    resolve_klines = object()
    iter_klines = object()
    resolve_funding = object()
    dataset_module = SimpleNamespace(
        resolve_registered_kline_dataset=resolve_klines,
        iter_registered_klines=iter_klines,
    )
    funding_module = SimpleNamespace(resolve_registered_funding_dataset=resolve_funding)

    def import_module(name: str):
        if name.endswith("funding_dataset_reader"):
            return funding_module
        return dataset_module

    monkeypatch.setattr(owner_runtime.importlib, "import_module", import_module)

    assert owner_runtime._market_data_api() == (resolve_klines, iter_klines, resolve_funding)


@pytest.mark.parametrize(
    ("drift", "message"),
    (
        ("dataset_id", "identity"),
        ("base_timeframe", "base timeframe"),
        ("available_timeframes", "timeframes"),
        ("file_format", "Parquet"),
    ),
)
def test_registered_selection_rejects_request_boundary_drift(drift: str, message: str) -> None:
    descriptor = _registered_descriptor()
    if drift == "dataset_id":
        descriptor = replace(descriptor, dataset_id="other-dataset")
    elif drift == "base_timeframe":
        descriptor = replace(
            descriptor,
            base_timeframe="1m",
            available_timeframes=("1m", *descriptor.available_timeframes),
        )
    elif drift == "available_timeframes":
        descriptor = replace(descriptor, available_timeframes=("5m", "4h"))
    else:
        descriptor = replace(descriptor, file_format="csv")

    with pytest.raises(CompatibilityError, match=message):
        _analyzer()._resolve_selection(descriptor)


@pytest.mark.parametrize(
    ("drift", "message"),
    (
        ("row_count", "row count"),
        ("symbol", "instrument"),
        ("exchange", "exchange"),
        ("market", "market type"),
        ("start", "start"),
        ("end", "end"),
    ),
)
def test_registered_selection_rejects_kline_owner_drift(
    monkeypatch, drift: str, message: str
) -> None:
    descriptor = _registered_descriptor()
    selection = _owner_selection(descriptor)
    if drift == "row_count":
        selection.row_count += 1
    elif drift == "symbol":
        selection.manifest.instrument.symbol = "ETHUSDT"
    elif drift == "exchange":
        selection.manifest.exchange.id = "other"
    elif drift == "market":
        selection.manifest.market_type.value = "spot"
    elif drift == "start":
        selection.partitions[0].start += timedelta(minutes=5)
    else:
        selection.partitions[-1].end -= timedelta(minutes=5)

    monkeypatch.setattr(
        owner_runtime,
        "_market_data_api",
        lambda: (
            lambda *args, **kwargs: selection,
            lambda *args, **kwargs: (),
            lambda *args, **kwargs: _funding_selection(descriptor),
        ),
    )

    with pytest.raises(CompatibilityError, match=message):
        _analyzer()._resolve_selection(descriptor)


def test_registered_selection_wraps_kline_owner_failure(monkeypatch) -> None:
    descriptor = _registered_descriptor()

    def fail(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("owner unavailable")

    monkeypatch.setattr(owner_runtime, "_market_data_api", lambda: (fail, None, None))

    with pytest.raises(ExternalComponentUnavailableError, match="Kline owner"):
        _analyzer()._resolve_selection(descriptor)


def test_registered_selection_requires_funding_lineage_for_futures(monkeypatch) -> None:
    descriptor = replace(_registered_descriptor(), funding_sha256=None)
    selection = _owner_selection(descriptor)
    monkeypatch.setattr(
        owner_runtime,
        "_market_data_api",
        lambda: (
            lambda *args, **kwargs: selection,
            lambda *args, **kwargs: (),
            lambda *args, **kwargs: None,
        ),
    )

    with pytest.raises(CompatibilityError, match="requires funding lineage"):
        _analyzer()._resolve_selection(descriptor)


def test_registered_selection_wraps_funding_owner_failure(monkeypatch) -> None:
    descriptor = _registered_descriptor()
    selection = _owner_selection(descriptor)

    def fail_funding(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("funding unavailable")

    monkeypatch.setattr(
        owner_runtime,
        "_market_data_api",
        lambda: (lambda *args, **kwargs: selection, lambda *args, **kwargs: (), fail_funding),
    )

    with pytest.raises(ExternalComponentUnavailableError, match="funding owner"):
        _analyzer()._resolve_selection(descriptor)


@pytest.mark.parametrize(
    ("drift", "message"),
    (
        ("hash", "funding hash"),
        ("symbol", "funding instrument"),
        ("exchange", "funding exchange"),
        ("market", "funding market"),
    ),
)
def test_registered_selection_rejects_funding_owner_drift(
    monkeypatch, drift: str, message: str
) -> None:
    descriptor = _registered_descriptor()
    selection = _owner_selection(descriptor)
    funding = _funding_selection(descriptor)
    if drift == "hash":
        funding.descriptor.dataset_hash = "9" * 64
    elif drift == "symbol":
        funding.manifest.instrument.symbol = "ETHUSDT"
    elif drift == "exchange":
        funding.manifest.exchange.id = "other"
    else:
        funding.manifest.market_type.value = "spot"

    monkeypatch.setattr(
        owner_runtime,
        "_market_data_api",
        lambda: (
            lambda *args, **kwargs: selection,
            lambda *args, **kwargs: (),
            lambda *args, **kwargs: funding,
        ),
    )

    with pytest.raises(CompatibilityError, match=message):
        _analyzer()._resolve_selection(descriptor)


def test_registered_selection_does_not_resolve_funding_for_spot(monkeypatch) -> None:
    descriptor = replace(
        _registered_descriptor(),
        market_type=MarketType.SPOT,
        funding_sha256=None,
    )
    selection = _owner_selection(descriptor)
    selection.manifest.market_type.value = "spot"

    def unexpected_funding(*args, **kwargs):
        del args, kwargs
        raise AssertionError("spot must not resolve funding")

    monkeypatch.setattr(
        owner_runtime,
        "_market_data_api",
        lambda: (
            lambda *args, **kwargs: selection,
            lambda *args, **kwargs: (),
            unexpected_funding,
        ),
    )

    assert _analyzer()._resolve_selection(descriptor) is selection


@pytest.mark.parametrize(
    ("field", "value", "message", "allow_zero_volume"),
    (
        ("open", "nan", "non-finite", True),
        ("high", "90", "invalid OHLC", True),
        ("volume", "-1", "negative volume", True),
        ("volume", "0", "forbidden zero volume", False),
    ),
)
def test_registered_profile_rejects_invalid_owner_records(
    monkeypatch,
    field: str,
    value: str,
    message: str,
    allow_zero_volume: bool,
) -> None:
    descriptor = _registered_descriptor()
    record = _records(descriptor)[0]
    setattr(record, field, value)

    monkeypatch.setattr(
        owner_runtime,
        "_market_data_api",
        lambda: (None, lambda *args, **kwargs: (record,), None),
    )
    analyzer = RegisteredDatasetAnalyzer(
        DatasetValidationPolicy(
            interval_seconds=300,
            minimum_rows=1,
            minimum_history_days=Decimal("0"),
            allow_zero_volume=allow_zero_volume,
        ),
        storage_root="/not-exposed",
        dataset_id=descriptor.dataset_id,
        dataset_version=1,
        derived_timeframes=("15m",),
    )

    with pytest.raises(CompatibilityError, match=message):
        analyzer._profile_selection(descriptor, object())


def test_registered_profile_wraps_unexpected_stream_failure(monkeypatch) -> None:
    descriptor = _registered_descriptor()

    def fail_stream(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("stream unavailable")

    monkeypatch.setattr(owner_runtime, "_market_data_api", lambda: (None, fail_stream, None))

    with pytest.raises(ExternalComponentUnavailableError, match="stream cannot be verified"):
        _analyzer()._profile_selection(descriptor, object())


def test_registered_analyzer_reports_empty_owner_stream(monkeypatch) -> None:
    descriptor = _registered_descriptor()
    _owner_api(monkeypatch, descriptor, records=())

    analysis = _analyzer().analyze(descriptor, now=datetime(2026, 1, 3, tzinfo=UTC))

    assert analysis.validation.status is DatasetGateStatus.REJECTED
    assert "EMPTY_OR_UNDECODABLE_DATASET" in analysis.validation.reason_codes
    assert "ROW_COUNT_MISMATCH" in analysis.validation.reason_codes


def test_registered_analyzer_reports_extreme_candle_warning(monkeypatch) -> None:
    descriptor = _registered_descriptor()
    records = list(_records(descriptor))
    records[0].high = "140"
    records[0].low = "90"
    _owner_api(monkeypatch, descriptor, records=tuple(records))
    analyzer = RegisteredDatasetAnalyzer(
        DatasetValidationPolicy(
            interval_seconds=300,
            minimum_rows=1,
            minimum_history_days=Decimal("0"),
            extreme_range_fraction=Decimal("0.10"),
        ),
        storage_root="/not-exposed",
        dataset_id=descriptor.dataset_id,
        dataset_version=1,
        derived_timeframes=("15m",),
    )

    analysis = analyzer.analyze(descriptor, now=datetime(2026, 1, 3, tzinfo=UTC))

    assert analysis.validation.status is DatasetGateStatus.ACCEPTED_WITH_WARNINGS
    assert analysis.validation.warnings == ("EXTREME_CANDLES_PRESENT",)
    assert analysis.validation.extreme_outliers == 1


def test_registered_analyzer_reports_profile_row_and_minimum_drift(monkeypatch) -> None:
    descriptor = _registered_descriptor()
    _owner_api(monkeypatch, descriptor, records=_records(descriptor)[:2])
    analyzer = RegisteredDatasetAnalyzer(
        DatasetValidationPolicy(
            interval_seconds=300,
            minimum_rows=4,
            minimum_history_days=Decimal("0"),
        ),
        storage_root="/not-exposed",
        dataset_id=descriptor.dataset_id,
        dataset_version=1,
        derived_timeframes=("15m",),
    )

    analysis = analyzer.analyze(descriptor, now=datetime(2026, 1, 3, tzinfo=UTC))

    assert analysis.validation.status is DatasetGateStatus.REJECTED
    assert "ROW_COUNT_MISMATCH" in analysis.validation.reason_codes
    assert "INSUFFICIENT_ROWS" in analysis.validation.reason_codes
    assert analysis.validation.rows_scanned == 2


def test_publishing_results_aggregator_republishes_only_after_runset_changes(
    monkeypatch,
    tmp_path,
) -> None:
    published: list[tuple[str, datetime]] = []
    closed: list[bool] = []

    class FakePublisher:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def publish_experiment(self, experiment_id: str, *, observed_at: datetime):
            published.append((experiment_id, observed_at))
            return ()

        def close(self) -> None:
            closed.append(True)

    class FakeExecution:
        def __init__(self) -> None:
            self.run_ids = ("run-a",)

        def status(self, experiment_id: str):
            return SimpleNamespace(
                experiment_id=experiment_id,
                status="completed",
                run_ids=self.run_ids,
                run_status_counts={"succeeded": len(self.run_ids)},
                has_nonterminal_runs=False,
            )

    monkeypatch.setattr(owner_runtime, "ResearchResultsPublicationAdapter", FakePublisher)
    execution = FakeExecution()
    generated_at = datetime(2026, 1, 3, tzinfo=UTC)
    aggregator = PublishingResultsAggregator(
        execution=execution,  # type: ignore[arg-type]
        request=object(),  # type: ignore[arg-type]
        package_root=tmp_path / "packages",
        registry_path=tmp_path / "results.sqlite3",
        producer_module_version="test-v1",
    )
    try:
        assert aggregator.candidates_for_experiment("experiment-a", generated_at=generated_at) == ()
        assert aggregator.candidates_for_experiment("experiment-a", generated_at=generated_at) == ()
        assert len(published) == 1

        execution.run_ids = ("run-a", "run-final-untouched")
        assert aggregator.candidates_for_experiment("experiment-a", generated_at=generated_at) == ()
        assert len(published) == 2
        assert published[0][0] == published[1][0] == "experiment-a"
    finally:
        aggregator.close()

    assert closed == [True]


def test_publishing_results_aggregator_rejects_nonterminal_owner_state(
    monkeypatch,
    tmp_path,
) -> None:
    class FakePublisher:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def close(self) -> None:
            pass

    execution = SimpleNamespace(
        status=lambda experiment_id: SimpleNamespace(
            experiment_id=experiment_id,
            status="running",
            run_ids=("run-a",),
            run_status_counts={"running": 1},
            has_nonterminal_runs=True,
        )
    )
    monkeypatch.setattr(owner_runtime, "ResearchResultsPublicationAdapter", FakePublisher)
    aggregator = PublishingResultsAggregator(
        execution=execution,  # type: ignore[arg-type]
        request=object(),  # type: ignore[arg-type]
        package_root=tmp_path / "packages",
        registry_path=tmp_path / "results.sqlite3",
        producer_module_version="test-v1",
    )
    try:
        with pytest.raises(CompatibilityError, match="terminal Research experiment"):
            aggregator.candidates_for_experiment(
                "experiment-a", generated_at=datetime(2026, 1, 3, tzinfo=UTC)
            )
    finally:
        aggregator.close()


def test_publishing_results_aggregator_close_tolerates_missing_publisher(
    monkeypatch,
    tmp_path,
) -> None:
    class FakePublisher:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def close(self) -> None:
            raise AssertionError("detached publisher must not be closed")

    monkeypatch.setattr(owner_runtime, "ResearchResultsPublicationAdapter", FakePublisher)
    aggregator = PublishingResultsAggregator(
        execution=object(),  # type: ignore[arg-type]
        request=object(),  # type: ignore[arg-type]
        package_root=tmp_path / "packages",
        registry_path=tmp_path / "results.sqlite3",
        producer_module_version="test-v1",
    )
    aggregator._publisher = None  # type: ignore[assignment]
    aggregator.close()
