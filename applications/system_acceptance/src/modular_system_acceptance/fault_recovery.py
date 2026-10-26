from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import cast

MATRIX_SCHEMA_VERSION = "1"
RESIDUAL_REGISTER_SCHEMA_VERSION = "1"
EVIDENCE_SCHEMA_VERSION = "1"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_EVIDENCE_TOKENS = (
    "token",
    "secret",
    "password",
    "credential",
    "private_key",
    "hostname",
    "runner_name",
    "absolute_path",
    "raw_payload",
    "raw_market_data",
)


class FaultClass(StrEnum):
    PROCESS_KILL = "process_kill"
    STALE_INTERRUPTED_MARKET_DATA = "stale_interrupted_market_data"
    DELAYED_OUT_OF_ORDER = "delayed_out_of_order"
    SQLITE_BUSY_LOCKED_WAL = "sqlite_busy_locked_wal"
    DISK_PRESSURE_READ_ONLY = "disk_pressure_read_only"
    CORRUPT_TRUNCATED_STATE = "corrupt_truncated_state"
    WALL_CLOCK_JUMP = "wall_clock_jump"
    SIGNAL_BOT_UNAVAILABLE = "signal_bot_unavailable"
    CONTROL_PLANE_RESTART = "control_plane_restart"
    PARTIAL_OUTAGE_RECOVERY_ORDER = "partial_outage_recovery_order"
    OPERATOR_RETRY_AFTER_TIMEOUT = "operator_retry_after_timeout"
    RESOURCE_PRESSURE_BOUNDED_QUEUE = "resource_pressure_bounded_queue"


class ScenarioSuite(StrEnum):
    SMOKE = "smoke"
    EXTENDED = "extended"


class ObservabilityKind(StrEnum):
    COMPONENT = "component"
    TRANSPORT = "transport"


class CIVerificationStatus(StrEnum):
    OWNER_BACKED_FAST = "owner_backed_fast"
    COMPOSED_CONTRACT_FAST = "composed_contract_fast"
    RESIDUAL_NOT_FULLY_REPRODUCED = "residual_not_fully_reproduced"


@dataclass(frozen=True, slots=True)
class RiskExpectation:
    state: str
    guard_code: str | None


@dataclass(frozen=True, slots=True)
class ObservabilityExpectation:
    kind: ObservabilityKind
    subject: str
    health: str
    reason_code: str | None


@dataclass(frozen=True, slots=True)
class FaultScenario:
    scenario_id: str
    fault_class: FaultClass
    suite: ScenarioSuite
    target_owner: str
    injector: str
    expected_owner_state: str
    risk_supervisor: RiskExpectation
    observability: ObservabilityExpectation
    recovery_action: str
    forbidden_side_effects: tuple[str, ...]
    factual_state_fields: tuple[str, ...]
    evidence_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FaultRecoveryMatrix:
    schema_version: str
    scenarios: tuple[FaultScenario, ...]

    def scenario(self, scenario_id: str) -> FaultScenario:
        for scenario in self.scenarios:
            if scenario.scenario_id == scenario_id:
                return scenario
        raise KeyError(scenario_id)

    @property
    def fault_classes(self) -> frozenset[FaultClass]:
        return frozenset(scenario.fault_class for scenario in self.scenarios)


@dataclass(frozen=True, slots=True)
class ResidualRiskEntry:
    scenario_id: str
    verification_status: CIVerificationStatus
    residual_risk_id: str | None
    residual_risk: str
    physical_required: bool


@dataclass(frozen=True, slots=True)
class ResidualRiskRegister:
    schema_version: str
    entries: tuple[ResidualRiskEntry, ...]

    def entry(self, scenario_id: str) -> ResidualRiskEntry:
        for item in self.entries:
            if item.scenario_id == scenario_id:
                return item
        raise KeyError(scenario_id)


@dataclass(frozen=True, slots=True)
class FactualStateFingerprint:
    schema_version: str
    digest_sha256: str
    field_names: tuple[str, ...]


def load_fault_recovery_matrix(path: Path) -> FaultRecoveryMatrix:
    raw = _load_json_object(path, "fault recovery matrix")
    if raw.get("schema_version") != MATRIX_SCHEMA_VERSION:
        raise ValueError("unsupported fault recovery matrix schema_version")
    raw_scenarios = raw.get("scenarios")
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise ValueError("fault recovery matrix scenarios must be a non-empty list")

    scenarios = tuple(_parse_scenario(item) for item in raw_scenarios)
    identifiers = tuple(scenario.scenario_id for scenario in scenarios)
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("fault recovery matrix scenario_id values must be unique")
    classes = frozenset(scenario.fault_class for scenario in scenarios)
    expected_classes = frozenset(FaultClass)
    if classes != expected_classes:
        missing = sorted(item.value for item in expected_classes - classes)
        extra = sorted(item.value for item in classes - expected_classes)
        raise ValueError(f"fault class coverage mismatch missing={missing} extra={extra}")
    if not any(scenario.suite is ScenarioSuite.SMOKE for scenario in scenarios):
        raise ValueError("fault recovery matrix must contain smoke scenarios")
    if not any(scenario.suite is ScenarioSuite.EXTENDED for scenario in scenarios):
        raise ValueError("fault recovery matrix must contain extended scenarios")
    return FaultRecoveryMatrix(schema_version=MATRIX_SCHEMA_VERSION, scenarios=scenarios)


def load_residual_risk_register(path: Path) -> ResidualRiskRegister:
    raw = _load_json_object(path, "fault recovery residual-risk register")
    if raw.get("schema_version") != RESIDUAL_REGISTER_SCHEMA_VERSION:
        raise ValueError("unsupported residual-risk register schema_version")
    raw_entries = raw.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError("residual-risk register entries must be a non-empty list")
    entries = tuple(_parse_residual_entry(item) for item in raw_entries)
    identifiers = tuple(item.scenario_id for item in entries)
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("residual-risk register scenario_id values must be unique")
    return ResidualRiskRegister(
        schema_version=RESIDUAL_REGISTER_SCHEMA_VERSION,
        entries=entries,
    )


def factual_state_fingerprint(
    state: Mapping[str, object],
    *,
    fields: Sequence[str],
) -> FactualStateFingerprint:
    normalized_fields = _string_tuple(fields, "fields", minimum=1)
    if len(set(normalized_fields)) != len(normalized_fields):
        raise ValueError("factual state fields must be unique")
    missing = [field for field in normalized_fields if field not in state]
    if missing:
        raise ValueError(f"factual state is missing required fields: {sorted(missing)}")
    selected = {field: _canonical_value(state[field], field) for field in normalized_fields}
    encoded = json.dumps(
        selected,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return FactualStateFingerprint(
        schema_version=EVIDENCE_SCHEMA_VERSION,
        digest_sha256=sha256(encoded).hexdigest(),
        field_names=tuple(sorted(normalized_fields)),
    )


def assert_recovered_state_matches(
    continuous: Mapping[str, object],
    recovered: Mapping[str, object],
    *,
    fields: Sequence[str],
) -> tuple[FactualStateFingerprint, FactualStateFingerprint]:
    continuous_fingerprint = factual_state_fingerprint(continuous, fields=fields)
    recovered_fingerprint = factual_state_fingerprint(recovered, fields=fields)
    if continuous_fingerprint != recovered_fingerprint:
        raise AssertionError(
            "recovered factual state does not match continuous factual state for declared fields"
        )
    return continuous_fingerprint, recovered_fingerprint


def sanitized_evidence_record(
    scenario: FaultScenario,
    residual: ResidualRiskEntry,
    *,
    candidate_sha: str,
    continuous: FactualStateFingerprint,
    recovered: FactualStateFingerprint,
) -> Mapping[str, object]:
    if residual.scenario_id != scenario.scenario_id:
        raise ValueError("residual-risk entry must match scenario_id")
    if re.fullmatch(r"[0-9a-f]{40}", candidate_sha) is None:
        raise ValueError("candidate_sha must be a lowercase 40-character Git SHA")
    if continuous.field_names != recovered.field_names:
        raise ValueError("continuous and recovered evidence fields must match")
    if _DIGEST_RE.fullmatch(continuous.digest_sha256) is None:
        raise ValueError("continuous digest must be a lowercase SHA-256")
    if _DIGEST_RE.fullmatch(recovered.digest_sha256) is None:
        raise ValueError("recovered digest must be a lowercase SHA-256")
    record: dict[str, object] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "scenario_id": scenario.scenario_id,
        "fault_class": scenario.fault_class.value,
        "suite": scenario.suite.value,
        "candidate_sha": candidate_sha,
        "target_owner": scenario.target_owner,
        "expected_owner_state": scenario.expected_owner_state,
        "risk_state": scenario.risk_supervisor.state,
        "risk_guard_code": scenario.risk_supervisor.guard_code,
        "observability_kind": scenario.observability.kind.value,
        "observability_subject": scenario.observability.subject,
        "observability_health": scenario.observability.health,
        "observability_reason_code": scenario.observability.reason_code,
        "verification_status": residual.verification_status.value,
        "residual_risk_id": residual.residual_risk_id,
        "continuous_state_sha256": continuous.digest_sha256,
        "recovered_state_sha256": recovered.digest_sha256,
        "factual_state_fields": continuous.field_names,
    }
    if frozenset(record) != frozenset(scenario.evidence_fields):
        raise ValueError(
            "scenario evidence_fields must exactly match the sanitized evidence schema"
        )
    return MappingProxyType(record)


def _load_json_object(path: Path, name: str) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} could not be loaded") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{name} root must be an object")
    return cast(dict[str, object], raw)


def _parse_scenario(raw: object) -> FaultScenario:
    if not isinstance(raw, dict):
        raise ValueError("fault recovery scenario must be an object")
    expected_keys = {
        "scenario_id",
        "fault_class",
        "suite",
        "target_owner",
        "injector",
        "expected_owner_state",
        "risk_supervisor",
        "observability",
        "recovery_action",
        "forbidden_side_effects",
        "factual_state_fields",
        "evidence_fields",
    }
    if set(raw) != expected_keys:
        raise ValueError("fault recovery scenario fields do not match schema")
    risk_raw = raw["risk_supervisor"]
    if not isinstance(risk_raw, dict) or set(risk_raw) != {"state", "guard_code"}:
        raise ValueError("risk_supervisor expectation does not match schema")
    guard = risk_raw["guard_code"]
    if guard is not None:
        guard = _identifier(guard, "risk_supervisor.guard_code")
    observability_raw = raw["observability"]
    if not isinstance(observability_raw, dict) or set(observability_raw) != {
        "kind",
        "subject",
        "health",
        "reason_code",
    }:
        raise ValueError("observability expectation does not match schema")
    reason = observability_raw["reason_code"]
    if reason is not None:
        reason = _identifier(reason, "observability.reason_code")

    scenario = FaultScenario(
        scenario_id=_identifier(raw["scenario_id"], "scenario_id"),
        fault_class=FaultClass(_identifier(raw["fault_class"], "fault_class")),
        suite=ScenarioSuite(_identifier(raw["suite"], "suite")),
        target_owner=_identifier(raw["target_owner"], "target_owner"),
        injector=_identifier(raw["injector"], "injector"),
        expected_owner_state=_identifier(raw["expected_owner_state"], "expected_owner_state"),
        risk_supervisor=RiskExpectation(
            state=_identifier(risk_raw["state"], "risk_supervisor.state"),
            guard_code=guard,
        ),
        observability=ObservabilityExpectation(
            kind=ObservabilityKind(_identifier(observability_raw["kind"], "observability.kind")),
            subject=_identifier(observability_raw["subject"], "observability.subject"),
            health=_identifier(observability_raw["health"], "observability.health"),
            reason_code=reason,
        ),
        recovery_action=_identifier(raw["recovery_action"], "recovery_action"),
        forbidden_side_effects=_string_tuple(
            raw["forbidden_side_effects"], "forbidden_side_effects", minimum=1
        ),
        factual_state_fields=_string_tuple(
            raw["factual_state_fields"], "factual_state_fields", minimum=1
        ),
        evidence_fields=_string_tuple(raw["evidence_fields"], "evidence_fields", minimum=1),
    )
    _validate_scenario(scenario)
    return scenario


def _parse_residual_entry(raw: object) -> ResidualRiskEntry:
    if not isinstance(raw, dict) or set(raw) != {
        "scenario_id",
        "verification_status",
        "residual_risk_id",
        "residual_risk",
        "physical_required",
    }:
        raise ValueError("residual-risk entry does not match schema")
    risk_id = raw["residual_risk_id"]
    if risk_id is not None:
        risk_id = _identifier(risk_id, "residual_risk_id")
    residual_risk = raw["residual_risk"]
    if not isinstance(residual_risk, str) or not residual_risk.strip():
        raise ValueError("residual_risk must be a non-empty string")
    physical_required = raw["physical_required"]
    if not isinstance(physical_required, bool):
        raise ValueError("physical_required must be bool")
    status = CIVerificationStatus(_identifier(raw["verification_status"], "verification_status"))
    if status is CIVerificationStatus.RESIDUAL_NOT_FULLY_REPRODUCED and risk_id is None:
        raise ValueError("residual verification requires residual_risk_id")
    if status is not CIVerificationStatus.RESIDUAL_NOT_FULLY_REPRODUCED and risk_id is not None:
        raise ValueError("non-residual verification cannot carry residual_risk_id")
    return ResidualRiskEntry(
        scenario_id=_identifier(raw["scenario_id"], "scenario_id"),
        verification_status=status,
        residual_risk_id=risk_id,
        residual_risk=residual_risk.strip(),
        physical_required=physical_required,
    )


def _validate_scenario(scenario: FaultScenario) -> None:
    if len(set(scenario.forbidden_side_effects)) != len(scenario.forbidden_side_effects):
        raise ValueError("forbidden_side_effects cannot contain duplicates")
    if len(set(scenario.factual_state_fields)) != len(scenario.factual_state_fields):
        raise ValueError("factual_state_fields cannot contain duplicates")
    if len(set(scenario.evidence_fields)) != len(scenario.evidence_fields):
        raise ValueError("evidence_fields cannot contain duplicates")
    for field in scenario.evidence_fields:
        lowered = field.lower()
        if any(token in lowered for token in _FORBIDDEN_EVIDENCE_TOKENS):
            raise ValueError(f"unsafe evidence field is forbidden: {field}")
    if scenario.risk_supervisor.state == "healthy":
        if scenario.risk_supervisor.guard_code is not None:
            raise ValueError("healthy risk expectation cannot include a guard code")
    elif scenario.risk_supervisor.guard_code is None:
        raise ValueError("non-healthy risk expectation requires a guard code")
    if scenario.observability.health == "healthy":
        if scenario.observability.reason_code is not None:
            raise ValueError("healthy observability cannot include a reason code")
    elif scenario.observability.reason_code is None:
        raise ValueError("non-healthy observability requires a reason code")
    if scenario.observability.kind is ObservabilityKind.TRANSPORT:
        if scenario.observability.subject != "control_plane_transport":
            raise ValueError("transport observability is reserved for control_plane_transport")


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a stable lowercase identifier")
    return value


def _string_tuple(value: object, name: str, *, minimum: int) -> tuple[str, ...]:
    if not isinstance(value, list) and not isinstance(value, tuple):
        raise ValueError(f"{name} must be a sequence")
    values = cast(Sequence[object], value)
    if len(values) < minimum:
        raise ValueError(f"{name} requires at least {minimum} item(s)")
    return tuple(_identifier(item, name) for item in values)


def _canonical_value(value: object, name: str) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{name} must be finite")
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item, f"{name}.{key}")
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item, name) for item in value]
    raise TypeError(f"{name} contains unsupported factual state type {type(value).__name__}")
