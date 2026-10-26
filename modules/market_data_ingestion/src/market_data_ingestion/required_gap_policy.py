from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .canonical import sha256_canonical, utc_datetime
from .enums import DataType
from .timeframes import timeframe_delta

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_POLICY_ADAPTER_MARKER = "+required-gap-policy."


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object with string keys")
    return value


def _sequence(value: object, name: str) -> tuple[object, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise ValueError(f"{name} must be an array")
    return tuple(value)


def _sha(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if _SHA256.fullmatch(text) is None:
        raise ValueError(f"{name} must be lowercase SHA-256")
    return text


def _time(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be valid ISO-8601") from exc
    return utc_datetime(parsed, name)


@dataclass(frozen=True, slots=True)
class GapArchiveEvidence:
    source_uri: str
    checksum_sha256: str

    def __post_init__(self) -> None:
        source_uri = _text(self.source_uri, "source_uri")
        if not source_uri.startswith("https://data.binance.vision/"):
            raise ValueError("gap archive evidence must use official data.binance.vision HTTPS")
        if "?" in source_uri or "#" in source_uri or not source_uri.endswith(".zip"):
            raise ValueError("gap archive evidence URI must be a query-free ZIP URL")
        object.__setattr__(self, "source_uri", source_uri)
        object.__setattr__(
            self,
            "checksum_sha256",
            _sha(self.checksum_sha256, "checksum_sha256"),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "source_uri": self.source_uri,
            "checksum_sha256": self.checksum_sha256,
        }


@dataclass(frozen=True, slots=True)
class RequiredGapException:
    data_type: DataType
    timeframe: str
    start: datetime
    end: datetime
    missing_intervals: int
    evidence: tuple[GapArchiveEvidence, ...]
    require_zero_funding_events: bool = True

    def __post_init__(self) -> None:
        if self.data_type is not DataType.MARK_PRICE:
            raise ValueError("required-gap policy v1 only permits MARK_PRICE exceptions")
        timeframe = _text(self.timeframe, "timeframe").lower()
        step = timeframe_delta(timeframe)
        object.__setattr__(self, "timeframe", timeframe)
        start = utc_datetime(self.start, "start")
        end = utc_datetime(self.end, "end")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        if start >= end:
            raise ValueError("gap exception start must precede end")
        duration = end - start
        if duration % step != timedelta(0):
            raise ValueError("gap exception boundaries must align to timeframe")
        expected_intervals = int(duration / step)
        if (
            not isinstance(self.missing_intervals, int)
            or isinstance(self.missing_intervals, bool)
            or self.missing_intervals <= 0
        ):
            raise ValueError("missing_intervals must be a positive integer")
        if self.missing_intervals != expected_intervals:
            raise ValueError("missing_intervals must exactly cover the enumerated gap")
        if not isinstance(self.require_zero_funding_events, bool):
            raise TypeError("require_zero_funding_events must be bool")
        if not self.require_zero_funding_events:
            raise ValueError("required-gap policy v1 requires zero funding events")
        if len(self.evidence) != 2 or any(
            not isinstance(item, GapArchiveEvidence) for item in self.evidence
        ):
            raise ValueError(
                "each gap exception requires exactly monthly and daily archive evidence"
            )
        uris = tuple(item.source_uri for item in self.evidence)
        if len(set(uris)) != len(uris):
            raise ValueError("gap archive evidence URIs must be unique")
        kinds = {
            "monthly" if "/monthly/" in uri else "daily" if "/daily/" in uri else "unknown"
            for uri in uris
        }
        if kinds != {"monthly", "daily"}:
            raise ValueError("gap evidence must contain one monthly and one daily archive")

    @property
    def timestamps(self) -> tuple[datetime, ...]:
        step = timeframe_delta(self.timeframe)
        cursor = self.start
        values: list[datetime] = []
        while cursor < self.end:
            values.append(cursor)
            cursor += step
        return tuple(values)

    def to_payload(self) -> dict[str, object]:
        return {
            "data_type": self.data_type.value,
            "timeframe": self.timeframe,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "missing_intervals": self.missing_intervals,
            "require_zero_funding_events": self.require_zero_funding_events,
            "evidence": [item.to_payload() for item in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class RequiredGapPolicy:
    exceptions: tuple[RequiredGapException, ...]
    policy_version: int = 1

    def __post_init__(self) -> None:
        if self.policy_version != 1:
            raise ValueError("unsupported required-gap policy version")
        if not self.exceptions:
            raise ValueError("required-gap policy must contain at least one exception")
        if any(not isinstance(item, RequiredGapException) for item in self.exceptions):
            raise TypeError("exceptions must contain RequiredGapException")
        ordered = tuple(sorted(self.exceptions, key=lambda item: (item.start, item.end)))
        if ordered != self.exceptions:
            raise ValueError("gap exceptions must be ordered by start time")
        for previous, current in zip(self.exceptions, self.exceptions[1:], strict=False):
            if previous.end > current.start:
                raise ValueError("gap exceptions must not overlap")

    @property
    def canonical_hash(self) -> str:
        return sha256_canonical(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            "policy_version": self.policy_version,
            "exceptions": [item.to_payload() for item in self.exceptions],
        }

    @classmethod
    def from_payload(cls, value: object) -> RequiredGapPolicy:
        root = _mapping(value, "required_gap_policy")
        unknown = sorted(set(root) - {"policy_version", "exceptions"})
        if unknown:
            raise ValueError(f"unknown required_gap_policy fields: {unknown}")
        raw_exceptions = _sequence(root.get("exceptions"), "required_gap_policy.exceptions")
        exceptions: list[RequiredGapException] = []
        for index, raw in enumerate(raw_exceptions):
            item = _mapping(raw, f"required_gap_policy.exceptions[{index}]")
            unknown_item = sorted(
                set(item)
                - {
                    "data_type",
                    "timeframe",
                    "start",
                    "end",
                    "missing_intervals",
                    "require_zero_funding_events",
                    "evidence",
                }
            )
            if unknown_item:
                raise ValueError(f"unknown gap exception fields: {unknown_item}")
            raw_evidence = _sequence(
                item.get("evidence"),
                f"required_gap_policy.exceptions[{index}].evidence",
            )
            evidence: list[GapArchiveEvidence] = []
            for evidence_index, raw_source in enumerate(raw_evidence):
                source = _mapping(raw_source, "gap archive evidence")
                unknown_source = sorted(set(source) - {"source_uri", "checksum_sha256"})
                if unknown_source:
                    raise ValueError(f"unknown gap archive evidence fields: {unknown_source}")
                evidence.append(
                    GapArchiveEvidence(
                        source_uri=_text(source.get("source_uri"), "source_uri"),
                        checksum_sha256=_sha(
                            source.get("checksum_sha256"),
                            f"evidence[{evidence_index}].checksum_sha256",
                        ),
                    )
                )
            try:
                data_type = DataType(_text(item.get("data_type"), "data_type"))
            except ValueError as exc:
                raise ValueError("gap exception data_type is unsupported") from exc
            require_zero = item.get("require_zero_funding_events", True)
            if not isinstance(require_zero, bool):
                raise ValueError("require_zero_funding_events must be a YAML boolean")
            missing = item.get("missing_intervals")
            if not isinstance(missing, int) or isinstance(missing, bool):
                raise ValueError("missing_intervals must be an integer")
            exceptions.append(
                RequiredGapException(
                    data_type=data_type,
                    timeframe=_text(item.get("timeframe"), "timeframe"),
                    start=_time(item.get("start"), "start"),
                    end=_time(item.get("end"), "end"),
                    missing_intervals=missing,
                    evidence=tuple(evidence),
                    require_zero_funding_events=require_zero,
                )
            )
        version = root.get("policy_version", 1)
        if not isinstance(version, int) or isinstance(version, bool):
            raise ValueError("policy_version must be an integer")
        return cls(exceptions=tuple(exceptions), policy_version=version)


def policy_adapter_version(base_version: str, policy: RequiredGapPolicy) -> str:
    return f"{base_version}{_POLICY_ADAPTER_MARKER}{policy.canonical_hash}"


def policy_hash_from_adapter_version(adapter_version: str) -> str | None:
    if _POLICY_ADAPTER_MARKER not in adapter_version:
        return None
    digest = adapter_version.rsplit(_POLICY_ADAPTER_MARKER, 1)[1]
    if _SHA256.fullmatch(digest) is None:
        raise ValueError("required-gap adapter version contains an invalid policy hash")
    return digest


def _policy_path(storage_root: Path, digest: str) -> Path:
    if _SHA256.fullmatch(digest) is None:
        raise ValueError("required-gap policy digest must be SHA-256")
    return storage_root / "policies" / "required-gap" / f"{digest}.json"


def persist_required_gap_policy(storage_root: Path, policy: RequiredGapPolicy) -> Path:
    root = storage_root.expanduser().resolve()
    digest = policy.canonical_hash
    path = _policy_path(root, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            policy.to_payload(),
            sort_keys=True,
            indent=2,
            separators=(",", ": "),
            ensure_ascii=True,
        ).encode("utf-8")
        + b"\n"
    )
    try:
        with path.open("xb") as handle:
            handle.write(payload)
    except FileExistsError:
        if path.read_bytes() != payload:
            raise ValueError("required-gap policy sidecar is not immutable") from None
    return path


def load_required_gap_policy(storage_root: Path, digest: str) -> RequiredGapPolicy:
    path = _policy_path(storage_root.expanduser().resolve(), digest)
    if not path.is_file() or path.is_symlink():
        raise ValueError("required-gap policy sidecar is missing or unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("required-gap policy sidecar is corrupt") from exc
    policy = RequiredGapPolicy.from_payload(payload)
    if policy.canonical_hash != digest:
        raise ValueError("required-gap policy sidecar digest mismatch")
    return policy
