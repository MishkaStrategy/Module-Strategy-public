from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import errno
import json
import os
import stat
from pathlib import Path
from tempfile import NamedTemporaryFile
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Protocol, Sequence, cast

from .models import Candle, Fill, PositionSnapshot, Side
from .serialization import deep_freeze, strict_json_loads, to_jsonable
from .validation import require_nonblank, utc_datetime


@dataclass(frozen=True, slots=True)
class OrderBookLevel:
    price: float
    quantity: float


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot:
    timestamp: datetime
    symbol: str
    bids: tuple[OrderBookLevel, ...]
    asks: tuple[OrderBookLevel, ...]
    sequence: int | None = None


@dataclass(frozen=True, slots=True)
class TradeTick:
    timestamp: datetime
    symbol: str
    price: float
    quantity: float
    aggressor_side: Side | None = None


class FundingCostModel(Protocol):
    def accrued_cost(
        self,
        *,
        position: PositionSnapshot,
        start: datetime,
        end: datetime,
    ) -> float: ...


class BorrowCostModel(Protocol):
    def accrued_cost(
        self,
        *,
        asset: str,
        quantity: float,
        start: datetime,
        end: datetime,
    ) -> float: ...


class MarginModel(Protocol):
    def maintenance_margin(self, *, notional: float, leverage: float) -> float: ...

    def liquidation_price(
        self,
        *,
        entry_price: float,
        side: Side,
        quantity: float,
        equity: float,
    ) -> float | None: ...


class LatencyModel(Protocol):
    def order_latency(self, *, source_module: str, timestamp: datetime) -> timedelta: ...


class SpreadModel(Protocol):
    def bid_ask(self, *, candle: Candle) -> tuple[float, float]: ...


class LiquidityModel(Protocol):
    def fillable_quantity(
        self,
        *,
        requested_quantity: float,
        price: float,
        candle: Candle,
        order_book: OrderBookSnapshot | None = None,
    ) -> float: ...


class SlippageModel(Protocol):
    def execution_price(
        self,
        *,
        reference_price: float,
        side: Side,
        quantity: float,
        liquidity: str,
    ) -> float: ...


class StateRepository(Protocol):
    def save(self, key: str, value: Mapping[str, Any]) -> None: ...

    def load(self, key: str) -> Mapping[str, Any] | None: ...

    def delete(self, key: str) -> None: ...


class InMemoryStateRepository:
    def __init__(self, *, max_state_bytes: int | None = 16 * 1024 * 1024) -> None:
        _validate_max_state_bytes(max_state_bytes)
        self.max_state_bytes = max_state_bytes
        self._values: dict[str, Mapping[str, Any]] = {}

    def save(self, key: str, value: Mapping[str, Any]) -> None:
        _validate_state_key(key)
        payload = _serialize_state(value, self.max_state_bytes)
        self._values[key] = deep_freeze(json.loads(payload))

    def load(self, key: str) -> Mapping[str, Any] | None:
        _validate_state_key(key)
        return self._values.get(key)

    def delete(self, key: str) -> None:
        _validate_state_key(key)
        self._values.pop(key, None)


class AtomicJsonStateRepository:
    """Durable JSON state repository using atomic replace semantics."""

    def __init__(
        self,
        directory: str | Path,
        *,
        max_state_bytes: int | None = 16 * 1024 * 1024,
    ) -> None:
        _validate_max_state_bytes(max_state_bytes)
        self.max_state_bytes = max_state_bytes
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        _validate_state_key(key)
        target = self.directory / f"{key}.json"
        if target.parent != self.directory:
            raise ValueError("state key escapes repository directory")
        return target

    def save(self, key: str, value: Mapping[str, Any]) -> None:
        target = self._path(key)
        payload = _serialize_state(value, self.max_state_bytes)
        temp_path: Path | None = None
        try:
            with NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.directory, delete=False
            ) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
                temp_path = Path(handle.name)
            os.replace(temp_path, target)
            temp_path = None
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def load(self, key: str) -> Mapping[str, Any] | None:
        target = self._path(key)
        try:
            payload = _read_bounded_regular_file(target, self.max_state_bytes)
        except FileNotFoundError:
            return None
        value = strict_json_loads(payload)
        if not isinstance(value, dict):
            raise ValueError("stored state must be a JSON object")
        return cast(Mapping[str, Any], deep_freeze(value))

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class ExecutionStateSnapshot:
    timestamp: datetime
    pending_order_ids: tuple[str, ...] = ()
    position: PositionSnapshot | None = None
    client_order_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "timestamp",
            utc_datetime(self.timestamp, "execution snapshot timestamp"),
        )
        object.__setattr__(
            self,
            "pending_order_ids",
            _canonical_identity_tuple(
                self.pending_order_ids,
                item_name="pending order id",
                duplicate_error="execution snapshot contains duplicate order ids",
            ),
        )
        object.__setattr__(
            self,
            "client_order_ids",
            _canonical_identity_tuple(
                self.client_order_ids,
                item_name="client order id",
                duplicate_error="execution snapshot contains duplicate client order ids",
            ),
        )
        if self.position is not None and not isinstance(self.position, PositionSnapshot):
            raise TypeError("execution snapshot position must be PositionSnapshot or None")


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    matched: bool
    missing_local_order_ids: tuple[str, ...] = ()
    missing_remote_order_ids: tuple[str, ...] = ()
    position_mismatch: bool = False
    safe_halt_required: bool = False
    diagnostics: Mapping[str, Any] = MappingProxyType({})
    missing_local_client_order_ids: tuple[str, ...] = ()
    missing_remote_client_order_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("matched", "position_mismatch", "safe_halt_required"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be bool")
        if not isinstance(self.diagnostics, Mapping):
            raise TypeError("reconciliation diagnostics must be a mapping")
        object.__setattr__(self, "diagnostics", deep_freeze(self.diagnostics))
        identity_groups = (
            "missing_local_order_ids",
            "missing_remote_order_ids",
            "missing_local_client_order_ids",
            "missing_remote_client_order_ids",
        )
        for name in identity_groups:
            object.__setattr__(
                self,
                name,
                _canonical_identity_tuple(
                    getattr(self, name),
                    item_name=name,
                    duplicate_error=f"{name} cannot contain duplicates",
                ),
            )
        has_mismatch = bool(
            self.missing_local_order_ids
            or self.missing_remote_order_ids
            or self.position_mismatch
            or self.missing_local_client_order_ids
            or self.missing_remote_client_order_ids
        )
        if self.matched != (not has_mismatch):
            raise ValueError("reconciliation matched flag is inconsistent with mismatches")
        if self.safe_halt_required != has_mismatch:
            raise ValueError("reconciliation mismatches require safe halt")

    @property
    def issue_count(self) -> int:
        return (
            len(self.missing_local_order_ids)
            + len(self.missing_remote_order_ids)
            + int(self.position_mismatch)
            + len(self.missing_local_client_order_ids)
            + len(self.missing_remote_client_order_ids)
        )


def reconcile_execution_state(
    local: ExecutionStateSnapshot, remote: ExecutionStateSnapshot
) -> ReconciliationReport:
    local_orders = set(local.pending_order_ids)
    remote_orders = set(remote.pending_order_ids)
    local_client_orders = set(local.client_order_ids)
    remote_client_orders = set(remote.client_order_ids)
    position_mismatch = local.position != remote.position
    missing_local = tuple(sorted(remote_orders - local_orders))
    missing_remote = tuple(sorted(local_orders - remote_orders))
    missing_local_clients = tuple(sorted(remote_client_orders - local_client_orders))
    missing_remote_clients = tuple(sorted(local_client_orders - remote_client_orders))
    matched = not (
        missing_local
        or missing_remote
        or missing_local_clients
        or missing_remote_clients
        or position_mismatch
    )
    return ReconciliationReport(
        matched=matched,
        missing_local_order_ids=missing_local,
        missing_remote_order_ids=missing_remote,
        position_mismatch=position_mismatch,
        safe_halt_required=not matched,
        diagnostics={
            "local_timestamp": local.timestamp,
            "remote_timestamp": remote.timestamp,
        },
        missing_local_client_order_ids=missing_local_clients,
        missing_remote_client_order_ids=missing_remote_clients,
    )


class PaperExecutionAdapter(ABC):
    @abstractmethod
    def submit(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    @abstractmethod
    def reconcile(self) -> None:
        raise NotImplementedError


class LiveExecutionAdapter(PaperExecutionAdapter):
    @abstractmethod
    def emergency_halt(self, reason: str) -> None:
        raise NotImplementedError


class ParameterOptimizer(Protocol):
    def generate(self) -> Iterable[Mapping[str, Any]]: ...


class ScenarioGenerator(Protocol):
    def generate(self, fills: Sequence[Fill], *, samples: int) -> Iterable[Sequence[Fill]]: ...


class DistributedExecutor(Protocol):
    def map(self, function: Any, items: Iterable[Any]) -> Iterable[Any]: ...


class ResearchRunner(Protocol):
    def run(self, configurations: Iterable[Mapping[str, Any]]) -> Iterable[Any]: ...


def _validate_max_state_bytes(value: int | None) -> None:
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("max_state_bytes must be int or None")
    if value < 1:
        raise ValueError("max_state_bytes must be >= 1")


def _read_bounded_regular_file(path: Path, max_bytes: int | None) -> bytes:
    path_metadata = os.lstat(path)
    if not stat.S_ISREG(path_metadata.st_mode):
        raise ValueError("stored state must be a regular file and cannot be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError("stored state cannot be a symlink") from exc
        raise
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("stored state must be a regular file")
        if (metadata.st_dev, metadata.st_ino) != (
            path_metadata.st_dev,
            path_metadata.st_ino,
        ):
            raise ValueError("stored state changed while opening")
        if max_bytes is not None and metadata.st_size > max_bytes:
            raise ValueError("stored state exceeds size limit")
        read_limit = -1 if max_bytes is None else max_bytes + 1
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            payload = handle.read(read_limit)
        if max_bytes is not None and len(payload) > max_bytes:
            raise ValueError("stored state exceeds size limit")
        return payload
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _canonical_identity_tuple(
    value: object,
    *,
    item_name: str,
    duplicate_error: str,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{item_name}s must be a sequence")
    identities = tuple(value)
    for identity in identities:
        if not isinstance(identity, str):
            raise TypeError(f"{item_name} must be str")
        require_nonblank(identity, item_name)
    if len(identities) != len(set(identities)):
        raise ValueError(duplicate_error)
    return identities


def _validate_state_key(key: str) -> None:
    if not isinstance(key, str) or not key.strip():
        raise ValueError("state key must be a safe relative name")
    if "/" in key or "\\" in key or key in {".", ".."}:
        raise ValueError("state key must be a safe relative name")


def _serialize_state(value: Mapping[str, Any], max_state_bytes: int | None) -> str:
    if not isinstance(value, Mapping):
        raise TypeError("state must be a mapping")
    payload = json.dumps(
        to_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if max_state_bytes is not None and len(payload.encode("utf-8")) > max_state_bytes:
        raise ValueError("state exceeds size limit")
    return payload
