from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from modular_observability import (
    BaselineIdentity,
    BindingReason,
    BindingReport,
    BindingStatus,
    DriftPolicyIdentity,
    MetricWindowPolicy,
    ObservationIdentity,
    PaperObservation,
    VerifiedBaselineEvidence,
    VerifiedDriftPolicy,
    VerifiedPaperObservationBatch,
    bind_drift_baseline,
)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
DATASET_A = "a" * 64
DATASET_B = "b" * 64


def _digest(text: str) -> str:
    return sha256(text.encode()).hexdigest()


def _baseline_identity() -> BaselineIdentity:
    return BaselineIdentity(
        strategy_id="mean-reversion/spot",
        strategy_version="3.0.0+build.7",
        candidate_id="candidate:17/accepted",
        package_hash=_digest("package"),
        configuration_hash=_digest("configuration"),
        audit_decision_hash=_digest("audit-decision"),
        assumptions_hash=_digest("assumptions"),
        dataset_hashes=(DATASET_B, DATASET_A),
        runtime_id="paper-runtime-17",
        runtime_config_hash=_digest("runtime-config"),
    )


def _verified_baseline(identity: BaselineIdentity | None = None) -> VerifiedBaselineEvidence:
    return VerifiedBaselineEvidence(
        identity=identity or _baseline_identity(),
        verifier_id="native-package-audit-adapter-v1",
        verification_ref=_digest("verification-reference"),
    )


def _policy(
    baseline: VerifiedBaselineEvidence | None = None,
    *,
    schema: str = "paper-drift-binding-v1",
    check_schema: str = "count-window-v1",
    window_seconds: int = 3600,
    minimum_samples: int = 2,
) -> VerifiedDriftPolicy:
    baseline_evidence = baseline or _verified_baseline()
    identity = DriftPolicyIdentity(
        policy_id="paper-drift-policy",
        version="2026-10",
        policy_hash=_digest("server-policy"),
        schema=schema,
        baseline_identity_hash=baseline_evidence.identity.identity_hash,
        evidence_semantics_id="accepted-paper-events-v1",
        false_alarm_policy_id="reviewed-false-alarm-policy-v3",
        metric_windows=(
            MetricWindowPolicy(
                metric_id="fill_rate",
                window_seconds=window_seconds,
                minimum_samples=minimum_samples,
                evidence_semantics_id="accepted-paper-events-v1",
                false_alarm_policy_id="reviewed-false-alarm-policy-v3",
                check_schema=check_schema,
            ),
        ),
    )
    return VerifiedDriftPolicy(
        identity=identity,
        registry_id="server-policy-registry",
        verification_ref=_digest("policy-registry-verification"),
    )


def _observation(
    baseline: VerifiedBaselineEvidence | None = None,
    *,
    event_id: str = "paper-event-1",
    at: datetime = NOW,
    metric_id: str = "fill_rate",
    runtime_id: str | None = None,
    evidence_ref: str | None = None,
) -> PaperObservation:
    identity = (baseline or _verified_baseline()).identity
    return PaperObservation(
        event_id=event_id,
        observed_at=at,
        metric_id=metric_id,
        evidence_ref=evidence_ref or _digest(event_id),
        identity=ObservationIdentity(
            baseline_identity_hash=identity.identity_hash,
            runtime_id=runtime_id or identity.runtime_id,
            runtime_config_hash=identity.runtime_config_hash,
            dataset_hashes=identity.dataset_hashes,
        ),
    )


def _batch(*observations: PaperObservation) -> VerifiedPaperObservationBatch:
    return VerifiedPaperObservationBatch(
        observations=tuple(observations),
        source_id="synthetic-paper-event-adapter-v1",
        verification_ref=_digest("synthetic-observation-batch-verification"),
    )


class SyntheticNativeOwnerAdapter:
    """Synthetic native-shaped package/audit verifier, not a production adapter."""

    def __init__(
        self, *, package_tampered: bool = False, audit_signature_valid: bool = True
    ) -> None:
        self.write_count = 0
        self.package_payload = {
            "strategy_id": "mean-reversion/spot",
            "strategy_version": "3.0.0+build.7",
            "candidate_id": "candidate:17/accepted",
            "configuration_hash": _digest("configuration"),
            "assumptions_hash": _digest("assumptions"),
            "dataset_hashes": [DATASET_A, DATASET_B],
            "runtime_id": "paper-runtime-17",
            "runtime_config_hash": _digest("runtime-config"),
        }
        self.package_hash = _digest(
            json.dumps(self.package_payload, sort_keys=True, separators=(",", ":"))
        )
        self.audit_payload = {
            "candidate_id": "candidate:17/accepted",
            "package_hash": self.package_hash,
            "outcome": "accepted",
        }
        self.audit_decision_hash = _digest(
            json.dumps(self.audit_payload, sort_keys=True, separators=(",", ":"))
        )
        self.stored_package_hash = (
            _digest("tampered-package") if package_tampered else self.package_hash
        )
        self.audit_signature_valid = audit_signature_valid

    def read_verified_baseline(self, *, now: datetime) -> VerifiedBaselineEvidence | None:
        assert now.tzinfo is not None
        if self.stored_package_hash != self.package_hash or not self.audit_signature_valid:
            return None
        if (
            self.audit_payload["candidate_id"] != self.package_payload["candidate_id"]
            or self.audit_payload["package_hash"] != self.package_hash
        ):
            return None
        identity = BaselineIdentity(
            strategy_id=str(self.package_payload["strategy_id"]),
            strategy_version=str(self.package_payload["strategy_version"]),
            candidate_id=str(self.package_payload["candidate_id"]),
            package_hash=self.package_hash,
            configuration_hash=str(self.package_payload["configuration_hash"]),
            audit_decision_hash=self.audit_decision_hash,
            assumptions_hash=str(self.package_payload["assumptions_hash"]),
            dataset_hashes=(DATASET_A, DATASET_B),
            runtime_id=str(self.package_payload["runtime_id"]),
            runtime_config_hash=str(self.package_payload["runtime_config_hash"]),
        )
        return VerifiedBaselineEvidence(
            identity=identity,
            verifier_id="synthetic-native-package-audit-adapter-v1",
            verification_ref=_digest("synthetic-verification-reference"),
        )


def test_exact_native_identity_replay_is_canonical_and_read_only() -> None:
    owner = SyntheticNativeOwnerAdapter()
    baseline = owner.read_verified_baseline(now=NOW)
    assert baseline is not None
    policy = _policy(baseline)
    observations = (
        _observation(baseline, event_id="event-b"),
        _observation(baseline, event_id="event-a"),
    )
    verified_observations = _batch(*observations)

    first = bind_drift_baseline(
        now=NOW, baseline=baseline, policy=policy, observations=verified_observations
    )
    replay = bind_drift_baseline(
        now=NOW, baseline=baseline, policy=policy, observations=verified_observations
    )
    reordered = bind_drift_baseline(
        now=NOW,
        baseline=baseline,
        policy=policy,
        observations=_batch(*reversed(observations)),
    )
    changed_facts = bind_drift_baseline(
        now=NOW,
        baseline=baseline,
        policy=policy,
        observations=_batch(
            _observation(baseline, event_id="event-b", evidence_ref=_digest("changed-b")),
            _observation(baseline, event_id="event-a"),
        ),
    )

    assert first.status is BindingStatus.IDENTITY_BOUND
    assert first.reason_codes == ()
    assert first.sample_counts == (("fill_rate", 2),)
    assert first.report_hash == replay.report_hash
    assert first.report_hash == reordered.report_hash
    assert first.observation_batch_hash != changed_facts.observation_batch_hash
    assert first.report_hash != changed_facts.report_hash
    assert baseline.identity.dataset_hashes == (DATASET_A, DATASET_B)
    assert owner.write_count == 0


@pytest.mark.parametrize(
    "owner",
    [
        SyntheticNativeOwnerAdapter(package_tampered=True),
        SyntheticNativeOwnerAdapter(audit_signature_valid=False),
    ],
)
def test_synthetic_native_verifier_fails_closed_before_identity_binding(
    owner: SyntheticNativeOwnerAdapter,
) -> None:
    baseline = owner.read_verified_baseline(now=NOW)

    report = bind_drift_baseline(
        now=NOW,
        baseline=baseline,
        policy=_policy(),
        observations=_batch(),
    )

    assert baseline is None
    assert report.status is BindingStatus.UNAVAILABLE
    assert BindingReason.BASELINE_UNAVAILABLE in report.reason_codes
    assert owner.write_count == 0


def test_identity_hash_is_stable_under_dataset_order_and_changes_on_identity_change() -> None:
    identity = _baseline_identity()
    reversed_identity = BaselineIdentity(
        **{
            **identity.to_mapping(),
            "dataset_hashes": tuple(reversed(identity.dataset_hashes)),
        }
    )

    assert identity.identity_hash == reversed_identity.identity_hash
    changed = replace(_baseline_identity(), package_hash=_digest("different-package"))
    assert changed.identity_hash != identity.identity_hash


def test_native_metadata_and_namespaced_identity_values_replay_exactly() -> None:
    owner = SyntheticNativeOwnerAdapter()
    evidence = owner.read_verified_baseline(now=NOW)

    assert evidence is not None
    assert evidence.identity.strategy_id == "mean-reversion/spot"
    assert evidence.identity.strategy_version == "3.0.0+build.7"
    assert evidence.identity.candidate_id == "candidate:17/accepted"
    replay_values = evidence.identity.to_mapping()
    replay_values["dataset_hashes"] = tuple(replay_values["dataset_hashes"])
    replay = BaselineIdentity(**replay_values)
    assert replay.to_mapping() == evidence.identity.to_mapping()
    assert replay.identity_hash == evidence.identity.identity_hash


@pytest.mark.parametrize("version", ["v3", "3.0.0+bad value", ""])
def test_baseline_rejects_versions_outside_native_contract(version: str) -> None:
    with pytest.raises(ValueError, match="native strategy version contract"):
        replace(_baseline_identity(), strategy_version=version)


@pytest.mark.parametrize("identifier", ["/leading", "has space", ""])
def test_baseline_rejects_ids_outside_native_contract(identifier: str) -> None:
    with pytest.raises(ValueError, match="native strategy identifier contract"):
        replace(_baseline_identity(), candidate_id=identifier)


def test_non_baseline_identifiers_keep_their_stricter_contract() -> None:
    with pytest.raises(ValueError, match="bounded stable identifier"):
        MetricWindowPolicy(
            metric_id="fill/rate",
            window_seconds=3600,
            minimum_samples=1,
            evidence_semantics_id="paper-events-v1",
            false_alarm_policy_id="false-alarm-v1",
        )


def test_observation_identity_mismatch_fails_closed() -> None:
    baseline = _verified_baseline()
    policy = _policy(baseline)

    report = bind_drift_baseline(
        now=NOW,
        baseline=baseline,
        policy=policy,
        observations=_batch(_observation(baseline, runtime_id="other-runtime")),
    )

    assert report.status is BindingStatus.UNAVAILABLE
    assert report.reason_codes == (BindingReason.OBSERVATION_IDENTITY_MISMATCH,)


def test_policy_bound_to_different_baseline_fails_closed() -> None:
    baseline = _verified_baseline()
    other_baseline = _verified_baseline(
        replace(baseline.identity, package_hash=_digest("other-package"))
    )
    report = bind_drift_baseline(
        now=NOW,
        baseline=baseline,
        policy=_policy(other_baseline),
        observations=_batch(_observation(baseline)),
    )

    assert report.status is BindingStatus.UNAVAILABLE
    assert report.reason_codes == (BindingReason.BASELINE_POLICY_MISMATCH,)


def test_missing_and_unverified_owner_evidence_fail_closed() -> None:
    missing = bind_drift_baseline(now=NOW, baseline=None, policy=None, observations=None)
    raw_claim = object()
    unverified = bind_drift_baseline(  # type: ignore[arg-type]
        now=NOW,
        baseline=raw_claim,
        policy=_policy(),
        observations=_batch(),
    )

    assert missing.status is BindingStatus.UNAVAILABLE
    assert set(missing.reason_codes) == {
        BindingReason.BASELINE_UNAVAILABLE,
        BindingReason.OBSERVATIONS_UNAVAILABLE,
        BindingReason.POLICY_UNAVAILABLE,
    }
    assert unverified.status is BindingStatus.UNAVAILABLE
    assert BindingReason.BASELINE_UNVERIFIED in unverified.reason_codes

    unverified_policy = bind_drift_baseline(  # type: ignore[arg-type]
        now=NOW,
        baseline=_verified_baseline(),
        policy=object(),
        observations=_batch(),
    )
    assert unverified_policy.status is BindingStatus.UNAVAILABLE
    assert unverified_policy.reason_codes == (BindingReason.POLICY_UNVERIFIED,)

    unverified_observations = bind_drift_baseline(  # type: ignore[arg-type]
        now=NOW,
        baseline=_verified_baseline(),
        policy=_policy(),
        observations=(),
    )
    assert unverified_observations.status is BindingStatus.UNAVAILABLE
    assert unverified_observations.reason_codes == (BindingReason.OBSERVATIONS_UNVERIFIED,)


def test_policy_sampling_semantics_must_match_policy_wide_evidence_and_false_alarm_ids() -> None:
    baseline = _verified_baseline()
    policy = _policy(baseline)
    check = replace(policy.identity.metric_windows[0], false_alarm_policy_id="other-false-alarm-v1")
    mismatched_identity = replace(policy.identity, metric_windows=(check,))
    mismatched_policy = replace(policy, identity=mismatched_identity)

    report = bind_drift_baseline(
        now=NOW,
        baseline=baseline,
        policy=mismatched_policy,
        observations=_batch(_observation(baseline), _observation(baseline, event_id="event-2")),
    )

    assert report.status is BindingStatus.UNAVAILABLE
    assert report.reason_codes == (BindingReason.POLICY_SEMANTICS_MISMATCH,)


@pytest.mark.parametrize(
    ("schema", "check_schema", "reason"),
    [
        ("future-policy-schema", "count-window-v1", BindingReason.POLICY_SCHEMA_UNSUPPORTED),
        ("paper-drift-binding-v1", "unreviewed-check", BindingReason.CHECK_SCHEMA_UNSUPPORTED),
    ],
)
def test_unsupported_policy_and_check_schemas_fail_closed(
    schema: str,
    check_schema: str,
    reason: BindingReason,
) -> None:
    baseline = _verified_baseline()
    report = bind_drift_baseline(
        now=NOW,
        baseline=baseline,
        policy=_policy(baseline, schema=schema, check_schema=check_schema),
        observations=_batch(
            _observation(baseline), _observation(baseline, event_id="paper-event-2")
        ),
    )

    assert report.status is BindingStatus.UNAVAILABLE
    assert report.reason_codes == (reason,)


def test_low_sample_count_is_insufficient_not_a_drift_verdict() -> None:
    baseline = _verified_baseline()
    report = bind_drift_baseline(
        now=NOW,
        baseline=baseline,
        policy=_policy(baseline, minimum_samples=2),
        observations=_batch(_observation(baseline)),
    )

    assert report.status is BindingStatus.INSUFFICIENT
    assert report.reason_codes == (BindingReason.INSUFFICIENT_SAMPLES,)


def test_window_start_and_now_are_inclusive_but_older_observation_is_excluded() -> None:
    baseline = _verified_baseline()
    policy = _policy(baseline, minimum_samples=2)
    observations = (
        _observation(baseline, event_id="at-start", at=NOW - timedelta(seconds=3600)),
        _observation(baseline, event_id="at-now", at=NOW),
        _observation(baseline, event_id="too-old", at=NOW - timedelta(seconds=3601)),
    )

    report = bind_drift_baseline(
        now=NOW, baseline=baseline, policy=policy, observations=_batch(*observations)
    )

    assert report.status is BindingStatus.IDENTITY_BOUND
    assert report.sample_counts == (("fill_rate", 2),)


def test_future_observation_fails_closed_even_if_not_in_requested_metric_window() -> None:
    baseline = _verified_baseline()

    report = bind_drift_baseline(
        now=NOW,
        baseline=baseline,
        policy=_policy(baseline),
        observations=_batch(_observation(baseline, at=NOW + timedelta(microseconds=1))),
    )

    assert report.status is BindingStatus.UNAVAILABLE
    assert report.reason_codes == (BindingReason.FUTURE_OBSERVATION,)


def test_invalid_observation_reasons_and_report_hash_are_order_independent() -> None:
    baseline = _verified_baseline()
    faulty = _observation(
        baseline,
        event_id="duplicate",
        at=NOW + timedelta(seconds=1),
        runtime_id="other-runtime",
    )
    matching = _observation(baseline, event_id="duplicate")

    first = bind_drift_baseline(
        now=NOW,
        baseline=baseline,
        policy=_policy(baseline),
        observations=_batch(faulty, matching),
    )
    reordered = bind_drift_baseline(
        now=NOW,
        baseline=baseline,
        policy=_policy(baseline),
        observations=_batch(matching, faulty),
    )

    assert first.status is BindingStatus.UNAVAILABLE
    assert first.reason_codes == (
        BindingReason.DUPLICATE_OBSERVATION,
        BindingReason.FUTURE_OBSERVATION,
        BindingReason.OBSERVATION_IDENTITY_MISMATCH,
    )
    assert first.observation_batch_hash == reordered.observation_batch_hash
    assert first.report_hash == reordered.report_hash


@pytest.mark.parametrize(
    ("now", "window_seconds"),
    [
        (NOW, 10**100),
        (datetime.min.replace(tzinfo=UTC), 1),
    ],
)
def test_unrepresentable_policy_window_fails_closed_without_overflow(
    now: datetime, window_seconds: int
) -> None:
    baseline = _verified_baseline()
    report = bind_drift_baseline(
        now=now,
        baseline=baseline,
        policy=_policy(baseline, window_seconds=window_seconds, minimum_samples=1),
        observations=_batch(_observation(baseline, at=now)),
    )

    assert report.status is BindingStatus.UNAVAILABLE
    assert report.reason_codes == (BindingReason.POLICY_WINDOW_UNREPRESENTABLE,)
    assert report.sample_counts == ()


def test_binding_report_rejects_malformed_counts_and_noncanonical_digest() -> None:
    baseline = _verified_baseline()
    report = bind_drift_baseline(
        now=NOW,
        baseline=baseline,
        policy=_policy(baseline),
        observations=_batch(_observation(baseline), _observation(baseline, event_id="event-2")),
    )

    with pytest.raises(ValueError, match="non-negative integers"):
        replace(report, sample_counts=(("fill_rate", -1),))
    with pytest.raises(ValueError, match="non-negative integers"):
        replace(report, sample_counts=(("fill_rate", True),))
    with pytest.raises(ValueError, match="bounded non-negative integers"):
        replace(report, sample_counts=(("fill_rate", 50_001),))
    with pytest.raises(ValueError, match="must be unique"):
        replace(report, sample_counts=(("fill_rate", 1), ("fill_rate", 2)))
    with pytest.raises(ValueError, match="does not match canonical"):
        replace(report, report_hash="f" * 64)

    with pytest.raises(ValueError, match="does not match canonical"):
        BindingReport(
            evaluated_at=report.evaluated_at,
            status=report.status,
            reason_codes=report.reason_codes,
            baseline_identity_hash=report.baseline_identity_hash,
            policy_hash=report.policy_hash,
            observation_batch_hash=report.observation_batch_hash,
            sample_counts=report.sample_counts,
            report_hash="f" * 64,
        )


def test_duplicate_event_and_bounded_inputs_are_rejected() -> None:
    baseline = _verified_baseline()
    duplicate = _observation(baseline, event_id="same-event")
    duplicate_report = bind_drift_baseline(
        now=NOW,
        baseline=baseline,
        policy=_policy(baseline),
        observations=_batch(duplicate, duplicate),
    )

    assert duplicate_report.status is BindingStatus.UNAVAILABLE
    assert duplicate_report.reason_codes == (BindingReason.DUPLICATE_OBSERVATION,)
    with pytest.raises(ValueError, match="bounded item limit"):
        VerifiedPaperObservationBatch(
            observations=tuple(duplicate for _ in range(50_001)),
            source_id="synthetic-paper-event-adapter-v1",
            verification_ref=_digest("oversized-batch"),
        )
    with pytest.raises(FrozenInstanceError):
        duplicate.event_id = "mutated"  # type: ignore[misc]


def test_timezone_is_required_and_policy_sample_values_are_explicit() -> None:
    baseline = _verified_baseline()
    naive = datetime(2026, 10, 16, 12, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        _observation(baseline, at=naive)
    with pytest.raises(ValueError, match="minimum_samples"):
        MetricWindowPolicy(
            metric_id="fill_rate",
            window_seconds=3600,
            minimum_samples=0,
            evidence_semantics_id="accepted-paper-events-v1",
            false_alarm_policy_id="reviewed-false-alarm-policy-v3",
        )
