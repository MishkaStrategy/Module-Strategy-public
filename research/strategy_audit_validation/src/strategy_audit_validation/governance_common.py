"""Strategy composition identity and fail-closed contract validation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from .canonical import canonical_json_bytes, canonical_sha256, strict_json_loads
from .errors import EvidenceMalformedError, EvidenceTamperedError

COMPOSITION_SCHEMA_VERSION: Final = "strategy-composition-v1"
CANONICAL_COMPOSITION_NAME: Final = "FVG-RT Final 15m + Frozen 1H Short + Frozen 4H Short"
CANONICAL_REQUIRED_MODULES: Final = frozenset(
    {"fvg_rt_final_15m", "frozen_1h_short", "frozen_4h_short"}
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,159}$")
_TIMEFRAME = re.compile(r"^[1-9][0-9]*(?:m|h|d|w)$")


class CompositionState(StrEnum):
    INCOMPLETE = "INCOMPLETE"
    CANDIDATE = "CANDIDATE"


@dataclass(frozen=True, slots=True)
class ModuleContract:
    module_id: str
    module_version: str
    required_parameters: frozenset[str]
    allowed_timeframes: frozenset[str]
    contract_complete: bool
    source_path: str
    source_commit_sha: str

    def __post_init__(self) -> None:
        _identifier(self.module_id, "module contract id")
        _identifier(self.module_version, "module contract version")
        if not self.source_path or self.source_path != self.source_path.strip():
            raise EvidenceMalformedError("module contract source_path must be non-blank")
        _git_sha(self.source_commit_sha, "module contract source_commit_sha")
        if any(not isinstance(item, str) or not item for item in self.required_parameters):
            raise EvidenceMalformedError("required parameter names must be non-empty strings")
        for timeframe in self.allowed_timeframes:
            _timeframe(timeframe, "module contract timeframe")
        if not isinstance(self.contract_complete, bool):
            raise EvidenceMalformedError("module contract completeness must be bool")


@dataclass(frozen=True, slots=True)
class CompositionCandidate:
    payload: Mapping[str, Any]
    composition_hash: str
    candidate_id: str
    created_by: str
    state: CompositionState
    complete: bool
    missing_contracts: tuple[str, ...]

    def __post_init__(self) -> None:
        _sha(self.composition_hash, "composition_hash")
        _identifier(self.candidate_id, "candidate_id")
        _identifier(self.created_by, "created_by")
        object.__setattr__(self, "payload", _freeze(self.payload))
        object.__setattr__(self, "missing_contracts", tuple(sorted(set(self.missing_contracts))))
        expected_id = f"strategy-candidate-{self.composition_hash[:24]}"
        if self.candidate_id != expected_id:
            raise EvidenceTamperedError("candidate identity does not match composition hash")
        if self.state is CompositionState.CANDIDATE and not self.complete:
            raise EvidenceMalformedError("incomplete composition cannot declare CANDIDATE state")

    @property
    def composition_name(self) -> str:
        return str(self.payload["composition_name"])

    @property
    def composition_version(self) -> str:
        return str(self.payload["composition_version"])

    def as_mapping(self) -> Mapping[str, Any]:
        return self.payload


def load_composition(
    payload: Mapping[str, Any],
    *,
    contracts: Mapping[str, ModuleContract],
    require_canonical: bool = False,
) -> CompositionCandidate:
    required = {
        "schema_version",
        "composition_name",
        "composition_version",
        "state",
        "timezone",
        "lookahead_allowed",
        "unfinished_htf_allowed",
        "created_by",
        "source_provenance",
        "modules",
        "execution_order",
        "dependency_order",
    }
    _exact_keys(payload, required, "strategy composition")
    if payload["schema_version"] != COMPOSITION_SCHEMA_VERSION:
        raise EvidenceMalformedError("unsupported strategy composition schema_version")
    name = payload["composition_name"]
    if not isinstance(name, str) or not name.strip() or name != name.strip():
        raise EvidenceMalformedError("composition_name must be a non-blank trimmed string")
    _identifier(payload["composition_version"], "composition_version")
    state = CompositionState(payload["state"])
    if payload["timezone"] != "UTC":
        raise EvidenceMalformedError("strategy composition timezone must be UTC")
    if payload["lookahead_allowed"] is not False:
        raise EvidenceMalformedError("strategy composition must forbid look-ahead")
    if payload["unfinished_htf_allowed"] is not False:
        raise EvidenceMalformedError("strategy composition must forbid unfinished HTF candles")
    created_by = _identifier(payload["created_by"], "composition created_by")
    provenance = _mapping(payload["source_provenance"], "source_provenance")
    _exact_keys(
        provenance,
        {"repository", "commit_sha", "paths"},
        "source_provenance",
    )
    if provenance["repository"] != "MishkaStrategy/Module-Strategy":
        raise EvidenceMalformedError("composition source repository mismatch")
    _git_sha(provenance["commit_sha"], "source provenance commit_sha")
    paths = _sequence(provenance["paths"], "source provenance paths")
    if not paths or any(not isinstance(item, str) or not item for item in paths):
        raise EvidenceMalformedError("source provenance paths must be non-empty strings")

    modules_raw = _sequence(payload["modules"], "composition modules")
    if not modules_raw:
        raise EvidenceMalformedError("composition requires at least one module")
    module_ids: list[str] = []
    dependency_map: dict[str, tuple[str, ...]] = {}
    missing_contracts: list[str] = []
    for raw in modules_raw:
        module = _mapping(raw, "composition module")
        _exact_keys(
            module,
            {
                "module_id",
                "module_version",
                "role",
                "parameters",
                "timeframes",
                "dependencies",
                "contract_complete",
                "source_provenance",
            },
            "composition module",
        )
        module_id = _identifier(module["module_id"], "module_id")
        if module_id in module_ids:
            raise EvidenceMalformedError("composition module IDs must be unique")
        module_ids.append(module_id)
        contract = contracts.get(module_id)
        if contract is None:
            raise EvidenceMalformedError(f"unknown strategy module: {module_id}")
        module_version = _identifier(module["module_version"], "module_version")
        if module_version != contract.module_version:
            raise EvidenceMalformedError(
                f"incompatible module version for {module_id}: {module_version}"
            )
        parameters = _mapping(module["parameters"], f"{module_id}.parameters")
        missing_parameters = sorted(contract.required_parameters - set(parameters))
        if missing_parameters:
            raise EvidenceMalformedError(
                f"missing required parameters for {module_id}: {missing_parameters}"
            )
        timeframes = _sequence(module["timeframes"], f"{module_id}.timeframes")
        if not timeframes:
            raise EvidenceMalformedError(f"{module_id} requires at least one timeframe")
        normalized_timeframes = tuple(
            _timeframe(item, f"{module_id}.timeframe") for item in timeframes
        )
        if contract.allowed_timeframes and not set(normalized_timeframes).issubset(
            contract.allowed_timeframes
        ):
            raise EvidenceMalformedError(f"unsupported timeframe for {module_id}")
        dependencies = tuple(
            _identifier(item, f"{module_id}.dependency")
            for item in _sequence(module["dependencies"], f"{module_id}.dependencies")
        )
        dependency_map[module_id] = dependencies
        source = _mapping(module["source_provenance"], f"{module_id}.source_provenance")
        _exact_keys(source, {"path", "commit_sha"}, f"{module_id}.source_provenance")
        if source["path"] != contract.source_path:
            raise EvidenceMalformedError(f"source path mismatch for {module_id}")
        if _git_sha(source["commit_sha"], f"{module_id}.source_commit_sha") != (
            contract.source_commit_sha
        ):
            raise EvidenceMalformedError(f"source commit mismatch for {module_id}")
        declared_complete = module["contract_complete"]
        if not isinstance(declared_complete, bool):
            raise EvidenceMalformedError("contract_complete must be bool")
        if declared_complete and not contract.contract_complete:
            raise EvidenceMalformedError(
                f"module {module_id} cannot declare a missing contract complete"
            )
        if not declared_complete or not contract.contract_complete:
            missing_contracts.append(module_id)

    known_ids = set(module_ids)
    for module_id, dependencies in dependency_map.items():
        unknown = sorted(set(dependencies) - known_ids)
        if unknown:
            raise EvidenceMalformedError(f"module {module_id} has unknown dependencies: {unknown}")
        if module_id in dependencies:
            raise EvidenceMalformedError(f"module {module_id} cannot depend on itself")

    execution_order = tuple(
        _identifier(item, "execution_order item")
        for item in _sequence(payload["execution_order"], "execution_order")
    )
    dependency_order = tuple(
        _identifier(item, "dependency_order item")
        for item in _sequence(payload["dependency_order"], "dependency_order")
    )
    if len(execution_order) != len(known_ids) or set(execution_order) != known_ids:
        raise EvidenceMalformedError("execution_order must contain every module exactly once")
    if len(dependency_order) != len(known_ids) or set(dependency_order) != known_ids:
        raise EvidenceMalformedError("dependency_order must contain every module exactly once")
    positions = {module_id: index for index, module_id in enumerate(dependency_order)}
    for module_id, dependencies in dependency_map.items():
        if any(positions[dependency] >= positions[module_id] for dependency in dependencies):
            raise EvidenceMalformedError("dependency_order violates module dependencies")

    if name == CANONICAL_COMPOSITION_NAME:
        missing_slots = CANONICAL_REQUIRED_MODULES - known_ids
        unknown_slots = known_ids - CANONICAL_REQUIRED_MODULES
        if missing_slots or unknown_slots:
            raise EvidenceMalformedError(
                "canonical composition module set mismatch; "
                f"missing={sorted(missing_slots)}, unknown={sorted(unknown_slots)}"
            )

    normalized = strict_json_loads(canonical_json_bytes(payload))
    if not isinstance(normalized, Mapping):
        raise EvidenceMalformedError("normalized composition root must be a mapping")
    complete = not missing_contracts
    if state is CompositionState.CANDIDATE and not complete:
        raise EvidenceMalformedError("incomplete composition cannot declare CANDIDATE")
    if require_canonical and (name != CANONICAL_COMPOSITION_NAME or not complete):
        raise EvidenceMalformedError("canonical composition is incomplete or misnamed")
    composition_hash = canonical_sha256(_composition_hash_payload(normalized))
    return CompositionCandidate(
        payload=normalized,
        composition_hash=composition_hash,
        candidate_id=f"strategy-candidate-{composition_hash[:24]}",
        created_by=created_by,
        state=state,
        complete=complete,
        missing_contracts=tuple(missing_contracts),
    )


def _composition_hash_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        key: payload[key]
        for key in (
            "schema_version",
            "composition_name",
            "composition_version",
            "timezone",
            "lookahead_allowed",
            "unfinished_htf_allowed",
            "created_by",
            "source_provenance",
            "modules",
            "execution_order",
            "dependency_order",
        )
    }


def _canonical_stored_mapping(payload: bytes, name: str) -> Mapping[str, Any]:
    decoded = strict_json_loads(payload)
    if not isinstance(decoded, Mapping):
        raise EvidenceTamperedError(f"stored {name} root is not a mapping")
    if canonical_json_bytes(decoded) != payload:
        raise EvidenceTamperedError(f"stored {name} is not canonical JSON")
    return decoded


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze(item) for item in value)
    return value


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceMalformedError(f"{name} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise EvidenceMalformedError(f"{name} keys must be strings")
    return value


def _sequence(value: object, name: str) -> tuple[Any, ...]:
    if not isinstance(value, (list, tuple)):
        raise EvidenceMalformedError(f"{name} must be an array")
    return tuple(value)


def _exact_keys(value: Mapping[str, Any], keys: set[str], name: str) -> None:
    missing = keys - set(value)
    unknown = set(value) - keys
    if missing or unknown:
        raise EvidenceMalformedError(
            f"{name} fields differ from schema; missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise EvidenceMalformedError(f"{name} must be a portable identifier")
    return value


def _sha(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise EvidenceMalformedError(f"{name} must be a lowercase SHA-256")
    return value


def _git_sha(value: object, name: str) -> str:
    if not isinstance(value, str) or _GIT_SHA.fullmatch(value) is None:
        raise EvidenceMalformedError(f"{name} must be a lowercase 40-character Git SHA")
    return value


def _timeframe(value: object, name: str) -> str:
    if not isinstance(value, str) or _TIMEFRAME.fullmatch(value) is None:
        raise EvidenceMalformedError(f"{name} must be a canonical timeframe")
    return value


def _datetime(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        rendered = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(rendered)
        except ValueError as exc:
            raise EvidenceMalformedError(f"{name} must be ISO-8601") from exc
    else:
        raise EvidenceMalformedError(f"{name} must be ISO-8601")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceMalformedError(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _datetime(value, "timestamp").isoformat().replace("+00:00", "Z")


__all__ = [
    "CANONICAL_COMPOSITION_NAME",
    "CANONICAL_REQUIRED_MODULES",
    "COMPOSITION_SCHEMA_VERSION",
    "CompositionCandidate",
    "CompositionState",
    "ModuleContract",
    "load_composition",
]
