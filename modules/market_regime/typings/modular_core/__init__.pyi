from datetime import datetime
from typing import Any, Mapping, Sequence

class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    symbol: str
    timeframe: str
    @property
    def close_time(self) -> datetime: ...

class Feature:
    key: str
    source_module: str
    created_at: datetime
    value: Any
    symbol: str
    timeframe: str | None
    valid_until: datetime | None
    schema_version: str
    feature_id: str | None
    invalidate_on_gap: bool
    metadata: Mapping[str, Any]
    def __init__(
        self,
        *,
        key: str,
        source_module: str,
        created_at: datetime,
        value: Any,
        symbol: str = "*",
        timeframe: str | None = None,
        valid_until: datetime | None = None,
        schema_version: str = "1",
        feature_id: str | None = None,
        invalidate_on_gap: bool = True,
        metadata: Mapping[str, Any] = ...,
    ) -> None: ...

class MarketView:
    base_bars: Sequence[Candle]
    index: int
    data_gap: bool
    @property
    def current(self) -> Candle: ...
    def current_for(self, timeframe: str) -> Candle | None: ...

class ModuleDecision:
    features: tuple[Feature, ...]
    skips: tuple[str, ...]
    diagnostics: Mapping[str, object]
    orders: tuple[object, ...]
    signals: tuple[object, ...]
    setups: tuple[object, ...]
    def __init__(
        self,
        *,
        features: tuple[Feature, ...] = (),
        skips: tuple[str, ...] = (),
        diagnostics: Mapping[str, object] = ...,
    ) -> None: ...

class StrategyModule:
    name: str
    manifest: ModuleManifest | None
    parameters: Mapping[str, object]
    def on_start(self) -> None: ...
    def on_bar(self, view: MarketView) -> ModuleDecision: ...
    def on_finish(self) -> None: ...
    def snapshot_state(self) -> Mapping[str, object]: ...
    def restore_state(self, state: Mapping[str, object]) -> None: ...

class ModuleManifest:
    name: str
    version: str
    api_version: str
    required_timeframes: tuple[str, ...]
    required_features: tuple[str, ...]
    optional_features: tuple[str, ...]
    produced_features: tuple[str, ...]
    warmup_bars: Mapping[str, int]
    parameters_schema: Mapping[str, Mapping[str, Any]]
    skip_until_ready: bool
    description: str
    state_schema_version: str
    def __init__(
        self,
        *,
        name: str,
        version: str = "0.1.0",
        api_version: str = "1.0",
        required_timeframes: tuple[str, ...] = (),
        required_features: tuple[str, ...] = (),
        optional_features: tuple[str, ...] = (),
        produced_features: tuple[str, ...] = (),
        warmup_bars: Mapping[str, int] = ...,
        parameters_schema: Mapping[str, Mapping[str, Any]] = ...,
        skip_until_ready: bool = True,
        description: str = "",
        state_schema_version: str = "1",
    ) -> None: ...
