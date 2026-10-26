from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..backtest import (
    BacktestComparisonData,
    BacktestExecution,
    BacktestInspection,
    BacktestMetricChange,
    BacktestRunnerError,
)
from ..contracts import SafeScalar


class ManagedBacktestRunner:
    """Server-owned adapter over the existing deterministic Backtest application.

    Browser/API callers provide only a bounded config identity. The adapter resolves the YAML
    under an allowlisted local root and overrides the result root, so no browser-supplied
    filesystem path or shell command reaches ``modular_backtest``.
    """

    def __init__(self, config_root: str | Path, output_root: str | Path) -> None:
        raw_config_root = Path(config_root).expanduser()
        if raw_config_root.is_symlink():
            raise ValueError("backtest config root must not be a symlink")
        self._config_root = raw_config_root.resolve()
        if not self._config_root.is_dir():
            raise ValueError("backtest config root must be an existing directory")

        raw_output_root = Path(output_root).expanduser()
        if raw_output_root.is_symlink():
            raise ValueError("backtest output root must not be a symlink")
        raw_output_root.mkdir(parents=True, exist_ok=True)
        self._output_root = raw_output_root.resolve()
        if not self._output_root.is_dir():
            raise ValueError("backtest output root must be a directory")

    @property
    def config_root(self) -> Path:
        return self._config_root

    @property
    def output_root(self) -> Path:
        return self._output_root

    def run(self, config_id: str) -> BacktestExecution:
        path = self._config_path(config_id)
        try:
            from modular_backtest.config import ConfigurationError, OutputConfig, load_config
            from modular_backtest.data import DataValidationError
            from modular_backtest.results import ResultError
            from modular_backtest.runner import run_backtest
            from modular_backtest.scalability import ResourceLimitExceeded
        except ImportError as exc:
            raise _dependency_unavailable() from exc

        try:
            config = load_config(path)
            if not config.data.is_registered:
                raise BacktestRunnerError(
                    code="BACKTEST_REGISTERED_DATASET_REQUIRED",
                    message="Control Plane Backtests require a registered immutable dataset.",
                    action="Use a server-owned Backtest config that references dataset_id and version.",
                )
            managed = replace(config, output=OutputConfig(directory=self._output_root))
            outcome = run_backtest(managed)
            return BacktestExecution(
                run_id=outcome.run_id,
                output_directory=outcome.output_directory,
                bars_processed=outcome.bars_processed,
                restart_verified=outcome.restart_verified,
            )
        except BacktestRunnerError:
            raise
        except ResourceLimitExceeded as exc:
            raise BacktestRunnerError(
                code="BACKTEST_RESOURCE_LIMIT",
                message="The Backtest exceeded a configured local resource limit.",
                action="Review the server-owned Backtest resource profile before retrying.",
                retryable=True,
                details={"error_type": type(exc).__name__},
            ) from exc
        except (ConfigurationError, DataValidationError) as exc:
            raise BacktestRunnerError(
                code="BACKTEST_CONFIG_OR_DATA_INVALID",
                message="The server-owned Backtest configuration or registered dataset is invalid.",
                action="Validate the managed Backtest config and registered dataset evidence.",
                details={"error_type": type(exc).__name__},
            ) from exc
        except ResultError as exc:
            raise BacktestRunnerError(
                code="BACKTEST_RESULT_PUBLICATION_FAILED",
                message="The Backtest result could not be published or verified safely.",
                action="Open Logs & Evidence and inspect the local Backtest result artifacts.",
                details={"error_type": type(exc).__name__},
            ) from exc
        except (OSError, ValueError) as exc:
            raise BacktestRunnerError(
                code="BACKTEST_EXECUTION_INVALID",
                message="The Backtest could not execute with the managed local inputs.",
                action="Validate the managed configuration and local registered dataset availability.",
                details={"error_type": type(exc).__name__},
            ) from exc

    def inspect(self, result_directory: Path) -> BacktestInspection:
        directory = self._managed_result_path(result_directory)
        try:
            from modular_backtest.results import ResultError, inspect_result
        except ImportError as exc:
            raise _dependency_unavailable() from exc

        try:
            snapshot = inspect_result(directory)
            return BacktestInspection(
                run_id=snapshot.run_id,
                metrics=_metrics(snapshot.metrics),
            )
        except ResultError as exc:
            raise BacktestRunnerError(
                code="BACKTEST_RESULT_INVALID",
                message="The stored Backtest result failed integrity verification.",
                action="Open Logs & Evidence and verify the immutable Backtest result artifacts.",
                details={"error_type": type(exc).__name__},
            ) from exc
        except OSError as exc:
            raise BacktestRunnerError(
                code="BACKTEST_RESULT_UNAVAILABLE",
                message="The stored Backtest result is not locally readable.",
                action="Verify the managed Backtest result storage is available.",
                retryable=True,
                details={"error_type": type(exc).__name__},
            ) from exc

    def compare(self, left: Path, right: Path) -> BacktestComparisonData:
        left_path = self._managed_result_path(left)
        right_path = self._managed_result_path(right)
        try:
            from modular_backtest.results import ResultError, compare_results
        except ImportError as exc:
            raise _dependency_unavailable() from exc

        try:
            raw = compare_results(left_path, right_path)
            left_run_id = _required_text(raw.get("left_run_id"), "left_run_id")
            right_run_id = _required_text(raw.get("right_run_id"), "right_run_id")
            changes_value = raw.get("changes")
            if not isinstance(changes_value, dict):
                raise ValueError("comparison changes must be a mapping")
            changes: dict[str, BacktestMetricChange] = {}
            for key, value in changes_value.items():
                if not isinstance(key, str) or not isinstance(value, dict):
                    raise ValueError("comparison entries must be keyed mappings")
                left_value = _safe_scalar(value.get("left"), key)
                right_value = _safe_scalar(value.get("right"), key)
                delta_value = value.get("delta")
                delta = None
                if delta_value is not None:
                    if not isinstance(delta_value, int | float) or isinstance(delta_value, bool):
                        raise ValueError("comparison delta must be numeric")
                    delta = float(delta_value)
                    if not math.isfinite(delta):
                        raise ValueError("comparison delta must be finite")
                changes[key] = BacktestMetricChange(
                    left=left_value,
                    right=right_value,
                    delta=delta,
                )
            return BacktestComparisonData(
                left_run_id=left_run_id,
                right_run_id=right_run_id,
                changes=changes,
            )
        except ResultError as exc:
            raise BacktestRunnerError(
                code="BACKTEST_COMPARISON_INVALID",
                message="One or both Backtest results failed integrity verification.",
                action="Open each result before retrying the comparison.",
                details={"error_type": type(exc).__name__},
            ) from exc
        except (OSError, ValueError) as exc:
            raise BacktestRunnerError(
                code="BACKTEST_COMPARISON_UNAVAILABLE",
                message="The Backtest comparison could not be produced safely.",
                action="Verify both managed Backtest results are available and valid.",
                details={"error_type": type(exc).__name__},
            ) from exc

    def _config_path(self, config_id: str) -> Path:
        candidate = self._config_root / f"{config_id}.yaml"
        if candidate.is_symlink():
            raise BacktestRunnerError(
                code="BACKTEST_CONFIG_UNSAFE",
                message="The managed Backtest config resolved to an unsafe filesystem entry.",
                action="Replace the config with a regular file under the configured Backtest root.",
            )
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise BacktestRunnerError(
                code="BACKTEST_CONFIG_NOT_FOUND",
                message="The requested managed Backtest config does not exist.",
                action="Choose a Backtest config identity registered on this local server.",
            ) from exc
        if resolved.parent != self._config_root or not resolved.is_file():
            raise BacktestRunnerError(
                code="BACKTEST_CONFIG_UNSAFE",
                message="The managed Backtest config is outside the allowlisted config root.",
                action="Use a regular YAML config directly under the configured Backtest root.",
            )
        return resolved

    def _managed_result_path(self, value: Path) -> Path:
        raw = value.expanduser()
        if raw.is_symlink():
            raise BacktestRunnerError(
                code="BACKTEST_RESULT_UNSAFE",
                message="The durable Backtest result points to an unsafe filesystem entry.",
                action="Inspect the local Backtest result storage before retrying.",
            )
        try:
            resolved = raw.resolve(strict=True)
        except FileNotFoundError as exc:
            raise BacktestRunnerError(
                code="BACKTEST_RESULT_UNAVAILABLE",
                message="The durable Backtest result no longer exists locally.",
                action="Verify managed Backtest result storage before retrying.",
                retryable=True,
            ) from exc
        if not resolved.is_dir() or not resolved.is_relative_to(self._output_root):
            raise BacktestRunnerError(
                code="BACKTEST_RESULT_UNSAFE",
                message="The durable Backtest result is outside the managed result root.",
                action="Inspect the local Backtest state database and result storage.",
            )
        return resolved


def _dependency_unavailable() -> BacktestRunnerError:
    return BacktestRunnerError(
        code="BACKTEST_DEPENDENCY_UNAVAILABLE",
        message="The Backtest owner package is not available in this Control Plane server environment.",
        action="Install the Backtest owner package before enabling or retrying Backtest Control.",
        retryable=False,
        details={"error_type": "DependencyUnavailable"},
    )


def _metrics(values: dict[str, Any]) -> dict[str, SafeScalar]:
    return {str(key): _safe_scalar(value, str(key)) for key, value in values.items()}


def _safe_scalar(value: object, name: str) -> SafeScalar:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError(f"metric {name} must be a finite scalar")


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value
