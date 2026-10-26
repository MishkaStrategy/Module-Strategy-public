from __future__ import annotations

import errno
import json
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from modular_backtest import registered_data, registered_recovery, runner, streaming_data
from modular_backtest.config import (
    BacktestAppConfig,
    CheckpointConfig,
    DataSourceConfig,
    OutputConfig,
)
from modular_backtest.data import DataValidationError, ValidationSummary
from modular_backtest.registered_data import (
    BRIDGE_VERSION,
    RegisteredCandleBatch,
    RegisteredCandleSource,
    RegisteredDataError,
    RegisteredSourcePosition,
    StreamingRegisteredCandleIterator,
    StreamingRegisteredCandleSource,
)
from modular_backtest.registered_recovery import RegisteredStreamingRecoveryError
from modular_backtest.streaming_data import frozen_registered_data_options
from modular_backtest.scalability import (
    PerformanceConfig,
    ResourceLimitExceeded,
    RuntimeCounters,
)
from modular_backtest.streaming_data import (
    PreparedCandleSource,
    _SourceMetadata,
    _ValidationPass,
)
from modular_core.models import Candle


_START = datetime(2024, 1, 1, tzinfo=UTC)
_STEP = timedelta(minutes=5)


def _candle(
    index: int = 0,
    *,
    symbol: str = "BTCUSDT",
    timeframe: str = "5m",
    timestamp: datetime | None = None,
) -> Candle:
    return Candle(
        timestamp=timestamp or _START + index * _STEP,
        open=100.0 + index,
        high=101.0 + index,
        low=99.0 + index,
        close=100.5 + index,
        volume=10.0 + index,
        symbol=symbol,
        timeframe=timeframe,
    )


def _file_config(tmp_path: Path, *, gap_policy: str = "reject") -> DataSourceConfig:
    path = tmp_path / "bars.csv"
    path.write_text("placeholder", encoding="utf-8")
    return DataSourceConfig(
        symbol="BTCUSDT",
        path=path,
        format="csv",
        gap_policy=gap_policy,
    )


def _registered_config(tmp_path: Path) -> DataSourceConfig:
    return DataSourceConfig(
        symbol="BTCUSDT",
        storage_root=tmp_path / "storage",
        dataset_id="coverage-dataset",
        dataset_version=1,
        registered_mode="streaming",
        stream_batch_size=2,
    )


def _summary(
    *,
    rows: int = 2,
    sha256: str = "a" * 64,
    gaps: int = 0,
    duplicates: int = 0,
) -> ValidationSummary:
    return ValidationSummary(
        path="memory://coverage",
        sha256=sha256,
        symbol="BTCUSDT",
        market_type="binance-usdt-m-perpetual",
        base_timeframe="5m",
        rows=rows,
        first_timestamp=_START.isoformat(),
        last_timestamp=(_START + (rows - 1) * _STEP).isoformat(),
        gaps=gaps,
        duplicates=duplicates,
        requested_start=None,
        requested_end=None,
    )


def _prepared(
    tmp_path: Path,
    *,
    source_mode: str = "file",
    stream_batch_size: int | None = None,
    rows: int = 2,
) -> PreparedCandleSource:
    config = _file_config(tmp_path)
    return PreparedCandleSource(
        config=config,
        validation=_summary(rows=rows),
        data_hash="hash",
        first_candle=_candle(),
        last_candle=_candle(rows - 1),
        source_mode=source_mode,
        stream_batch_size=stream_batch_size,
    )


def _position(
    *,
    partition_index: int = 0,
    row_offset: int = 0,
    last_timestamp: datetime | None = None,
    dataset_id: str = "coverage-dataset",
    dataset_version: int = 1,
    dataset_hash: str = "d" * 64,
    manifest_checksum: str = "m" * 64,
    schema_fingerprint: str = "schema",
    source_version: str = BRIDGE_VERSION,
) -> RegisteredSourcePosition:
    return RegisteredSourcePosition(
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        dataset_hash=dataset_hash,
        manifest_checksum=manifest_checksum,
        schema_fingerprint=schema_fingerprint,
        source_version=source_version,
        partition_index=partition_index,
        row_offset=row_offset,
        last_timestamp=last_timestamp,
    )


def _streaming_source(
    tmp_path: Path,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> StreamingRegisteredCandleSource:
    config = _registered_config(tmp_path)
    if start is not None or end is not None:
        config = replace(config, start=start, end=end)
    partitions = (
        SimpleNamespace(start=_START, end=_START + 2 * _STEP, row_count=2),
        SimpleNamespace(start=_START + 2 * _STEP, end=_START + 4 * _STEP, row_count=2),
    )
    return StreamingRegisteredCandleSource(
        config=config,
        dataset_id="coverage-dataset",
        dataset_version=1,
        dataset_hash="d" * 64,
        manifest_checksum="m" * 64,
        schema_fingerprint="schema",
        partition_count=2,
        source_uri="dataset://coverage-dataset/v1",
        row_count=4,
        first_timestamp=_START,
        last_timestamp=_START + 3 * _STEP,
        symbol="BTCUSDT",
        timeframe="5m",
        interval=_STEP,
        batch_size=2,
        _selection=SimpleNamespace(partitions=partitions),
    )


def test_streaming_helpers_cover_fail_closed_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _registered_config(tmp_path)

    with pytest.raises(DataValidationError, match="must be a string"):
        streaming_data._registered_options(config, 7, None)  # type: ignore[arg-type]
    with pytest.raises(DataValidationError, match="must be one of"):
        streaming_data._registered_options(config, "invalid", None)
    with pytest.raises(DataValidationError, match="requires registered dataset mode streaming"):
        streaming_data._registered_options(config, "legacy_materialized", 2)
    with pytest.raises(DataValidationError, match="positive integer"):
        streaming_data._registered_options(config, "streaming", True)
    with pytest.raises(DataValidationError, match="positive integer"):
        streaming_data._registered_options(config, "streaming", "bad")  # type: ignore[arg-type]
    with pytest.raises(DataValidationError, match="positive integer"):
        streaming_data._registered_options(config, "streaming", 0)

    monkeypatch.delenv("MODULAR_BACKTEST_REGISTERED_STREAM_BATCH_SIZE", raising=False)
    assert streaming_data._registered_options(config, "streaming", None) == ("streaming", 2)
    monkeypatch.setenv("MODULAR_BACKTEST_REGISTERED_STREAM_BATCH_SIZE", "9")
    assert streaming_data._registered_options(config, "streaming", None) == ("streaming", 9)


def test_streaming_validation_helpers_reject_all_bad_rows(tmp_path: Path) -> None:
    config = _file_config(tmp_path)
    expected = _STEP

    with pytest.raises(DataValidationError, match="non-Candle"):
        streaming_data._validate_next(
            object(),  # type: ignore[arg-type]
            config=config,
            previous=None,
            expected_interval=expected,
            gaps=0,
            duplicates=0,
        )
    with pytest.raises(DataValidationError, match="symbol differs"):
        streaming_data._validate_next(
            _candle(symbol="ETHUSDT"),
            config=config,
            previous=None,
            expected_interval=expected,
            gaps=0,
            duplicates=0,
        )
    with pytest.raises(DataValidationError, match="timeframe differs"):
        streaming_data._validate_next(
            _candle(timeframe="1m"),
            config=config,
            previous=None,
            expected_interval=expected,
            gaps=0,
            duplicates=0,
        )
    assert streaming_data._validate_next(
        _candle(),
        config=config,
        previous=None,
        expected_interval=expected,
        gaps=0,
        duplicates=0,
    ) == (0, 0)
    with pytest.raises(DataValidationError, match="duplicate timestamp"):
        streaming_data._validate_next(
            _candle(),
            config=config,
            previous=_START,
            expected_interval=expected,
            gaps=0,
            duplicates=0,
        )
    with pytest.raises(DataValidationError, match="strictly increasing"):
        streaming_data._validate_next(
            _candle(timestamp=_START - _STEP),
            config=config,
            previous=_START,
            expected_interval=expected,
            gaps=0,
            duplicates=0,
        )
    with pytest.raises(DataValidationError, match="5m gap"):
        streaming_data._validate_next(
            _candle(timestamp=_START + 2 * _STEP),
            config=config,
            previous=_START,
            expected_interval=expected,
            gaps=0,
            duplicates=0,
        )
    allow = _file_config(tmp_path, gap_policy="allow")
    assert streaming_data._validate_next(
        _candle(timestamp=_START + 2 * _STEP),
        config=allow,
        previous=_START,
        expected_interval=expected,
        gaps=0,
        duplicates=0,
    ) == (1, 0)


def test_streaming_expected_interval_and_empty_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(streaming_data, "timeframe_to_seconds", lambda value: None)
    with pytest.raises(DataValidationError, match="unsupported base timeframe"):
        streaming_data._expected_interval("unknown")

    monkeypatch.setattr(streaming_data, "timeframe_to_seconds", lambda value: 300)
    with pytest.raises(DataValidationError, match="data set is empty"):
        streaming_data._run_validation_pass(_file_config(tmp_path), ())


def test_open_source_wraps_loader_errors_and_missing_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _registered_config(tmp_path)
    monkeypatch.setattr(
        streaming_data,
        "_registered_options",
        lambda *_args, **_kwargs: ("streaming", None),
    )
    with pytest.raises(DataValidationError, match="requires a batch size"):
        streaming_data._open_source(config, registered_mode="streaming")

    monkeypatch.setattr(
        streaming_data,
        "_registered_options",
        lambda *_args, **_kwargs: ("streaming", 2),
    )
    monkeypatch.setattr(
        streaming_data,
        "open_streaming_registered_candles",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RegisteredDataError("broken")),
    )
    with pytest.raises(DataValidationError, match="broken"):
        streaming_data._open_source(config, registered_mode="streaming", stream_batch_size=2)

    missing = DataSourceConfig(
        symbol="BTCUSDT",
        path=tmp_path / "missing.csv",
        format="csv",
    )
    with pytest.raises(DataValidationError, match="does not exist"):
        streaming_data._open_source(missing)


def test_prepare_and_replay_detect_all_identity_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candles = (_candle(), _candle(1))
    prepared = _prepared(tmp_path)
    assert prepared.symbol == "BTCUSDT"
    assert prepared.base_timeframe == "5m"

    monkeypatch.setattr(
        streaming_data,
        "_open_source",
        lambda *_args, **_kwargs: (
            _SourceMetadata(path="memory://x", sha256="different"),
            iter(candles),
        ),
    )
    with pytest.raises(DataValidationError, match="identity changed"):
        list(prepared.iter_candles())

    monkeypatch.setattr(
        streaming_data,
        "_open_source",
        lambda *_args, **_kwargs: (
            _SourceMetadata(
                path="memory://x",
                sha256=prepared.validation.sha256,
                source_mode="streaming",
                stream_batch_size=2,
            ),
            iter(candles),
        ),
    )
    with pytest.raises(DataValidationError, match="loading mode changed"):
        list(prepared.iter_candles())

    metadata = _SourceMetadata(path="memory://x", sha256=prepared.validation.sha256)
    monkeypatch.setattr(
        streaming_data,
        "_open_source",
        lambda *_args, **_kwargs: (metadata, iter(())),
    )
    with pytest.raises(DataValidationError, match="data set is empty"):
        list(prepared.iter_candles())

    monkeypatch.setattr(
        streaming_data,
        "_open_source",
        lambda *_args, **_kwargs: (metadata, iter((_candle(),))),
    )
    with pytest.raises(DataValidationError, match="changed between validation"):
        list(prepared.iter_candles())


def test_prepare_rejects_registered_options_for_files_and_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _file_config(tmp_path)
    with pytest.raises(DataValidationError, match="require a registered data source"):
        streaming_data.prepare_candle_source(config, registered_mode="streaming")

    metadata = _SourceMetadata(path=str(config.path), sha256="before")
    validated = _ValidationPass(
        data_hash="hash",
        rows=1,
        first=_candle(),
        last=_candle(),
        gaps=0,
        duplicates=0,
    )
    monkeypatch.setattr(
        streaming_data,
        "_open_source",
        lambda *_args, **_kwargs: (metadata, (_candle(),)),
    )
    monkeypatch.setattr(
        streaming_data,
        "_run_validation_pass",
        lambda *_args, **_kwargs: validated,
    )
    monkeypatch.setattr(streaming_data, "file_sha256", lambda path: "after")
    with pytest.raises(DataValidationError, match="changed during validation"):
        streaming_data.prepare_candle_source(config)


def test_runner_strict_json_and_sync_fallbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(runner.ResultPublicationError, match="canonical finite JSON"):
        runner._json_text(float("nan"))
    with pytest.raises(runner.ResultPublicationError, match="non-finite"):
        runner._reject_nonfinite_json_constant("NaN")

    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    with pytest.raises(runner.ResultPublicationError, match="invalid JSON"):
        runner._verify_strict_json_files(tmp_path)

    invalid.write_text('{"value": NaN}', encoding="utf-8")
    with pytest.raises(runner.ResultPublicationError, match="non-finite"):
        runner._verify_strict_json_files(tmp_path)

    invalid.unlink()
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(runner.ResultPublicationError, match="not a regular file"):
        runner._verify_strict_json_files(tmp_path)

    called: list[str] = []
    monkeypatch.setattr(
        runner.os,
        "fsync",
        lambda descriptor: (_ for _ in ()).throw(OSError(errno.EINVAL, "x")),
    )
    monkeypatch.setattr(runner.os, "sync", lambda: called.append("sync"))
    runner._sync_fd(1)
    assert called == ["sync"]

    monkeypatch.setattr(
        runner.os,
        "fsync",
        lambda descriptor: (_ for _ in ()).throw(OSError(errno.EBADF, "x")),
    )
    with pytest.raises(OSError):
        runner._sync_fd(1)


def test_runner_tree_and_resource_failure_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "payload.txt").write_text("x", encoding="utf-8")
    nested = tree / "nested"
    nested.mkdir()
    monkeypatch.setattr(runner, "_fsync_file", lambda path: None)
    monkeypatch.setattr(runner, "_fsync_directory", lambda path: None)
    runner._fsync_tree(tree)

    link = tree / "unsafe"
    try:
        link.symlink_to(tree / "payload.txt")
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(runner.ResultPublicationError, match="must not contain symlinks"):
        runner._fsync_tree(tree)
    link.unlink()

    counters = RuntimeCounters(
        bars_processed=5,
        events=2,
        skip_ranges=0,
        equity_points=5,
        retained_equity_points=5,
        diagnostics=0,
        trades=0,
        rss_bytes=100,
        peak_rss_bytes=120,
    )
    error = ResourceLimitExceeded("limit", counters=counters)
    output = tmp_path / "failures"
    failure = runner._write_failure_artifact(output, error, PerformanceConfig())
    payload = json.loads((failure / "failure.json").read_text(encoding="utf-8"))
    assert payload["runtime_counters"]["bars_processed"] == 5
    assert getattr(error, "failure_directory") == str(failure)

    symlink_output = tmp_path / "failure-link"
    try:
        symlink_output.symlink_to(output, target_is_directory=True)
    except OSError:
        return
    with pytest.raises(runner.ResultPublicationError, match="must not be a symlink"):
        runner._write_failure_artifact(
            symlink_output,
            ResourceLimitExceeded("limit"),
            PerformanceConfig(),
        )


def test_runner_factory_and_single_pass_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = BacktestAppConfig(
        data=_file_config(tmp_path),
        modules=tmp_path / "modules.yaml",
        output=OutputConfig(tmp_path / "results"),
        checkpoint=CheckpointConfig(),
    )

    monkeypatch.setattr(runner, "_engine_factory", lambda *_args, **_kwargs: lambda: object())
    with pytest.raises(RuntimeError, match="incompatible engine"):
        runner._scalable_engine_factory(config, PerformanceConfig())()

    prepared = _prepared(tmp_path)
    state = random.getstate()

    class FakeEngine:
        def run_prepared(self, source: object) -> object:
            assert source is prepared
            return SimpleNamespace()

    monkeypatch.setattr(
        runner,
        "_scalable_engine_factory",
        lambda *_args, **_kwargs: lambda: FakeEngine(),
    )
    replay = runner._run_single_pass(config, prepared, PerformanceConfig())
    assert replay.result is not None
    assert replay.restart_verified is False
    assert random.getstate() == state


def test_runner_registered_checkpoint_mode_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_config = BacktestAppConfig(
        data=_file_config(tmp_path),
        modules=tmp_path / "modules.yaml",
    )
    assert runner._registered_checkpoint_uses_streaming(file_config) is False

    registered = BacktestAppConfig(
        data=_registered_config(tmp_path),
        modules=tmp_path / "modules.yaml",
    )
    monkeypatch.setenv("MODULAR_BACKTEST_REGISTERED_DATASET_MODE", "bad")
    with pytest.raises(ValueError, match="must be one of"):
        runner._registered_checkpoint_uses_streaming(registered)

    monkeypatch.setenv("MODULAR_BACKTEST_REGISTERED_DATASET_MODE", "legacy_materialized")
    monkeypatch.setenv("MODULAR_BACKTEST_REGISTERED_STREAM_BATCH_SIZE", "2")
    with pytest.raises(ValueError, match="requires registered dataset mode streaming"):
        runner._registered_checkpoint_uses_streaming(registered)

    monkeypatch.setenv("MODULAR_BACKTEST_REGISTERED_DATASET_MODE", "streaming")
    monkeypatch.delenv("MODULAR_BACKTEST_REGISTERED_STREAM_BATCH_SIZE", raising=False)
    assert runner._registered_checkpoint_uses_streaming(registered) is True
    with frozen_registered_data_options("streaming", 8192):
        monkeypatch.setenv("MODULAR_BACKTEST_REGISTERED_DATASET_MODE", "legacy_materialized")
        monkeypatch.setenv("MODULAR_BACKTEST_REGISTERED_STREAM_BATCH_SIZE", "invalid")
        assert runner._registered_checkpoint_uses_streaming(registered) is True


def test_registered_source_positions_cover_all_validation_branches(tmp_path: Path) -> None:
    source = _streaming_source(tmp_path)
    assert source.initial_position() == _position()

    middle = _streaming_source(tmp_path, start=_START + _STEP)
    assert middle.initial_position() == _position(row_offset=1, last_timestamp=_START)

    exhausted = _streaming_source(tmp_path, start=_START + 10 * _STEP)
    assert exhausted.initial_position() == _position(
        partition_index=2,
        row_offset=0,
        last_timestamp=_START + 3 * _STEP,
    )

    wrong_identity = replace(_position(), dataset_hash="x")
    with pytest.raises(RegisteredDataError, match="different dataset identity"):
        source._validate_position(wrong_identity)

    for bad_index in (-1, 3, True):
        with pytest.raises(RegisteredDataError, match="partition index"):
            source._validate_position(replace(_position(), partition_index=bad_index))

    with pytest.raises(RegisteredDataError, match="timezone-aware"):
        source._validate_position(replace(_position(), last_timestamp=datetime(2024, 1, 1)))
    with pytest.raises(RegisteredDataError, match="must be UTC"):
        source._validate_position(
            replace(
                _position(),
                last_timestamp=datetime(2024, 1, 1, tzinfo=timezone(timedelta(hours=1))),
            )
        )
    with pytest.raises(RegisteredDataError, match="row_offset zero"):
        source._validate_position(
            _position(
                partition_index=2,
                row_offset=1,
                last_timestamp=_START + 3 * _STEP,
            )
        )
    for bad_row in (-1, 2, True):
        with pytest.raises(RegisteredDataError, match="row offset"):
            source._validate_position(replace(_position(), row_offset=bad_row))
    with pytest.raises(RegisteredDataError, match="timestamp does not match"):
        source._validate_position(replace(_position(), last_timestamp=_START))


def test_registered_iterator_close_and_empty_boundaries(tmp_path: Path) -> None:
    source = _streaming_source(tmp_path)
    iterator = StreamingRegisteredCandleIterator(source, _position(), resumed=False)
    iterator.close()
    with pytest.raises(StopIteration):
        next(iterator)

    end_position = _position(
        partition_index=2,
        row_offset=0,
        last_timestamp=_START + 3 * _STEP,
    )
    resumed = StreamingRegisteredCandleIterator(source, end_position, resumed=True)
    with pytest.raises(StopIteration):
        next(resumed)

    fresh = StreamingRegisteredCandleIterator(source, end_position, resumed=False)
    with pytest.raises(RegisteredDataError, match="selection is empty"):
        next(fresh)


def test_registered_iterator_validates_records_and_wraps_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _streaming_source(tmp_path)
    module = pytest.importorskip("market_data_ingestion.streaming_dataset_reader")

    def batch(record: object) -> SimpleNamespace:
        return SimpleNamespace(partition_index=0, row_offset=0, records=(record,))

    wrong_symbol = SimpleNamespace(
        symbol="ETHUSDT",
        timeframe="5m",
        open_time=_START,
        open=1,
        high=2,
        low=0,
        close=1,
        volume=1,
    )
    monkeypatch.setattr(
        module,
        "iter_registered_kline_batches",
        lambda *_args, **_kwargs: iter((batch(wrong_symbol),)),
    )
    with pytest.raises(RegisteredDataError, match="symbol differs"):
        next(source.open())

    wrong_timeframe = SimpleNamespace(
        **{**wrong_symbol.__dict__, "symbol": "BTCUSDT", "timeframe": "1m"}
    )
    monkeypatch.setattr(
        module,
        "iter_registered_kline_batches",
        lambda *_args, **_kwargs: iter((batch(wrong_timeframe),)),
    )
    with pytest.raises(RegisteredDataError, match="timeframe differs"):
        next(source.open())

    monkeypatch.setattr(
        module,
        "iter_registered_kline_batches",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("broken batch")),
    )
    with pytest.raises(RegisteredDataError, match="validation failed"):
        next(source.open())


def test_registered_legacy_load_and_open_argument_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incomplete = SimpleNamespace(
        storage_root=None,
        dataset_id=None,
        dataset_version=None,
    )
    with pytest.raises(RegisteredDataError, match="incomplete"):
        registered_data.open_registered_candles(incomplete)  # type: ignore[arg-type]
    with pytest.raises(RegisteredDataError, match="incomplete"):
        registered_data.open_streaming_registered_candles(incomplete)  # type: ignore[arg-type]

    config = _registered_config(tmp_path)
    for batch_size in (0, True):
        with pytest.raises(RegisteredDataError, match="positive integer"):
            registered_data.open_streaming_registered_candles(config, batch_size=batch_size)

    source = RegisteredCandleSource(
        candles=iter((_candle(), _candle(1))),
        dataset_id="coverage-dataset",
        dataset_version=1,
        dataset_hash="d" * 64,
        partition_count=1,
        source_uri="dataset://coverage-dataset/v1",
    )
    monkeypatch.setattr(registered_data, "open_registered_candles", lambda _config: source)
    loaded = registered_data.load_registered_candles(config)
    assert isinstance(loaded, RegisteredCandleBatch)
    assert loaded.candles == (_candle(), _candle(1))


def test_recovery_manifest_and_identity_failures(tmp_path: Path) -> None:
    store = SimpleNamespace(path=tmp_path / "events.jsonl", flush=lambda: None)
    journal = SimpleNamespace(streaming_store=store)

    store.path.write_text("{", encoding="utf-8")
    with pytest.raises(RegisteredStreamingRecoveryError, match="cannot read"):
        registered_recovery._run_manifest_payload(journal)  # type: ignore[arg-type]

    store.path.write_text(json.dumps({"event_type": "wrong"}) + "\n", encoding="utf-8")
    with pytest.raises(RegisteredStreamingRecoveryError, match="does not begin"):
        registered_recovery._run_manifest_payload(journal)  # type: ignore[arg-type]

    store.path.write_text(
        json.dumps({"event_type": "run.started", "payload": []}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RegisteredStreamingRecoveryError, match="payload is invalid"):
        registered_recovery._run_manifest_payload(journal)  # type: ignore[arg-type]

    store.path.write_text(
        json.dumps({"event_type": "run.started", "payload": {}}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RegisteredStreamingRecoveryError, match="no run manifest"):
        registered_recovery._run_manifest_payload(journal)  # type: ignore[arg-type]

    with pytest.raises(RegisteredStreamingRecoveryError, match="is invalid"):
        registered_recovery._required_manifest_text({}, "run_id")

    with pytest.raises(TypeError, match="must return ScalableBacktestEngine"):
        registered_recovery._instrument_engine(
            object(),  # type: ignore[arg-type]
            target_bars=1,
            observer=lambda boundary: None,
            position_provider=_position,
        )


def test_recovery_source_identity_and_prepared_source_guards(tmp_path: Path) -> None:
    source = _streaming_source(tmp_path)
    validation = streaming_data.RegisteredValidationSummary(
        path="dataset://coverage-dataset/v1",
        sha256=source.dataset_hash,
        symbol="BTCUSDT",
        market_type="binance-usdt-m-perpetual",
        base_timeframe="5m",
        rows=4,
        first_timestamp=_START.isoformat(),
        last_timestamp=(_START + 3 * _STEP).isoformat(),
        gaps=0,
        duplicates=0,
        requested_start=None,
        requested_end=None,
        dataset_id=source.dataset_id,
        dataset_version=source.dataset_version,
        dataset_hash=source.dataset_hash,
        partition_count=source.partition_count,
        source_mode="streaming",
        stream_batch_size=2,
    )
    prepared = PreparedCandleSource(
        config=source.config,
        validation=validation,
        data_hash="hash",
        first_candle=_candle(),
        last_candle=_candle(3),
        source_mode="streaming",
        stream_batch_size=2,
    )

    for changed in (
        replace(source, dataset_hash="x"),
        replace(source, symbol="ETHUSDT"),
        replace(source, timeframe="1m"),
    ):
        with pytest.raises(RegisteredStreamingRecoveryError):
            registered_recovery._validate_source_identity(prepared, changed)

    replay = registered_recovery._ReplayPreparedSource(prepared)
    assert replay.source_open_count == 0
    replay.close()
    with pytest.raises(RegisteredStreamingRecoveryError, match="not open"):
        replay.checkpoint_position()

    fake_iterator = SimpleNamespace(
        checkpoint_position=lambda: _position(),
        source_open_count=1,
        close=lambda: None,
    )
    replay._iterator = fake_iterator  # type: ignore[assignment]
    assert replay.checkpoint_position() == _position()
    assert replay.source_open_count == 1
    with pytest.raises(RegisteredStreamingRecoveryError, match="only once"):
        replay.iter_candles()


def test_recovery_public_validation_errors(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path, source_mode="streaming", stream_batch_size=2)
    with pytest.raises(TypeError, match="seed must be int"):
        registered_recovery.run_with_registered_streaming_replay_restart(
            lambda: object(),  # type: ignore[arg-type]
            prepared,
            restart_at_index=0,
            seed=True,
        )
