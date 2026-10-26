"""Owner-native Market Data boundary for the governed MS5A multi-strategy SEARCH."""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .contracts import (
    DatasetDescriptor,
    FactoryMode,
    FactoryStage,
    MarketType,
    ResourceBudget,
    StrategyFactoryRequest,
    TimeRange,
)
from .errors import CompatibilityError, ExternalComponentUnavailableError
from .owner_runtime import RegisteredDatasetAnalyzer

MS5A_DATASET_ID = "binance_usdt-m-perpetual_btcusdt_5m"
MS5A_DATASET_VERSION = 1
MS5A_FUNDING_VERSION = 6
MS5A_REQUIRED_GAP_POLICY_HASH = "2ec4189035fc3d65d09c71e8894dbd6b438b2e4aabacad77f9d57b318670fc36"
MS5A_EXECUTION_EXCHANGE = "binance_usdtm"
MS5A_OWNER_EXCHANGE = "binance"
MS5A_SYMBOL = "BTCUSDT"
MS5A_RANDOM_SEED = 312
MS5A_MINIMUM_TRADE_COUNT = 30
MS5A_MAXIMUM_DRAWDOWN = Decimal("0.15")
MS5A_INITIAL_CAPITAL = Decimal("10000")
MS5A_RISK_FRACTION = Decimal("0.005")
MS5A_TESTED_PERIOD_START = datetime.fromisoformat("2023-11-10T04:05:00+00:00")
MS5A_REQUEST_CREATED_AT = datetime.fromisoformat("2026-09-24T21:09:00+00:00")
MS5A_DATASET_HASH = "49ee948c7c22dc3c552bf6a96d2fdce9c5bba97662a694ae98ab85fc521c04f5"
MS5A_FUNDING_HASH = "9d9a6d6aa1b71e69ea4b666b57c4da7fb7d1270aea6dd887732c9a381ca8c9de"
MS5A_REQUIRED_TIMEFRAMES = ("15m", "1h", "4h", "1d")
MS5A_ALLOWED_FAMILIES = frozenset({"breakout_return", "liquidity_sweep", "fvg_rt"})
MS5A_COST_STRESS_SCENARIO_ID = "cost-double-v1"
MS5A_COST_STRESS_MULTIPLIER = 2

# Equal family budgets after the MS5A fairness audit:
# 4 architecture slots, 32 structural points, 8 coarse candidates and
# 4 local candidates per family. Unused capacity is deliberately not reallocated.
MS5A_MAXIMUM_ARCHITECTURES = 12
MS5A_STRUCTURAL_SCREENING_LIMIT = 96
MS5A_COARSE_SEARCH_LIMIT = 24
MS5A_LOCAL_REFINEMENT_LIMIT = 12
MS5A_MAXIMUM_EXPERIMENTS = (
    MS5A_STRUCTURAL_SCREENING_LIMIT + MS5A_COARSE_SEARCH_LIMIT + MS5A_LOCAL_REFINEMENT_LIMIT
)
MS5A_MAXIMUM_FINALISTS = 3
MS5A_MAXIMUM_DURATION_SECONDS = 86_400
MS5A_MAXIMUM_MEMORY_BYTES = 8 * 1024 * 1024 * 1024
MS5A_MAXIMUM_CPU_WORKERS = 1
MS5A_MAXIMUM_PARALLEL_TASKS = 1


@dataclass(frozen=True, slots=True)
class OwnerState:
    request: StrategyFactoryRequest
    dataset_file_size: int
    owner_row_count: int
    funding_source: Any


class _OwnerResolutionDeadlineExceeded(TimeoutError):
    """Cooperative deadline signal; distinct from a resolver's own timeout."""


@dataclass(frozen=True, slots=True)
class OwnerResolutionDiagnostics:
    """Optional bounded observer and cooperative deadline for owner localization."""

    deadline_monotonic: float
    record: Callable[[str, str | None, int | None], None]
    monotonic: Callable[[], float] = time.monotonic

    def check_deadline(self) -> None:
        if self.monotonic() >= self.deadline_monotonic:
            raise _OwnerResolutionDeadlineExceeded(
                "registered owner setup exhausted native deadline"
            )

    def start(self, phase: str) -> None:
        if phase not in _OWNER_DIAGNOSTIC_PHASES:
            raise CompatibilityError("owner diagnostic phase is unsupported")
        self.check_deadline()
        self.record(f"owner_{phase}_started", None, None)

    def finish(
        self,
        phase: str,
        *,
        counter_name: str | None = None,
        counter_value: int | None = None,
    ) -> None:
        if phase not in _OWNER_DIAGNOSTIC_PHASES:
            raise CompatibilityError("owner diagnostic phase is unsupported")
        if (counter_name is None) != (counter_value is None):
            raise CompatibilityError("owner diagnostic counter is incomplete")
        if counter_name != _OWNER_DIAGNOSTIC_PHASE_COUNTERS[phase]:
            raise CompatibilityError("owner diagnostic counter does not match phase")
        if counter_value is not None and (
            isinstance(counter_value, bool)
            or not isinstance(counter_value, int)
            or not 0 <= counter_value <= 2**63 - 1
        ):
            raise CompatibilityError("owner diagnostic counter is invalid")
        self.record(f"owner_{phase}_returned", counter_name, counter_value)
        self.check_deadline()


_OWNER_DIAGNOSTIC_PHASES = frozenset(
    {
        "kline_resolver",
        "funding_resolver",
        "mark_price_resolver",
        "kline_overlap_count",
        "registered_file_size",
        "funding_mark_merge",
    }
)
_OWNER_DIAGNOSTIC_PHASE_COUNTERS = {
    "kline_resolver": "partitions_processed",
    "funding_resolver": "partitions_processed",
    "mark_price_resolver": "partitions_processed",
    "kline_overlap_count": "rows_processed",
    "registered_file_size": "files_processed",
    "funding_mark_merge": "events_built",
}


def _check_owner_deadline(diagnostics: OwnerResolutionDiagnostics | None) -> None:
    if diagnostics is not None:
        diagnostics.check_deadline()


def _market_data_api() -> tuple[Any, Any, Any, Any, Any, Any, Any, Any]:
    try:
        dataset_reader = importlib.import_module("market_data_ingestion.dataset_reader")
        funding_reader = importlib.import_module("market_data_ingestion.funding_dataset_reader")
        price_reader = importlib.import_module("market_data_ingestion.price_dataset_reader")
        enums = importlib.import_module("market_data_ingestion.enums")
        integrity = importlib.import_module("market_data_ingestion.integrity")
    except ImportError as exc:
        raise ExternalComponentUnavailableError("Market Data owner package is unavailable") from exc
    return (
        dataset_reader.resolve_registered_kline_dataset,
        dataset_reader.iter_registered_klines,
        funding_reader.resolve_registered_funding_dataset,
        funding_reader.iter_registered_funding,
        price_reader.resolve_registered_price_dataset,
        price_reader.iter_registered_prices,
        enums.DataType.MARK_PRICE,
        integrity.resolve_registered_path,
    )


def _required_gap_api() -> tuple[Any, Any]:
    try:
        required_gap = importlib.import_module("market_data_ingestion.required_gap_policy")
    except ImportError as exc:
        raise ExternalComponentUnavailableError("Market Data gap policy is unavailable") from exc
    return (
        required_gap.policy_hash_from_adapter_version,
        required_gap.load_required_gap_policy,
    )


def _ms5a_required_gap_policy(selection: Any) -> Any:
    policy_hash_from_adapter_version, load_required_gap_policy = _required_gap_api()
    try:
        policy_hash = policy_hash_from_adapter_version(selection.manifest.exchange.adapter_version)
    except (AttributeError, TypeError, ValueError) as exc:
        raise CompatibilityError("MS5A mark-price policy identity is invalid") from exc
    if policy_hash != MS5A_REQUIRED_GAP_POLICY_HASH:
        raise CompatibilityError("MS5A mark-price policy hash differs from approved identity")
    try:
        policy = load_required_gap_policy(selection.storage_root, policy_hash)
    except Exception as exc:
        raise CompatibilityError("MS5A mark-price policy sidecar cannot be verified") from exc
    if policy.canonical_hash != MS5A_REQUIRED_GAP_POLICY_HASH:
        raise CompatibilityError(
            "MS5A mark-price policy sidecar hash differs from approved identity"
        )
    return policy


def _iter_ms5a_mark_prices(
    selection: Any,
    iter_prices: Any,
    policy: Any,
    *,
    deadline_check: Callable[[], None] | None = None,
) -> Any:
    cursor = selection.partition_start_inclusive
    end = selection.partition_end_exclusive
    observed = 0
    exceptions = (
        policy.exceptions
        if deadline_check is None
        else _deadline_checked(policy.exceptions, deadline_check)
    )
    for exception in exceptions:
        if exception.start < cursor or exception.end > end:
            raise CompatibilityError("approved mark-price gap escapes registered coverage")
        if cursor < exception.start:
            records = iter_prices(selection, start=cursor, end=exception.start)
            if deadline_check is not None:
                records = _deadline_checked(records, deadline_check)
            for record in records:
                observed += 1
                yield record
        cursor = exception.end
    if cursor < end:
        records = iter_prices(selection, start=cursor, end=end)
        if deadline_check is not None:
            records = _deadline_checked(records, deadline_check)
        for record in records:
            observed += 1
            yield record
    if observed != selection.row_count:
        raise CompatibilityError("registered mark-price rows differ from approved sparse policy")


def _funding_records_outside_required_gaps(
    records: Any,
    policy: Any,
    *,
    deadline_check: Callable[[], None] | None = None,
) -> Any:
    for record in records:
        if deadline_check is not None:
            deadline_check()
        if deadline_check is None:
            intersects_gap = any(
                exception.start <= record.funding_time < exception.end
                for exception in policy.exceptions
            )
        else:
            intersects_gap = False
            exceptions = _deadline_checked(policy.exceptions, deadline_check)
            for exception in exceptions:
                if exception.start <= record.funding_time < exception.end:
                    intersects_gap = True
                    break
        if intersects_gap:
            raise CompatibilityError("funding event intersects an approved mark-price gap")
        yield record


def _funding_records_in_period(
    records: Any,
    start: datetime,
    end: datetime,
    *,
    deadline_check: Callable[[], None] | None = None,
) -> Any:
    if deadline_check is None:
        return (record for record in records if start <= record.funding_time < end)
    return (
        record
        for record in _deadline_checked(records, deadline_check)
        if start <= record.funding_time < end
    )


def _registered_dataset_file_size(
    selection: Any,
    resolve_registered_path: Any,
    *,
    deadline_check: Callable[[], None] | None = None,
) -> int:
    total = 0
    for partition in selection.partitions:
        if deadline_check is not None:
            deadline_check()
        path = resolve_registered_path(
            selection.storage_root,
            partition.storage_uri,
            kind="canonical kline partition",
        )
        if path.is_symlink() or not path.is_file():
            raise CompatibilityError("registered kline partition is missing or unsafe")
        size = path.stat().st_size
        if size < 1:
            raise CompatibilityError("registered kline partition is empty")
        total += size
        if deadline_check is not None:
            deadline_check()
    if total < 1:
        raise CompatibilityError("registered canonical dataset has no physical bytes")
    return total


def _funding_event(
    record: Any,
    *,
    manifest: Any,
    ordinal: int,
    mark_price_text: object,
    funding_event_type: Any,
) -> Any:
    if (
        record.symbol != manifest.instrument.symbol
        or record.exchange_id != manifest.exchange.id
        or record.market_type != manifest.market_type.value
    ):
        raise CompatibilityError("funding record identity differs from registered owner")
    try:
        mark_price = Decimal(str(mark_price_text))
    except Exception as exc:
        raise CompatibilityError("official Backtest funding mark price is invalid") from exc
    if not mark_price.is_finite() or mark_price <= 0:
        raise CompatibilityError("official Backtest funding mark price must be positive")
    return funding_event_type(
        ordinal=ordinal,
        timestamp=record.funding_time,
        symbol=record.symbol,
        rate=Decimal(record.funding_rate),
        mark_price=mark_price,
    )


def _registered_funding_source(
    selection: Any,
    records: Any,
    price_selection: Any,
    price_records: Any,
    *,
    scope_start: datetime,
    scope_end: datetime,
    deadline_check: Callable[[], None] | None = None,
) -> Any:
    try:
        from modular_backtest.funding import (
            FundingEvent,
            FundingLineageEntry,
            RegisteredFundingSource,
        )
    except ImportError as exc:
        raise ExternalComponentUnavailableError("Backtest funding contract is unavailable") from exc

    if price_selection.descriptor.dataset_hash != selection.descriptor.dataset_hash:
        raise CompatibilityError("funding and mark-price owner hashes differ")
    if price_selection.descriptor.dataset_id != selection.descriptor.dataset_id:
        raise CompatibilityError("funding and mark-price owner identities differ")
    if price_selection.descriptor.version != selection.descriptor.version:
        raise CompatibilityError("funding and mark-price owner versions differ")

    manifest = selection.manifest
    events: list[Any] = []
    if deadline_check is None:
        scoped_funding_records = (
            record for record in records if scope_start <= record.funding_time < scope_end
        )
        scoped_price_records = price_records
    else:
        scoped_funding_records = _deadline_checked(
            (record for record in records if scope_start <= record.funding_time < scope_end),
            deadline_check,
        )
        scoped_price_records = _deadline_checked(price_records, deadline_check)
    funding_iterator = iter(scoped_funding_records)
    current = next(funding_iterator, None)

    for price_record in scoped_price_records:
        while current is not None and current.funding_time < price_record.open_time:
            if current.mark_price is None:
                raise CompatibilityError(
                    "registered mark-price owner lacks exact funding timestamp"
                )
            events.append(
                _funding_event(
                    current,
                    manifest=manifest,
                    ordinal=len(events),
                    mark_price_text=current.mark_price,
                    funding_event_type=FundingEvent,
                )
            )
            current = next(funding_iterator, None)

        if current is None or current.funding_time != price_record.open_time:
            continue
        mark_price_text = (
            current.mark_price if current.mark_price is not None else price_record.open
        )
        events.append(
            _funding_event(
                current,
                manifest=manifest,
                ordinal=len(events),
                mark_price_text=mark_price_text,
                funding_event_type=FundingEvent,
            )
        )
        current = next(funding_iterator, None)

    while current is not None:
        if current.mark_price is None:
            raise CompatibilityError("registered mark-price owner lacks exact funding timestamp")
        events.append(
            _funding_event(
                current,
                manifest=manifest,
                ordinal=len(events),
                mark_price_text=current.mark_price,
                funding_event_type=FundingEvent,
            )
        )
        current = next(funding_iterator, None)

    lineage = tuple(
        FundingLineageEntry(
            artifact_id=item.artifact_id,
            source_kind=(item.source.value if hasattr(item.source, "value") else str(item.source)),
            checksum_sha256=item.checksum_sha256,
            record_count=item.record_count,
            start=item.start,
            end=item.end,
        )
        for item in sorted(
            selection.source_artifacts,
            key=lambda value: (value.start, value.end, value.artifact_id),
        )
    )
    source = RegisteredFundingSource(
        dataset_id=selection.descriptor.dataset_id,
        dataset_version=selection.descriptor.version,
        dataset_hash=selection.descriptor.dataset_hash,
        symbol=manifest.instrument.symbol,
        coverage_start_inclusive=selection.coverage_start_inclusive,
        coverage_end_exclusive=selection.coverage_end_exclusive,
        event_count=len(events),
        source_record_count=selection.source_record_count,
        lineage=lineage,
        events=tuple(events),
        schema_version=2,
        scope_start_inclusive=scope_start,
        scope_end_exclusive=scope_end,
    )
    if source.dataset_hash != selection.descriptor.dataset_hash:
        raise CompatibilityError("adapted funding source hash differs from owner")
    return source


def _deadline_checked(records: Any, deadline_check: Callable[[], None] | None) -> Any:
    iterator = iter(records)
    while True:
        if deadline_check is not None:
            deadline_check()
        try:
            record = next(iterator)
        except StopIteration:
            return
        if deadline_check is not None:
            deadline_check()
        yield record


def _count_owner_rows(records: Any, diagnostics: OwnerResolutionDiagnostics | None) -> int:
    count = 0
    if diagnostics is None:
        return sum(1 for _record in records)
    for _record in records:
        count += 1
        _check_owner_deadline(diagnostics)
    return count


class MS5ARegisteredDatasetAnalyzer(RegisteredDatasetAnalyzer):
    """Allow only the deep-verified OHLCV/funding/mark-price overlap for execution."""

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
        (
            resolve_klines,
            _,
            resolve_funding,
            _,
            resolve_prices,
            _,
            mark_price_type,
            _,
        ) = _market_data_api()
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
        if descriptor.row_count > selection.row_count:
            raise CompatibilityError("Factory row count exceeds registered owner rows")
        if manifest.instrument.symbol != descriptor.symbol:
            raise CompatibilityError("Dataset Registry instrument differs from Factory descriptor")
        if descriptor.exchange != MS5A_EXECUTION_EXCHANGE:
            raise CompatibilityError("Factory MS5A execution exchange alias changed")
        if manifest.exchange.id != MS5A_OWNER_EXCHANGE:
            raise CompatibilityError("Dataset Registry owner exchange differs from Binance")
        if manifest.market_type.value != "usdt_m_perpetual":
            raise CompatibilityError("Dataset Registry market type differs from Factory descriptor")
        if (
            descriptor.period.start < selection.partitions[0].start
            or descriptor.period.end > selection.partitions[-1].end
        ):
            raise CompatibilityError("Factory execution period exceeds registered OHLCV coverage")
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
            mark_prices = resolve_prices(
                self._storage_root,
                funding_id,
                funding_version,
                price_type=mark_price_type,
                timeframe=self._base_timeframe,
                consumer="strategy_factory",
                deep=True,
            )
        except Exception as exc:
            raise ExternalComponentUnavailableError(
                "registered Market Data funding valuation owner cannot be verified"
            ) from exc
        for owner in (funding, mark_prices):
            if owner.descriptor.dataset_hash != descriptor.funding_sha256:
                raise CompatibilityError(
                    "registered funding valuation hash differs from Factory descriptor"
                )
            if owner.manifest.instrument.symbol != descriptor.symbol:
                raise CompatibilityError(
                    "registered funding valuation instrument differs from Factory descriptor"
                )
            if owner.manifest.exchange.id != MS5A_OWNER_EXCHANGE:
                raise CompatibilityError(
                    "registered funding valuation owner exchange differs from Binance"
                )
            if owner.manifest.market_type.value != "usdt_m_perpetual":
                raise CompatibilityError(
                    "registered funding valuation market differs from Factory descriptor"
                )
        _ms5a_required_gap_policy(mark_prices)
        if (
            funding.coverage_start_inclusive > descriptor.period.start
            or funding.coverage_end_exclusive < descriptor.period.end
        ):
            raise CompatibilityError("registered funding does not cover Factory execution period")
        if (
            mark_prices.partition_start_inclusive > descriptor.period.start
            or mark_prices.partition_end_exclusive < descriptor.period.end
        ):
            raise CompatibilityError(
                "registered mark-price owner does not cover Factory execution period"
            )
        return selection


def build_ms5a_owner_state(
    market_data_root: str | Path,
    *,
    created_at: datetime,
    diagnostics: OwnerResolutionDiagnostics | None = None,
) -> OwnerState:
    root = Path(market_data_root).expanduser()
    if root.is_symlink():
        raise CompatibilityError("Market Data owner root must not be a symlink")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise CompatibilityError("Market Data owner root must be a real directory")
    (
        resolve_klines,
        iter_klines,
        resolve_funding,
        iter_funding,
        resolve_prices,
        iter_prices,
        mark_price_type,
        resolve_path,
    ) = _market_data_api()
    try:
        if diagnostics is not None:
            diagnostics.start("kline_resolver")
        klines = resolve_klines(
            root,
            MS5A_DATASET_ID,
            MS5A_DATASET_VERSION,
            timeframe="5m",
            consumer="strategy_factory",
            deep=True,
        )
        if diagnostics is not None:
            diagnostics.finish(
                "kline_resolver",
                counter_name="partitions_processed",
                counter_value=len(klines.partitions),
            )
            diagnostics.start("funding_resolver")
        funding = resolve_funding(
            root,
            MS5A_DATASET_ID,
            MS5A_FUNDING_VERSION,
            consumer="strategy_factory",
            deep=True,
        )
        if diagnostics is not None:
            diagnostics.finish(
                "funding_resolver",
                counter_name="partitions_processed",
                counter_value=len(funding.partitions),
            )
            diagnostics.start("mark_price_resolver")
        mark_prices = resolve_prices(
            root,
            MS5A_DATASET_ID,
            MS5A_FUNDING_VERSION,
            price_type=mark_price_type,
            timeframe="5m",
            consumer="strategy_factory",
            deep=True,
        )
        if diagnostics is not None:
            diagnostics.finish(
                "mark_price_resolver",
                counter_name="partitions_processed",
                counter_value=len(mark_prices.partitions),
            )
    except _OwnerResolutionDeadlineExceeded:
        raise
    except Exception as exc:
        raise ExternalComponentUnavailableError(
            "MS5A registered Market Data owner cannot be deep-verified"
        ) from exc
    if klines.descriptor.dataset_hash != MS5A_DATASET_HASH:
        raise CompatibilityError("MS5A OHLCV owner hash differs from frozen identity")
    for selection, label in (
        (klines, "OHLCV"),
        (funding, "funding"),
        (mark_prices, "mark-price"),
    ):
        if selection.manifest.instrument.symbol != MS5A_SYMBOL:
            raise CompatibilityError(f"canonical {label} owner symbol mismatch")
        if selection.manifest.exchange.id != MS5A_OWNER_EXCHANGE:
            raise CompatibilityError(f"canonical {label} owner exchange mismatch")
        if selection.manifest.market_type.value != "usdt_m_perpetual":
            raise CompatibilityError(f"canonical {label} owner market mismatch")
    for selection, label in ((funding, "funding"), (mark_prices, "mark-price")):
        if selection.descriptor.dataset_id != MS5A_DATASET_ID:
            raise CompatibilityError(
                f"MS5A {label} owner dataset identity differs from frozen v6 identity"
            )
        if selection.descriptor.version != MS5A_FUNDING_VERSION:
            raise CompatibilityError(f"MS5A {label} owner version differs from frozen v6 identity")
    if funding.descriptor.dataset_hash != MS5A_FUNDING_HASH:
        raise CompatibilityError("MS5A funding owner hash differs from frozen v6 identity")
    if mark_prices.descriptor.dataset_hash != MS5A_FUNDING_HASH:
        raise CompatibilityError("MS5A mark-price owner hash differs from frozen v6 identity")
    if funding.descriptor.dataset_hash != mark_prices.descriptor.dataset_hash:
        raise CompatibilityError("MS5A funding and mark-price owner hashes differ")
    if funding.descriptor.dataset_id != mark_prices.descriptor.dataset_id:
        raise CompatibilityError("MS5A funding and mark-price owner identities differ")
    if funding.descriptor.version != mark_prices.descriptor.version:
        raise CompatibilityError("MS5A funding and mark-price owner versions differ")
    if mark_prices.partition_start_inclusive > funding.event_span_start_inclusive:
        raise CompatibilityError("MS5A mark-price owner starts after funding events")
    if mark_prices.partition_end_exclusive < funding.event_span_end_exclusive:
        raise CompatibilityError("MS5A mark-price owner ends before funding events")
    gap_policy = _ms5a_required_gap_policy(mark_prices)

    period_start = max(
        MS5A_TESTED_PERIOD_START,
        klines.partitions[0].start,
        funding.coverage_start_inclusive,
        mark_prices.partition_start_inclusive,
    )
    period_end = min(
        klines.partitions[-1].end,
        mark_prices.partition_end_exclusive,
    )
    if funding.coverage_end_exclusive < period_end:
        raise CompatibilityError("MS5A funding owner ends before execution coverage")
    if period_end <= period_start:
        raise CompatibilityError(
            "MS5A OHLCV, funding, and mark-price owner coverage do not overlap"
        )
    if diagnostics is not None:
        diagnostics.start("kline_overlap_count")
    row_count = _count_owner_rows(
        iter_klines(klines, start=period_start, end=period_end), diagnostics
    )
    if diagnostics is not None:
        diagnostics.finish(
            "kline_overlap_count", counter_name="rows_processed", counter_value=row_count
        )
    if row_count < 1:
        raise CompatibilityError("canonical owner overlap contains no OHLCV rows")
    if diagnostics is not None:
        diagnostics.start("registered_file_size")
    dataset_file_size = _registered_dataset_file_size(
        klines,
        resolve_path,
        deadline_check=None if diagnostics is None else diagnostics.check_deadline,
    )
    if diagnostics is not None:
        diagnostics.finish(
            "registered_file_size",
            counter_name="files_processed",
            counter_value=len(klines.partitions),
        )
        diagnostics.start("funding_mark_merge")
    funding_source = _registered_funding_source(
        funding,
        _funding_records_outside_required_gaps(
            _funding_records_in_period(
                iter_funding(funding),
                period_start,
                period_end,
                deadline_check=None if diagnostics is None else diagnostics.check_deadline,
            ),
            gap_policy,
            deadline_check=None if diagnostics is None else diagnostics.check_deadline,
        ),
        mark_prices,
        _iter_ms5a_mark_prices(
            mark_prices,
            iter_prices,
            gap_policy,
            deadline_check=None if diagnostics is None else diagnostics.check_deadline,
        ),
        scope_start=period_start,
        scope_end=period_end,
        deadline_check=None if diagnostics is None else diagnostics.check_deadline,
    )
    funding_source.assert_covers(period_start, period_end)
    if diagnostics is not None:
        diagnostics.finish(
            "funding_mark_merge",
            counter_name="events_built",
            counter_value=funding_source.event_count,
        )
    dataset = DatasetDescriptor(
        dataset_id=MS5A_DATASET_ID,
        source="market_data_registry",
        symbol=MS5A_SYMBOL,
        exchange=MS5A_EXECUTION_EXCHANGE,
        market_type=MarketType.LINEAR_FUTURES,
        base_timeframe="5m",
        available_timeframes=("5m", *MS5A_REQUIRED_TIMEFRAMES),
        period=TimeRange(period_start, period_end),
        timezone="UTC",
        file_format="parquet",
        uri=f"market-data-registry:{MS5A_DATASET_ID}:v{MS5A_DATASET_VERSION}",
        sha256=klines.descriptor.dataset_hash,
        row_count=row_count,
        received_at=klines.manifest.created_at,
        funding_uri=(f"market-data-registry:{MS5A_DATASET_ID}:v{MS5A_FUNDING_VERSION}:funding"),
        funding_sha256=funding.descriptor.dataset_hash,
    )
    budget = ResourceBudget(
        maximum_experiments=MS5A_MAXIMUM_EXPERIMENTS,
        maximum_architectures=MS5A_MAXIMUM_ARCHITECTURES,
        maximum_finalists=MS5A_MAXIMUM_FINALISTS,
        maximum_duration_seconds=MS5A_MAXIMUM_DURATION_SECONDS,
        maximum_memory_bytes=MS5A_MAXIMUM_MEMORY_BYTES,
        maximum_cpu_workers=MS5A_MAXIMUM_CPU_WORKERS,
        maximum_parallel_tasks=MS5A_MAXIMUM_PARALLEL_TASKS,
        stage_experiment_limits={
            FactoryStage.STRUCTURAL_SCREENING: MS5A_STRUCTURAL_SCREENING_LIMIT,
            FactoryStage.COARSE_SEARCH: MS5A_COARSE_SEARCH_LIMIT,
            FactoryStage.LOCAL_REFINEMENT: MS5A_LOCAL_REFINEMENT_LIMIT,
        },
    )
    request = StrategyFactoryRequest(
        request_id="ms5a-multi-strategy-search-v2",
        mode=FactoryMode.CREATE,
        symbol=MS5A_SYMBOL,
        exchange=MS5A_EXECUTION_EXCHANGE,
        market_type=MarketType.LINEAR_FUTURES,
        dataset=dataset,
        allowed_timeframes=("5m", *MS5A_REQUIRED_TIMEFRAMES),
        initial_capital=MS5A_INITIAL_CAPITAL,
        research_risk_fraction=MS5A_RISK_FRACTION,
        maximum_drawdown_fraction=MS5A_MAXIMUM_DRAWDOWN,
        minimum_trade_count=MS5A_MINIMUM_TRADE_COUNT,
        objectives={
            "profit_factor": "maximize",
            "max_drawdown": "minimize",
            "strategy_version": "0.1.0",
            "cost_scenarios": (
                {
                    "scenario_id": MS5A_COST_STRESS_SCENARIO_ID,
                    "roles": ("oos",),
                    "execution_cost_multiplier": MS5A_COST_STRESS_MULTIPLIER,
                },
            ),
        },
        budget=budget,
        random_seed=MS5A_RANDOM_SEED,
        allowed_strategy_families=MS5A_ALLOWED_FAMILIES,
        created_at=created_at,
    )
    return OwnerState(request, dataset_file_size, klines.row_count, funding_source)


__all__ = [
    "MS5A_ALLOWED_FAMILIES",
    "MS5A_COARSE_SEARCH_LIMIT",
    "MS5A_DATASET_HASH",
    "MS5A_DATASET_ID",
    "MS5A_DATASET_VERSION",
    "MS5A_EXECUTION_EXCHANGE",
    "MS5A_FUNDING_HASH",
    "MS5A_FUNDING_VERSION",
    "MS5A_INITIAL_CAPITAL",
    "MS5A_LOCAL_REFINEMENT_LIMIT",
    "MS5A_MAXIMUM_ARCHITECTURES",
    "MS5A_MAXIMUM_DRAWDOWN",
    "MS5A_MAXIMUM_EXPERIMENTS",
    "MS5A_MAXIMUM_FINALISTS",
    "MS5A_MINIMUM_TRADE_COUNT",
    "MS5A_OWNER_EXCHANGE",
    "MS5A_RANDOM_SEED",
    "MS5A_REQUIRED_GAP_POLICY_HASH",
    "MS5A_REQUEST_CREATED_AT",
    "MS5A_REQUIRED_TIMEFRAMES",
    "MS5A_RISK_FRACTION",
    "MS5A_STRUCTURAL_SCREENING_LIMIT",
    "MS5A_SYMBOL",
    "MS5A_TESTED_PERIOD_START",
    "MS5ARegisteredDatasetAnalyzer",
    "OwnerState",
    "build_ms5a_owner_state",
]
