from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import quote, urlencode, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..market_data import (
    MarketDatasetList,
    MarketDatasetSummary,
    MarketDatasetValidation,
)
from .market_data import MarketDataHealthSnapshot

_MAX_HEALTH_BYTES = 64 * 1024
_MAX_CATALOG_BYTES = 1024 * 1024
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class _HttpHealthSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    status: str = Field(min_length=1, max_length=64)
    component: str = Field(min_length=1, max_length=64)
    api_version: str = Field(min_length=1, max_length=16)
    build_version: str = Field(min_length=1, max_length=64)
    checks: dict[str, str] = Field(default_factory=dict)


class _NativePagination(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    limit: int = Field(ge=1, le=200)
    offset: int = Field(ge=0)
    total: int = Field(ge=0)


class _NativeDatasetDetails(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    dataset_id: str = Field(min_length=1, max_length=128)
    version: int = Field(gt=0)
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    exchange_id: str = Field(min_length=1, max_length=64)
    market_type: str = Field(min_length=1, max_length=64)
    instrument_id: str = Field(min_length=1, max_length=128)
    data_types: tuple[str, ...] = ()
    timeframes: tuple[str, ...] = ()
    start: datetime
    end: datetime
    dataset_schema_version: int = Field(gt=0)
    validation_status: str = Field(min_length=1, max_length=64)
    unresolved_issues: tuple[str, ...] = ()
    lineage: tuple[str, ...] = ()
    compatible_consumers: tuple[str, ...] = ()
    descriptor_version: int = Field(gt=0)
    registered_at: datetime

    @field_validator("start", "end", "registered_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("native Market Data timestamp must be timezone-aware")
        return value.astimezone(UTC)


class _NativeDatasetList(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    items: tuple[_NativeDatasetDetails, ...]
    pagination: _NativePagination


class _NativeDatasetValidation(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    dataset_id: str = Field(min_length=1, max_length=128)
    version: int = Field(gt=0)
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_status: str = Field(min_length=1, max_length=64)
    status: Literal["ok", "error"]
    deep: bool
    partitions_checked: int = Field(ge=0)
    raw_artifacts_checked: int = Field(ge=0)
    rows_checked: int = Field(ge=0)
    issues: tuple[str, ...] = ()


class MarketDataHttpHealthError(RuntimeError):
    """Safe transport/contract failure without response bodies or credentials."""


class MarketDataHttpCatalogError(RuntimeError):
    """Safe catalog failure without response bodies or private native payload fields."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MarketDataHttpHealthReader:
    """Read the existing Market Data v1 health endpoint over a bounded loopback request."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 2.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = validate_market_data_base_url(base_url)
        _validate_token(token)
        _validate_timeout(timeout_seconds)
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def __call__(self) -> MarketDataHealthSnapshot:
        payload = _request_json_bytes(
            self._base_url,
            "/api/v1/health",
            token=self._token,
            timeout_seconds=self._timeout_seconds,
            transport=self._transport,
            maximum_bytes=_MAX_HEALTH_BYTES,
            error_type=MarketDataHttpHealthError,
            operation="health",
        )

        try:
            decoded = json.loads(payload)
            snapshot = _HttpHealthSnapshot.model_validate(decoded)
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, TypeError) as exc:
            raise MarketDataHttpHealthError(
                f"Market Data health contract validation failed: {type(exc).__name__}"
            ) from exc
        return snapshot

    def __repr__(self) -> str:
        return (
            "MarketDataHttpHealthReader("
            f"base_url={self._base_url!r}, token_configured={self._token is not None})"
        )


class MarketDataHttpCatalogReader:
    """Read native Market Data catalog state and project only Control Plane-safe fields."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 2.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = validate_market_data_base_url(base_url)
        _validate_token(token)
        _validate_timeout(timeout_seconds)
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def list_datasets(self, *, limit: int, offset: int) -> MarketDatasetList:
        _pagination(limit=limit, offset=offset)
        query = urlencode({"limit": limit, "offset": offset})
        native = self._get_model(f"/api/v1/datasets?{query}", _NativeDatasetList)
        if native.pagination.limit != limit or native.pagination.offset != offset:
            raise MarketDataHttpCatalogError("Market Data dataset pagination identity mismatch")
        try:
            return MarketDatasetList(
                items=tuple(_project_dataset(item) for item in native.items),
                limit=native.pagination.limit,
                offset=native.pagination.offset,
                total=native.pagination.total,
            )
        except ValidationError:
            raise MarketDataHttpCatalogError(
                "Market Data dataset list projection is not safe for Control Plane"
            ) from None

    def get_dataset(self, dataset_id: str, *, version: int | None) -> MarketDatasetSummary:
        segment = _dataset_segment(dataset_id)
        _version(version)
        query = "" if version is None else f"?{urlencode({'version': version})}"
        native = self._get_model(f"/api/v1/datasets/{segment}{query}", _NativeDatasetDetails)
        if native.dataset_id != dataset_id or (version is not None and native.version != version):
            raise MarketDataHttpCatalogError("Market Data dataset response identity mismatch")
        return _project_dataset(native)

    def validate_dataset(
        self,
        dataset_id: str,
        *,
        version: int | None,
        deep: bool,
    ) -> MarketDatasetValidation:
        segment = _dataset_segment(dataset_id)
        _version(version)
        if not isinstance(deep, bool):
            raise TypeError("deep must be bool")
        parameters: dict[str, str | int] = {"deep": "true" if deep else "false"}
        if version is not None:
            parameters["version"] = version
        query = urlencode(parameters)
        native = self._get_model(
            f"/api/v1/datasets/{segment}/validation?{query}",
            _NativeDatasetValidation,
        )
        if native.dataset_id != dataset_id or (version is not None and native.version != version):
            raise MarketDataHttpCatalogError("Market Data validation response identity mismatch")
        if native.deep is not deep:
            raise MarketDataHttpCatalogError("Market Data validation depth mismatch")
        try:
            return MarketDatasetValidation(
                dataset_id=native.dataset_id,
                version=native.version,
                dataset_hash=native.dataset_hash,
                validation_status=native.validation_status,
                status=native.status,
                deep=native.deep,
                partitions_checked=native.partitions_checked,
                raw_artifacts_checked=native.raw_artifacts_checked,
                rows_checked=native.rows_checked,
                issue_count=len(native.issues),
            )
        except ValidationError:
            raise MarketDataHttpCatalogError(
                "Market Data validation projection is not safe for Control Plane"
            ) from None

    def _get_model[T: BaseModel](self, path: str, model: type[T]) -> T:
        payload = _request_json_bytes(
            self._base_url,
            path,
            token=self._token,
            timeout_seconds=self._timeout_seconds,
            transport=self._transport,
            maximum_bytes=_MAX_CATALOG_BYTES,
            error_type=MarketDataHttpCatalogError,
            operation="catalog",
        )
        try:
            decoded = json.loads(payload)
            return model.model_validate(decoded)
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, TypeError) as exc:
            raise MarketDataHttpCatalogError(
                f"Market Data catalog contract validation failed: {type(exc).__name__}"
            ) from exc

    def __repr__(self) -> str:
        return (
            "MarketDataHttpCatalogReader("
            f"base_url={self._base_url!r}, token_configured={self._token is not None})"
        )


def validate_market_data_base_url(base_url: str) -> str:
    candidate = base_url.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme != "http":
        raise ValueError("Market Data URL must use plain HTTP on the local loopback boundary")
    if parsed.hostname is None or parsed.hostname.lower() not in _LOOPBACK_HOSTS:
        raise ValueError("Market Data URL must target a loopback host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Market Data URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("Market Data URL must not contain query or fragment data")
    if parsed.path not in {"", "/"}:
        raise ValueError("Market Data URL must not contain a path prefix")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Market Data URL contains an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Market Data URL port must be between 1 and 65535")
    normalized_host = parsed.hostname.lower()
    host = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    return f"http://{host}{f':{port}' if port is not None else ''}"


def _request_json_bytes(
    base_url: str,
    path: str,
    *,
    token: str | None,
    timeout_seconds: float,
    transport: httpx.BaseTransport | None,
    maximum_bytes: int,
    error_type: type[RuntimeError],
    operation: str,
) -> bytes:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with (
            httpx.Client(
                base_url=base_url,
                headers=headers,
                timeout=timeout_seconds,
                transport=transport,
                trust_env=False,
                follow_redirects=False,
            ) as client,
            client.stream("GET", path) as response,
        ):
            if response.status_code != 200:
                if error_type is MarketDataHttpCatalogError:
                    raise MarketDataHttpCatalogError(
                        f"Market Data {operation} returned HTTP {response.status_code}",
                        status_code=response.status_code,
                    )
                raise error_type(f"Market Data {operation} returned HTTP {response.status_code}")
            return _read_bounded(response, maximum_bytes, error_type, operation)
    except error_type:
        raise
    except httpx.HTTPError as exc:
        raise error_type(f"Market Data {operation} transport failed: {type(exc).__name__}") from exc


def _read_bounded(
    response: httpx.Response,
    maximum_bytes: int,
    error_type: type[RuntimeError],
    operation: str,
) -> bytes:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > maximum_bytes:
            if error_type is MarketDataHttpHealthError:
                raise MarketDataHttpHealthError("Market Data health payload exceeds 64 KiB")
            raise error_type(f"Market Data {operation} payload exceeds bounded response limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _project_dataset(native: _NativeDatasetDetails) -> MarketDatasetSummary:
    if native.start >= native.end:
        raise MarketDataHttpCatalogError("Market Data dataset range is invalid")
    try:
        return MarketDatasetSummary(
            dataset_id=native.dataset_id,
            version=native.version,
            dataset_hash=native.dataset_hash,
            exchange_id=native.exchange_id,
            market_type=native.market_type,
            instrument_id=native.instrument_id,
            data_types=_bounded_strings(native.data_types, "data_types"),
            timeframes=_bounded_strings(native.timeframes, "timeframes"),
            start=native.start,
            end=native.end,
            dataset_schema_version=native.dataset_schema_version,
            validation_status=native.validation_status,
            unresolved_issue_count=len(native.unresolved_issues),
            lineage_count=len(native.lineage),
            compatible_consumers=_bounded_strings(
                native.compatible_consumers,
                "compatible_consumers",
            ),
            descriptor_version=native.descriptor_version,
            registered_at=native.registered_at,
        )
    except ValidationError:
        raise MarketDataHttpCatalogError(
            "Market Data dataset projection is not safe for Control Plane"
        ) from None


def _bounded_strings(values: tuple[str, ...], name: str) -> tuple[str, ...]:
    if len(values) > 128:
        raise MarketDataHttpCatalogError(f"Market Data {name} exceeds bounded item count")
    if any(not value or value != value.strip() or len(value) > 128 for value in values):
        raise MarketDataHttpCatalogError(f"Market Data {name} contains invalid values")
    return values


def _dataset_segment(dataset_id: str) -> str:
    if not isinstance(dataset_id, str):
        raise TypeError("dataset_id must be str")
    if not dataset_id or dataset_id != dataset_id.strip() or len(dataset_id) > 128:
        raise ValueError("dataset_id must contain 1..128 characters without surrounding whitespace")
    return quote(dataset_id, safe="")


def _pagination(*, limit: int, offset: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")


def _version(value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("version must be a positive integer or None")


def _validate_token(token: str | None) -> None:
    if token is not None and (not token or len(token) > 4096):
        raise ValueError("token must contain 1..4096 characters when configured")


def _validate_timeout(timeout_seconds: float) -> None:
    if not 0.05 <= timeout_seconds <= 30.0:
        raise ValueError("timeout_seconds must be between 0.05 and 30")
