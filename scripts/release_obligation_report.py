"""Deeply immutable normalized facts from the successful v2 validation pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NewType

from scripts.platform_release_manifest import _validate_candidate_and_report
from scripts.validated_release_facts import (
    BuildContextFact,
    EvidenceFact,
    LockFact,
    ValidatedReleaseFacts,
)

Sha256 = NewType("Sha256", str)
GitCommit = NewType("GitCommit", str)
RuleId = Literal[
    "artifact.binding",
    "compatibility.coverage",
    "compatibility.predecessor",
    "compatibility.relation_declaration",
    "compatibility.runtime_scope",
    "evidence.acceptance",
    "migration.evidence",
    "rollback.evidence",
    "python.lock.applicability",
    "python.lock.reconstruction",
    "python.build.context",
    "python.build.hook",
    "python.build.attestation",
    "web_console.inputs",
    "web_console.npm_lock",
    "web_console.artifact_members",
    "web_console.toolchain",
    "web_console.linux_build",
    "web_console.linux_serving",
    "web_console.linux_deployment",
    "web_console.darwin_acceptance",
]
StructuralStatus = Literal["satisfied", "missing", "unknown"]
FactKind = Literal[
    "acceptance_requirement",
    "artifact",
    "build_attestation",
    "build_context",
    "compatibility_relation",
    "compatibility_subject",
    "evidence",
    "hook_attestation",
    "lock",
    "reconstruction",
    "required_scope",
    "web_console_input",
    "web_console_artifact_members",
]


@dataclass(frozen=True, slots=True)
class CandidateIdentity:
    candidate_id: Sha256
    profile_id: Sha256
    candidate_digest: Sha256
    profile_digest: Sha256
    source_commit: GitCommit


@dataclass(frozen=True, slots=True)
class PolicyBindings:
    manifest_schema_digest: Sha256
    component_inventory_digest: Sha256
    compatibility_coverage_policy_digest: Sha256
    acceptance_policy_digest: Sha256


@dataclass(frozen=True, slots=True)
class ObligationScope:
    component_id: str | None = None
    gate_id: str | None = None
    environment_id: str | None = None
    runtime_id: str | None = None
    lock_id: str | None = None
    operation: str | None = None
    format_id: str | None = None
    relation_id: str | None = None


@dataclass(frozen=True, slots=True)
class FactRef:
    kind: FactKind
    identity: str
    digest: Sha256 | None = None


@dataclass(frozen=True, slots=True)
class Obligation:
    rule_id: RuleId
    status: StructuralStatus
    scope: ObligationScope
    required_refs: tuple[FactRef, ...]
    matching_refs: tuple[FactRef, ...]


@dataclass(frozen=True, slots=True)
class EvidenceClaim:
    evidence_sha256: Sha256
    gate: str
    component_id: str
    environment: str
    runtime_id: str | None
    compatibility_subject_sha256: Sha256 | None
    outcome: str
    artifact_sha256: Sha256


@dataclass(frozen=True, slots=True)
class ArtifactBinding:
    component_id: str
    kind: str
    artifact_sha256: Sha256
    origin: str
    path: str


@dataclass(frozen=True, slots=True)
class ReconstructionBinding:
    lock_id: str
    runtime_id: str
    environment_id: str
    lock_sha256: Sha256
    receipt_sha256: Sha256
    outcome: str


@dataclass(frozen=True, slots=True)
class BuildContextBinding:
    context_sha256: Sha256
    component_id: str
    runtime_id: str
    environment_id: str
    bootstrap_lock_id: str
    bootstrap_lock_sha256: Sha256
    bootstrap_receipt_sha256: Sha256


@dataclass(frozen=True, slots=True)
class HookBinding:
    attestation_sha256: Sha256
    context_sha256: Sha256
    operation: str
    status: str
    final_lock_id: str | None
    final_lock_sha256: Sha256 | None
    reconstruction_receipt_sha256: Sha256 | None


@dataclass(frozen=True, slots=True)
class BuildBinding:
    attestation_sha256: Sha256
    context_sha256: Sha256
    hook_attestation_sha256: Sha256
    runtime_id: str
    environment_id: str
    operation: str
    final_lock_id: str
    final_lock_sha256: Sha256
    reconstruction_receipt_sha256: Sha256
    outcome: str
    artifact_sha256: Sha256 | None


@dataclass(frozen=True, slots=True)
class ObligationReport:
    report_version: int
    manifest_schema_version: int
    manifest_digest: Sha256
    identity: CandidateIdentity
    policy_bindings: PolicyBindings
    obligations: tuple[Obligation, ...]
    evidence_claims: tuple[EvidenceClaim, ...]
    artifact_bindings: tuple[ArtifactBinding, ...]
    reconstructions: tuple[ReconstructionBinding, ...]
    build_contexts: tuple[BuildContextBinding, ...]
    hook_bindings: tuple[HookBinding, ...]
    build_bindings: tuple[BuildBinding, ...]


def _ref(kind: FactKind, identity: str, digest: str | None = None) -> FactRef:
    return FactRef(kind, identity, Sha256(digest) if digest is not None else None)


def _scope_key(scope: ObligationScope) -> tuple[str, ...]:
    return tuple(
        value or ""
        for value in (
            scope.component_id,
            scope.gate_id,
            scope.environment_id,
            scope.runtime_id,
            scope.lock_id,
            scope.operation,
            scope.format_id,
            scope.relation_id,
        )
    )


def _obligations(facts: ValidatedReleaseFacts) -> tuple[Obligation, ...]:
    evidence_rows = facts.evidence
    result: list[Obligation] = []
    for requirement in facts.lock_requirements:
        matching_locks: list[LockFact] = [
            row
            for row in facts.locks
            if requirement.component_id in row.covered_components
            and row.ecosystem == requirement.ecosystem
            and row.purpose == requirement.purpose
            and row.environment_id == requirement.environment_id
            and row.runtime_id == requirement.runtime_id
        ]
        result.append(
            Obligation(
                "python.lock.applicability",
                "satisfied" if matching_locks else "missing",
                ObligationScope(
                    requirement.component_id,
                    environment_id=requirement.environment_id,
                    runtime_id=requirement.runtime_id,
                ),
                (
                    _ref(
                        "required_scope",
                        f"{requirement.component_id}|{requirement.ecosystem}|{requirement.purpose}|{requirement.environment_id}|{requirement.runtime_id or ''}",
                    ),
                ),
                tuple(_ref("lock", row.lock_id, row.reference.sha256) for row in matching_locks),
            )
        )

    for acceptance in facts.acceptance_requirements:
        matching_acceptance_evidence: list[EvidenceFact] = [
            row
            for row in evidence_rows
            if row.gate_id == acceptance.gate_id
            and row.component_id == acceptance.component_id
            and row.environment_id == acceptance.environment_id
        ]
        passed = any(row.outcome == "passed" for row in matching_acceptance_evidence)
        result.append(
            Obligation(
                "evidence.acceptance",
                "satisfied" if passed else "unknown" if matching_acceptance_evidence else "missing",
                ObligationScope(
                    acceptance.component_id, acceptance.gate_id, acceptance.environment_id
                ),
                (
                    _ref(
                        "acceptance_requirement",
                        f"{acceptance.gate_id}|{acceptance.component_id}|{acceptance.environment_id}",
                    ),
                ),
                tuple(
                    _ref("evidence", row.evidence_sha256, row.evidence_sha256)
                    for row in matching_acceptance_evidence
                ),
            )
        )

    providers = {row.component_id: row for row in facts.compatibility_providers}
    subjects = facts.compatibility_subjects
    approved_evidence = set(facts.compatibility_evidence)
    for component in sorted({row.component_id for row in facts.compatibility_scopes}):
        provider = providers[component]
        coverage_unknown = provider.status == "unknown-blocking"
        result.append(
            Obligation(
                "compatibility.coverage",
                "unknown" if coverage_unknown else "satisfied",
                ObligationScope(component),
                (_ref("required_scope", f"compatibility|{component}"),),
                (_ref("required_scope", f"compatibility|{component}|{provider.status}"),),
            )
        )
        if coverage_unknown:
            result.append(
                Obligation(
                    "compatibility.predecessor",
                    "unknown",
                    ObligationScope(component),
                    (_ref("required_scope", f"{component}|predecessor-material"),),
                    (),
                )
            )
        for runtime_scope in (
            row for row in facts.compatibility_scopes if row.component_id == component
        ):
            required_subjects = {
                row.digest
                for row in subjects
                if row.component_id == component
                and row.environment_id == runtime_scope.environment_id
                and row.runtime_id == runtime_scope.runtime_id
                and row.runtime_status == runtime_scope.runtime_status
            }
            matching_compatibility: list[EvidenceFact] = [
                row
                for row in evidence_rows
                if row.compatibility_subject_sha256 in required_subjects
                and row.evidence_sha256 in approved_evidence
            ]
            unknown = coverage_unknown or runtime_scope.runtime_status == "unknown-blocking"
            result.append(
                Obligation(
                    "compatibility.runtime_scope",
                    "unknown" if unknown else "satisfied",
                    ObligationScope(
                        component,
                        environment_id=runtime_scope.environment_id,
                        runtime_id=runtime_scope.runtime_id,
                    ),
                    (
                        _ref(
                            "required_scope",
                            f"{component}|{runtime_scope.environment_id}|{runtime_scope.runtime_id or ''}",
                        ),
                    ),
                    tuple(
                        _ref("evidence", row.evidence_sha256, row.evidence_sha256)
                        for row in matching_compatibility
                    ),
                )
            )
        if coverage_unknown:
            continue
        relations = [row for row in facts.compatibility_relations if row.component_id == component]
        for relation in relations:
            scope = ObligationScope(
                component,
                environment_id=relation.environment_id,
                runtime_id=relation.runtime_id,
                format_id=relation.format_id,
                relation_id=relation.relation_id,
            )
            result.append(
                Obligation(
                    "compatibility.relation_declaration",
                    "unknown" if relation.disposition == "unknown-blocking" else "satisfied",
                    scope,
                    (_ref("required_scope", relation.relation_id),),
                    (_ref("compatibility_relation", relation.relation_id),),
                )
            )
            result.append(
                Obligation(
                    "compatibility.predecessor",
                    "unknown",
                    scope,
                    (
                        _ref(
                            "required_scope",
                            f"{relation.relation_id}|resolved-predecessor-material",
                        ),
                    ),
                    (),
                )
            )
        if provider.inventory_status != "none":
            for format_id in provider.format_ids:
                if not any(row.format_id == format_id for row in relations):
                    result.append(
                        Obligation(
                            "compatibility.predecessor",
                            "missing",
                            ObligationScope(component, format_id=format_id),
                            (_ref("required_scope", f"{component}|{format_id}|predecessor"),),
                            (),
                        )
                    )

    for row in facts.migrations_rollbacks:
        if not row.evidence_required:
            continue
        gate = f"{row.kind}:{row.item_id}"
        matching_migration_evidence: list[EvidenceFact] = [
            item for item in evidence_rows if item.gate_id == gate
        ]
        passed = any(item.outcome == "passed" for item in matching_migration_evidence)
        rule_id: RuleId = "migration.evidence" if row.kind == "migration" else "rollback.evidence"
        result.append(
            Obligation(
                rule_id,
                "satisfied" if passed else "unknown" if matching_migration_evidence else "missing",
                ObligationScope(gate_id=gate, relation_id=row.item_id),
                (_ref("required_scope", gate),),
                tuple(
                    _ref("evidence", item.evidence_sha256, item.evidence_sha256)
                    for item in matching_migration_evidence
                ),
            )
        )

    reconstructions = {
        (row.lock_id, row.runtime_id, row.environment_id): row for row in facts.reconstructions
    }
    for lock in facts.locks:
        if lock.ecosystem != "python":
            continue
        key = (lock.lock_id, lock.runtime_id or "", lock.environment_id)
        receipt = reconstructions.get(key)
        result.append(
            Obligation(
                "python.lock.reconstruction",
                "missing"
                if receipt is None
                else "satisfied"
                if receipt.outcome == "passed"
                else "unknown",
                ObligationScope(
                    environment_id=lock.environment_id,
                    runtime_id=lock.runtime_id,
                    lock_id=lock.lock_id,
                ),
                (_ref("lock", lock.lock_id, lock.reference.sha256),),
                (_ref("reconstruction", lock.lock_id, receipt.receipt_sha256),) if receipt else (),
            )
        )

    build_locks = [
        row for row in facts.locks if row.ecosystem == "python" and row.purpose == "build"
    ]
    expected_contexts = {
        (component.component_id, lock.runtime_id, lock.environment_id)
        for component in facts.components
        if component.kind == "python_distribution"
        for lock in build_locks
        if component.component_id in lock.covered_components and lock.runtime_id is not None
    }
    contexts: dict[tuple[str, str, str], list[BuildContextFact]] = {}
    for context_fact in facts.build_contexts:
        contexts.setdefault(
            (
                context_fact.component_id,
                context_fact.runtime_id,
                context_fact.environment_id,
            ),
            [],
        ).append(context_fact)
    for rows in contexts.values():
        rows.sort(key=lambda row: row.context_sha256)
    for component, runtime, environment in sorted(expected_contexts):
        matching_contexts = contexts.get((component, runtime, environment), [])
        required = (_ref("required_scope", f"{component}|{runtime}|{environment}|build-context"),)
        scope = ObligationScope(component, environment_id=environment, runtime_id=runtime)
        if not matching_contexts:
            result.append(Obligation("python.build.context", "missing", scope, required, ()))
            for operation in ("wheel", "sdist"):
                op_scope = ObligationScope(
                    component, environment_id=environment, runtime_id=runtime, operation=operation
                )
                result.append(Obligation("python.build.hook", "missing", op_scope, required, ()))
                result.append(
                    Obligation("python.build.attestation", "missing", op_scope, required, ())
                )
            continue
        for context in matching_contexts:
            digest = context.context_sha256
            result.append(
                Obligation(
                    "python.build.context",
                    "satisfied",
                    scope,
                    (
                        _ref("lock", context.bootstrap_lock_id, context.bootstrap_lock_sha256),
                        _ref(
                            "reconstruction",
                            context.bootstrap_lock_id,
                            context.bootstrap_receipt_sha256,
                        ),
                    ),
                    (_ref("build_context", digest, digest),),
                )
            )
            for operation in ("wheel", "sdist"):
                hook = next(
                    (
                        row
                        for row in facts.hooks
                        if row.context_sha256 == digest and row.operation == operation
                    ),
                    None,
                )
                op_scope = ObligationScope(
                    component,
                    environment_id=environment,
                    runtime_id=runtime,
                    operation=operation,
                )
                hook_status: StructuralStatus = (
                    "missing"
                    if hook is None
                    else "unknown"
                    if hook.status in {"failed", "unavailable"}
                    else "satisfied"
                )
                result.append(
                    Obligation(
                        "python.build.hook",
                        hook_status,
                        op_scope,
                        (_ref("build_context", digest, digest),),
                        (
                            _ref(
                                "hook_attestation",
                                hook.attestation_sha256,
                                hook.attestation_sha256,
                            ),
                        )
                        if hook
                        else (),
                    )
                )
                build = next(
                    (
                        row
                        for row in facts.builds
                        if row.context_sha256 == digest and row.operation == operation
                    ),
                    None,
                )
                build_status: StructuralStatus = (
                    "missing"
                    if build is None
                    else "satisfied"
                    if build.outcome == "passed"
                    else "unknown"
                )
                result.append(
                    Obligation(
                        "python.build.attestation",
                        build_status,
                        op_scope,
                        (_ref("build_context", digest, digest),),
                        (
                            _ref(
                                "build_attestation",
                                build.attestation_sha256,
                                build.attestation_sha256,
                            ),
                        )
                        if build
                        else (),
                    )
                )

    for artifact in facts.artifacts:
        result.append(
            Obligation(
                "artifact.binding",
                "satisfied",
                ObligationScope(artifact.component_id),
                (_ref("required_scope", f"artifact|{artifact.component_id}|{artifact.kind}"),),
                (_ref("artifact", artifact.reference.path, artifact.reference.sha256),),
            )
        )

    web_console = facts.web_console
    if web_console is not None:
        web_scope = ObligationScope(component_id=web_console.component_id)
        result.extend(
            (
                Obligation(
                    "web_console.inputs",
                    "satisfied",
                    web_scope,
                    (
                        _ref(
                            "required_scope",
                            f"web-console-inputs|{web_console.component_id}",
                        ),
                    ),
                    tuple(
                        _ref(
                            "web_console_input",
                            reference.path,
                            reference.sha256,
                        )
                        for reference in web_console.source_inputs
                    ),
                ),
                Obligation(
                    "web_console.npm_lock",
                    "satisfied",
                    web_scope,
                    (_ref("required_scope", f"web-console-lock|{web_console.npm_lock_id}"),),
                    (
                        _ref(
                            "lock",
                            web_console.npm_lock_id,
                            web_console.npm_lock_reference.sha256,
                        ),
                    ),
                ),
                Obligation(
                    "web_console.artifact_members",
                    "satisfied",
                    web_scope,
                    (
                        _ref(
                            "required_scope",
                            f"web-console-members|{web_console.component_id}",
                        ),
                    ),
                    (
                        _ref(
                            "artifact",
                            web_console.artifact_reference.path,
                            web_console.artifact_reference.sha256,
                        ),
                        _ref(
                            "web_console_artifact_members",
                            web_console.component_id,
                            web_console.member_manifest_sha256,
                        ),
                    ),
                ),
            )
        )
        for pending_path in web_console.pending_bindings:
            binding_path = pending_path.removesuffix(" (bound, unverified)")
            if binding_path.startswith("toolchain."):
                web_rule_id: RuleId = "web_console.toolchain"
            elif binding_path.startswith("linux_build."):
                web_rule_id = "web_console.linux_build"
            elif binding_path.startswith("linux_serving."):
                web_rule_id = "web_console.linux_serving"
            elif binding_path.startswith("linux_deployment."):
                web_rule_id = "web_console.linux_deployment"
            else:
                web_rule_id = "web_console.darwin_acceptance"
            result.append(
                Obligation(
                    web_rule_id,
                    "unknown",
                    web_scope,
                    (_ref("required_scope", f"web-console-binding|{binding_path}"),),
                    (),
                )
            )

    def obligation_key(item: Obligation) -> tuple[Any, ...]:
        def refs_key(refs: tuple[FactRef, ...]) -> tuple[tuple[str, str, str], ...]:
            return tuple((ref.kind, ref.identity, str(ref.digest or "")) for ref in refs)

        return (
            item.rule_id,
            _scope_key(item.scope),
            item.status,
            refs_key(item.required_refs),
            refs_key(item.matching_refs),
        )

    return tuple(sorted(result, key=obligation_key))


def _report_from_validated_facts(facts: ValidatedReleaseFacts) -> ObligationReport:
    """Project immutable typed facts; no candidate dictionary is reinterpreted."""
    candidate = facts.candidate
    policies = facts.policies
    return ObligationReport(
        report_version=1,
        manifest_schema_version=candidate.schema_version,
        manifest_digest=Sha256(candidate.manifest_digest),
        identity=CandidateIdentity(
            candidate_id=Sha256(candidate.candidate_id),
            profile_id=Sha256(candidate.profile_id),
            candidate_digest=Sha256(candidate.candidate_digest),
            profile_digest=Sha256(candidate.profile_digest),
            source_commit=GitCommit(candidate.source_commit),
        ),
        policy_bindings=PolicyBindings(
            Sha256(policies.schema_digest),
            Sha256(policies.component_inventory_digest),
            Sha256(policies.compatibility_policy_digest),
            Sha256(policies.acceptance_policy_digest),
        ),
        obligations=_obligations(facts),
        evidence_claims=tuple(
            sorted(
                (
                    EvidenceClaim(
                        Sha256(row.evidence_sha256),
                        row.gate_id,
                        row.component_id,
                        row.environment_id,
                        row.runtime_id,
                        Sha256(row.compatibility_subject_sha256)
                        if row.compatibility_subject_sha256
                        else None,
                        row.outcome,
                        Sha256(row.artifact_sha256),
                    )
                    for row in facts.evidence
                ),
                key=lambda row: row.evidence_sha256,
            )
        ),
        artifact_bindings=tuple(
            sorted(
                (
                    ArtifactBinding(
                        row.component_id,
                        row.kind,
                        Sha256(row.reference.sha256),
                        row.reference.origin,
                        row.reference.path,
                    )
                    for row in facts.artifacts
                ),
                key=lambda row: (row.component_id, row.path, row.artifact_sha256),
            )
        ),
        reconstructions=tuple(
            sorted(
                (
                    ReconstructionBinding(
                        row.lock_id,
                        row.runtime_id,
                        row.environment_id,
                        Sha256(
                            next(
                                lock.reference.sha256
                                for lock in facts.locks
                                if lock.lock_id == row.lock_id
                            )
                        ),
                        Sha256(row.receipt_sha256),
                        row.outcome,
                    )
                    for row in facts.reconstructions
                ),
                key=lambda row: (row.lock_id, row.runtime_id, row.environment_id),
            )
        ),
        build_contexts=tuple(
            sorted(
                (
                    BuildContextBinding(
                        Sha256(row.context_sha256),
                        row.component_id,
                        row.runtime_id,
                        row.environment_id,
                        row.bootstrap_lock_id,
                        Sha256(row.bootstrap_lock_sha256),
                        Sha256(row.bootstrap_receipt_sha256),
                    )
                    for row in facts.build_contexts
                ),
                key=lambda row: (
                    row.component_id,
                    row.runtime_id,
                    row.environment_id,
                    row.context_sha256,
                ),
            )
        ),
        hook_bindings=tuple(
            sorted(
                (
                    HookBinding(
                        Sha256(row.attestation_sha256),
                        Sha256(row.context_sha256),
                        row.operation,
                        row.status,
                        row.final_lock_id,
                        Sha256(row.final_lock_sha256) if row.final_lock_sha256 else None,
                        Sha256(row.reconstruction_receipt_sha256)
                        if row.reconstruction_receipt_sha256
                        else None,
                    )
                    for row in facts.hooks
                ),
                key=lambda row: (row.context_sha256, row.operation, row.attestation_sha256),
            )
        ),
        build_bindings=tuple(
            sorted(
                (
                    BuildBinding(
                        Sha256(row.attestation_sha256),
                        Sha256(row.context_sha256),
                        Sha256(row.hook_attestation_sha256),
                        row.runtime_id,
                        row.environment_id,
                        row.operation,
                        row.final_lock_id,
                        Sha256(row.final_lock_sha256),
                        Sha256(row.reconstruction_receipt_sha256),
                        row.outcome,
                        Sha256(row.artifact_sha256) if row.artifact_sha256 else None,
                    )
                    for row in facts.builds
                ),
                key=lambda row: (row.context_sha256, row.operation, row.attestation_sha256),
            )
        ),
    )


def build_obligation_report(
    payload: dict[str, Any], source_root: str | Path, candidate_bundle_root: str | Path
) -> ObligationReport:
    """Run v2 validation and return its report; never returns a disposition."""
    return _validate_candidate_and_report(payload, source_root, candidate_bundle_root)
