"""Fail-closed identity binding for a future PAPER drift monitor.

This module deliberately does not evaluate drift. ``VerifiedBaselineEvidence``
and ``VerifiedDriftPolicy`` are trust-boundary values: production adapters must
obtain them from the native Strategy Package builder/verifier, native audit
signature verifier, factual PAPER event reader, and server-owned policy
registry. Comparing hashes in this module is only identity binding; it does not
authenticate package, audit, observation, or policy contents.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Protocol

MAX_DATASETS = 128
MAX_METRICS = 128
MAX_OBSERVATIONS = 50_000
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_NATIVE_STRATEGY_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_NATIVE_STRATEGY_VERSION_RE = re.compile(r"^\d+(?:\.\d+){0,2}(?:[-+][A-Za-z0-9.-]+)?$")
_POLICY_SCHEMA = "paper-drift-binding-v1"
_CHECK_SCHEMA = "count-window-v1"


class BindingStatus(StrEnum):
    """Identity-only outcomes; none of these is a drift-health verdict."""

    IDENTITY_BOUND = "identity_bound"
    INSUFFICIENT = "insufficient"
    UNAVAILABLE = "unavailable"


class BindingReason(StrEnum):
    BASELINE_UNAVAILABLE = "baseline_unavailable"
    BASELINE_UNVERIFIED = "baseline_unverified"
    POLICY_UNAVAILABLE = "policy_unavailable"
    POLICY_UNVERIFIED = "policy_unverified"
    POLICY_SCHEMA_UNSUPPORTED = "policy_schema_unsupported"
    CHECK_SCHEMA_UNSUPPORTED = "check_schema_unsupported"
    POLICY_SEMANTICS_MISMATCH = "policy_semantics_mismatch"
    BASELINE_POLICY_MISMATCH = "baseline_policy_mismatch"
    OBSERVATIONS_UNAVAILABLE = "observations_unavailable"
    OBSERVATIONS_UNVERIFIED = "observations_unverified"
    OBSERVATION_IDENTITY_MISMATCH = "observation_identity_mismatch"
    FUTURE_OBSERVATION = "future_observation"
    DUPLICATE_OBSERVATION = "duplicate_observation"
    POLICY_WINDOW_UNREPRESENTABLE = "policy_window_unrepresentable"
    INSUFFICIENT_SAMPLES = "insufficient_samples"


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a bounded stable identifier")
    return value


def _native_strategy_identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or _NATIVE_STRATEGY_IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must match the native strategy identifier contract")
    return value


def _native_strategy_version(value: object) -> str:
    if not isinstance(value, str) or _NATIVE_STRATEGY_VERSION_RE.fullmatch(value) is None:
        raise ValueError("strategy_version must match the native strategy version contract")
    return value


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _dataset_hashes(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple) or not values or len(values) > MAX_DATASETS:
        raise ValueError("dataset_hashes must be a non-empty bounded tuple")
    normalized = tuple(sorted(_digest(item, "dataset_hashes item") for item in values))
    if len(set(normalized)) != len(normalized):
        raise ValueError("dataset_hashes cannot contain duplicates")
    return normalized


@dataclass(frozen=True, slots=True)
class BaselineIdentity:
    """Exact accepted strategy, audit, dataset, assumptions, and runtime identity."""

    strategy_id: str
    strategy_version: str
    candidate_id: str
    package_hash: str
    configuration_hash: str
    audit_decision_hash: str
    assumptions_hash: str
    dataset_hashes: tuple[str, ...]
    runtime_id: str
    runtime_config_hash: str

    def __post_init__(self) -> None:
        for name in ("strategy_id", "candidate_id"):
            object.__setattr__(self, name, _native_strategy_identifier(getattr(self, name), name))
        object.__setattr__(
            self, "strategy_version", _native_strategy_version(self.strategy_version)
        )
        object.__setattr__(self, "runtime_id", _identifier(self.runtime_id, "runtime_id"))
        for name in (
            "package_hash",
            "configuration_hash",
            "audit_decision_hash",
            "assumptions_hash",
            "runtime_config_hash",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        object.__setattr__(self, "dataset_hashes", _dataset_hashes(self.dataset_hashes))

    def to_mapping(self) -> dict[str, object]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "candidate_id": self.candidate_id,
            "package_hash": self.package_hash,
            "configuration_hash": self.configuration_hash,
            "audit_decision_hash": self.audit_decision_hash,
            "assumptions_hash": self.assumptions_hash,
            "dataset_hashes": list(self.dataset_hashes),
            "runtime_id": self.runtime_id,
            "runtime_config_hash": self.runtime_config_hash,
        }

    @property
    def identity_hash(self) -> str:
        payload = json.dumps(self.to_mapping(), sort_keys=True, separators=(",", ":"))
        return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class VerifiedBaselineEvidence:
    """Identity emitted only after native package and audit verification."""

    identity: BaselineIdentity
    verifier_id: str
    verification_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, BaselineIdentity):
            raise TypeError("identity must be BaselineIdentity")
        object.__setattr__(self, "verifier_id", _identifier(self.verifier_id, "verifier_id"))
        object.__setattr__(
            self, "verification_ref", _digest(self.verification_ref, "verification_ref")
        )


class VerifiedBaselineReader(Protocol):
    """Read-only adapter over native package-builder and audit verification."""

    def read_verified_baseline(self, *, now: datetime) -> VerifiedBaselineEvidence | None: ...


@dataclass(frozen=True, slots=True)
class ObservationIdentity:
    """Owner-adapter verified identity attached to each factual PAPER observation."""

    baseline_identity_hash: str
    runtime_id: str
    runtime_config_hash: str
    dataset_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "baseline_identity_hash",
            _digest(self.baseline_identity_hash, "baseline_identity_hash"),
        )
        object.__setattr__(self, "runtime_id", _identifier(self.runtime_id, "runtime_id"))
        object.__setattr__(
            self, "runtime_config_hash", _digest(self.runtime_config_hash, "runtime_config_hash")
        )
        object.__setattr__(self, "dataset_hashes", _dataset_hashes(self.dataset_hashes))


@dataclass(frozen=True, slots=True)
class PaperObservation:
    """Bounded fact reference and timestamp; raw private payloads are not accepted."""

    event_id: str
    observed_at: datetime
    metric_id: str
    evidence_ref: str
    identity: ObservationIdentity

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _identifier(self.event_id, "event_id"))
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        object.__setattr__(self, "metric_id", _identifier(self.metric_id, "metric_id"))
        object.__setattr__(self, "evidence_ref", _digest(self.evidence_ref, "evidence_ref"))
        if not isinstance(self.identity, ObservationIdentity):
            raise TypeError("identity must be ObservationIdentity")


@dataclass(frozen=True, slots=True)
class VerifiedPaperObservationBatch:
    """Bounded event facts returned by a trusted factual PAPER adapter."""

    observations: tuple[PaperObservation, ...]
    source_id: str
    verification_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.observations, tuple):
            raise TypeError("observations must be an immutable tuple")
        if len(self.observations) > MAX_OBSERVATIONS:
            raise ValueError("observations exceeds bounded item limit")
        if any(not isinstance(item, PaperObservation) for item in self.observations):
            raise TypeError("observations must contain PaperObservation values")
        object.__setattr__(self, "source_id", _identifier(self.source_id, "source_id"))
        object.__setattr__(
            self, "verification_ref", _digest(self.verification_ref, "verification_ref")
        )


class VerifiedPaperObservationReader(Protocol):
    """Read-only adapter over the factual PAPER event owner."""

    def read_verified_observations(
        self, *, now: datetime
    ) -> VerifiedPaperObservationBatch | None: ...


@dataclass(frozen=True, slots=True)
class MetricWindowPolicy:
    """Server-owned sampling semantics; no statistical threshold is inferred here."""

    metric_id: str
    window_seconds: int
    minimum_samples: int
    evidence_semantics_id: str
    false_alarm_policy_id: str
    check_schema: str = _CHECK_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "metric_id", _identifier(self.metric_id, "metric_id"))
        for name in ("window_seconds", "minimum_samples"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive explicit policy value")
        object.__setattr__(
            self,
            "evidence_semantics_id",
            _identifier(self.evidence_semantics_id, "evidence_semantics_id"),
        )
        object.__setattr__(
            self,
            "false_alarm_policy_id",
            _identifier(self.false_alarm_policy_id, "false_alarm_policy_id"),
        )
        object.__setattr__(self, "check_schema", _identifier(self.check_schema, "check_schema"))


@dataclass(frozen=True, slots=True)
class DriftPolicyIdentity:
    """Versioned, server-owned policy identity tied to one baseline."""

    policy_id: str
    version: str
    policy_hash: str
    schema: str
    baseline_identity_hash: str
    evidence_semantics_id: str
    false_alarm_policy_id: str
    metric_windows: tuple[MetricWindowPolicy, ...]

    def __post_init__(self) -> None:
        for name in (
            "policy_id",
            "version",
            "schema",
            "evidence_semantics_id",
            "false_alarm_policy_id",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        object.__setattr__(self, "policy_hash", _digest(self.policy_hash, "policy_hash"))
        object.__setattr__(
            self,
            "baseline_identity_hash",
            _digest(self.baseline_identity_hash, "baseline_identity_hash"),
        )
        if (
            not isinstance(self.metric_windows, tuple)
            or not self.metric_windows
            or len(self.metric_windows) > MAX_METRICS
        ):
            raise ValueError("metric_windows must be a non-empty bounded tuple")
        if any(not isinstance(item, MetricWindowPolicy) for item in self.metric_windows):
            raise TypeError("metric_windows must contain MetricWindowPolicy values")
        ordered = tuple(sorted(self.metric_windows, key=lambda item: item.metric_id))
        if len({item.metric_id for item in ordered}) != len(ordered):
            raise ValueError("metric_windows metric_id values must be unique")
        object.__setattr__(self, "metric_windows", ordered)


@dataclass(frozen=True, slots=True)
class VerifiedDriftPolicy:
    """Policy emitted by the trusted server-owned policy registry."""

    identity: DriftPolicyIdentity
    registry_id: str
    verification_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, DriftPolicyIdentity):
            raise TypeError("identity must be DriftPolicyIdentity")
        object.__setattr__(self, "registry_id", _identifier(self.registry_id, "registry_id"))
        object.__setattr__(
            self, "verification_ref", _digest(self.verification_ref, "verification_ref")
        )


class VerifiedDriftPolicyReader(Protocol):
    """Read-only adapter over the server-owned drift-policy registry."""

    def read_verified_policy(self, *, now: datetime) -> VerifiedDriftPolicy | None: ...


@dataclass(frozen=True, slots=True)
class BindingReport:
    """Deterministic identity/sample contract result, not a drift score or verdict."""

    evaluated_at: datetime
    status: BindingStatus
    reason_codes: tuple[BindingReason, ...]
    baseline_identity_hash: str | None
    policy_hash: str | None
    observation_batch_hash: str | None
    sample_counts: tuple[tuple[str, int], ...]
    report_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "evaluated_at", _utc(self.evaluated_at, "evaluated_at"))
        if not isinstance(self.status, BindingStatus):
            raise TypeError("status must be BindingStatus")
        if not isinstance(self.reason_codes, tuple) or any(
            not isinstance(item, BindingReason) for item in self.reason_codes
        ):
            raise TypeError("reason_codes must be an immutable tuple of BindingReason values")
        if tuple(sorted(set(self.reason_codes), key=lambda item: item.value)) != self.reason_codes:
            raise ValueError("reason_codes must be sorted and unique")
        if self.baseline_identity_hash is not None:
            _digest(self.baseline_identity_hash, "baseline_identity_hash")
        if self.policy_hash is not None:
            _digest(self.policy_hash, "policy_hash")
        if self.observation_batch_hash is not None:
            _digest(self.observation_batch_hash, "observation_batch_hash")
        if not isinstance(self.sample_counts, tuple) or len(self.sample_counts) > MAX_METRICS:
            raise ValueError("sample_counts must be an immutable bounded tuple")
        normalized_counts: list[tuple[str, int]] = []
        for entry in self.sample_counts:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise TypeError("sample_counts entries must be (metric_id, count) tuples")
            metric_id, count = entry
            normalized_counts.append((_identifier(metric_id, "sample_counts metric_id"), count))
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or not 0 <= count <= MAX_OBSERVATIONS
            ):
                raise ValueError("sample_counts counts must be bounded non-negative integers")
        if len({metric_id for metric_id, _ in normalized_counts}) != len(normalized_counts):
            raise ValueError("sample_counts metric_id values must be unique")
        if tuple(sorted(normalized_counts)) != self.sample_counts:
            raise ValueError("sample_counts must be sorted by unique metric_id")
        expected = _binding_report_hash(
            evaluated_at=self.evaluated_at,
            status=self.status,
            reason_codes=self.reason_codes,
            baseline_identity_hash=self.baseline_identity_hash,
            policy_hash=self.policy_hash,
            observation_batch_hash=self.observation_batch_hash,
            sample_counts=self.sample_counts,
        )
        if _digest(self.report_hash, "report_hash") != expected:
            raise ValueError("report_hash does not match canonical report fields")


def _binding_report_payload(
    *,
    evaluated_at: datetime,
    status: BindingStatus,
    reason_codes: tuple[BindingReason, ...],
    baseline_identity_hash: str | None,
    policy_hash: str | None,
    observation_batch_hash: str | None,
    sample_counts: tuple[tuple[str, int], ...],
) -> dict[str, object]:
    return {
        "evaluated_at": _utc(evaluated_at, "evaluated_at").isoformat(),
        "status": status.value,
        "reason_codes": [item.value for item in reason_codes],
        "baseline_identity_hash": baseline_identity_hash,
        "policy_hash": policy_hash,
        "observation_batch_hash": observation_batch_hash,
        "sample_counts": [[metric, count] for metric, count in sample_counts],
    }


def _binding_report_hash(
    *,
    evaluated_at: datetime,
    status: BindingStatus,
    reason_codes: tuple[BindingReason, ...],
    baseline_identity_hash: str | None,
    policy_hash: str | None,
    observation_batch_hash: str | None,
    sample_counts: tuple[tuple[str, int], ...],
) -> str:
    payload = _binding_report_payload(
        evaluated_at=evaluated_at,
        status=status,
        reason_codes=reason_codes,
        baseline_identity_hash=baseline_identity_hash,
        policy_hash=policy_hash,
        observation_batch_hash=observation_batch_hash,
        sample_counts=sample_counts,
    )
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def bind_drift_baseline(
    *,
    now: datetime,
    baseline: VerifiedBaselineEvidence | None,
    policy: VerifiedDriftPolicy | None,
    observations: VerifiedPaperObservationBatch | None,
) -> BindingReport:
    """Validate verified identities and bounded sample availability without scoring drift.

    The inclusive window is ``[now - window_seconds, now]``. Observations after
    ``now`` fail closed, even when they would otherwise be outside a window.
    """

    moment = _utc(now, "now")
    status = BindingStatus.UNAVAILABLE
    reasons: set[BindingReason] = set()
    counts: dict[str, int] = {}
    baseline_hash: str | None = None
    policy_hash: str | None = None
    observation_batch_hash: str | None = None

    if baseline is None:
        reasons.add(BindingReason.BASELINE_UNAVAILABLE)
    elif not isinstance(baseline, VerifiedBaselineEvidence):
        reasons.add(BindingReason.BASELINE_UNVERIFIED)
    else:
        baseline_hash = baseline.identity.identity_hash

    if policy is None:
        reasons.add(BindingReason.POLICY_UNAVAILABLE)
    elif not isinstance(policy, VerifiedDriftPolicy):
        reasons.add(BindingReason.POLICY_UNVERIFIED)
    else:
        policy_hash = policy.identity.policy_hash
        if policy.identity.schema != _POLICY_SCHEMA:
            reasons.add(BindingReason.POLICY_SCHEMA_UNSUPPORTED)
        if any(check.check_schema != _CHECK_SCHEMA for check in policy.identity.metric_windows):
            reasons.add(BindingReason.CHECK_SCHEMA_UNSUPPORTED)
        if any(
            check.evidence_semantics_id != policy.identity.evidence_semantics_id
            or check.false_alarm_policy_id != policy.identity.false_alarm_policy_id
            for check in policy.identity.metric_windows
        ):
            reasons.add(BindingReason.POLICY_SEMANTICS_MISMATCH)

    if isinstance(baseline, VerifiedBaselineEvidence) and isinstance(policy, VerifiedDriftPolicy):
        if baseline.identity.identity_hash != policy.identity.baseline_identity_hash:
            reasons.add(BindingReason.BASELINE_POLICY_MISMATCH)

    if observations is None:
        reasons.add(BindingReason.OBSERVATIONS_UNAVAILABLE)
    elif not isinstance(observations, VerifiedPaperObservationBatch):
        reasons.add(BindingReason.OBSERVATIONS_UNVERIFIED)
    else:
        observation_rows = [
            {
                "event_id": item.event_id,
                "observed_at": item.observed_at.isoformat(),
                "metric_id": item.metric_id,
                "evidence_ref": item.evidence_ref,
                "identity": {
                    "baseline_identity_hash": item.identity.baseline_identity_hash,
                    "runtime_id": item.identity.runtime_id,
                    "runtime_config_hash": item.identity.runtime_config_hash,
                    "dataset_hashes": list(item.identity.dataset_hashes),
                },
            }
            for item in observations.observations
        ]
        observation_rows.sort(
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"))
        )
        observation_batch_payload = {
            "source_id": observations.source_id,
            "verification_ref": observations.verification_ref,
            "observations": observation_rows,
        }
        observation_batch_hash = sha256(
            json.dumps(
                observation_batch_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    if (
        isinstance(baseline, VerifiedBaselineEvidence)
        and isinstance(policy, VerifiedDriftPolicy)
        and isinstance(observations, VerifiedPaperObservationBatch)
        and not reasons
    ):
        expected = baseline.identity
        event_ids: set[str] = set()
        for observation in observations.observations:
            if observation.event_id in event_ids:
                reasons.add(BindingReason.DUPLICATE_OBSERVATION)
            else:
                event_ids.add(observation.event_id)
            if observation.observed_at > moment:
                reasons.add(BindingReason.FUTURE_OBSERVATION)
            identity = observation.identity
            if (
                identity.baseline_identity_hash != expected.identity_hash
                or identity.runtime_id != expected.runtime_id
                or identity.runtime_config_hash != expected.runtime_config_hash
                or identity.dataset_hashes != expected.dataset_hashes
            ):
                reasons.add(BindingReason.OBSERVATION_IDENTITY_MISMATCH)

        if not reasons:
            window_starts: dict[str, datetime] = {}
            for check in policy.identity.metric_windows:
                try:
                    window_starts[check.metric_id] = moment - timedelta(
                        seconds=check.window_seconds
                    )
                except OverflowError:
                    reasons.add(BindingReason.POLICY_WINDOW_UNREPRESENTABLE)
            if not reasons:
                for check in policy.identity.metric_windows:
                    start = window_starts[check.metric_id]
                    counts[check.metric_id] = sum(
                        1
                        for observation in observations.observations
                        if observation.metric_id == check.metric_id
                        and start <= observation.observed_at <= moment
                    )
            if reasons:
                counts.clear()
            elif any(
                counts[check.metric_id] < check.minimum_samples
                for check in policy.identity.metric_windows
            ):
                reasons.add(BindingReason.INSUFFICIENT_SAMPLES)
                status = BindingStatus.INSUFFICIENT
            else:
                status = BindingStatus.IDENTITY_BOUND

    ordered_reasons = tuple(sorted(reasons, key=lambda item: item.value))
    sample_counts = tuple(sorted(counts.items()))
    report_digest = _binding_report_hash(
        evaluated_at=moment,
        status=status,
        reason_codes=ordered_reasons,
        baseline_identity_hash=baseline_hash,
        policy_hash=policy_hash,
        observation_batch_hash=observation_batch_hash,
        sample_counts=sample_counts,
    )
    return BindingReport(
        evaluated_at=moment,
        status=status,
        reason_codes=ordered_reasons,
        baseline_identity_hash=baseline_hash,
        policy_hash=policy_hash,
        observation_batch_hash=observation_batch_hash,
        sample_counts=sample_counts,
        report_hash=report_digest,
    )
