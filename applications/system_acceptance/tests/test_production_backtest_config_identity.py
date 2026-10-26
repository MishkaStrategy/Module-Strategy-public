from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from modular_system_acceptance.contracts import AcceptanceOwnerError
from modular_system_acceptance.production_backtest_config_identity import (
    BacktestExecutionIdentity,
    build_backtest_execution_identity,
)

DATASET_ID = "btc-usdtm-5m"
DATASET_DIGEST = "a" * 64
FUNDING_DIGEST = "b" * 64
MARK_DIGEST = "c" * 64


@dataclass(frozen=True)
class DataConfig:
    symbol: str = "BTCUSDT"
    path: Path | None = None
    format: str | None = None
    market_type: str = "binance-usdt-m-perpetual"
    base_timeframe: str = "5m"
    gap_policy: str = "reject"
    timestamp_column: str = "open_time"
    start: object | None = None
    end: object | None = None
    columns: dict[str, str] | None = None
    storage_root: Path | None = None
    dataset_id: str | None = DATASET_ID
    dataset_version: int | None = 1
    registered_mode: str = "streaming"
    stream_batch_size: int | None = 8192

    def __post_init__(self) -> None:
        if self.columns is None:
            object.__setattr__(
                self,
                "columns",
                {
                    "open": "open",
                    "high": "high",
                    "low": "low",
                    "close": "close",
                    "volume": "volume",
                },
            )


@dataclass(frozen=True)
class OutputConfig:
    directory: Path


@dataclass(frozen=True)
class CheckpointConfig:
    mode: str = "disabled"
    restart_at_bar: int | None = None


@dataclass(frozen=True)
class BacktestConfig:
    data: DataConfig
    modules: Path
    initial_capital: float = 10_000.0
    requested_timeframes: tuple[str, ...] = ("15m", "1h", "4h")
    execution: dict[str, Any] | None = None
    fees: dict[str, Any] | None = None
    risk: dict[str, Any] | None = None
    instrument: dict[str, Any] | None = None
    output: OutputConfig | None = None
    seed: int = 0
    checkpoint: CheckpointConfig = CheckpointConfig()
    end_of_test_policy: str = "force_close"

    def __post_init__(self) -> None:
        for name in ("execution", "fees", "risk", "instrument"):
            if getattr(self, name) is None:
                object.__setattr__(self, name, {})
        if self.output is None:
            object.__setattr__(self, "output", OutputConfig(Path("results")))


@dataclass(frozen=True)
class PerformanceConfig:
    mode: str = "canonical_audit"
    max_memory_mb: int | None = 9216
    max_events: int | None = 2_000_000
    max_skip_ranges: int | None = 250_000
    max_diagnostics: int | None = 250_000
    max_equity_points: int | None = 5_000_000
    progress_interval_bars: int = 10_000
    retained_event_window: int = 2_000
    equity_sampling: str | None = "every_bar"
    equity_every_n_bars: int = 12
    max_base_history: int | None = 65_536
    max_timeframe_history: int | None = 4_096
    disable_cyclic_gc_in_hot_loop: bool = False


@dataclass(frozen=True)
class ExtendedPerformanceConfig(PerformanceConfig):
    new_execution_field: int = 1


@dataclass(frozen=True)
class OwnerSource:
    digest: str

    def identity_sha256(self) -> str:
        return self.digest


def configs(
    tmp_path: Path,
) -> tuple[BacktestConfig, BacktestConfig, BacktestConfig, PerformanceConfig]:
    modules = tmp_path / "modules.yaml"
    modules.write_text("modules:\n  - name: strategy\n", encoding="utf-8")
    data = DataConfig(storage_root=tmp_path / "registry")
    backtest_a = BacktestConfig(
        data=data,
        modules=modules,
        output=OutputConfig(tmp_path / "backtest-a-results"),
    )
    backtest_b = replace(
        backtest_a,
        output=OutputConfig(tmp_path / "backtest-b-results"),
    )
    recovery = replace(
        backtest_a,
        output=OutputConfig(tmp_path / "recovery-results"),
        checkpoint=CheckpointConfig(mode="replay", restart_at_bar=100),
    )
    return backtest_a, backtest_b, recovery, PerformanceConfig()


def identity(
    tmp_path: Path,
    *,
    backtest_a: BacktestConfig | None = None,
    backtest_b: BacktestConfig | None = None,
    recovery: BacktestConfig | None = None,
    performance: object | None = None,
    environment: dict[str, str] | None = None,
    valuation_policy: str = "last",
    funding_source: object | None = None,
    mark_source: object | None = None,
) -> BacktestExecutionIdentity:
    if backtest_a is None or backtest_b is None or recovery is None or performance is None:
        default_a, default_b, default_recovery, default_performance = configs(tmp_path)
        if backtest_a is None:
            backtest_a = default_a
        if backtest_b is None:
            backtest_b = default_b
        if recovery is None:
            recovery = default_recovery
        if performance is None:
            performance = default_performance
    return build_backtest_execution_identity(
        backtest_a_config=backtest_a,
        backtest_b_config=backtest_b,
        recovery_config=recovery,
        performance=performance,
        dataset_id=DATASET_ID,
        dataset_digest=DATASET_DIGEST,
        dataset_version=1,
        dataset_timeframe="5m",
        valuation_policy=valuation_policy,
        funding_source=funding_source,
        mark_source=mark_source,
        environment={} if environment is None else environment,
    )


def test_backtest_identity_is_deterministic(tmp_path: Path) -> None:
    backtest_a, backtest_b, recovery, performance = configs(tmp_path)

    first = identity(
        tmp_path,
        backtest_a=backtest_a,
        backtest_b=backtest_b,
        recovery=recovery,
        performance=performance,
    )
    second = identity(
        tmp_path,
        backtest_a=backtest_a,
        backtest_b=backtest_b,
        recovery=recovery,
        performance=performance,
    )

    assert first == second
    assert len(first.config_digest) == 64
    assert len(first.performance_digest) == 64


def test_backtest_a_b_semantic_mismatch_fails_closed(tmp_path: Path) -> None:
    backtest_a, backtest_b, recovery, performance = configs(tmp_path)

    with pytest.raises(AcceptanceOwnerError, match="A and B execution semantics"):
        identity(
            tmp_path,
            backtest_a=replace(backtest_a, seed=7),
            backtest_b=backtest_b,
            recovery=recovery,
            performance=performance,
        )


def test_recovery_checkpoint_change_changes_config_digest(tmp_path: Path) -> None:
    backtest_a, backtest_b, recovery, performance = configs(tmp_path)
    baseline = identity(
        tmp_path,
        backtest_a=backtest_a,
        backtest_b=backtest_b,
        recovery=recovery,
        performance=performance,
    )
    changed = identity(
        tmp_path,
        backtest_a=backtest_a,
        backtest_b=backtest_b,
        recovery=replace(recovery, checkpoint=CheckpointConfig("replay", 200)),
        performance=performance,
    )

    assert changed.config_digest != baseline.config_digest


def test_recovery_non_checkpoint_mismatch_fails_closed(tmp_path: Path) -> None:
    backtest_a, backtest_b, recovery, performance = configs(tmp_path)

    with pytest.raises(AcceptanceOwnerError, match="only by checkpoint semantics"):
        identity(
            tmp_path,
            backtest_a=backtest_a,
            backtest_b=backtest_b,
            recovery=replace(recovery, seed=11),
            performance=performance,
        )


def test_backtest_a_b_must_disable_checkpoint(tmp_path: Path) -> None:
    backtest_a, backtest_b, recovery, performance = configs(tmp_path)
    replay = CheckpointConfig("replay", 50)

    with pytest.raises(AcceptanceOwnerError, match="A and B must use checkpoint mode disabled"):
        identity(
            tmp_path,
            backtest_a=replace(backtest_a, checkpoint=replay),
            backtest_b=replace(backtest_b, checkpoint=replay),
            recovery=recovery,
            performance=performance,
        )


def test_recovery_must_use_replay_checkpoint(tmp_path: Path) -> None:
    backtest_a, backtest_b, recovery, performance = configs(tmp_path)

    with pytest.raises(AcceptanceOwnerError, match="recovery must use checkpoint mode replay"):
        identity(
            tmp_path,
            backtest_a=backtest_a,
            backtest_b=backtest_b,
            recovery=replace(recovery, checkpoint=CheckpointConfig()),
            performance=performance,
        )


def test_performance_change_changes_performance_digest(tmp_path: Path) -> None:
    backtest_a, backtest_b, recovery, performance = configs(tmp_path)
    baseline = identity(
        tmp_path,
        backtest_a=backtest_a,
        backtest_b=backtest_b,
        recovery=recovery,
        performance=performance,
    )
    changed = identity(
        tmp_path,
        backtest_a=backtest_a,
        backtest_b=backtest_b,
        recovery=recovery,
        performance=replace(performance, retained_event_window=3_000),
    )

    assert changed.config_digest == baseline.config_digest
    assert changed.performance_digest != baseline.performance_digest


def test_module_configuration_content_change_changes_config_digest(tmp_path: Path) -> None:
    backtest_a, backtest_b, recovery, performance = configs(tmp_path)
    baseline = identity(
        tmp_path,
        backtest_a=backtest_a,
        backtest_b=backtest_b,
        recovery=recovery,
        performance=performance,
    )
    backtest_a.modules.write_text("modules:\n  - name: changed\n", encoding="utf-8")
    changed = identity(
        tmp_path,
        backtest_a=backtest_a,
        backtest_b=backtest_b,
        recovery=recovery,
        performance=performance,
    )

    assert changed.config_digest != baseline.config_digest


def test_output_paths_do_not_change_execution_identity(tmp_path: Path) -> None:
    backtest_a, backtest_b, recovery, performance = configs(tmp_path)
    baseline = identity(
        tmp_path,
        backtest_a=backtest_a,
        backtest_b=backtest_b,
        recovery=recovery,
        performance=performance,
    )
    changed = identity(
        tmp_path,
        backtest_a=replace(backtest_a, output=OutputConfig(tmp_path / "other-a")),
        backtest_b=replace(backtest_b, output=OutputConfig(tmp_path / "other-b")),
        recovery=replace(recovery, output=OutputConfig(tmp_path / "other-recovery")),
        performance=performance,
    )

    assert changed == baseline


def test_registered_stream_batch_override_changes_config_digest(tmp_path: Path) -> None:
    backtest_a, backtest_b, recovery, performance = configs(tmp_path)
    baseline = identity(
        tmp_path,
        backtest_a=backtest_a,
        backtest_b=backtest_b,
        recovery=recovery,
        performance=performance,
    )
    changed = identity(
        tmp_path,
        backtest_a=backtest_a,
        backtest_b=backtest_b,
        recovery=recovery,
        performance=performance,
        environment={"MODULAR_BACKTEST_REGISTERED_STREAM_BATCH_SIZE": "4096"},
    )

    assert changed.config_digest != baseline.config_digest


def test_illegal_legacy_batch_override_fails_closed(tmp_path: Path) -> None:
    backtest_a, backtest_b, recovery, performance = configs(tmp_path)
    legacy_data = replace(
        backtest_a.data,
        registered_mode="legacy_materialized",
        stream_batch_size=None,
    )
    backtest_a = replace(backtest_a, data=legacy_data)
    backtest_b = replace(backtest_b, data=legacy_data)
    recovery = replace(recovery, data=legacy_data)

    with pytest.raises(AcceptanceOwnerError, match="requires streaming mode"):
        identity(
            tmp_path,
            backtest_a=backtest_a,
            backtest_b=backtest_b,
            recovery=recovery,
            performance=performance,
            environment={"MODULAR_BACKTEST_REGISTERED_STREAM_BATCH_SIZE": "4096"},
        )


def test_backtest_schema_growth_fails_closed(tmp_path: Path) -> None:
    backtest_a, backtest_b, recovery, _ = configs(tmp_path)

    with pytest.raises(AcceptanceOwnerError, match="schema differs"):
        identity(
            tmp_path,
            backtest_a=backtest_a,
            backtest_b=backtest_b,
            recovery=recovery,
            performance=ExtendedPerformanceConfig(),
        )


def test_registered_dataset_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    backtest_a, backtest_b, recovery, performance = configs(tmp_path)
    wrong = replace(backtest_a.data, dataset_version=2)

    with pytest.raises(AcceptanceOwnerError, match="dataset identity differs"):
        identity(
            tmp_path,
            backtest_a=replace(backtest_a, data=wrong),
            backtest_b=backtest_b,
            recovery=recovery,
            performance=performance,
        )


def test_noncanonical_nested_configuration_fails_closed(tmp_path: Path) -> None:
    backtest_a, backtest_b, recovery, performance = configs(tmp_path)
    bad = replace(backtest_a, execution={"unsupported": {"set-value"}})

    with pytest.raises(AcceptanceOwnerError, match="noncanonical value"):
        identity(
            tmp_path,
            backtest_a=bad,
            backtest_b=backtest_b,
            recovery=recovery,
            performance=performance,
        )


def test_owner_funding_and_mark_identities_are_bound(tmp_path: Path) -> None:
    backtest_a, backtest_b, recovery, performance = configs(tmp_path)
    funding = OwnerSource(FUNDING_DIGEST)
    mark = OwnerSource(MARK_DIGEST)

    last = identity(
        tmp_path,
        backtest_a=backtest_a,
        backtest_b=backtest_b,
        recovery=recovery,
        performance=performance,
        funding_source=funding,
    )
    marked = identity(
        tmp_path,
        backtest_a=backtest_a,
        backtest_b=backtest_b,
        recovery=recovery,
        performance=performance,
        valuation_policy="mark",
        funding_source=funding,
        mark_source=mark,
    )

    assert last.funding_identity_digest == FUNDING_DIGEST
    assert last.mark_identity_digest is None
    assert marked.funding_identity_digest == FUNDING_DIGEST
    assert marked.mark_identity_digest == MARK_DIGEST
