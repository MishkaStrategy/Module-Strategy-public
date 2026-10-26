"""Reference-only protected admission issuer boundary.

The controller composes trusted input loading, an explicitly authenticated
provider adapter, deterministic workload planning and the local one-use ledger.
It never dispatches work and does not establish production trust. For exact
reference replay, the local ledger stores the returned bearer-grant bundle;
production must replace this with a protected, access-controlled response store.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field as dataclass_field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from uuid import uuid4

from scripts.external_admission_ledger import (
    CANONICAL_REPOSITORY,
    Admission,
    AdmissionConflict,
    AdmissionExpired,
    AdmissionLedger,
    NetworkPolicy,
    ResourcePolicy,
    admission_digest,
)
from scripts.fixed_workload_planner import AdmissionPlan, PlannerError, plan_fixed_workloads

MAX_CANDIDATE_BYTES = 2_000_000
MAX_PROFILE_BYTES = 1_000_000
MAX_INVENTORY_BYTES = 4_000_000
MAX_PLAN_BYTES = 8_000_000
MAX_WORKLOADS = 256
MAX_GRANT_BYTES = 16_384
MAX_GRANT_ENVELOPE_BYTES = 12_288
MAX_GRANT_PAYLOAD_BYTES = 8_192


class ControllerAdmissionError(ValueError):
    """Admission was denied before a grant or claim could be created."""


@dataclass(frozen=True, slots=True)
class ProtectedControllerPolicy:
    schema_version: int
    policy_id: str
    repository: str
    provider_id: str
    workflow_id: str
    workflow_digest: str
    event: str
    ref: str
    audience: str
    grant_ttl_seconds: int
    resource_policy_id: str
    resource_policy_digest: str
    network_policy_id: str
    network_policy_digest: str
    resource_policy: ResourcePolicy
    network_policy: NetworkPolicy

    def __post_init__(self) -> None:
        if self.schema_version != 1 or isinstance(self.schema_version, bool):
            raise ControllerAdmissionError("policy schema_version must be 1")
        for name in (
            "policy_id",
            "provider_id",
            "workflow_id",
            "event",
            "ref",
            "audience",
            "resource_policy_id",
            "network_policy_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 256:
                raise ControllerAdmissionError(f"policy {name} must be bounded nonempty text")
        if self.repository != CANONICAL_REPOSITORY:
            raise ControllerAdmissionError("policy repository is not the pinned repository")
        if len(self.workflow_digest) != 64 or any(
            c not in "0123456789abcdef" for c in self.workflow_digest
        ):
            raise ControllerAdmissionError("policy workflow_digest must be lowercase SHA-256")
        if isinstance(self.grant_ttl_seconds, bool) or not isinstance(self.grant_ttl_seconds, int):
            raise ControllerAdmissionError("policy grant TTL must be an integer")
        if not 1 <= self.grant_ttl_seconds <= 3600:
            raise ControllerAdmissionError("policy grant TTL is outside the allowed bound")
        if not isinstance(self.resource_policy, ResourcePolicy) or not isinstance(
            self.network_policy, NetworkPolicy
        ):
            raise ControllerAdmissionError(
                "policy resource and network limits must use closed policy types"
            )
        _digest_text(self.resource_policy_digest, "policy resource digest")
        _digest_text(self.network_policy_digest, "policy network digest")
        if _sha(_canonical(self.resource_policy.to_mapping())) != self.resource_policy_digest:
            raise ControllerAdmissionError(
                "resource policy digest does not match its closed policy"
            )
        if _sha(_canonical(self.network_policy.to_mapping())) != self.network_policy_digest:
            raise ControllerAdmissionError("network policy digest does not match its closed policy")

    @classmethod
    def from_mapping(cls, value: Any) -> ProtectedControllerPolicy:
        fields = {
            "schema_version",
            "policy_id",
            "repository",
            "provider_id",
            "workflow_id",
            "workflow_digest",
            "event",
            "ref",
            "audience",
            "grant_ttl_seconds",
            "resource_policy_id",
            "resource_policy_digest",
            "network_policy_id",
            "network_policy_digest",
            "resource_policy",
            "network_policy",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ControllerAdmissionError("protected policy has missing or unknown fields")
        resource = value["resource_policy"]
        network = value["network_policy"]
        if not isinstance(resource, dict) or set(resource) != {
            "cpu_millis",
            "memory_bytes",
            "storage_bytes",
            "process_limit",
            "execution_timeout_seconds",
        }:
            raise ControllerAdmissionError("protected resource policy shape is invalid")
        if not isinstance(network, dict) or set(network) != {"mode", "allowed_destinations"}:
            raise ControllerAdmissionError("protected network policy shape is invalid")
        try:
            return cls(
                **{key: value[key] for key in fields - {"resource_policy", "network_policy"}},
                resource_policy=ResourcePolicy.from_mapping(resource),
                network_policy=NetworkPolicy.from_mapping(network),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ControllerAdmissionError):
                raise
            raise ControllerAdmissionError("protected policy is invalid") from exc

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "repository": self.repository,
            "provider_id": self.provider_id,
            "workflow_id": self.workflow_id,
            "workflow_digest": self.workflow_digest,
            "event": self.event,
            "ref": self.ref,
            "audience": self.audience,
            "grant_ttl_seconds": self.grant_ttl_seconds,
            "resource_policy_id": self.resource_policy_id,
            "resource_policy_digest": self.resource_policy_digest,
            "network_policy_id": self.network_policy_id,
            "network_policy_digest": self.network_policy_digest,
            "resource_policy": self.resource_policy.to_mapping(),
            "network_policy": self.network_policy.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class AuthenticatedProviderObservation:
    """Facts returned only after a configured adapter validates provider auth."""

    provider_id: str
    run_id: str
    attempt: int
    repository: str
    source_sha: str
    workflow_id: str
    workflow_digest: str
    event: str
    ref: str
    audience: str
    authentication_evidence_digest: str


@dataclass(frozen=True, slots=True)
class TrustedControllerInputs:
    """Snapshots returned by the configured trusted input loader."""

    policy: ProtectedControllerPolicy
    candidate: Mapping[str, Any]
    profile: Mapping[str, Any]
    inventory: Mapping[str, Any]
    material_id: str
    material_digest: str


class TrustedInputLoader(Protocol):
    def load_for_source(self, source_sha: str) -> TrustedControllerInputs: ...


class ProviderAuthenticationAdapter(Protocol):
    def authenticate_current_run(self) -> AuthenticatedProviderObservation: ...


@dataclass(frozen=True, slots=True)
class AuthenticatedExecutorObservation:
    """Executor identity returned after a configured credential check."""

    executor_id: str
    admission_id: str
    grant_digest: str
    authentication_evidence_digest: str


class ExecutorAuthenticationAdapter(Protocol):
    def authenticate_executor(
        self, *, credential: str, admission_id: str, grant_digest: str
    ) -> AuthenticatedExecutorObservation: ...


@dataclass(frozen=True, slots=True)
class IssuedAdmission:
    admission_id: str
    grant: str
    admission_digest: str
    workload_id: str


@dataclass(frozen=True, slots=True)
class AdmissionIssueResult:
    plan: AdmissionPlan
    admissions: tuple[IssuedAdmission, ...]
    external_trust_established: bool = dataclass_field(default=False, init=False)
    acceptance_eligible: bool = dataclass_field(default=False, init=False)
    dispatch_authorized: bool = dataclass_field(default=False, init=False)


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ControllerAdmissionError("controller grant cannot be canonicalized") from exc


def _unique_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ControllerAdmissionError("grant contains a duplicate JSON field")
        output[key] = value
    return output


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest_text(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ControllerAdmissionError(f"{label} must be lowercase SHA-256")
    return value


def _admission_binding_digest(admission: Admission) -> str:
    mapping = admission.to_mapping()
    mapping.pop("grant_digest")
    return _sha(_canonical(mapping))


def _bounded_snapshot(value: Any, label: str, maximum: int) -> None:
    if not isinstance(value, Mapping):
        raise ControllerAdmissionError(f"trusted {label} must be a mapping")
    try:
        encoded = json.dumps(
            dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ControllerAdmissionError(f"trusted {label} cannot be bounded and encoded") from exc
    if len(encoded) > maximum:
        raise ControllerAdmissionError(f"trusted {label} exceeds the configured size limit")


def _validate_provider_facts(facts: Any) -> AuthenticatedProviderObservation:
    if not isinstance(facts, AuthenticatedProviderObservation):
        raise ControllerAdmissionError(
            "provider adapter did not return authenticated observation facts"
        )
    for field in (
        "provider_id",
        "run_id",
        "repository",
        "workflow_id",
        "event",
        "ref",
        "audience",
    ):
        value = getattr(facts, field)
        if not isinstance(value, str) or not value or len(value) > 256:
            raise ControllerAdmissionError(f"authenticated provider {field} is invalid")
    if facts.repository != CANONICAL_REPOSITORY:
        raise ControllerAdmissionError("authenticated provider repository is not canonical")
    for field in ("workflow_digest", "authentication_evidence_digest"):
        _digest_text(getattr(facts, field), f"provider {field}")
    if len(facts.source_sha) != 40 or any(
        char not in "0123456789abcdef" for char in facts.source_sha
    ):
        raise ControllerAdmissionError("authenticated provider source SHA is invalid")
    if isinstance(facts.attempt, bool) or not isinstance(facts.attempt, int) or facts.attempt < 1:
        raise ControllerAdmissionError("authenticated provider attempt is invalid")
    return facts


def _validate_executor_observation(facts: AuthenticatedExecutorObservation) -> None:
    for name in ("executor_id", "admission_id"):
        value = getattr(facts, name)
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ControllerAdmissionError(f"authenticated executor {name} is invalid")
    _digest_text(facts.grant_digest, "executor grant digest")
    _digest_text(facts.authentication_evidence_digest, "executor authentication evidence digest")


def _validate_grant_payload_fields(payload: Mapping[str, Any]) -> None:
    for field, maximum in (
        ("admission_id", 36),
        ("grant_nonce", 32),
        ("provider_id", 256),
        ("provider_run_id", 128),
        ("repository", 256),
        ("audience", 256),
        ("issued_at", 27),
        ("expires_at", 27),
    ):
        value = payload.get(field)
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise ControllerAdmissionError(f"grant {field} is invalid or exceeds its size limit")
    if len(payload["grant_nonce"]) != 32 or any(
        char not in "0123456789abcdef" for char in payload["grant_nonce"]
    ):
        raise ControllerAdmissionError("grant nonce encoding is invalid")
    attempt = payload.get("provider_attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ControllerAdmissionError("grant provider attempt is invalid")
    if (
        not isinstance(payload.get("source_sha"), str)
        or len(payload["source_sha"]) != 40
        or any(char not in "0123456789abcdef" for char in payload["source_sha"])
    ):
        raise ControllerAdmissionError("grant source SHA is invalid")
    for field in (
        "admission_binding_digest",
        "policy_digest",
        "inventory_digest",
        "workflow_scope_digest",
        "token_digest",
    ):
        _digest_text(payload.get(field), f"grant {field}")


def _provider_matches(
    policy: ProtectedControllerPolicy, facts: AuthenticatedProviderObservation
) -> None:
    facts = _validate_provider_facts(facts)
    for field, expected in (
        ("provider_id", policy.provider_id),
        ("repository", policy.repository),
        ("workflow_id", policy.workflow_id),
        ("workflow_digest", policy.workflow_digest),
        ("event", policy.event),
        ("ref", policy.ref),
        ("audience", policy.audience),
    ):
        if getattr(facts, field) != expected:
            raise ControllerAdmissionError(f"authenticated provider {field} does not match policy")
    if not isinstance(facts.run_id, str) or not facts.run_id or len(facts.run_id) > 128:
        raise ControllerAdmissionError("authenticated provider run_id is invalid")
    if isinstance(facts.attempt, bool) or not isinstance(facts.attempt, int) or facts.attempt < 1:
        raise ControllerAdmissionError("authenticated provider attempt is invalid")
    if len(facts.source_sha) != 40 or any(c not in "0123456789abcdef" for c in facts.source_sha):
        raise ControllerAdmissionError("authenticated provider source SHA is invalid")
    _digest_text(facts.authentication_evidence_digest, "provider authentication evidence digest")


class ProtectedAdmissionController:
    """Issue and consume signed reference grants after fail-closed preflight."""

    def __init__(
        self,
        *,
        trusted_loader: TrustedInputLoader | None,
        provider_authenticator: ProviderAuthenticationAdapter | None,
        ledger: AdmissionLedger,
        grant_signing_key: bytes | None,
        executor_authenticator: ExecutorAuthenticationAdapter | None = None,
        utc_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._loader = trusted_loader
        self._provider = provider_authenticator
        self._ledger = ledger
        self._key = grant_signing_key
        self._executor_authenticator = executor_authenticator
        self._clock = utc_clock or (lambda: datetime.now(timezone.utc))

    def issue(self) -> AdmissionIssueResult:
        if (
            self._loader is None
            or self._provider is None
            or not isinstance(self._key, bytes)
            or len(self._key) < 32
        ):
            raise ControllerAdmissionError(
                "trusted loader, provider authenticator and signing configuration are required"
            )
        try:
            facts = _validate_provider_facts(self._provider.authenticate_current_run())
        except ControllerAdmissionError:
            raise
        except Exception as exc:
            raise ControllerAdmissionError("provider authentication failed") from exc
        # Load snapshots only after provider authentication; the loader is keyed by authenticated SHA.
        loaded = self._loader.load_for_source(facts.source_sha)
        if not isinstance(loaded, TrustedControllerInputs):
            raise ControllerAdmissionError("trusted loader returned an invalid closed input bundle")
        policy = loaded.policy
        if not isinstance(policy, ProtectedControllerPolicy):
            raise ControllerAdmissionError("trusted loader returned an invalid protected policy")
        _provider_matches(policy, facts)
        _bounded_snapshot(loaded.candidate, "candidate", MAX_CANDIDATE_BYTES)
        _bounded_snapshot(loaded.profile, "profile", MAX_PROFILE_BYTES)
        _bounded_snapshot(loaded.inventory, "inventory", MAX_INVENTORY_BYTES)
        workloads = loaded.inventory.get("workloads")
        if not isinstance(workloads, list) or not workloads or len(workloads) > MAX_WORKLOADS:
            raise ControllerAdmissionError(
                "trusted workload inventory is empty or exceeds the workload limit"
            )
        _digest_text(loaded.material_digest, "trusted material digest")
        if (
            not isinstance(loaded.material_id, str)
            or loaded.material_id != loaded.material_id.strip()
            or not loaded.material_id
            or len(loaded.material_id) > 128
        ):
            raise ControllerAdmissionError("trusted material identity is invalid")
        try:
            plan = plan_fixed_workloads(loaded.candidate, loaded.profile, loaded.inventory)
        except PlannerError as exc:
            raise ControllerAdmissionError(f"trusted workload planning failed: {exc}") from exc
        if plan.blockers:
            raise ControllerAdmissionError("planner produced blockers; no admission was issued")
        _bounded_snapshot(plan.to_mapping(), "admission plan", MAX_PLAN_BYTES)
        candidate_source = loaded.candidate.get("source")
        if (
            not isinstance(candidate_source, Mapping)
            or candidate_source.get("commit") != facts.source_sha
        ):
            raise ControllerAdmissionError(
                "trusted candidate source does not match authenticated provider SHA"
            )
        candidate_digest = _digest_text(plan.candidate_snapshot_sha256, "candidate snapshot digest")
        profile_digest = _digest_text(plan.profile_snapshot_sha256, "profile snapshot digest")
        inventory_digest = _digest_text(plan.inventory_snapshot_sha256, "inventory snapshot digest")
        policy_mapping = policy.to_mapping()
        policy_digest = _sha(
            _canonical({"policy": policy_mapping, "inventory_digest": inventory_digest})
        )
        workflow_scope_digest = _sha(
            _canonical(
                {
                    "workflow_id": facts.workflow_id,
                    "repository": facts.repository,
                    "workflow_digest": facts.workflow_digest,
                    "event": facts.event,
                    "ref": facts.ref,
                    "audience": facts.audience,
                }
            )
        )
        issued_at = self._clock()
        if issued_at.tzinfo is None or issued_at.utcoffset() != timedelta(0):
            raise ControllerAdmissionError("now must be UTC-aware")
        issued_at = issued_at.astimezone(timezone.utc)
        expires_at = issued_at + timedelta(seconds=policy.grant_ttl_seconds)
        deadline = expires_at + timedelta(hours=2)
        publication_deadline = deadline + timedelta(hours=1)
        prepared: list[tuple[Admission, str, str]] = []
        # Preflight the full matrix above before the first durable grant is written.
        for workload in plan.workloads:
            if (
                workload.resource_policy.policy_id != policy.resource_policy_id
                or workload.resource_policy.sha256 != policy.resource_policy_digest
                or workload.network_policy.policy_id != policy.network_policy_id
                or workload.network_policy.sha256 != policy.network_policy_digest
            ):
                raise ControllerAdmissionError(
                    "planned resource or network policy is not the protected policy"
                )
            material_digest = _sha(
                _canonical(
                    {
                        "loader_material_digest": loaded.material_digest,
                        "workload_materials": [
                            {"material_id": row.material_id, "sha256": row.sha256}
                            for row in workload.input_materials
                        ],
                    }
                )
            )
            token = secrets.token_urlsafe(32)
            admission_id = str(uuid4())
            issued_text = issued_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
            expires_text = expires_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
            environment = next(
                env
                for env in loaded.inventory["environments"]
                if env["environment_id"] == workload.environment_id
            )
            admission = Admission(
                admission_id=admission_id,
                grant_digest="0" * 64,
                repository=policy.repository,
                source_sha=facts.source_sha,
                workflow_digest=workflow_scope_digest,
                runtime_id=workload.runtime_id,
                workload_id=workload.workload_id,
                workload_digest=workload.workload_sha256,
                policy_id=policy.policy_id,
                policy_digest=policy_digest,
                environment_id=workload.environment_id,
                image_digest=environment["image_sha256"],
                image_os=environment["os"],
                image_arch=environment["architecture"],
                provider_id=facts.provider_id,
                provider_run_id=facts.run_id,
                provider_attempt=facts.attempt,
                candidate_id=plan.candidate_id,
                candidate_digest=candidate_digest,
                profile_id=plan.profile_id,
                profile_digest=profile_digest,
                material_id=loaded.material_id,
                material_digest=material_digest,
                required_output_paths=tuple(
                    output.output_path for output in workload.required_outputs
                ),
                resource_policy=policy.resource_policy,
                network_policy=policy.network_policy,
                issued_at=issued_text,
                expires_at=expires_text,
                execution_deadline=deadline.isoformat(timespec="microseconds").replace(
                    "+00:00", "Z"
                ),
                publication_deadline=publication_deadline.isoformat(
                    timespec="microseconds"
                ).replace("+00:00", "Z"),
            )
            grant_payload = {
                "schema_version": 1,
                "admission_id": admission_id,
                "admission_binding_digest": _admission_binding_digest(admission),
                "grant_nonce": secrets.token_hex(16),
                "policy_digest": policy_digest,
                "inventory_digest": inventory_digest,
                "workflow_scope_digest": workflow_scope_digest,
                "provider_id": facts.provider_id,
                "provider_run_id": facts.run_id,
                "provider_attempt": facts.attempt,
                "repository": facts.repository,
                "source_sha": facts.source_sha,
                "audience": facts.audience,
                "issued_at": issued_text,
                "expires_at": expires_text,
                "token_digest": _sha(token.encode("ascii")),
            }
            signed = _canonical(grant_payload)
            signature = hmac.new(self._key, signed, hashlib.sha256).digest()
            envelope = _canonical(
                {
                    "payload": grant_payload,
                    "signature": base64.urlsafe_b64encode(signature).decode("ascii").rstrip("="),
                }
            )
            grant = base64.urlsafe_b64encode(envelope).decode("ascii").rstrip("=") + "." + token
            admission = replace(admission, grant_digest=_sha(grant.encode("ascii")))
            prepared.append((admission, grant, workload.workload_id))
        # Every admission has passed its closed record validation before atomic persistence.
        issuance_key = _sha(
            _canonical(
                {
                    "provider_id": facts.provider_id,
                    "run_id": facts.run_id,
                    "attempt": facts.attempt,
                    "source_sha": facts.source_sha,
                }
            )
        )
        content_digest = _sha(
            _canonical(
                {
                    "policy_digest": policy_digest,
                    "inventory_digest": inventory_digest,
                    "candidate_digest": candidate_digest,
                    "profile_digest": profile_digest,
                    "material_id": loaded.material_id,
                    "material_digest": loaded.material_digest,
                    "workflow_scope_digest": workflow_scope_digest,
                    "provider_id": facts.provider_id,
                    "provider_run_id": facts.run_id,
                    "provider_attempt": facts.attempt,
                    "plan": plan.to_mapping(),
                }
            )
        )
        response_mapping = {
            "schema_version": 1,
            "admissions": [
                {
                    "admission_id": admission.admission_id,
                    "grant": grant,
                    "admission_digest": admission_digest(admission),
                    "workload_id": workload_id,
                }
                for admission, grant, workload_id in prepared
            ],
        }
        response_bytes = _canonical(response_mapping)
        if len(response_bytes) > MAX_PLAN_BYTES:
            raise ControllerAdmissionError("issued grant bundle exceeds the response size limit")
        try:
            inserted = self._ledger.issue_many(
                tuple(admission for admission, _, _ in prepared),
                issuance_key=issuance_key,
                content_digest=content_digest,
                response=response_mapping,
            )
        except AdmissionConflict as exc:
            raise ControllerAdmissionError(
                "provider run attempt replay conflicts with its immutable admission bundle"
            ) from exc
        if any(inserted):
            issued = tuple(
                IssuedAdmission(
                    admission.admission_id, grant, admission_digest(admission), workload_id
                )
                for admission, grant, workload_id in prepared
            )
        else:
            stored = self._ledger.read_issuance(issuance_key)
            if stored is None or stored[0] != content_digest:
                raise ControllerAdmissionError("immutable issuance replay response is unavailable")
            stored_response = stored[1]
            if (
                not isinstance(stored_response, dict)
                or set(stored_response) != {"schema_version", "admissions"}
                or stored_response["schema_version"] != 1
                or not isinstance(stored_response["admissions"], list)
                or len(stored_response["admissions"]) != len(prepared)
            ):
                raise ControllerAdmissionError("immutable issuance replay response is malformed")
            replayed: list[IssuedAdmission] = []
            for row in stored_response["admissions"]:
                if not isinstance(row, dict) or set(row) != {
                    "admission_id",
                    "grant",
                    "admission_digest",
                    "workload_id",
                }:
                    raise ControllerAdmissionError("immutable issuance replay row is malformed")
                admission = self._ledger.get(row["admission_id"])
                if (
                    admission is None
                    or admission.workload_id != row["workload_id"]
                    or admission.grant_digest != _sha(row["grant"].encode("ascii"))
                    or admission_digest(admission) != row["admission_digest"]
                ):
                    raise ControllerAdmissionError(
                        "immutable issuance replay row does not match ledger"
                    )
                replayed.append(
                    IssuedAdmission(
                        row["admission_id"],
                        row["grant"],
                        row["admission_digest"],
                        row["workload_id"],
                    )
                )
            issued = tuple(replayed)
        return AdmissionIssueResult(plan=plan, admissions=issued)

    def claim(
        self,
        *,
        admission_id: str,
        grant: str,
        credential: str,
        executor_id: str | None = None,
    ) -> Any:
        if (
            self._provider is None
            or not isinstance(self._key, bytes)
            or len(self._key) < 32
            or self._executor_authenticator is None
        ):
            raise ControllerAdmissionError(
                "provider and executor authenticators and grant verification key are required"
            )
        if not isinstance(credential, str) or not credential or len(credential) > 4096:
            raise ControllerAdmissionError(
                "executor credential is empty or exceeds the input limit"
            )
        try:
            facts = _validate_provider_facts(self._provider.authenticate_current_run())
        except ControllerAdmissionError:
            raise
        except Exception as exc:
            raise ControllerAdmissionError("provider authentication failed") from exc
        try:
            if not isinstance(grant, str) or not grant or len(grant) > MAX_GRANT_BYTES:
                raise ControllerAdmissionError("grant is empty or exceeds the input size limit")
            if grant.count(".") != 1:
                raise ControllerAdmissionError("grant token format is invalid")
            encoded, token = grant.split(".", 1)
            if (
                not encoded
                or len(encoded) > ((MAX_GRANT_ENVELOPE_BYTES + 2) // 3) * 4
                or not token
                or len(token) != 43
                or any(
                    char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
                    for char in token
                )
                or any(
                    char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
                    for char in encoded
                )
            ):
                raise ControllerAdmissionError("grant token format is invalid")
            padded = encoded + "=" * (-len(encoded) % 4)
            envelope_bytes = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
            if len(envelope_bytes) > MAX_GRANT_ENVELOPE_BYTES:
                raise ControllerAdmissionError("grant envelope exceeds the decoded size limit")
            envelope = json.loads(
                envelope_bytes.decode("ascii"), object_pairs_hook=_unique_object_pairs
            )
            if not isinstance(envelope, dict) or set(envelope) != {"payload", "signature"}:
                raise ControllerAdmissionError("grant envelope shape is invalid")
            payload = envelope["payload"]
            if not isinstance(payload, dict):
                raise ControllerAdmissionError("grant payload shape is invalid")
            payload_raw = _canonical(payload)
            if len(payload_raw) > MAX_GRANT_PAYLOAD_BYTES:
                raise ControllerAdmissionError("grant payload exceeds the decoded size limit")
            signature_text = envelope["signature"]
            if (
                not isinstance(signature_text, str)
                or len(signature_text) != 43
                or any(
                    char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
                    for char in signature_text
                )
            ):
                raise ControllerAdmissionError("grant signature encoding is invalid")
            signature = base64.b64decode(signature_text + "=", altchars=b"-_", validate=True)
            if len(signature) != hashlib.sha256().digest_size:
                raise ControllerAdmissionError("grant signature length is invalid")
            expected_signature = hmac.new(self._key, payload_raw, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected_signature):
                raise ControllerAdmissionError("grant signature is invalid")
            expected_fields = {
                "schema_version",
                "admission_id",
                "admission_binding_digest",
                "grant_nonce",
                "policy_digest",
                "inventory_digest",
                "workflow_scope_digest",
                "provider_id",
                "provider_run_id",
                "provider_attempt",
                "repository",
                "source_sha",
                "audience",
                "issued_at",
                "expires_at",
                "token_digest",
            }
            if (
                not isinstance(payload, dict)
                or set(payload) != expected_fields
                or isinstance(payload.get("schema_version"), bool)
                or payload.get("schema_version") != 1
            ):
                raise ControllerAdmissionError("grant version or shape is invalid")
            _validate_grant_payload_fields(payload)
            if payload.get("admission_id") != admission_id or payload.get("token_digest") != _sha(
                token.encode("ascii")
            ):
                raise ControllerAdmissionError("grant identity is invalid")
            if (
                payload.get("provider_id") != facts.provider_id
                or payload.get("provider_run_id") != facts.run_id
                or payload.get("provider_attempt") != facts.attempt
                or payload.get("source_sha") != facts.source_sha
                or payload.get("repository") != facts.repository
            ):
                raise ControllerAdmissionError("authenticated provider run does not match grant")
            if payload.get("audience") != facts.audience:
                raise ControllerAdmissionError(
                    "authenticated provider audience does not match grant"
                )
            if payload.get("workflow_scope_digest") != _sha(
                _canonical(
                    {
                        "workflow_id": facts.workflow_id,
                        "repository": facts.repository,
                        "workflow_digest": facts.workflow_digest,
                        "event": facts.event,
                        "ref": facts.ref,
                        "audience": facts.audience,
                    }
                )
            ):
                raise ControllerAdmissionError(
                    "authenticated provider workflow context does not match grant"
                )
        except ControllerAdmissionError:
            raise
        except Exception as exc:
            raise ControllerAdmissionError("grant is malformed") from exc
        admission = self._ledger.get(admission_id)
        if (
            admission is None
            or admission.grant_digest != _sha(grant.encode("ascii"))
            or payload.get("admission_binding_digest") != _admission_binding_digest(admission)
            or payload.get("expires_at") != admission.expires_at
        ):
            raise ControllerAdmissionError("grant does not bind the stored immutable admission")
        grant_digest = _sha(grant.encode("ascii"))
        try:
            executor = self._executor_authenticator.authenticate_executor(
                credential=credential, admission_id=admission_id, grant_digest=grant_digest
            )
        except Exception as exc:
            raise ControllerAdmissionError("executor authentication failed") from exc
        if not isinstance(executor, AuthenticatedExecutorObservation):
            raise ControllerAdmissionError("executor adapter returned no authenticated identity")
        _validate_executor_observation(executor)
        if (
            executor.admission_id != admission_id
            or executor.grant_digest != grant_digest
            or (executor_id is not None and executor_id != executor.executor_id)
        ):
            raise ControllerAdmissionError("authenticated executor identity does not match claim")
        try:
            return self._ledger.claim(admission_id, grant_digest, executor.executor_id, self._clock)
        except AdmissionExpired as exc:
            raise ControllerAdmissionError("grant expired") from exc
