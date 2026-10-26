from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Protocol

from .contracts import (
    AuthoritativeRiskEvidence,
    OperatorAction,
    OperatorCommand,
    RiskGuardCode,
    RiskSupervisorPolicy,
    SafetyDecision,
    SafetyState,
    canonical_hash,
    decimal,
    identifier,
    nonnegative_float,
    nonnegative_int,
    utc,
)


class StateRepository(Protocol):
    def save(self, key: str, value: Mapping[str, Any]) -> None: ...

    def load(self, key: str) -> Mapping[str, Any] | None: ...


@dataclass(slots=True)
class _SupervisorState:
    sequence: int
    state: SafetyState
    latest_evidence: AuthoritativeRiskEvidence | None
    latest_decision: SafetyDecision
    latched_codes: tuple[RiskGuardCode, ...]
    breach_counts: dict[RiskGuardCode, int]
    replay_records: dict[str, tuple[str, SafetyDecision]]
    operator_commands: dict[str, tuple[str, SafetyDecision]]
    audit_log: list[dict[str, object]]


class RiskSupervisor:
    STATE_SCHEMA_VERSION = "2"
    OPERATOR_COMMAND_MAX_AGE_SECONDS = 60.0

    def __init__(
        self,
        *,
        runtime_id: str,
        policy: RiskSupervisorPolicy,
        repository: StateRepository,
        operator_hmac_key: bytes,
    ) -> None:
        self.runtime_id = identifier(runtime_id, "runtime_id")
        if not isinstance(policy, RiskSupervisorPolicy):
            raise TypeError("policy must be RiskSupervisorPolicy")
        if not isinstance(operator_hmac_key, bytes) or len(operator_hmac_key) < 32:
            raise ValueError("operator_hmac_key must contain at least 32 bytes")
        if not callable(getattr(repository, "load", None)) or not callable(
            getattr(repository, "save", None)
        ):
            raise TypeError("repository must implement load() and save()")
        self.policy = policy
        self.repository = repository
        self._operator_hmac_key = operator_hmac_key
        self._state: _SupervisorState | None = None
        self._state_invalid = False
        self._restore()

    @property
    def state_key(self) -> str:
        return f"risk-supervisor:{self.runtime_id}:v2"

    @property
    def breach_counts(self) -> Mapping[RiskGuardCode, int]:
        if self._state is None:
            return MappingProxyType({})
        return MappingProxyType(dict(self._state.breach_counts))

    @property
    def audit_log(self) -> tuple[Mapping[str, object], ...]:
        if self._state is None:
            return ()
        return tuple(MappingProxyType(dict(row)) for row in self._state.audit_log)

    @property
    def policy_hash(self) -> str:
        payload: dict[str, object] = {
            "maximum_daily_loss_fraction": str(self.policy.maximum_daily_loss_fraction),
            "maximum_drawdown_fraction": str(self.policy.maximum_drawdown_fraction),
            "maximum_exposure_notional": str(self.policy.maximum_exposure_notional),
            "maximum_gross_leverage": str(self.policy.maximum_gross_leverage),
            "maximum_rejection_rate_fraction": str(self.policy.maximum_rejection_rate_fraction),
            "maximum_cancel_failure_rate_fraction": str(
                self.policy.maximum_cancel_failure_rate_fraction
            ),
            "minimum_execution_samples": self.policy.minimum_execution_samples,
            "maximum_mean_abs_slippage_bps": str(self.policy.maximum_mean_abs_slippage_bps),
            "minimum_fill_quality_fraction": str(self.policy.minimum_fill_quality_fraction),
            "maximum_evidence_age_seconds": self.policy.maximum_evidence_age_seconds,
            "maximum_market_data_age_seconds": self.policy.maximum_market_data_age_seconds,
            "maximum_consumer_stall_seconds": self.policy.maximum_consumer_stall_seconds,
            "maximum_execution_latency_seconds": self.policy.maximum_execution_latency_seconds,
            "minimum_disk_free_bytes": self.policy.minimum_disk_free_bytes,
            "maximum_future_skew_seconds": self.policy.maximum_future_skew_seconds,
            "mandatory_guards": sorted(code.value for code in self.policy.mandatory_guards),
        }
        return canonical_hash(payload)

    def evaluate(
        self,
        evidence: AuthoritativeRiskEvidence,
        *,
        now: datetime,
    ) -> SafetyDecision:
        if not isinstance(evidence, AuthoritativeRiskEvidence):
            raise TypeError("evidence must be AuthoritativeRiskEvidence")
        moment = utc(now, "now")
        if self._state_invalid:
            return self._ephemeral_decision(
                SafetyState.MANUAL_REVIEW,
                (RiskGuardCode.STATE_INVALID,),
                evidence,
                moment,
            )

        replay = self._replay_decision(evidence)
        if replay is not None:
            return replay
        if (
            self._state is not None
            and self._state.latest_evidence is not None
            and evidence.observed_at < self._state.latest_evidence.observed_at
        ):
            return self._ephemeral_decision(
                SafetyState.MANUAL_REVIEW,
                (RiskGuardCode.SNAPSHOT_OUT_OF_ORDER,),
                evidence,
                moment,
            )
        if self._has_clock_anomaly(evidence, moment):
            return self._ephemeral_decision(
                SafetyState.MANUAL_REVIEW,
                (RiskGuardCode.CLOCK_ANOMALY,),
                evidence,
                moment,
            )

        active_codes = self._evaluate_guard_codes(evidence, moment)
        previous = self._state
        counts = dict(previous.breach_counts) if previous is not None else {}
        for code in active_codes:
            counts[code] = counts.get(code, 0) + 1

        latched = set(previous.latched_codes) if previous is not None else set()
        mandatory_active = set(active_codes).intersection(self.policy.mandatory_guards)
        latched.update(mandatory_active)
        sequence = previous.sequence + 1 if previous is not None else 1

        if latched:
            decision_codes = tuple(
                sorted(set(active_codes).union(latched, {RiskGuardCode.RECOVERY_REQUIRED}), key=str)
            )
            state = SafetyState.HALT
        elif active_codes:
            decision_codes = active_codes
            state = SafetyState.DEGRADED
        else:
            decision_codes = ()
            state = SafetyState.HEALTHY

        decision = SafetyDecision(
            sequence=sequence,
            state=state,
            evaluated_at=moment,
            guard_codes=decision_codes,
            snapshot_id=evidence.snapshot_id,
            snapshot_hash=evidence.content_hash,
            source_state_ids=evidence.source_state_ids,
        )
        replay_records = dict(previous.replay_records) if previous is not None else {}
        replay_records[evidence.snapshot_id] = (evidence.content_hash, decision)
        operator_commands = dict(previous.operator_commands) if previous is not None else {}
        audit_log = list(previous.audit_log) if previous is not None else []
        audit_log.append(
            self._audit_row(
                sequence=sequence,
                timestamp=moment,
                event_type="evidence_evaluated",
                decision=decision,
                subject_id=evidence.snapshot_id,
                subject_hash=evidence.content_hash,
            )
        )
        self._state = _SupervisorState(
            sequence=sequence,
            state=state,
            latest_evidence=evidence,
            latest_decision=decision,
            latched_codes=tuple(sorted(latched, key=str)),
            breach_counts=counts,
            replay_records=replay_records,
            operator_commands=operator_commands,
            audit_log=audit_log,
        )
        self._persist()
        return decision

    def decision(self, *, now: datetime) -> SafetyDecision:
        moment = utc(now, "now")
        if self._state_invalid:
            return SafetyDecision(
                sequence=0,
                state=SafetyState.MANUAL_REVIEW,
                evaluated_at=moment,
                guard_codes=(RiskGuardCode.STATE_INVALID,),
                snapshot_id=None,
                snapshot_hash=None,
                source_state_ids={},
            )
        if self._state is None:
            return SafetyDecision(
                sequence=0,
                state=SafetyState.HALT,
                evaluated_at=moment,
                guard_codes=(RiskGuardCode.SUPERVISOR_UNINITIALIZED,),
                snapshot_id=None,
                snapshot_hash=None,
                source_state_ids={},
            )
        evidence = self._state.latest_evidence
        if evidence is None:
            codes = tuple(
                sorted(
                    set(self._state.latched_codes).union(
                        {
                            RiskGuardCode.SUPERVISOR_UNINITIALIZED,
                            RiskGuardCode.RECOVERY_REQUIRED,
                        }
                    ),
                    key=str,
                )
            )
            return SafetyDecision(
                sequence=self._state.sequence,
                state=SafetyState.HALT,
                evaluated_at=moment,
                guard_codes=codes,
                snapshot_id=None,
                snapshot_hash=None,
                source_state_ids={},
            )
        active_codes = self._evaluate_guard_codes(evidence, moment)
        latched = set(self._state.latched_codes)
        if latched:
            codes = tuple(
                sorted(set(active_codes).union(latched, {RiskGuardCode.RECOVERY_REQUIRED}), key=str)
            )
            state = SafetyState.HALT
        elif active_codes:
            codes = active_codes
            state = (
                SafetyState.HALT
                if set(active_codes).intersection(self.policy.mandatory_guards)
                else SafetyState.DEGRADED
            )
        else:
            codes = ()
            state = SafetyState.HEALTHY
        return SafetyDecision(
            sequence=self._state.sequence,
            state=state,
            evaluated_at=moment,
            guard_codes=codes,
            snapshot_id=evidence.snapshot_id,
            snapshot_hash=evidence.content_hash,
            source_state_ids=evidence.source_state_ids,
        )

    def apply_operator_command(
        self,
        command: OperatorCommand,
        *,
        now: datetime,
    ) -> SafetyDecision:
        if not isinstance(command, OperatorCommand):
            raise TypeError("command must be OperatorCommand")
        moment = utc(now, "now")
        if self._state_invalid:
            return self._ephemeral_decision(
                SafetyState.MANUAL_REVIEW,
                (RiskGuardCode.STATE_INVALID,),
                None,
                moment,
            )
        command_hash = canonical_hash(command.signing_mapping())
        if self._state is not None:
            prior = self._state.operator_commands.get(command.command_id)
            if prior is not None:
                prior_hash, prior_decision = prior
                if prior_hash == command_hash:
                    return prior_decision
                return self._ephemeral_decision(
                    SafetyState.MANUAL_REVIEW,
                    (RiskGuardCode.OPERATOR_COMMAND_REPLAY_MISMATCH,),
                    self._state.latest_evidence,
                    moment,
                )
        if not self._operator_command_is_valid(command, moment):
            evidence = self._state.latest_evidence if self._state is not None else None
            return self._ephemeral_decision(
                SafetyState.MANUAL_REVIEW,
                (RiskGuardCode.OPERATOR_AUTH_FAILED,),
                evidence,
                moment,
            )
        if command.action is OperatorAction.HALT:
            return self._apply_operator_halt(command, command_hash, moment)
        return self._apply_operator_recovery(command, command_hash, moment)

    def _apply_operator_halt(
        self,
        command: OperatorCommand,
        command_hash: str,
        now: datetime,
    ) -> SafetyDecision:
        previous = self._state
        sequence = previous.sequence + 1 if previous is not None else 1
        evidence = previous.latest_evidence if previous is not None else None
        decision = SafetyDecision(
            sequence=sequence,
            state=SafetyState.HALT,
            evaluated_at=now,
            guard_codes=(
                RiskGuardCode.OPERATOR_EMERGENCY_HALT,
                RiskGuardCode.RECOVERY_REQUIRED,
            ),
            snapshot_id=evidence.snapshot_id if evidence is not None else None,
            snapshot_hash=evidence.content_hash if evidence is not None else None,
            source_state_ids=evidence.source_state_ids if evidence is not None else {},
        )
        latched = set(previous.latched_codes) if previous is not None else set()
        latched.add(RiskGuardCode.OPERATOR_EMERGENCY_HALT)
        commands = dict(previous.operator_commands) if previous is not None else {}
        commands[command.command_id] = (command_hash, decision)
        audit_log = list(previous.audit_log) if previous is not None else []
        audit_log.append(
            self._audit_row(
                sequence=sequence,
                timestamp=now,
                event_type="operator_halt",
                decision=decision,
                subject_id=command.command_id,
                subject_hash=command_hash,
            )
        )
        self._state = _SupervisorState(
            sequence=sequence,
            state=SafetyState.HALT,
            latest_evidence=evidence,
            latest_decision=decision,
            latched_codes=tuple(sorted(latched, key=str)),
            breach_counts=dict(previous.breach_counts) if previous is not None else {},
            replay_records=dict(previous.replay_records) if previous is not None else {},
            operator_commands=commands,
            audit_log=audit_log,
        )
        self._persist()
        return decision

    def _apply_operator_recovery(
        self,
        command: OperatorCommand,
        command_hash: str,
        now: datetime,
    ) -> SafetyDecision:
        if self._state is None:
            return self._ephemeral_decision(
                SafetyState.MANUAL_REVIEW,
                (RiskGuardCode.SUPERVISOR_UNINITIALIZED,),
                None,
                now,
            )
        evidence = self._state.latest_evidence
        if evidence is None:
            codes = tuple(
                sorted(
                    set(self._state.latched_codes).union(
                        {
                            RiskGuardCode.SUPERVISOR_UNINITIALIZED,
                            RiskGuardCode.RECOVERY_REQUIRED,
                        }
                    ),
                    key=str,
                )
            )
            return self._ephemeral_decision(
                SafetyState.MANUAL_REVIEW,
                codes,
                None,
                now,
            )
        active = self._evaluate_guard_codes(evidence, now)
        if active:
            return self._ephemeral_decision(
                SafetyState.MANUAL_REVIEW,
                tuple(sorted(set(active).union({RiskGuardCode.RECOVERY_REQUIRED}), key=str)),
                evidence,
                now,
            )
        sequence = self._state.sequence + 1
        decision = SafetyDecision(
            sequence=sequence,
            state=SafetyState.HEALTHY,
            evaluated_at=now,
            guard_codes=(),
            snapshot_id=evidence.snapshot_id,
            snapshot_hash=evidence.content_hash,
            source_state_ids=evidence.source_state_ids,
        )
        commands = dict(self._state.operator_commands)
        commands[command.command_id] = (command_hash, decision)
        audit_log = list(self._state.audit_log)
        audit_log.append(
            self._audit_row(
                sequence=sequence,
                timestamp=now,
                event_type="operator_recovery",
                decision=decision,
                subject_id=command.command_id,
                subject_hash=command_hash,
            )
        )
        self._state = _SupervisorState(
            sequence=sequence,
            state=SafetyState.HEALTHY,
            latest_evidence=evidence,
            latest_decision=decision,
            latched_codes=(),
            breach_counts=dict(self._state.breach_counts),
            replay_records=dict(self._state.replay_records),
            operator_commands=commands,
            audit_log=audit_log,
        )
        self._persist()
        return decision

    def _replay_decision(self, evidence: AuthoritativeRiskEvidence) -> SafetyDecision | None:
        if self._state is None:
            return None
        prior = self._state.replay_records.get(evidence.snapshot_id)
        if prior is None:
            return None
        prior_hash, prior_decision = prior
        if prior_hash == evidence.content_hash:
            return prior_decision
        return self._ephemeral_decision(
            SafetyState.MANUAL_REVIEW,
            (RiskGuardCode.SNAPSHOT_ID_CONTENT_MISMATCH,),
            evidence,
            evidence.observed_at,
        )

    def _has_clock_anomaly(self, evidence: AuthoritativeRiskEvidence, now: datetime) -> bool:
        skew = self.policy.maximum_future_skew_seconds
        return any(
            (timestamp - now).total_seconds() > skew
            for timestamp in (
                evidence.observed_at,
                evidence.market_data_observed_at,
                evidence.consumer_progress_at,
            )
        )

    def _evaluate_guard_codes(
        self,
        evidence: AuthoritativeRiskEvidence,
        now: datetime,
    ) -> tuple[RiskGuardCode, ...]:
        codes: set[RiskGuardCode] = set()
        if self._has_clock_anomaly(evidence, now):
            codes.add(RiskGuardCode.CLOCK_ANOMALY)
        if (now - evidence.observed_at).total_seconds() > self.policy.maximum_evidence_age_seconds:
            codes.add(RiskGuardCode.EVIDENCE_STALE)
        session_loss = max(
            Decimal(0),
            (evidence.session_start_equity - evidence.current_equity)
            / evidence.session_start_equity,
        )
        drawdown = max(
            Decimal(0),
            (evidence.peak_equity - evidence.current_equity) / evidence.peak_equity,
        )
        if session_loss >= self.policy.maximum_daily_loss_fraction:
            codes.add(RiskGuardCode.DAILY_LOSS_LIMIT)
        if drawdown >= self.policy.maximum_drawdown_fraction:
            codes.add(RiskGuardCode.DRAWDOWN_LIMIT)
        if evidence.aggregate_exposure_notional >= self.policy.maximum_exposure_notional:
            codes.add(RiskGuardCode.EXPOSURE_NOTIONAL_LIMIT)
        if evidence.gross_leverage >= self.policy.maximum_gross_leverage:
            codes.add(RiskGuardCode.LEVERAGE_LIMIT)
        if (
            now - evidence.market_data_observed_at
        ).total_seconds() > self.policy.maximum_market_data_age_seconds:
            codes.add(RiskGuardCode.MARKET_DATA_STALE)
        if (
            now - evidence.consumer_progress_at
        ).total_seconds() > self.policy.maximum_consumer_stall_seconds:
            codes.add(RiskGuardCode.CONSUMER_STALLED)
        if evidence.reconciliation_blocked_scopes > 0:
            codes.add(RiskGuardCode.RECONCILIATION_BLOCKED)
        if evidence.reconciliation_loss_count > 0:
            codes.add(RiskGuardCode.RECONCILIATION_LOSS)
        if evidence.execution_attempts >= self.policy.minimum_execution_samples:
            rejection_rate = Decimal(evidence.execution_rejections) / Decimal(
                evidence.execution_attempts
            )
            if rejection_rate >= self.policy.maximum_rejection_rate_fraction:
                codes.add(RiskGuardCode.REJECTION_RATE_LIMIT)
        if evidence.cancel_attempts >= self.policy.minimum_execution_samples:
            cancel_failure_rate = Decimal(evidence.cancel_failures) / Decimal(
                evidence.cancel_attempts
            )
            if cancel_failure_rate >= self.policy.maximum_cancel_failure_rate_fraction:
                codes.add(RiskGuardCode.CANCEL_FAILURE_RATE_LIMIT)
        if evidence.fill_samples >= self.policy.minimum_execution_samples:
            if evidence.mean_abs_slippage_bps >= self.policy.maximum_mean_abs_slippage_bps:
                codes.add(RiskGuardCode.SLIPPAGE_LIMIT)
            if evidence.fill_quality_fraction <= self.policy.minimum_fill_quality_fraction:
                codes.add(RiskGuardCode.FILL_QUALITY_LIMIT)
        if not evidence.checkpoint_integrity_ok:
            codes.add(RiskGuardCode.CHECKPOINT_INTEGRITY_FAILURE)
        if evidence.disk_free_bytes <= self.policy.minimum_disk_free_bytes:
            codes.add(RiskGuardCode.RESOURCE_PRESSURE)
        if not evidence.transport_healthy:
            codes.add(RiskGuardCode.TRANSPORT_UNHEALTHY)
        if evidence.execution_latency_seconds >= self.policy.maximum_execution_latency_seconds:
            codes.add(RiskGuardCode.EXECUTION_LATENCY_LIMIT)
        return tuple(sorted(codes, key=str))

    def _operator_command_is_valid(self, command: OperatorCommand, now: datetime) -> bool:
        if command.runtime_id != self.runtime_id:
            return False
        age = (now - command.issued_at).total_seconds()
        if age < -self.policy.maximum_future_skew_seconds:
            return False
        if age > self.OPERATOR_COMMAND_MAX_AGE_SECONDS:
            return False
        expected = sign_operator_command(
            self._operator_hmac_key,
            command_id=command.command_id,
            runtime_id=command.runtime_id,
            action=command.action,
            issued_at=command.issued_at,
        )
        return hmac.compare_digest(expected, command.signature)

    def _ephemeral_decision(
        self,
        state: SafetyState,
        codes: tuple[RiskGuardCode, ...],
        evidence: AuthoritativeRiskEvidence | None,
        now: datetime,
    ) -> SafetyDecision:
        sequence = self._state.sequence if self._state is not None else 0
        return SafetyDecision(
            sequence=sequence,
            state=state,
            evaluated_at=now,
            guard_codes=codes,
            snapshot_id=evidence.snapshot_id if evidence is not None else None,
            snapshot_hash=evidence.content_hash if evidence is not None else None,
            source_state_ids=evidence.source_state_ids if evidence is not None else {},
        )

    def _audit_row(
        self,
        *,
        sequence: int,
        timestamp: datetime,
        event_type: str,
        decision: SafetyDecision,
        subject_id: str,
        subject_hash: str,
    ) -> dict[str, object]:
        return {
            "sequence": sequence,
            "timestamp": timestamp.isoformat(),
            "event_type": event_type,
            "subject_id": subject_id,
            "subject_hash": subject_hash,
            "decision_hash": decision.decision_hash,
            "state": decision.state.value,
            "guard_codes": [code.value for code in decision.guard_codes],
        }

    def _persist(self) -> None:
        if self._state is None:
            raise RuntimeError("cannot persist missing supervisor state")
        state = self._state
        payload: dict[str, object] = {
            "state_schema_version": self.STATE_SCHEMA_VERSION,
            "runtime_id": self.runtime_id,
            "policy_hash": self.policy_hash,
            "sequence": state.sequence,
            "state": state.state.value,
            "latest_evidence": (
                None
                if state.latest_evidence is None
                else dict(state.latest_evidence.canonical_mapping())
            ),
            "latest_decision": _decision_mapping(state.latest_decision),
            "latched_codes": [code.value for code in state.latched_codes],
            "breach_counts": {
                code.value: count
                for code, count in sorted(
                    state.breach_counts.items(), key=lambda item: item[0].value
                )
            },
            "replay_records": {
                snapshot_id: {
                    "snapshot_hash": snapshot_hash,
                    "decision": _decision_mapping(decision),
                }
                for snapshot_id, (snapshot_hash, decision) in sorted(state.replay_records.items())
            },
            "operator_commands": {
                command_id: {
                    "command_hash": command_hash,
                    "decision": _decision_mapping(decision),
                }
                for command_id, (command_hash, decision) in sorted(state.operator_commands.items())
            },
            "audit_log": list(state.audit_log),
        }
        payload["state_checksum"] = canonical_hash(payload)
        self.repository.save(self.state_key, payload)

    def _restore(self) -> None:
        try:
            raw = self.repository.load(self.state_key)
            if raw is None:
                return
            payload = dict(raw)
            checksum = payload.pop("state_checksum", None)
            if not isinstance(checksum, str) or checksum != canonical_hash(payload):
                raise ValueError("state checksum mismatch")
            if payload.get("state_schema_version") != self.STATE_SCHEMA_VERSION:
                raise ValueError("unsupported state schema")
            if payload.get("runtime_id") != self.runtime_id:
                raise ValueError("runtime identity mismatch")
            if payload.get("policy_hash") != self.policy_hash:
                raise ValueError("policy identity mismatch")
            sequence = nonnegative_int(payload.get("sequence"), "sequence")
            latest_evidence_value = payload.get("latest_evidence")
            latest_evidence = (
                None
                if latest_evidence_value is None
                else _evidence_from_mapping(_mapping(latest_evidence_value, "latest_evidence"))
            )
            latest_decision = _decision_from_mapping(
                _mapping(payload.get("latest_decision"), "latest_decision")
            )
            latched_raw = _list(payload.get("latched_codes"), "latched_codes")
            latched_codes = tuple(RiskGuardCode(str(value)) for value in latched_raw)
            breach_raw = _mapping(payload.get("breach_counts"), "breach_counts")
            breach_counts = {
                RiskGuardCode(str(code)): nonnegative_int(count, "breach count")
                for code, count in breach_raw.items()
            }
            replay_raw = _mapping(payload.get("replay_records"), "replay_records")
            replay_records: dict[str, tuple[str, SafetyDecision]] = {}
            for snapshot_id, row in replay_raw.items():
                row_mapping = _mapping(row, "replay record")
                snapshot_hash = _string(row_mapping.get("snapshot_hash"), "snapshot_hash")
                decision = _decision_from_mapping(
                    _mapping(row_mapping.get("decision"), "replay decision")
                )
                replay_records[identifier(snapshot_id, "snapshot_id")] = (
                    snapshot_hash,
                    decision,
                )
            command_raw = _mapping(payload.get("operator_commands"), "operator_commands")
            operator_commands: dict[str, tuple[str, SafetyDecision]] = {}
            for command_id, row in command_raw.items():
                row_mapping = _mapping(row, "operator command record")
                command_hash = _string(row_mapping.get("command_hash"), "command_hash")
                decision = _decision_from_mapping(
                    _mapping(row_mapping.get("decision"), "operator decision")
                )
                operator_commands[identifier(command_id, "command_id")] = (
                    command_hash,
                    decision,
                )
            audit_raw = _list(payload.get("audit_log"), "audit_log")
            audit_log = [dict(_mapping(row, "audit row")) for row in audit_raw]
            state = SafetyState(_string(payload.get("state"), "state"))
            if latest_decision.sequence > sequence:
                raise ValueError("decision sequence exceeds state sequence")
            if latest_evidence is None:
                if replay_records:
                    raise ValueError("state without evidence cannot contain snapshot replays")
                if (
                    latest_decision.snapshot_id is not None
                    or latest_decision.snapshot_hash is not None
                ):
                    raise ValueError("state without evidence cannot reference a snapshot")
                if RiskGuardCode.OPERATOR_EMERGENCY_HALT not in latched_codes:
                    raise ValueError("state without evidence requires operator emergency halt")
            self._state = _SupervisorState(
                sequence=sequence,
                state=state,
                latest_evidence=latest_evidence,
                latest_decision=latest_decision,
                latched_codes=latched_codes,
                breach_counts=breach_counts,
                replay_records=replay_records,
                operator_commands=operator_commands,
                audit_log=audit_log,
            )
        except (KeyError, TypeError, ValueError):
            self._state = None
            self._state_invalid = True


def sign_operator_command(
    key: bytes,
    *,
    command_id: str,
    runtime_id: str,
    action: OperatorAction,
    issued_at: datetime,
) -> str:
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("operator HMAC key must contain at least 32 bytes")
    payload = {
        "command_id": identifier(command_id, "command_id"),
        "runtime_id": identifier(runtime_id, "runtime_id"),
        "action": action.value,
        "issued_at": utc(issued_at, "issued_at").isoformat(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(key, encoded, sha256).hexdigest()


def _decision_mapping(decision: SafetyDecision) -> dict[str, object]:
    return {
        "sequence": decision.sequence,
        "state": decision.state.value,
        "evaluated_at": decision.evaluated_at.isoformat(),
        "guard_codes": [code.value for code in decision.guard_codes],
        "snapshot_id": decision.snapshot_id,
        "snapshot_hash": decision.snapshot_hash,
        "source_state_ids": dict(decision.source_state_ids),
        "decision_hash": decision.decision_hash,
    }


def _decision_from_mapping(raw: Mapping[str, object]) -> SafetyDecision:
    source_ids = _mapping(raw.get("source_state_ids"), "source_state_ids")
    guard_values = _list(raw.get("guard_codes"), "guard_codes")
    snapshot_id_value = raw.get("snapshot_id")
    snapshot_hash_value = raw.get("snapshot_hash")
    decision = SafetyDecision(
        sequence=nonnegative_int(raw.get("sequence"), "sequence"),
        state=SafetyState(_string(raw.get("state"), "state")),
        evaluated_at=datetime.fromisoformat(_string(raw.get("evaluated_at"), "evaluated_at")),
        guard_codes=tuple(RiskGuardCode(str(value)) for value in guard_values),
        snapshot_id=None
        if snapshot_id_value is None
        else _string(snapshot_id_value, "snapshot_id"),
        snapshot_hash=(
            None if snapshot_hash_value is None else _string(snapshot_hash_value, "snapshot_hash")
        ),
        source_state_ids={
            str(key): _string(value, "source state id") for key, value in source_ids.items()
        },
    )
    if raw.get("decision_hash") != decision.decision_hash:
        raise ValueError("decision hash mismatch")
    return decision


def _evidence_from_mapping(raw: Mapping[str, object]) -> AuthoritativeRiskEvidence:
    source_ids = _mapping(raw.get("source_state_ids"), "source_state_ids")
    return AuthoritativeRiskEvidence(
        schema_version=_string(raw.get("schema_version"), "schema_version"),
        snapshot_id=_string(raw.get("snapshot_id"), "snapshot_id"),
        observed_at=datetime.fromisoformat(_string(raw.get("observed_at"), "observed_at")),
        source_state_ids={
            str(key): _string(value, "source state id") for key, value in source_ids.items()
        },
        session_start_equity=decimal(raw.get("session_start_equity"), "session_start_equity"),
        current_equity=decimal(raw.get("current_equity"), "current_equity"),
        peak_equity=decimal(raw.get("peak_equity"), "peak_equity"),
        aggregate_exposure_notional=decimal(
            raw.get("aggregate_exposure_notional"), "aggregate_exposure_notional"
        ),
        market_data_observed_at=datetime.fromisoformat(
            _string(raw.get("market_data_observed_at"), "market_data_observed_at")
        ),
        consumer_progress_at=datetime.fromisoformat(
            _string(raw.get("consumer_progress_at"), "consumer_progress_at")
        ),
        reconciliation_blocked_scopes=nonnegative_int(
            raw.get("reconciliation_blocked_scopes"), "reconciliation_blocked_scopes"
        ),
        reconciliation_loss_count=nonnegative_int(
            raw.get("reconciliation_loss_count"), "reconciliation_loss_count"
        ),
        execution_attempts=nonnegative_int(raw.get("execution_attempts"), "execution_attempts"),
        execution_rejections=nonnegative_int(
            raw.get("execution_rejections"), "execution_rejections"
        ),
        cancel_attempts=nonnegative_int(raw.get("cancel_attempts"), "cancel_attempts"),
        cancel_failures=nonnegative_int(raw.get("cancel_failures"), "cancel_failures"),
        fill_samples=nonnegative_int(raw.get("fill_samples"), "fill_samples"),
        mean_abs_slippage_bps=decimal(raw.get("mean_abs_slippage_bps"), "mean_abs_slippage_bps"),
        fill_quality_fraction=decimal(raw.get("fill_quality_fraction"), "fill_quality_fraction"),
        checkpoint_integrity_ok=_bool(
            raw.get("checkpoint_integrity_ok"), "checkpoint_integrity_ok"
        ),
        disk_free_bytes=nonnegative_int(raw.get("disk_free_bytes"), "disk_free_bytes"),
        transport_healthy=_bool(raw.get("transport_healthy"), "transport_healthy"),
        execution_latency_seconds=nonnegative_float(
            raw.get("execution_latency_seconds"), "execution_latency_seconds"
        ),
    )


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} keys must be strings")
    return value


def _list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be bool")
    return value
