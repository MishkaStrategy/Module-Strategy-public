"""Owner-backed production bridges for Strategy Factory research admission.

The Strategy Factory must not reinterpret local files or in-memory test doubles as
owner truth.  This module adapts the accepted Market Data and Results Registry
owners to the existing Factory contracts while leaving both owners authoritative.
"""

from __future__ import annotations

import importlib
import math
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .adapters import ResearchRunnerExecutionAdapter, ResultsAggregatorAdapter
from .canonical import canonical_sha256
from .contracts import (
    DatasetDescriptor,
    DatasetGateStatus,
    DatasetValidationReport,
    MarketType,
    StrategyFactoryRequest,
)
from .dataset import DatasetAnalysis, DatasetAnalyzer, DatasetValidationPolicy, _ProfileAccumulator
from .errors import CompatibilityError, ExternalComponentUnavailableError
from .results_publication import ResearchResultsPublicationAdapter, ResultsPublicationPolicy


def _market_data_api() -> tuple[Any, Any, Any]:
    """Load optional Market Data owner APIs without changing base import boundaries."""

    try:
        dataset_reader = importlib.import_module("market_data_ingestion.dataset_reader")
        funding_reader = importlib.import_module("market_data_ingestion.funding_dataset_reader")
    except ImportError as exc:
        raise ExternalComponentUnavailableError("Market Data owner package is unavailable") from exc
    return (
        dataset_reader.resolve_registered_kline_dataset,
        dataset_reader.iter_registered_klines,
        funding_reader.resolve_registered_funding_dataset,
    )


def _owner_market(value: MarketType) -> str:
    return {
        MarketType.SPOT: "spot",
        MarketType.LINEAR_FUTURES: "usdt_m_perpetual",
        MarketType.INVERSE_FUTURES: "coin_m_perpetual",
    }[value]


class RegisteredDatasetAnalyzer(DatasetAnalyzer):
    """Validate/profile a DatasetDescriptor through the registered Market Data owner.

    ``DatasetDescriptor.sha256`` remains the canonical Dataset Registry identity.
    Physical Parquet partition hashes are verified by the Market Data owner and are
    deliberately not collapsed into a synthetic single-file hash.
    """

    def __init__(
        self,
        policy: DatasetValidationPolicy,
        *,
        storage_root: str | Path,
        dataset_id: str,
        dataset_version: int,
        base_timeframe: str = "5m",
        derived_timeframes: tuple[str, ...] = (),
        funding_dataset_id: str | None = None,
        funding_dataset_version: int | None = None,
    ) -> None:
        super().__init__(policy)
        if not isinstance(dataset_id, str) or not dataset_id.strip():
            raise ValueError("dataset_id must be a non-blank string")
        if (
            isinstance(dataset_version, bool)
            or not isinstance(dataset_version, int)
            or dataset_version < 1
        ):
            raise ValueError("dataset_version must be a positive integer")
        normalized_base = base_timeframe.strip().lower()
        if not normalized_base:
            raise ValueError("base_timeframe must be a non-blank string")
        normalized_derived = tuple(item.strip().lower() for item in derived_timeframes)
        if any(not item for item in normalized_derived) or len(normalized_derived) != len(
            set(normalized_derived)
        ):
            raise ValueError("derived_timeframes must contain unique non-blank values")
        if (funding_dataset_id is None) != (funding_dataset_version is None):
            raise ValueError("funding dataset identity must be complete or absent")
        if funding_dataset_id is not None and (
            not isinstance(funding_dataset_id, str) or not funding_dataset_id.strip()
        ):
            raise ValueError("funding_dataset_id must be a non-blank string")
        if funding_dataset_version is not None and (
            isinstance(funding_dataset_version, bool)
            or not isinstance(funding_dataset_version, int)
            or funding_dataset_version < 1
        ):
            raise ValueError("funding_dataset_version must be a positive integer")
        self._registered_policy = policy
        self._storage_root = Path(storage_root)
        self._dataset_id = dataset_id.strip()
        self._dataset_version = dataset_version
        self._base_timeframe = normalized_base
        self._derived_timeframes = normalized_derived
        self._funding_dataset_id = funding_dataset_id.strip() if funding_dataset_id else None
        self._funding_dataset_version = funding_dataset_version

    def analyze(
        self,
        descriptor: DatasetDescriptor,
        *,
        now: datetime | None = None,
    ) -> DatasetAnalysis:
        checked_at = (now or datetime.now(UTC)).astimezone(UTC)
        try:
            selection = self._resolve_selection(descriptor)
            profile, first_timestamp, last_timestamp, rows, extreme_outliers = (
                self._profile_selection(
                    descriptor,
                    selection,
                )
            )
        except (CompatibilityError, ExternalComponentUnavailableError, OSError, ValueError) as exc:
            return self._rejected_owner(
                descriptor,
                checked_at,
                reason="REGISTERED_OWNER_VERIFICATION_FAILED",
                detail=type(exc).__name__,
            )

        reasons: list[str] = []
        if rows != descriptor.row_count:
            reasons.append("ROW_COUNT_MISMATCH")
        if rows < self._registered_policy.minimum_rows:
            reasons.append("INSUFFICIENT_ROWS")
        if first_timestamp is None or last_timestamp is None:
            reasons.append("EMPTY_OR_UNDECODABLE_DATASET")
        else:
            history_days = Decimal(str((last_timestamp - first_timestamp).total_seconds() / 86_400))
            if history_days < self._registered_policy.minimum_history_days:
                reasons.append("INSUFFICIENT_HISTORY")
            if first_timestamp < descriptor.period.start or last_timestamp >= descriptor.period.end:
                reasons.append("PERIOD_BOUNDARY_MISMATCH")
        if reasons:
            return self._rejected_owner(
                descriptor,
                checked_at,
                reason=",".join(sorted(set(reasons))),
                rows_scanned=rows,
            )

        warnings = ("EXTREME_CANDLES_PRESENT",) if extreme_outliers else ()
        status = (
            DatasetGateStatus.ACCEPTED_WITH_WARNINGS if warnings else DatasetGateStatus.ACCEPTED
        )
        report = DatasetValidationReport(
            report_id=(
                "dataset-report-"
                + canonical_sha256(
                    (
                        descriptor.fingerprint,
                        self._dataset_id,
                        self._dataset_version,
                        checked_at,
                    )
                )[:24]
            ),
            dataset_id=descriptor.dataset_id,
            dataset_fingerprint=descriptor.fingerprint,
            status=status,
            checked_at=checked_at,
            rows_scanned=rows,
            duplicates=0,
            gaps=0,
            out_of_order=0,
            invalid_ohlc=0,
            invalid_volume=0,
            extreme_outliers=extreme_outliers,
            computed_sha256=descriptor.sha256,
            reason_codes=(),
            warnings=warnings,
            evidence={
                "source": "registered_market_data_owner",
                "dataset_id": self._dataset_id,
                "dataset_version": self._dataset_version,
                "registry_dataset_hash": descriptor.sha256,
                "partition_count": len(selection.partitions),
                "first_timestamp": first_timestamp,
                "last_timestamp": last_timestamp,
                "base_timeframe": self._base_timeframe,
            },
        )
        return DatasetAnalysis(report, profile.build(descriptor, checked_at))

    def _resolve_selection(self, descriptor: DatasetDescriptor) -> Any:
        if descriptor.dataset_id != self._dataset_id:
            raise CompatibilityError("configured Dataset Registry identity differs from request")
        if descriptor.base_timeframe.lower() != self._base_timeframe:
            raise CompatibilityError(
                "Factory base timeframe differs from registered owner selection"
            )
        declared_timeframes = {self._base_timeframe, *self._derived_timeframes}
        if not set(descriptor.available_timeframes).issubset(declared_timeframes):
            raise CompatibilityError("Factory dataset timeframes exceed owner-backed declarations")
        if descriptor.file_format != "parquet":
            raise CompatibilityError(
                "registered owner analyzer requires a Parquet execution materialization"
            )

        resolve_klines, _, resolve_funding = _market_data_api()
        try:
            selection = resolve_klines(
                self._storage_root,
                self._dataset_id,
                self._dataset_version,
                timeframe=self._base_timeframe,
                consumer="strategy_factory",
                deep=True,
            )
        except Exception as exc:
            raise ExternalComponentUnavailableError(
                "registered Market Data Kline owner cannot be verified"
            ) from exc
        owner_descriptor = selection.descriptor
        manifest = selection.manifest
        if owner_descriptor.dataset_hash != descriptor.sha256:
            raise CompatibilityError("Dataset Registry hash differs from Factory descriptor")
        if selection.row_count != descriptor.row_count:
            raise CompatibilityError("Dataset Registry row count differs from Factory descriptor")
        if manifest.instrument.symbol != descriptor.symbol:
            raise CompatibilityError("Dataset Registry instrument differs from Factory descriptor")
        if manifest.exchange.id != descriptor.exchange:
            raise CompatibilityError("Dataset Registry exchange differs from Factory descriptor")
        if manifest.market_type.value != _owner_market(descriptor.market_type):
            raise CompatibilityError("Dataset Registry market type differs from Factory descriptor")
        if selection.partitions[0].start != descriptor.period.start:
            raise CompatibilityError("Dataset Registry start differs from Factory descriptor")
        if selection.partitions[-1].end != descriptor.period.end:
            raise CompatibilityError("Dataset Registry end differs from Factory descriptor")

        if descriptor.market_type in {MarketType.LINEAR_FUTURES, MarketType.INVERSE_FUTURES}:
            if descriptor.funding_sha256 is None:
                raise CompatibilityError("futures Factory dataset requires funding lineage")
            funding_id = self._funding_dataset_id or self._dataset_id
            funding_version = self._funding_dataset_version or self._dataset_version
            try:
                funding = resolve_funding(
                    self._storage_root,
                    funding_id,
                    funding_version,
                    consumer="strategy_factory",
                    deep=True,
                )
            except Exception as exc:
                raise ExternalComponentUnavailableError(
                    "registered Market Data funding owner cannot be verified"
                ) from exc
            if funding.descriptor.dataset_hash != descriptor.funding_sha256:
                raise CompatibilityError("registered funding hash differs from Factory descriptor")
            if funding.manifest.instrument.symbol != descriptor.symbol:
                raise CompatibilityError(
                    "registered funding instrument differs from Factory descriptor"
                )
            if funding.manifest.exchange.id != descriptor.exchange:
                raise CompatibilityError(
                    "registered funding exchange differs from Factory descriptor"
                )
            if funding.manifest.market_type.value != _owner_market(descriptor.market_type):
                raise CompatibilityError(
                    "registered funding market differs from Factory descriptor"
                )
        return selection

    def _profile_selection(
        self,
        descriptor: DatasetDescriptor,
        selection: Any,
    ) -> tuple[_ProfileAccumulator, datetime | None, datetime | None, int, int]:
        _, iter_klines, _ = _market_data_api()
        profile = _ProfileAccumulator()
        previous_close: float | None = None
        first_timestamp: datetime | None = None
        last_timestamp: datetime | None = None
        rows = 0
        extreme_outliers = 0
        try:
            records = iter_klines(
                selection,
                start=descriptor.period.start,
                end=descriptor.period.end,
            )
            for record in records:
                open_ = float(record.open)
                high = float(record.high)
                low = float(record.low)
                close = float(record.close)
                volume = float(record.volume)
                values = (open_, high, low, close, volume)
                if not all(math.isfinite(value) for value in values):
                    raise CompatibilityError("registered Kline contains non-finite numeric data")
                if (
                    open_ <= 0
                    or close <= 0
                    or low <= 0
                    or high < max(open_, close)
                    or low > min(open_, close)
                ):
                    raise CompatibilityError("registered Kline contains invalid OHLC data")
                if volume < 0:
                    raise CompatibilityError("registered Kline contains negative volume")
                if volume == 0 and not self._registered_policy.allow_zero_volume:
                    raise CompatibilityError("registered Kline contains forbidden zero volume")
                rows += 1
                first_timestamp = first_timestamp or record.open_time
                last_timestamp = record.open_time
                if (high - low) / close >= float(self._registered_policy.extreme_range_fraction):
                    extreme_outliers += 1
                profile.add(
                    timestamp=record.open_time,
                    open_=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    previous_close=previous_close,
                )
                previous_close = close
        except Exception as exc:
            if isinstance(exc, CompatibilityError):
                raise
            raise ExternalComponentUnavailableError(
                "registered Market Data stream cannot be verified"
            ) from exc
        return profile, first_timestamp, last_timestamp, rows, extreme_outliers

    def _rejected_owner(
        self,
        descriptor: DatasetDescriptor,
        checked_at: datetime,
        *,
        reason: str,
        detail: str | None = None,
        rows_scanned: int = 0,
    ) -> DatasetAnalysis:
        reason_codes = tuple(sorted(set(item for item in reason.split(",") if item)))
        evidence: dict[str, Any] = {
            "source": "registered_market_data_owner",
            "dataset_id": self._dataset_id,
            "dataset_version": self._dataset_version,
        }
        if detail is not None:
            evidence["failure_type"] = detail
        return DatasetAnalysis(
            DatasetValidationReport(
                report_id=(
                    "dataset-report-"
                    + canonical_sha256(
                        (
                            descriptor.fingerprint,
                            self._dataset_id,
                            self._dataset_version,
                            checked_at,
                            reason_codes,
                        )
                    )[:24]
                ),
                dataset_id=descriptor.dataset_id,
                dataset_fingerprint=descriptor.fingerprint,
                status=DatasetGateStatus.REJECTED,
                checked_at=checked_at,
                rows_scanned=rows_scanned,
                duplicates=0,
                gaps=0,
                out_of_order=0,
                invalid_ohlc=0,
                invalid_volume=0,
                extreme_outliers=0,
                computed_sha256=descriptor.sha256,
                reason_codes=reason_codes or ("REGISTERED_OWNER_VERIFICATION_FAILED",),
                evidence=evidence,
            ),
            None,
        )


class PublishingResultsAggregator(ResultsAggregatorAdapter):
    """Publish terminal Research owner packages before every Results Registry read.

    A single Research experiment may gain optimization, walk-forward, or final-test
    runs over time.  The terminal run-set fingerprint prevents redundant publication
    in one process while still publishing again after the owner adds new runs.
    """

    def __init__(
        self,
        *,
        execution: ResearchRunnerExecutionAdapter,
        request: StrategyFactoryRequest,
        package_root: str | Path,
        registry_path: str | Path,
        producer_module_version: str,
        policy: ResultsPublicationPolicy | None = None,
        dataset_report: DatasetValidationReport | None = None,
    ) -> None:
        self._execution = execution
        self._publisher = ResearchResultsPublicationAdapter(
            execution=execution,
            request=request,
            package_root=package_root,
            registry_path=registry_path,
            producer_module_version=producer_module_version,
            policy=policy,
            dataset_report=dataset_report,
        )
        self._published_runsets: dict[str, str] = {}
        super().__init__(registry_path)

    def candidates_for_experiment(
        self,
        experiment_id: str,
        *,
        generated_at: datetime,
    ) -> tuple[Any, ...]:
        snapshot = self._execution.status(experiment_id)
        if snapshot.has_nonterminal_runs or snapshot.status not in {
            "completed",
            "completed_with_failures",
        }:
            raise CompatibilityError(
                "Results Registry publication requires a terminal Research experiment"
            )
        runset = canonical_sha256(
            {
                "experiment_id": snapshot.experiment_id,
                "status": snapshot.status,
                "run_ids": snapshot.run_ids,
                "run_status_counts": dict(snapshot.run_status_counts),
            }
        )
        if self._published_runsets.get(experiment_id) != runset:
            self._publisher.publish_experiment(
                experiment_id,
                observed_at=generated_at,
            )
            self._published_runsets[experiment_id] = runset
        return super().candidates_for_experiment(
            experiment_id,
            generated_at=generated_at,
        )

    def close(self) -> None:
        publisher = getattr(self, "_publisher", None)
        try:
            super().close()
        finally:
            if publisher is not None:
                publisher.close()


__all__ = ["PublishingResultsAggregator", "RegisteredDatasetAnalyzer"]
