from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from modular_core import Candle, Feature, FeatureStore, StateRepository, StrategyModule

from .canonical import (
    canonical_sha256,
    from_jsonable,
    require_json_mapping,
    to_jsonable,
)
from .errors import CheckpointIntegrityError
from .journal import RuntimeEventJournal
from .market_data import (
    BoundedCriticalQueue,
    MarketEventEnvelope,
    MarketEventValidator,
    _StreamCursor,
)
from .pipeline import CoreMarketPipeline
from .ports import RuntimeClock
from .routing import FactualEventEnvelope, FactualEventRouter

_CHECKPOINT_SCHEMA = "1"
type _StreamState = tuple[
    dict[tuple[str, str, str, str], _StreamCursor],
    OrderedDict[str, str],
]
type _MarketQueueState = tuple[list[MarketEventEnvelope], int]
type _FactualQueueState = tuple[list[FactualEventEnvelope], int] | None
type _PipelineModuleState = tuple[StrategyModule, Mapping[str, Any]]
type _PipelineState = tuple[
    dict[tuple[str, str], deque[Candle]],
    dict[tuple[str, str], int],
    FeatureStore,
    tuple[_PipelineModuleState, ...],
]


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CheckpointIntegrityError(f"{name} must be a non-blank string")
    if value != value.strip():
        raise CheckpointIntegrityError(f"{name} cannot contain surrounding whitespace")
    if len(value) > 256:
        raise CheckpointIntegrityError(f"{name} exceeds maximum length")
    return value


def _digest(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise CheckpointIntegrityError(f"{name} must be a lowercase SHA-256 digest")
    return text


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CheckpointIntegrityError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _decode_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, Mapping) or set(value) != {"$datetime"}:
        raise CheckpointIntegrityError(f"{name} must be a canonical datetime mapping")
    raw = value["$datetime"]
    if not isinstance(raw, str):
        raise CheckpointIntegrityError(f"{name} datetime value must be a string")
    try:
        return _utc(datetime.fromisoformat(raw), name)
    except ValueError as exc:
        raise CheckpointIntegrityError(f"{name} is not valid ISO-8601") from exc


def _sequence(value: object, name: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise CheckpointIntegrityError(f"{name} must be a sequence")
    return tuple(value)


def _required_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise CheckpointIntegrityError(f"{name} must be numeric")
    try:
        result = float(value)
    except ValueError as exc:
        raise CheckpointIntegrityError(f"{name} must be numeric") from exc
    if not result < float("inf") or not result > float("-inf"):
        raise CheckpointIntegrityError(f"{name} must be finite")
    return result


def _required_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise CheckpointIntegrityError(f"{name} must be bool")
    return value


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, name)


def _optional_datetime(value: object, name: str) -> datetime | None:
    if value is None:
        return None
    return _decode_datetime(value, name)


class RuntimeMetadataBoundary(Protocol):
    def snapshot_runtime_metadata(self) -> Mapping[str, Any]: ...

    def restore_runtime_metadata(self, state: Mapping[str, Any]) -> None: ...


class RuntimeCheckpointCoordinator:
    """Checksummed orchestration checkpoint stored through Core StateRepository."""

    def __init__(
        self,
        *,
        run_id: str,
        config_hash: str,
        module_graph_hash: str,
        repository: StateRepository,
        clock: RuntimeClock,
        journal: RuntimeEventJournal,
        market_validator: MarketEventValidator,
        market_queue: BoundedCriticalQueue[MarketEventEnvelope],
        pipeline: CoreMarketPipeline,
        factual_router: FactualEventRouter | None = None,
        runtime_metadata: RuntimeMetadataBoundary | None = None,
    ) -> None:
        self.run_id = _text(run_id, "run_id")
        self.config_hash = _digest(config_hash, "config_hash")
        self.module_graph_hash = _digest(module_graph_hash, "module_graph_hash")
        self.repository = repository
        self.clock = clock
        self.journal = journal
        self.market_validator = market_validator
        self.market_queue = market_queue
        self.pipeline = pipeline
        self.factual_router = factual_router
        self.runtime_metadata = runtime_metadata

    @property
    def state_key(self) -> str:
        return f"trading-runner-checkpoint:{self.run_id}"

    def capture(self) -> Mapping[str, Any]:
        captured_at = _utc(self.clock.now(), "clock.now")
        payload: dict[str, Any] = {
            "schema_version": _CHECKPOINT_SCHEMA,
            "run_id": self.run_id,
            "config_hash": self.config_hash,
            "module_graph_hash": self.module_graph_hash,
            "captured_at": to_jsonable(captured_at),
            "journal": dict(self.journal.attestation()),
            "market_validator": self._encode_market_validator(),
            "market_queue": self._encode_market_queue(),
            "pipeline": self._encode_pipeline(),
            "factual_queue": self._encode_factual_queue(),
            "runtime_metadata": self._encode_runtime_metadata(),
        }
        payload["integrity_hash"] = canonical_sha256(payload)
        normalized = require_json_mapping(payload, "runtime checkpoint")
        self.repository.save(self.state_key, normalized)
        return normalized

    def restore(self) -> Mapping[str, Any] | None:
        snapshot = self.repository.load(self.state_key)
        if snapshot is None:
            return None
        payload = dict(snapshot)
        stored_integrity = _digest(payload.pop("integrity_hash", None), "integrity_hash")
        if canonical_sha256(payload) != stored_integrity:
            raise CheckpointIntegrityError("runtime checkpoint integrity hash mismatch")
        if (
            payload.get("schema_version") != _CHECKPOINT_SCHEMA
            or payload.get("run_id") != self.run_id
            or payload.get("config_hash") != self.config_hash
            or payload.get("module_graph_hash") != self.module_graph_hash
        ):
            raise CheckpointIntegrityError("runtime checkpoint identity is incompatible")
        _decode_datetime(payload.get("captured_at"), "captured_at")
        journal_state = payload.get("journal")
        if not isinstance(journal_state, Mapping):
            raise CheckpointIntegrityError("checkpoint journal attestation is invalid")
        self.journal.verify_attestation(journal_state)

        validator_state = self._decode_validator_state(payload.get("market_validator"))
        market_queue_state = self._decode_market_queue(payload.get("market_queue"))
        pipeline_state = self._decode_pipeline(payload.get("pipeline"))
        factual_queue_state = self._decode_factual_queue(payload.get("factual_queue"))
        runtime_metadata = self._decode_runtime_metadata(payload.get("runtime_metadata"))

        original_validator = self._snapshot_validator_state()
        original_market_queue = self._snapshot_market_queue_state()
        original_pipeline = self._snapshot_pipeline_state()
        original_factual_queue = self._snapshot_factual_queue_state()
        original_metadata = self._encode_runtime_metadata()
        try:
            self._apply_validator_state(validator_state)
            self._apply_market_queue(market_queue_state)
            self._apply_pipeline(pipeline_state)
            self._apply_factual_queue(factual_queue_state)
            if self.runtime_metadata is not None:
                self.runtime_metadata.restore_runtime_metadata(runtime_metadata)
        except Exception as exc:
            rollback_errors: list[Exception] = []
            restore_actions: tuple[Callable[[], None], ...] = (
                lambda: self._apply_validator_state(original_validator),
                lambda: self._apply_market_queue(original_market_queue),
                lambda: self._apply_pipeline(original_pipeline),
                lambda: self._apply_factual_queue(original_factual_queue),
                lambda: self._restore_metadata(original_metadata),
            )
            for restore_action in restore_actions:
                try:
                    restore_action()
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
            if rollback_errors:
                details = "; ".join(f"{type(error).__name__}: {error}" for error in rollback_errors)
                raise CheckpointIntegrityError(
                    f"runtime checkpoint restore failed and rollback was incomplete: {details}"
                ) from exc
            if isinstance(exc, CheckpointIntegrityError):
                raise
            raise CheckpointIntegrityError("runtime checkpoint restore failed") from exc
        return snapshot

    def _restore_metadata(self, state: Mapping[str, Any]) -> None:
        if self.runtime_metadata is not None:
            self.runtime_metadata.restore_runtime_metadata(state)

    def _snapshot_validator_state(self) -> _StreamState:
        return (
            dict(self.market_validator._streams),
            OrderedDict(self.market_validator._event_hashes),
        )

    def _encode_market_validator(self) -> Mapping[str, Any]:
        streams = []
        for key, cursor in sorted(self.market_validator._streams.items()):
            streams.append(
                {
                    "stream_key": list(key),
                    "sequence": cursor.sequence,
                    "event_id": cursor.event_id,
                    "payload_hash": cursor.payload_hash,
                    "candle_open_time": to_jsonable(cursor.candle_open_time),
                    "candle_close_time": to_jsonable(cursor.candle_close_time),
                }
            )
        return {
            "streams": streams,
            "event_hashes": [list(item) for item in self.market_validator._event_hashes.items()],
        }

    def _decode_validator_state(self, value: object) -> _StreamState:
        if not isinstance(value, Mapping):
            raise CheckpointIntegrityError("market_validator checkpoint must be a mapping")
        streams_raw = _sequence(value.get("streams"), "market validator streams")
        hashes_raw = _sequence(value.get("event_hashes"), "market validator event hashes")
        streams: dict[tuple[str, str, str, str], _StreamCursor] = {}
        for row_raw in streams_raw:
            if not isinstance(row_raw, Mapping):
                raise CheckpointIntegrityError("market stream row must be a mapping")
            key_raw = _sequence(row_raw.get("stream_key"), "market stream key")
            if len(key_raw) != 4:
                raise CheckpointIntegrityError("market stream key must have four fields")
            key = (
                _text(key_raw[0], "stream source"),
                _text(key_raw[1], "stream market"),
                _text(key_raw[2], "stream instrument"),
                _text(key_raw[3], "stream timeframe"),
            )
            if key in streams:
                raise CheckpointIntegrityError("duplicate market stream key")
            if (
                key[0] not in self.market_validator.policy.sources
                or key[1] != self.market_validator.policy.market
                or key[2] not in self.market_validator.policy.instruments
                or key[3] not in self.market_validator.policy.timeframes
            ):
                raise CheckpointIntegrityError("market stream identity is not allowed")
            sequence = row_raw.get("sequence")
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
                raise CheckpointIntegrityError("market cursor sequence is invalid")
            cursor = _StreamCursor(
                sequence=sequence,
                event_id=_text(row_raw.get("event_id"), "event_id"),
                payload_hash=_digest(row_raw.get("payload_hash"), "payload_hash"),
                candle_open_time=_decode_datetime(
                    row_raw.get("candle_open_time"),
                    "candle_open_time",
                ),
                candle_close_time=_decode_datetime(
                    row_raw.get("candle_close_time"),
                    "candle_close_time",
                ),
            )
            if cursor.candle_close_time <= cursor.candle_open_time:
                raise CheckpointIntegrityError("market cursor candle interval is invalid")
            streams[key] = cursor
        hashes: OrderedDict[str, str] = OrderedDict()
        for item_raw in hashes_raw:
            item = _sequence(item_raw, "market event hash row")
            if len(item) != 2:
                raise CheckpointIntegrityError("market event hash row is invalid")
            event_id = _text(item[0], "event_id")
            if event_id in hashes:
                raise CheckpointIntegrityError("duplicate market event identity")
            hashes[event_id] = _digest(item[1], "payload_hash")
        if len(hashes) > self.market_validator.policy.max_tracked_events:
            raise CheckpointIntegrityError("market event hash checkpoint exceeds configured limit")
        return streams, hashes

    def _apply_validator_state(self, state: _StreamState) -> None:
        streams, hashes = state
        self.market_validator._streams = dict(streams)
        self.market_validator._event_hashes = OrderedDict(hashes)

    def _snapshot_market_queue_state(self) -> _MarketQueueState:
        return list(self.market_queue.snapshot()), self.market_queue.high_water_mark

    def _encode_market_queue(self) -> Mapping[str, Any]:
        return {
            "capacity": self.market_queue.capacity,
            "high_water_mark": self.market_queue.high_water_mark,
            "items": [self._encode_market_event(item) for item in self.market_queue.snapshot()],
        }

    def _decode_market_queue(self, value: object) -> _MarketQueueState:
        if not isinstance(value, Mapping):
            raise CheckpointIntegrityError("market queue checkpoint must be a mapping")
        if value.get("capacity") != self.market_queue.capacity:
            raise CheckpointIntegrityError("market queue capacity changed since checkpoint")
        items_raw = _sequence(value.get("items"), "market queue items")
        high_water = value.get("high_water_mark")
        if isinstance(high_water, bool) or not isinstance(high_water, int) or high_water < 0:
            raise CheckpointIntegrityError("market queue high-water mark is invalid")
        items = [self._decode_market_event(item) for item in items_raw]
        if len(items) > self.market_queue.capacity or high_water < len(items):
            raise CheckpointIntegrityError("market queue checkpoint exceeds capacity")
        return items, high_water

    def _apply_market_queue(self, state: _MarketQueueState) -> None:
        items, high_water = state
        self.market_queue._items = deque(items)
        self.market_queue._high_water_mark = high_water

    def _snapshot_pipeline_state(self) -> _PipelineState:
        histories = {
            key: deque(candles, maxlen=self.pipeline._max_history_bars)
            for key, candles in self.pipeline._histories.items()
        }
        indices = dict(self.pipeline._indices)
        features = self.pipeline._features.copy()
        modules: list[_PipelineModuleState] = []
        for module in self.pipeline._modules:
            encoded = require_json_mapping(module.snapshot_state(), "module state")
            decoded = from_jsonable(encoded)
            if not isinstance(decoded, Mapping):
                raise CheckpointIntegrityError("module state must restore to a mapping")
            modules.append((module, decoded))
        return histories, indices, features, tuple(modules)

    def _encode_pipeline(self) -> Mapping[str, Any]:
        histories = []
        for (symbol, timeframe), candles in sorted(self.pipeline._histories.items()):
            histories.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "candles": [self._encode_candle(candle) for candle in candles],
                    "index": self.pipeline._indices[(symbol, timeframe)],
                }
            )
        modules = []
        for module in self.pipeline._modules:
            manifest = self.pipeline._manifests[module.name]
            modules.append(
                {
                    "name": module.name,
                    "version": manifest.version,
                    "state_schema_version": manifest.state_schema_version,
                    "state": to_jsonable(module.snapshot_state()),
                }
            )
        return {
            "max_history_bars": self.pipeline._max_history_bars,
            "max_features": self.pipeline._features.max_features,
            "histories": histories,
            "features": [
                self._encode_feature(feature) for feature in self.pipeline._features.all()
            ],
            "modules": modules,
        }

    def _decode_pipeline(self, value: object) -> _PipelineState:
        if not isinstance(value, Mapping):
            raise CheckpointIntegrityError("pipeline checkpoint must be a mapping")
        if value.get("max_history_bars") != self.pipeline._max_history_bars:
            raise CheckpointIntegrityError("pipeline history limit changed since checkpoint")
        max_features_raw = value.get("max_features")
        if max_features_raw is not None and (
            isinstance(max_features_raw, bool) or not isinstance(max_features_raw, int)
        ):
            raise CheckpointIntegrityError("pipeline max_features is invalid")
        max_features: int | None = max_features_raw
        if max_features != self.pipeline._features.max_features:
            raise CheckpointIntegrityError("pipeline feature limit changed since checkpoint")
        histories_raw = _sequence(value.get("histories"), "pipeline histories")
        features_raw = _sequence(value.get("features"), "pipeline features")
        modules_raw = _sequence(value.get("modules"), "pipeline modules")
        histories: dict[tuple[str, str], deque[Candle]] = {}
        indices: dict[tuple[str, str], int] = {}
        for row_raw in histories_raw:
            if not isinstance(row_raw, Mapping):
                raise CheckpointIntegrityError("pipeline history row must be a mapping")
            key = (
                _text(row_raw.get("symbol"), "history symbol"),
                _text(row_raw.get("timeframe"), "history timeframe"),
            )
            if key in histories:
                raise CheckpointIntegrityError("duplicate pipeline history stream")
            candles_raw = _sequence(row_raw.get("candles"), "pipeline candles")
            index = row_raw.get("index")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise CheckpointIntegrityError("pipeline index is invalid")
            candles = [self._decode_candle(item) for item in candles_raw]
            if not candles or len(candles) > self.pipeline._max_history_bars:
                raise CheckpointIntegrityError("pipeline history length is invalid")
            if index < len(candles) - 1:
                raise CheckpointIntegrityError("pipeline index precedes retained history")
            previous_close: datetime | None = None
            for candle in candles:
                if (candle.symbol, candle.timeframe) != key:
                    raise CheckpointIntegrityError("pipeline candle stream identity mismatch")
                if previous_close is not None and candle.timestamp < previous_close:
                    raise CheckpointIntegrityError("pipeline candle history overlaps")
                previous_close = candle.close_time
            histories[key] = deque(candles, maxlen=self.pipeline._max_history_bars)
            indices[key] = index
        features = FeatureStore(max_features=max_features)
        for row_raw in features_raw:
            features.publish(self._decode_feature(row_raw))
        expected_modules = {module.name: module for module in self.pipeline._modules}
        decoded_modules: list[_PipelineModuleState] = []
        seen: set[str] = set()
        for row_raw in modules_raw:
            if not isinstance(row_raw, Mapping):
                raise CheckpointIntegrityError("module checkpoint row must be a mapping")
            name = _text(row_raw.get("name"), "module name")
            module = expected_modules.get(name)
            if module is None or name in seen:
                raise CheckpointIntegrityError("checkpoint module set is incompatible")
            manifest = self.pipeline._manifests[name]
            if (
                row_raw.get("version") != manifest.version
                or row_raw.get("state_schema_version") != manifest.state_schema_version
            ):
                raise CheckpointIntegrityError("module checkpoint version is incompatible")
            state = from_jsonable(row_raw.get("state"))
            if not isinstance(state, Mapping):
                raise CheckpointIntegrityError("module state must be a mapping")
            decoded_modules.append((module, state))
            seen.add(name)
        if seen != set(expected_modules):
            raise CheckpointIntegrityError("checkpoint is missing configured modules")
        return histories, indices, features, tuple(decoded_modules)

    def _apply_pipeline(self, state: _PipelineState) -> None:
        histories, indices, features, modules = state
        original_states: list[_PipelineModuleState] = []
        for module, _ in modules:
            encoded = require_json_mapping(module.snapshot_state(), "module state")
            decoded = from_jsonable(encoded)
            if not isinstance(decoded, Mapping):
                raise CheckpointIntegrityError("module state must restore to a mapping")
            original_states.append((module, decoded))
        attempted: list[StrategyModule] = []
        try:
            for module, module_state in modules:
                attempted.append(module)
                module.restore_state(module_state)
        except Exception as exc:
            rollback_errors: list[Exception] = []
            originals = {module.name: snapshot for module, snapshot in original_states}
            for module in reversed(attempted):
                try:
                    module.restore_state(originals[module.name])
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
            if rollback_errors:
                raise CheckpointIntegrityError(
                    "module checkpoint restore failed and rollback was incomplete"
                ) from exc
            raise CheckpointIntegrityError("module checkpoint restore failed") from exc
        self.pipeline._histories = {
            key: deque(candles, maxlen=self.pipeline._max_history_bars)
            for key, candles in histories.items()
        }
        self.pipeline._indices = dict(indices)
        self.pipeline._features = features.copy()

    def _snapshot_factual_queue_state(self) -> _FactualQueueState:
        if self.factual_router is None:
            return None
        return (
            list(self.factual_router.queue.snapshot()),
            self.factual_router.queue.high_water_mark,
        )

    def _encode_factual_queue(self) -> Mapping[str, Any] | None:
        if self.factual_router is None:
            return None
        return {
            "capacity": self.factual_router.queue.capacity,
            "high_water_mark": self.factual_router.queue.high_water_mark,
            "items": [item.to_mapping() for item in self.factual_router.queue.snapshot()],
        }

    def _decode_factual_queue(self, value: object) -> _FactualQueueState:
        if self.factual_router is None:
            if value is not None:
                raise CheckpointIntegrityError("checkpoint unexpectedly contains factual queue")
            return None
        if not isinstance(value, Mapping):
            raise CheckpointIntegrityError("factual queue checkpoint must be a mapping")
        if value.get("capacity") != self.factual_router.queue.capacity:
            raise CheckpointIntegrityError("factual queue capacity changed since checkpoint")
        items_raw = _sequence(value.get("items"), "factual queue items")
        high_water = value.get("high_water_mark")
        if isinstance(high_water, bool) or not isinstance(high_water, int) or high_water < 0:
            raise CheckpointIntegrityError("factual queue high-water mark is invalid")
        items: list[FactualEventEnvelope] = []
        for item in items_raw:
            if not isinstance(item, Mapping):
                raise CheckpointIntegrityError("factual queue item must be a mapping")
            items.append(FactualEventEnvelope.from_mapping(item))
        if len(items) > self.factual_router.queue.capacity or high_water < len(items):
            raise CheckpointIntegrityError("factual queue checkpoint exceeds capacity")
        return items, high_water

    def _apply_factual_queue(self, state: _FactualQueueState) -> None:
        if self.factual_router is None or state is None:
            return
        items, high_water = state
        self.factual_router.queue._items = deque(items)
        self.factual_router.queue._high_water_mark = high_water

    def _encode_runtime_metadata(self) -> Mapping[str, Any]:
        if self.runtime_metadata is None:
            return {}
        return require_json_mapping(
            self.runtime_metadata.snapshot_runtime_metadata(),
            "runtime metadata",
        )

    @staticmethod
    def _decode_runtime_metadata(value: object) -> Mapping[str, Any]:
        decoded = from_jsonable(value)
        if not isinstance(decoded, Mapping):
            raise CheckpointIntegrityError("runtime metadata must be a mapping")
        return decoded

    @staticmethod
    def _encode_candle(candle: Candle) -> Mapping[str, Any]:
        return require_json_mapping(candle, "candle")

    @staticmethod
    def _decode_candle(value: object) -> Candle:
        if not isinstance(value, Mapping):
            raise CheckpointIntegrityError("candle checkpoint must be a mapping")
        return Candle(
            timestamp=_decode_datetime(value.get("timestamp"), "candle.timestamp"),
            open=_required_float(value.get("open"), "candle.open"),
            high=_required_float(value.get("high"), "candle.high"),
            low=_required_float(value.get("low"), "candle.low"),
            close=_required_float(value.get("close"), "candle.close"),
            volume=_required_float(value.get("volume"), "candle.volume"),
            symbol=_text(value.get("symbol"), "candle.symbol"),
            timeframe=_text(value.get("timeframe"), "candle.timeframe"),
        )

    @classmethod
    def _encode_market_event(cls, event: MarketEventEnvelope) -> Mapping[str, Any]:
        return {
            "event_id": event.event_id,
            "source_id": event.source_id,
            "market": event.market,
            "sequence": event.sequence,
            "available_at": to_jsonable(event.available_at),
            "received_at": to_jsonable(event.received_at),
            "candle": cls._encode_candle(event.candle),
            "is_closed": event.is_closed,
            "metadata_version": event.metadata_version,
            "payload_hash": event.payload_hash,
        }

    @classmethod
    def _decode_market_event(cls, value: object) -> MarketEventEnvelope:
        if not isinstance(value, Mapping):
            raise CheckpointIntegrityError("market event checkpoint must be a mapping")
        sequence = value.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise CheckpointIntegrityError("market event sequence is invalid")
        event = MarketEventEnvelope(
            event_id=_text(value.get("event_id"), "event_id"),
            source_id=_text(value.get("source_id"), "source_id"),
            market=_text(value.get("market"), "market"),
            sequence=sequence,
            available_at=_decode_datetime(value.get("available_at"), "available_at"),
            received_at=_decode_datetime(value.get("received_at"), "received_at"),
            candle=cls._decode_candle(value.get("candle")),
            is_closed=_required_bool(value.get("is_closed"), "is_closed"),
            metadata_version=_text(value.get("metadata_version"), "metadata_version"),
        )
        if _digest(value.get("payload_hash"), "payload_hash") != event.payload_hash:
            raise CheckpointIntegrityError("market event payload hash mismatch")
        return event

    @staticmethod
    def _encode_feature(feature: Feature) -> Mapping[str, Any]:
        return require_json_mapping(feature, "feature")

    @staticmethod
    def _decode_feature(value: object) -> Feature:
        if not isinstance(value, Mapping):
            raise CheckpointIntegrityError("feature checkpoint must be a mapping")
        decoded_value = from_jsonable(value.get("value"))
        decoded_metadata = from_jsonable(value.get("metadata", {}))
        if not isinstance(decoded_metadata, Mapping):
            raise CheckpointIntegrityError("feature metadata must be a mapping")
        return Feature(
            key=_text(value.get("key"), "feature.key"),
            source_module=_text(value.get("source_module"), "feature.source_module"),
            created_at=_decode_datetime(value.get("created_at"), "feature.created_at"),
            value=decoded_value,
            symbol=_text(value.get("symbol"), "feature.symbol"),
            timeframe=_optional_text(value.get("timeframe"), "feature.timeframe"),
            valid_until=_optional_datetime(
                value.get("valid_until"),
                "feature.valid_until",
            ),
            schema_version=_text(value.get("schema_version"), "feature.schema_version"),
            feature_id=_optional_text(value.get("feature_id"), "feature.feature_id"),
            invalidate_on_gap=_required_bool(
                value.get("invalidate_on_gap"),
                "feature.invalidate_on_gap",
            ),
            metadata=decoded_metadata,
        )
