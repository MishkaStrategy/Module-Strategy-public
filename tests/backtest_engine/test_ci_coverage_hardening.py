from __future__ import annotations

import builtins
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

import modular_backtest.data as data_module
import modular_backtest.registered_data as registered_module
import modular_backtest.streaming_data as streaming_module
from modular_backtest.config import DataSourceConfig
from modular_backtest.data import DataValidationError
from modular_backtest.registered_data import RegisteredDataError
from modular_backtest.scalability.buffers import BoundedRecordBuffer
from modular_backtest.scalability.equity import CompactEquitySeries, EquitySampler
from modular_backtest.scalability.events import StreamingEventStore
from modular_backtest.scalability.manifest import (
    CandleDataHasher,
    _canonical_float,
    build_scalable_run_manifest,
    build_scalable_run_manifest_from_hash,
)
from modular_backtest.streaming_data import PreparedCandleSource
from modular_core.config import EngineConfig
from modular_core.events import EventEnvelope
from modular_core.manifest import ModuleManifest
from modular_core.models import Candle, EquityPoint


def _bar(index: int = 0, *, symbol: str = "BTCUSDT", timeframe: str = "5m") -> Candle:
    timestamp = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(minutes=5 * index)
    return Candle(timestamp, 100.0, 101.0, 99.0, 100.5, 10.0, symbol, timeframe)


def _point(index: int = 0, *, equity: float = 100.0, open_: bool = False) -> EquityPoint:
    return EquityPoint(
        timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=index),
        cash=equity,
        unrealized_pnl=0.0,
        equity=equity,
        position_open=open_,
    )


def _event(sequence: int, *, run_id: str = "run-a", event_id: str | None = None) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id or f"evt-{sequence:010d}",
        run_id=run_id,
        sequence=sequence,
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        event_type="test.event",
        source="test",
        payload={"sequence": sequence},
    )


def _summary(path: Path, bars: tuple[Candle, ...], sha: str = "a" * 64):
    return data_module.ValidationSummary(
        path=str(path),
        sha256=sha,
        symbol="BTCUSDT",
        market_type="usdt_m_futures",
        base_timeframe="5m",
        rows=len(bars),
        first_timestamp=bars[0].timestamp.isoformat(),
        last_timestamp=bars[-1].timestamp.isoformat(),
        gaps=0,
        duplicates=0,
        requested_start=None,
        requested_end=None,
    )


def test_bounded_buffer_validation_clear_and_non_sequence_equality() -> None:
    for value in (True, 1.5, "2"):
        with pytest.raises(TypeError):
            BoundedRecordBuffer(value)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        BoundedRecordBuffer(0)
    buffer = BoundedRecordBuffer[int](2)
    assert buffer.maxlen == 2
    buffer.append(1)
    assert buffer != object()
    buffer.clear()
    assert list(buffer) == []


def test_compact_equity_validation_slices_and_bounds() -> None:
    series = CompactEquitySeries()
    with pytest.raises(TypeError):
        series.append(object())  # type: ignore[arg-type]
    series.append(_point())
    series.append(_point(1, equity=101.0, open_=True))
    assert series[:] == [_point(), _point(1, equity=101.0, open_=True)]
    for index in (2, -3):
        with pytest.raises(IndexError):
            _ = series[index]
        with pytest.raises(IndexError):
            series[index] = _point()
    with pytest.raises(TypeError):
        series[0] = object()  # type: ignore[assignment]


def test_equity_sampler_covers_all_policies() -> None:
    point = _point()
    assert EquitySampler("every_bar", 2).accept(point, bar_index=1, trade_count_changed=False)
    assert EquitySampler("every_n_bars", 2).accept(point, bar_index=2, trade_count_changed=False)
    assert not EquitySampler("every_n_bars", 2).accept(
        point, bar_index=1, trade_count_changed=False
    )
    hourly = EquitySampler("hourly", 2)
    assert hourly.accept(point, bar_index=0, trade_count_changed=False)
    assert not hourly.accept(point, bar_index=1, trade_count_changed=False)
    daily = EquitySampler("daily", 2)
    assert daily.accept(point, bar_index=0, trade_count_changed=False)
    assert not daily.accept(_point(1), bar_index=1, trade_count_changed=False)
    changed = EquitySampler("on_change", 2)
    assert changed.accept(point, bar_index=0, trade_count_changed=False)
    assert not changed.accept(point, bar_index=1, trade_count_changed=False)
    assert changed.accept(_point(equity=101.0), bar_index=2, trade_count_changed=False)
    assert EquitySampler("on_trade", 2).accept(point, bar_index=0, trade_count_changed=True)
    hybrid = EquitySampler("hybrid", 100)
    assert hybrid.accept(point, bar_index=1, trade_count_changed=False)
    assert not hybrid.accept(point, bar_index=2, trade_count_changed=False)
    assert hybrid.accept(point, bar_index=3, trade_count_changed=False, force=True)


def test_streaming_event_store_validation_and_lifecycle(tmp_path: Path) -> None:
    for value in (True, 1.5, "2"):
        with pytest.raises(TypeError):
            StreamingEventStore(tmp_path / f"bad-{value}", retained_window=value)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        StreamingEventStore(tmp_path / "zero", retained_window=0)
    store = StreamingEventStore(tmp_path / "events" / "events.jsonl", retained_window=2)
    store.append_many(())
    with pytest.raises(TypeError):
        store.append_many((object(),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="event_id"):
        store.append(_event(1, event_id="wrong"))
    store.append(_event(1))
    with pytest.raises(ValueError, match="run_id"):
        store.append(_event(2, run_id="run-b"))
    for value in (True, 1.5):
        with pytest.raises(TypeError):
            store.read(after_sequence=value)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        store.read(after_sequence=-1)
    destination = tmp_path / "copy.jsonl"
    store.copy_to(destination)
    assert destination.read_bytes() == store.path.read_bytes()
    store.close()
    store.close()
    store.flush()
    with pytest.raises(ValueError, match="closed"):
        store.append(_event(2))
    extra = store.path.parent / "keep"
    extra.write_text("x")
    store.cleanup()
    store.cleanup()
    assert extra.exists()


def test_streaming_event_store_rolls_back_failed_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = StreamingEventStore(tmp_path / "events.jsonl")
    original_write = os.write
    monkeypatch.setattr(os, "write", lambda *_args: 0)
    with pytest.raises(OSError, match="failed to append"):
        store.append(_event(1))
    monkeypatch.setattr(os, "write", original_write)
    assert store.path.stat().st_size == 0
    store.cleanup()


def test_candle_hasher_validation_and_properties() -> None:
    hasher = CandleDataHasher()
    assert hasher.count == 0 and hasher.symbol is None and hasher.timeframe is None
    with pytest.raises(ValueError, match="at least one"):
        hasher.hexdigest()
    with pytest.raises(TypeError):
        hasher.update(object())  # type: ignore[arg-type]
    hasher.update(_bar())
    assert (hasher.count, hasher.symbol, hasher.timeframe) == (1, "BTCUSDT", "5m")
    with pytest.raises(ValueError, match="share symbol"):
        hasher.update(_bar(1, symbol="ETHUSDT"))
    digest = hasher.hexdigest()
    assert hasher.hexdigest() == digest
    with pytest.raises(RuntimeError, match="finalized"):
        hasher.update(_bar(1))
    with pytest.raises(ValueError, match="non-finite"):
        _canonical_float(float("inf"))


def test_scalable_manifest_validation_branches() -> None:
    manifest = ModuleManifest(name="noop", version="1.0.0", api_version="1.0")
    kwargs: dict[str, Any] = dict(
        kernel_version="1",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        data_hash="a" * 64,
        symbol="BTCUSDT",
        base_timeframe="5m",
        bars=1,
        config=EngineConfig(),
        manifests=(manifest,),
    )
    for value in (True, 1.5):
        with pytest.raises(TypeError):
            build_scalable_run_manifest_from_hash(**{**kwargs, "bars": value})
    with pytest.raises(ValueError, match="positive"):
        build_scalable_run_manifest_from_hash(**{**kwargs, "bars": 0})
    with pytest.raises(ValueError, match="align"):
        build_scalable_run_manifest_from_hash(**{**kwargs, "module_parameters": ({}, {})})
    built = build_scalable_run_manifest_from_hash(**kwargs)
    assert built.bars == 1 and len(built.module_parameters) == 1
    with pytest.raises(ValueError, match="at least one"):
        build_scalable_run_manifest(
            kernel_version="1",
            created_at=datetime(2024, 1, 1, tzinfo=UTC),
            bars=[],
            config=EngineConfig(),
            manifests=(manifest,),
        )


def test_data_parser_and_stream_suffix_validation(tmp_path: Path) -> None:
    with pytest.raises(DataValidationError, match="outside"):
        data_module._parse_timestamp(10**30)
    with pytest.raises(DataValidationError, match="invalid ISO"):
        data_module._parse_timestamp("not-a-date")
    with pytest.raises(DataValidationError, match="unsupported"):
        data_module._parse_timestamp(object())
    config = DataSourceConfig(symbol="BTCUSDT", path=tmp_path / "missing.csv", format="csv")
    object.__setattr__(config, "path", None)
    with pytest.raises(DataValidationError, match="path is missing"):
        next(data_module._stream_csv(config))
    empty = tmp_path / "empty.csv"
    empty.write_text("")
    with pytest.raises(DataValidationError, match="no header"):
        next(data_module._stream_csv(DataSourceConfig(symbol="BTCUSDT", path=empty, format="csv")))
    parquet_missing = DataSourceConfig(
        symbol="BTCUSDT", path=tmp_path / "missing.parquet", format="parquet"
    )
    object.__setattr__(parquet_missing, "path", None)
    with pytest.raises(DataValidationError, match="path is missing"):
        next(data_module._stream_parquet(parquet_missing))
    invalid = tmp_path / "invalid.parquet"
    invalid.write_text("not parquet")
    with pytest.raises(DataValidationError, match="cannot read"):
        next(
            data_module._stream_parquet(
                DataSourceConfig(symbol="BTCUSDT", path=invalid, format="parquet")
            )
        )
    for value in (True, 1.5):
        with pytest.raises(TypeError):
            next(data_module.stream_candles_from(config, value))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="negative"):
        next(data_module.stream_candles_from(config, -1))
    with pytest.raises(ValueError, match="timezone-aware"):
        next(data_module.stream_candles_from(config, 1, start_timestamp=datetime(2024, 1, 1)))
    with pytest.raises(DataValidationError, match="does not exist"):
        next(
            data_module.stream_candles_from(
                DataSourceConfig(symbol="BTCUSDT", path=tmp_path / "x", format="csv"), 1
            )
        )
    with pytest.raises(DataValidationError, match="unsupported base"):
        data_module._expected_interval("nope")
    with pytest.raises(DataValidationError, match="path is missing"):
        data_module.load_validated_candles(config)


def test_streaming_validation_helpers_cover_fail_closed_paths(tmp_path: Path) -> None:
    config = DataSourceConfig(symbol="BTCUSDT", path=tmp_path / "x.csv", format="csv")
    interval = timedelta(minutes=5)
    with pytest.raises(DataValidationError, match="non-Candle"):
        streaming_module._validate_next(
            object(), config=config, previous=None, expected_interval=interval, gaps=0, duplicates=0
        )  # type: ignore[arg-type]
    with pytest.raises(DataValidationError, match="symbol differs"):
        streaming_module._validate_next(
            _bar(symbol="ETHUSDT"),
            config=config,
            previous=None,
            expected_interval=interval,
            gaps=0,
            duplicates=0,
        )
    with pytest.raises(DataValidationError, match="timeframe differs"):
        streaming_module._validate_next(
            _bar(timeframe="1h"),
            config=config,
            previous=None,
            expected_interval=interval,
            gaps=0,
            duplicates=0,
        )
    assert streaming_module._validate_next(
        _bar(), config=config, previous=None, expected_interval=interval, gaps=0, duplicates=0
    ) == (0, 0)
    with pytest.raises(DataValidationError, match="duplicate"):
        streaming_module._validate_next(
            _bar(),
            config=config,
            previous=_bar().timestamp,
            expected_interval=interval,
            gaps=0,
            duplicates=0,
        )
    with pytest.raises(DataValidationError, match="strictly increasing"):
        streaming_module._validate_next(
            _bar(),
            config=config,
            previous=_bar(1).timestamp,
            expected_interval=interval,
            gaps=0,
            duplicates=0,
        )
    allow = DataSourceConfig(
        symbol="BTCUSDT", path=tmp_path / "x.csv", format="csv", gap_policy="allow"
    )
    assert streaming_module._validate_next(
        _bar(2),
        config=allow,
        previous=_bar().timestamp,
        expected_interval=interval,
        gaps=0,
        duplicates=0,
    ) == (1, 0)
    with pytest.raises(DataValidationError, match="empty"):
        streaming_module._run_validation_pass(config, ())
    with pytest.raises(DataValidationError, match="unsupported base"):
        streaming_module._expected_interval("nope")
    with pytest.raises(DataValidationError, match="does not exist"):
        streaming_module._open_source(config)


def test_prepared_source_argument_and_integrity_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bars = tuple(_bar(i) for i in range(3))
    path = tmp_path / "source.csv"
    path.write_text("placeholder")
    summary = _summary(path, bars)
    prepared = PreparedCandleSource(
        config=DataSourceConfig(symbol="BTCUSDT", path=path, format="csv"),
        validation=summary,
        data_hash="b" * 64,
        first_candle=bars[0],
        last_candle=bars[-1],
    )
    assert (
        prepared.total_bars == 3
        and prepared.symbol == "BTCUSDT"
        and prepared.base_timeframe == "5m"
    )
    for value in (True, 1.5):
        with pytest.raises(TypeError):
            next(prepared.iter_candles_from(value))  # type: ignore[arg-type]
    for value in (-1, 3):
        with pytest.raises(ValueError):
            next(prepared.iter_candles_from(value))

    metadata = streaming_module._SourceMetadata(path=str(path), sha256="c" * 64)
    monkeypatch.setattr(streaming_module, "_open_source", lambda _config: (metadata, iter(bars)))
    with pytest.raises(DataValidationError, match="identity changed after"):
        next(prepared.iter_candles())
    with pytest.raises(DataValidationError, match="identity changed before"):
        next(prepared.iter_candles_from(1))


def _install_registered_reader_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> ModuleType:
    package = ModuleType("market_data_ingestion")
    package.__path__ = []  # type: ignore[attr-defined]
    reader = ModuleType("market_data_ingestion.dataset_reader")
    errors = ModuleType("market_data_ingestion.errors")

    class StructuredIngestionError(Exception):
        pass

    reader.resolve_registered_kline_dataset = (  # type: ignore[attr-defined]
        lambda *_args, **_kwargs: None
    )
    reader.iter_registered_klines = (  # type: ignore[attr-defined]
        lambda *_args, **_kwargs: iter(())
    )
    errors.StructuredIngestionError = StructuredIngestionError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "market_data_ingestion", package)
    monkeypatch.setitem(sys.modules, "market_data_ingestion.dataset_reader", reader)
    monkeypatch.setitem(sys.modules, "market_data_ingestion.errors", errors)
    return reader


def test_registered_source_errors_and_empty_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incomplete = DataSourceConfig(
        symbol="BTCUSDT", storage_root=tmp_path, dataset_id="dataset-x", dataset_version=1
    )
    object.__setattr__(incomplete, "dataset_version", None)
    with pytest.raises(RegisteredDataError, match="incomplete"):
        registered_module.open_registered_candles(incomplete)

    config = DataSourceConfig(
        symbol="BTCUSDT", storage_root=tmp_path, dataset_id="dataset-x", dataset_version=1
    )
    real_import = builtins.__import__

    def deny(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("market_data_ingestion.dataset_reader"):
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny)
    with pytest.raises(RegisteredDataError, match="requires"):
        registered_module.open_registered_candles(config)
    monkeypatch.setattr(builtins, "__import__", real_import)

    reader = _install_registered_reader_stub(monkeypatch)

    selection = SimpleNamespace(
        manifest=SimpleNamespace(instrument=SimpleNamespace(symbol="BTCUSDT")),
        descriptor=SimpleNamespace(dataset_id="dataset-x", version=1, dataset_hash="a" * 64),
        partitions=(object(),),
    )
    monkeypatch.setattr(reader, "resolve_registered_kline_dataset", lambda *_a, **_k: selection)
    monkeypatch.setattr(reader, "iter_registered_klines", lambda *_a, **_k: iter(()))
    source = registered_module.open_registered_candles(config)
    with pytest.raises(RegisteredDataError, match="empty"):
        tuple(source.candles)


def test_registered_resolution_and_iteration_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = _install_registered_reader_stub(monkeypatch)

    config = DataSourceConfig(
        symbol="BTCUSDT", storage_root=tmp_path, dataset_id="dataset-x", dataset_version=1
    )
    monkeypatch.setattr(
        reader,
        "resolve_registered_kline_dataset",
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("bad selection")),
    )
    with pytest.raises(RegisteredDataError, match="validation failed"):
        registered_module.open_registered_candles(config)

    selection = SimpleNamespace(
        manifest=SimpleNamespace(instrument=SimpleNamespace(symbol="BTCUSDT")),
        descriptor=SimpleNamespace(dataset_id="dataset-x", version=1, dataset_hash="a" * 64),
        partitions=(object(),),
    )
    monkeypatch.setattr(reader, "resolve_registered_kline_dataset", lambda *_a, **_k: selection)
    monkeypatch.setattr(
        reader,
        "iter_registered_klines",
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("bad row")),
    )
    source = registered_module.open_registered_candles(config)
    with pytest.raises(RegisteredDataError, match="validation failed"):
        tuple(source.candles)


def test_parquet_resume_and_registered_stream_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    path = tmp_path / "bars.parquet"
    table = pa.table(
        {
            "open_time": [datetime(2024, 1, 1, tzinfo=UTC)],
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.5],
            "volume": [10.0],
        }
    )
    pq.write_table(table, path)
    config = DataSourceConfig(symbol="BTCUSDT", path=path, format="parquet")

    original = data_module._row_to_candle
    monkeypatch.setattr(
        data_module,
        "_row_to_candle",
        lambda *_args: (_ for _ in ()).throw(DataValidationError("row invalid")),
    )
    with pytest.raises(DataValidationError, match="row invalid"):
        next(data_module._stream_parquet(config))
    monkeypatch.setattr(data_module, "_row_to_candle", original)

    with pytest.raises(DataValidationError, match="exceeds"):
        list(data_module._stream_parquet_from(config, 2))
    invalid = tmp_path / "broken.parquet"
    invalid.write_text("broken")
    with pytest.raises(DataValidationError, match="cannot resume"):
        list(
            data_module._stream_parquet_from(
                DataSourceConfig(symbol="BTCUSDT", path=invalid, format="parquet"), 1
            )
        )

    missing = DataSourceConfig(symbol="BTCUSDT", path=path, format="parquet")
    object.__setattr__(missing, "path", None)
    with pytest.raises(DataValidationError, match="path is missing"):
        next(data_module._stream_parquet_from(missing, 1))

    registered = DataSourceConfig(
        symbol="BTCUSDT",
        storage_root=tmp_path,
        dataset_id="dataset-x",
        dataset_version=1,
    )
    source = SimpleNamespace(candles=iter((_bar(0), _bar(1), _bar(2))))
    monkeypatch.setattr(data_module, "open_registered_candles", lambda _config: source)
    assert list(data_module.stream_candles_from(registered, 1)) == [_bar(1), _bar(2)]
    monkeypatch.setattr(
        data_module,
        "open_registered_candles",
        lambda _config: SimpleNamespace(candles=iter((_bar(1), _bar(2)))),
    )
    assert list(
        data_module.stream_candles_from(
            registered,
            1,
            start_timestamp=_bar(1).timestamp,
        )
    ) == [_bar(1), _bar(2)]
    assert list(data_module.stream_candles(registered)) == [_bar(1), _bar(2)]
    monkeypatch.setattr(
        data_module,
        "open_registered_candles",
        lambda _config: (_ for _ in ()).throw(RegisteredDataError("registry failed")),
    )
    with pytest.raises(DataValidationError, match="registry failed"):
        list(data_module.stream_candles_from(registered, 1))
    with pytest.raises(DataValidationError, match="registry failed"):
        list(data_module.stream_candles(registered))


def test_data_suffix_with_date_range_uses_generic_parquet_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bars.parquet"
    path.write_bytes(b"x")
    config = DataSourceConfig(
        symbol="BTCUSDT",
        path=path,
        format="parquet",
        start=_bar(0).timestamp,
    )
    monkeypatch.setattr(data_module, "_stream_parquet", lambda _config: iter((_bar(0), _bar(1))))
    assert list(data_module.stream_candles_from(config, 1)) == [_bar(1)]


def _prepared_for(
    tmp_path: Path,
    bars: tuple[Candle, ...],
    *,
    registered: bool = False,
) -> PreparedCandleSource:
    path = tmp_path / "source.csv"
    path.write_text("x")
    config = (
        DataSourceConfig(
            symbol="BTCUSDT",
            storage_root=tmp_path,
            dataset_id="dataset-x",
            dataset_version=1,
        )
        if registered
        else DataSourceConfig(symbol="BTCUSDT", path=path, format="csv")
    )
    from modular_backtest.scalability.manifest import incremental_candle_data_hash

    return PreparedCandleSource(
        config=config,
        validation=_summary(path, bars),
        data_hash=incremental_candle_data_hash(bars),
        first_candle=bars[0],
        last_candle=bars[-1],
    )


def test_prepared_source_post_iteration_integrity_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bars = tuple(_bar(i) for i in range(3))
    prepared = _prepared_for(tmp_path, bars)
    metadata = streaming_module._SourceMetadata(
        path=str(prepared.config.path), sha256=prepared.validation.sha256
    )
    monkeypatch.setattr(streaming_module, "file_sha256", lambda _path: prepared.validation.sha256)

    monkeypatch.setattr(streaming_module, "_open_source", lambda _config: (metadata, iter(())))
    with pytest.raises(DataValidationError, match="empty"):
        list(prepared.iter_candles())

    monkeypatch.setattr(streaming_module, "_open_source", lambda _config: (metadata, iter(bars)))
    original_hash = prepared.data_hash
    object.__setattr__(prepared, "data_hash", "f" * 64)
    with pytest.raises(DataValidationError, match="changed between"):
        list(prepared.iter_candles())
    object.__setattr__(prepared, "data_hash", original_hash)

    monkeypatch.setattr(streaming_module, "_open_source", lambda _config: (metadata, iter(bars)))
    monkeypatch.setattr(streaming_module, "file_sha256", lambda _path: "f" * 64)
    with pytest.raises(DataValidationError, match="changed during simulation"):
        list(prepared.iter_candles())


def test_prepared_source_resume_integrity_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bars = tuple(_bar(i) for i in range(3))
    prepared = _prepared_for(tmp_path, bars)
    metadata = streaming_module._SourceMetadata(
        path=str(prepared.config.path), sha256=prepared.validation.sha256
    )
    monkeypatch.setattr(streaming_module, "_open_source", lambda _config: (metadata, iter(bars)))
    monkeypatch.setattr(streaming_module, "file_sha256", lambda _path: prepared.validation.sha256)
    assert list(prepared.iter_candles_from(0)) == list(bars)
    monkeypatch.setattr(streaming_module, "_open_source", lambda _config: (metadata, iter(())))

    monkeypatch.setattr(streaming_module, "stream_candles_from", lambda *_a, **_k: iter((_bar(2),)))
    with pytest.raises(DataValidationError, match="start boundary"):
        list(prepared.iter_candles_from(1))

    monkeypatch.setattr(streaming_module, "stream_candles_from", lambda *_a, **_k: iter(()))
    with pytest.raises(DataValidationError, match="suffix changed"):
        list(prepared.iter_candles_from(1))

    original_last = prepared.last_candle
    object.__setattr__(prepared, "last_candle", _bar(3))
    monkeypatch.setattr(
        streaming_module, "stream_candles_from", lambda *_a, **_k: iter((_bar(1), _bar(2)))
    )
    with pytest.raises(DataValidationError, match="end boundary"):
        list(prepared.iter_candles_from(1))
    object.__setattr__(prepared, "last_candle", original_last)

    monkeypatch.setattr(
        streaming_module, "stream_candles_from", lambda *_a, **_k: iter((_bar(1), _bar(2)))
    )
    monkeypatch.setattr(streaming_module, "file_sha256", lambda _path: "0" * 64)
    with pytest.raises(DataValidationError, match="resumed simulation"):
        list(prepared.iter_candles_from(1))


def test_streaming_registered_open_and_prepare_mutation_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = DataSourceConfig(
        symbol="BTCUSDT",
        storage_root=tmp_path,
        dataset_id="dataset-x",
        dataset_version=1,
    )
    monkeypatch.setattr(
        streaming_module,
        "open_registered_candles",
        lambda _config: (_ for _ in ()).throw(RegisteredDataError("bad registry")),
    )
    with pytest.raises(DataValidationError, match="bad registry"):
        streaming_module._open_source(registered)

    bars = tuple(_bar(i) for i in range(2))
    path = tmp_path / "source.csv"
    path.write_text("x")
    config = DataSourceConfig(symbol="BTCUSDT", path=path, format="csv")
    metadata = streaming_module._SourceMetadata(path=str(path), sha256="a" * 64)
    monkeypatch.setattr(streaming_module, "_open_source", lambda _config: (metadata, iter(bars)))
    monkeypatch.setattr(streaming_module, "file_sha256", lambda _path: "b" * 64)
    with pytest.raises(DataValidationError, match="changed during validation"):
        streaming_module.prepare_candle_source(config)


def test_scalable_journal_remaining_limits_and_row_writer(tmp_path: Path) -> None:
    from modular_backtest.scalability import PerformanceConfig, ScalableJournal
    from modular_backtest.scalability.journal import ObservabilityLimitExceeded

    timestamp = datetime(2024, 1, 1, tzinfo=UTC)

    store = StreamingEventStore(tmp_path / "events.jsonl")
    journal = ScalableJournal(
        run_id="run-a",
        event_store=store,
        performance=PerformanceConfig(max_memory_mb=None, max_events=1),
    )
    journal._commit_event_batch(())
    event = journal.emit_event(
        timestamp=timestamp,
        event_type="one",
        source="test",
        payload={},
        entity_key="entity-1",
    )
    assert journal._last_event_by_entity["entity-1"] == event.event_id
    with pytest.raises(ObservabilityLimitExceeded, match="max_events"):
        journal._commit_event_batch(
            (
                journal._next_event(
                    offset=1, timestamp=timestamp, event_type="two", source="test", payload={}
                ),
            )
        )
    store.cleanup()

    skip_store = StreamingEventStore(tmp_path / "skip.jsonl")
    skip_journal = ScalableJournal(
        run_id="run-skip",
        event_store=skip_store,
        performance=PerformanceConfig(max_memory_mb=None, max_skip_ranges=1),
    )
    skip_journal._skip_range_count = 1
    skip_journal.set_bar_context(0)
    skip_journal.log_skip(timestamp, "module", "not_ready")
    with pytest.raises(ObservabilityLimitExceeded, match="max_skip_ranges"):
        skip_journal.close_skip_range("module")
    skip_journal.close_skip_range("missing")
    skip_journal._active_skip_ranges.clear()
    skip_journal.close_all_skip_ranges()
    skip_store.cleanup()

    diag_store = StreamingEventStore(tmp_path / "diag.jsonl")
    diag_journal = ScalableJournal(
        run_id="run-diag",
        event_store=diag_store,
        performance=PerformanceConfig(max_memory_mb=None, max_diagnostics=1),
    )
    diag_journal.log_diagnostics(timestamp, "module", {})
    diag_journal.diagnostics.append({"existing": True})
    with pytest.raises(ObservabilityLimitExceeded, match="max_diagnostics"):
        diag_journal.log_diagnostics(timestamp, "module", {"x": 1})
    diag_store.cleanup()

    equity_store = StreamingEventStore(tmp_path / "equity.jsonl")
    equity_journal = ScalableJournal(
        run_id="run-equity",
        event_store=equity_store,
        performance=PerformanceConfig(max_memory_mb=None, max_equity_points=1),
    )
    equity_journal.equity.append(_point())
    with pytest.raises(ObservabilityLimitExceeded, match="max_equity_points"):
        equity_journal.record_equity(_point(1))

    empty = tmp_path / "empty.csv"
    equity_journal._write_rows(empty, ())
    assert empty.read_text() == ""
    rows = tmp_path / "rows.csv"
    equity_journal._write_rows(rows, ({"a": 1}, {"a": 2}))
    assert rows.read_text().splitlines() == ["a", "1", "2"]
    equity_store.cleanup()


def test_cli_nonnegative_limit_parser() -> None:
    import argparse

    from modular_backtest.cli import _nonnegative_int, _optional_limit

    assert _nonnegative_int("3") == 3
    with pytest.raises(argparse.ArgumentTypeError, match="non-negative"):
        _nonnegative_int("-1")
    assert _optional_limit(0) is None
    assert _optional_limit(4) == 4
