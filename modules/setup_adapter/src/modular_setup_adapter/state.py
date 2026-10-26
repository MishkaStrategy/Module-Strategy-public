from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .enums import SetupAdapterStateError


@dataclass(frozen=True, slots=True)
class ProcessedSource:
    fingerprint: str
    status: str


class SetupAdapterState:
    def __init__(self, capacity: int, config_fingerprint: str) -> None:
        self.capacity = capacity
        self.config_fingerprint = config_fingerprint
        self.processed: OrderedDict[str, ProcessedSource] = OrderedDict()
        self.counters: dict[str, int] = {}

    def increment(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    def remember(self, source_id: str, fingerprint: str, status: str) -> None:
        self.processed[source_id] = ProcessedSource(fingerprint=fingerprint, status=status)
        self.processed.move_to_end(source_id)
        while len(self.processed) > self.capacity:
            self.processed.popitem(last=False)

    def snapshot(self) -> Mapping[str, object]:
        return {
            "state_schema_version": "1",
            "config_fingerprint": self.config_fingerprint,
            "capacity": self.capacity,
            "processed": [
                [source_id, item.fingerprint, item.status]
                for source_id, item in self.processed.items()
            ],
            "counters": dict(sorted(self.counters.items())),
        }

    def restore(self, payload: Mapping[str, object]) -> None:
        if str(payload.get("state_schema_version", "")) != "1":
            raise SetupAdapterStateError("unsupported setup adapter state schema")
        if payload.get("config_fingerprint") != self.config_fingerprint:
            raise SetupAdapterStateError("setup adapter config fingerprint mismatch")
        capacity = payload.get("capacity")
        if capacity != self.capacity:
            raise SetupAdapterStateError("setup adapter state capacity mismatch")
        raw = payload.get("processed", ())
        if isinstance(raw, str | bytes) or not isinstance(raw, Sequence):
            raise SetupAdapterStateError("processed must be a sequence")
        if len(raw) > self.capacity:
            raise SetupAdapterStateError("processed exceeds configured capacity")
        restored: OrderedDict[str, ProcessedSource] = OrderedDict()
        for row in raw:
            if isinstance(row, str | bytes) or not isinstance(row, Sequence) or len(row) != 3:
                raise SetupAdapterStateError("invalid processed row")
            source_id, fingerprint, status = (str(row[0]), str(row[1]), str(row[2]))
            if not source_id or len(fingerprint) != 64 or not status:
                raise SetupAdapterStateError("invalid processed row values")
            if source_id in restored:
                raise SetupAdapterStateError("duplicate processed source id")
            restored[source_id] = ProcessedSource(fingerprint=fingerprint, status=status)
        counters_raw = payload.get("counters", {})
        if not isinstance(counters_raw, Mapping):
            raise SetupAdapterStateError("counters must be a mapping")
        counters: dict[str, int] = {}
        for key, value in counters_raw.items():
            if not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, int):
                raise SetupAdapterStateError("invalid counter")
            if value < 0:
                raise SetupAdapterStateError("counter cannot be negative")
            counters[key] = value
        self.processed = restored
        self.counters = counters
