from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import pytest

from scripts.deterministic_executor_manager import (
    Allocation,
    CapturedOutput,
    DeterministicExecutorManager,
    DestroyEvidence,
    EnvironmentAttestation,
    ExecutorManagerRequest,
    ManagerAdmissionReserved,
    ManagerDenied,
    ManagerPhase,
    ManagerRequestConflict,
    ManagerStateConflict,
    QuiescenceEvidence,
    ReconciliationEvidence,
    SQLiteManagerStateStore,
    VerifiedDestroy,
    VerifiedQuiescence,
    VerifiedReconciliation,
    WorkloadHandle,
    WorkloadObservation,
    WorkloadOutcome,
)
from scripts.external_admission_ledger import Admission, Claim, NetworkPolicy, ResourcePolicy


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_digest(value: object) -> str:
    return _digest(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    )


def _admission() -> Admission:
    return Admission(
        admission_id="11111111-2222-4333-8444-555555555555",
        grant_digest="a" * 64,
        repository="https://github.com/MishkaStrategy/Module-Strategy.git",
        source_sha="b" * 40,
        workflow_digest="c" * 64,
        runtime_id="python312-linux-x86_64",
        workload_id="release-validation",
        workload_digest="d" * 64,
        policy_id="protected-paper-v1",
        policy_digest="e" * 64,
        environment_id="isolated-linux-x86_64",
        image_digest="f" * 64,
        image_os="linux",
        image_arch="x86_64",
        provider_id="provider-a",
        provider_run_id="run-12345",
        provider_attempt=1,
        candidate_id="candidate-v1",
        candidate_digest="1" * 64,
        profile_id="linux-small",
        profile_digest="2" * 64,
        material_id="materials-v1",
        material_digest="3" * 64,
        required_output_paths=("artifacts/result.json", "artifacts/summary.txt"),
        resource_policy=ResourcePolicy(1000, 1_000_000, 2_000_000, 32, 120),
        network_policy=NetworkPolicy("disabled", ()),
        issued_at="2030-01-01T00:00:00Z",
        expires_at="2030-01-01T01:00:00Z",
        execution_deadline="2030-01-01T02:00:00Z",
        publication_deadline="2030-01-01T03:00:00Z",
    )


class FakeAdmissionAuthority:
    def __init__(self, admission: Admission) -> None:
        self.admission = admission
        self.claim_calls = 0
        self.claimed: Claim | None = None
        self.get_claim_calls = 0
        self.on_get_claim = None

    def is_ready(self) -> bool:
        return True

    def resolve_admission(self, admission_id: str) -> Admission:
        if admission_id != self.admission.admission_id:
            raise ValueError("unknown synthetic admission")
        return self.admission

    def get_claim(self, admission_id: str) -> Claim | None:
        self.get_claim_calls += 1
        if self.on_get_claim is not None:
            self.on_get_claim()
        if admission_id != self.admission.admission_id:
            raise ValueError("unknown synthetic admission")
        return self.claimed

    def claim(self, admission_id: str, executor_id: str) -> Claim:
        self.claim_calls += 1
        claim = Claim(admission_id, executor_id, "2030-01-01T00:30:00Z", "2030-01-01T02:00:00Z")
        self.claimed = claim
        return claim


class FakeEnvironmentAuthenticator:
    def is_ready(self) -> bool:
        return True

    def authenticate(self, allocation: Allocation, admission: Admission) -> EnvironmentAttestation:
        return EnvironmentAttestation(
            allocation.allocation_id,
            admission.environment_id,
            admission.image_digest,
            _canonical_digest(admission.resource_policy.to_mapping()),
            _canonical_digest(admission.network_policy.to_mapping()),
            admission.material_digest,
            "4" * 64,
        )


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.events: list[str] = []
        self.fail_at: str | None = None
        self.output_type = "regular"
        self.mutate_output = False
        self.reconciliation_resolved = True
        self.reconciliation_operation_settled: object = True
        self.reconciliation_operation_fenced: object = True
        self.reconciliation_whole_domain_quiescent: object = True
        self.reconciliation_absent = True
        self.reconciliation_allocation_id: str | None = None
        self.force_reconciliation_allocation_id = False
        self.reconcile_calls: list[tuple[str, str, str | None]] = []
        self.allocation_operation_keys: list[str] = []
        self.store: SQLiteManagerStateStore | None = None
        self.request_id = "request-1"

    def is_ready(self) -> bool:
        return True

    def _call(self, name: str) -> None:
        self.calls.append(name)
        self.events.append(name)
        if name == "allocate" and self.store is not None:
            state = self.store.get(self.request_id)
            assert state is not None
            assert state.phase is ManagerPhase.ALLOCATING
            assert isinstance(state.state.get("allocation_operation_key"), str)
        if self.fail_at == name:
            raise TimeoutError(f"synthetic ambiguous {name}")

    def allocate(self, admission: Admission, allocation_operation_key: str) -> Allocation:
        self.allocation_operation_keys.append(allocation_operation_key)
        self._call("allocate")
        return Allocation("allocation-1", admission.environment_id, admission.image_digest)

    def start_once(
        self, allocation: Allocation, admission: Admission, claim: Claim
    ) -> WorkloadHandle:
        self._call("start_once")
        return WorkloadHandle(allocation.allocation_id, "handle-1")

    def observe(self, handle: WorkloadHandle) -> WorkloadObservation:
        self._call("observe")
        return WorkloadObservation(WorkloadOutcome.SUCCEEDED, "5" * 64)

    def stop_and_quiesce(
        self, allocation: Allocation, handle: WorkloadHandle
    ) -> QuiescenceEvidence:
        self._call("stop_quiesce")
        return QuiescenceEvidence(allocation.allocation_id, "whole-domain", "6" * 64)

    def stop_allocation_and_quiesce(
        self, allocation: Allocation, allocation_operation_key: str
    ) -> QuiescenceEvidence:
        self._call("stop_allocation_quiesce")
        return QuiescenceEvidence(allocation.allocation_id, "whole-domain", "6" * 64)

    def collect_snapshot(
        self,
        allocation: Allocation,
        required_output_paths: tuple[str, ...],
        max_snapshot_bytes: int,
        quiescence: VerifiedQuiescence,
    ) -> tuple[CapturedOutput, ...]:
        self._call("collect_snapshot")
        outputs = []
        for index, path in enumerate(required_output_paths):
            content = f"synthetic-output-{index}".encode("ascii")
            digest = _digest(content)
            after = "0" * 64 if self.mutate_output and index == 0 else digest
            file_type = self.output_type if index == 0 else "regular"
            outputs.append(CapturedOutput(path, file_type, content, digest, after))
        return tuple(outputs)

    def destroy(self, allocation: Allocation) -> DestroyEvidence:
        self._call("destroy")
        return DestroyEvidence(allocation.allocation_id, "7" * 64)

    def reconcile(
        self,
        request_id: str,
        allocation_operation_key: str,
        allocation_id: str | None,
        state: object,
    ) -> ReconciliationEvidence:
        self._call("reconcile")
        self.reconcile_calls.append((request_id, allocation_operation_key, allocation_id))
        result_allocation_id = allocation_id
        if self.force_reconciliation_allocation_id:
            result_allocation_id = self.reconciliation_allocation_id
        return ReconciliationEvidence(
            request_id,
            allocation_operation_key,
            result_allocation_id,
            self.reconciliation_resolved,
            self.reconciliation_operation_settled,
            self.reconciliation_operation_fenced,
            self.reconciliation_whole_domain_quiescent,
            self.reconciliation_absent,
            "8" * 64,
        )


class FakeEvidenceVerifier:
    def __init__(self) -> None:
        self.whole_domain = True
        self.authenticated_quiescence = True
        self.destroyed: object = True
        self.authenticated_destroy: object = True
        self.reconciliation_key_override: str | None = None
        self.reconciliation_allocation_override: str | None = None
        self.reconciliation_allocation_override_set = False
        self.resolved_reconciliation: object | None = None
        self.operation_settled_reconciliation: object | None = None
        self.operation_fenced_reconciliation: object | None = None
        self.whole_domain_quiescent_reconciliation: object | None = None
        self.allocation_absent_reconciliation: object | None = None
        self.authenticated_reconciliation: object = True
        self.events: list[str] = []

    def is_ready(self) -> bool:
        return True

    def verify_quiescence(
        self, evidence: QuiescenceEvidence, allocation: Allocation
    ) -> VerifiedQuiescence:
        self.events.append("verify_quiescence")
        return VerifiedQuiescence(
            evidence.allocation_id,
            evidence.scope_id,
            evidence.evidence_digest,
            self.whole_domain,
            self.authenticated_quiescence,
        )

    def verify_destroy(self, evidence: DestroyEvidence, allocation: Allocation) -> VerifiedDestroy:
        self.events.append("verify_destroy")
        return VerifiedDestroy(
            evidence.allocation_id,
            evidence.evidence_digest,
            self.destroyed,
            self.authenticated_destroy,
        )

    def verify_reconciliation(self, evidence: ReconciliationEvidence) -> VerifiedReconciliation:
        self.events.append("verify_reconciliation")
        return VerifiedReconciliation(
            evidence.request_id,
            self.reconciliation_key_override or evidence.allocation_operation_key,
            self.reconciliation_allocation_override
            if self.reconciliation_allocation_override_set
            else evidence.allocation_id,
            evidence.resolved
            if self.resolved_reconciliation is None
            else self.resolved_reconciliation,
            evidence.operation_settled
            if self.operation_settled_reconciliation is None
            else self.operation_settled_reconciliation,
            evidence.operation_fenced
            if self.operation_fenced_reconciliation is None
            else self.operation_fenced_reconciliation,
            evidence.whole_domain_quiescent
            if self.whole_domain_quiescent_reconciliation is None
            else self.whole_domain_quiescent_reconciliation,
            evidence.allocation_absent
            if self.allocation_absent_reconciliation is None
            else self.allocation_absent_reconciliation,
            evidence.evidence_digest,
            self.authenticated_reconciliation,
        )


def _manager(
    tmp_path,
    *,
    backend=None,
    auth=None,
    authority=None,
    verifier=None,
    store=None,
    admission=None,
    now=None,
):
    admission = admission or _admission()
    state_store = store or SQLiteManagerStateStore(tmp_path / "manager.sqlite3")
    backend_port = backend if backend is not None else FakeBackend()
    backend_port.store = state_store
    verifier_port = verifier or FakeEvidenceVerifier()
    verifier_port.events = backend_port.events
    clock = (
        now if callable(now) else lambda: now or datetime(2030, 1, 1, 0, 30, tzinfo=timezone.utc)
    )
    manager = DeterministicExecutorManager(
        state_store=state_store,
        backend=backend_port,
        environment_authenticator=auth or FakeEnvironmentAuthenticator(),
        admission_authority=authority or FakeAdmissionAuthority(admission),
        evidence_verifier=verifier_port,
        utc_clock=clock,
    )
    return manager, state_store, backend_port, admission


def _request(admission: Admission, *, request_id: str = "request-1", max_bytes: int = 1024):
    return ExecutorManagerRequest(request_id, admission.admission_id, "executor-1", max_bytes)


def test_complete_reference_flow_is_journaled_and_replay_does_not_reexecute(tmp_path) -> None:
    manager, store, backend, admission = _manager(tmp_path)
    request = _request(admission)

    result = manager.submit(request)
    assert result.phase is ManagerPhase.RECONCILED
    assert result.workload_outcome is WorkloadOutcome.SUCCEEDED
    assert result.snapshot_digest is not None
    assert result.snapshot_size_bytes is not None
    calls = tuple(backend.calls)
    assert calls == (
        "allocate",
        "start_once",
        "observe",
        "stop_quiesce",
        "collect_snapshot",
        "destroy",
        "reconcile",
    )
    replay = manager.submit(request)
    assert replay == result
    assert tuple(backend.calls) == calls
    assert store.event_count(request.request_id) > 10
    state = store.get(request.request_id)
    assert state is not None
    assert backend.allocation_operation_keys == [state.state["allocation_operation_key"]]
    assert "snapshot_bytes_b64" in state.state
    assert "passing_receipt" not in state.state
    with pytest.raises(ManagerRequestConflict):
        manager.submit(_request(admission, max_bytes=2048))


def test_start_once_ambiguity_requires_reconciliation_and_is_never_retried(tmp_path) -> None:
    manager, store, backend, admission = _manager(tmp_path)
    backend.fail_at = "start_once"
    request = _request(admission)

    result = manager.submit(request)
    assert result.phase is ManagerPhase.RECONCILIATION_REQUIRED
    assert result.pending_operation == "start_once"
    assert backend.calls.count("start_once") == 1
    replay = manager.submit(request)
    assert replay == result
    assert backend.calls.count("start_once") == 1

    restarted_manager = DeterministicExecutorManager(
        state_store=store,
        backend=backend,
        environment_authenticator=FakeEnvironmentAuthenticator(),
        admission_authority=FakeAdmissionAuthority(admission),
        evidence_verifier=FakeEvidenceVerifier(),
        utc_clock=lambda: datetime(2030, 1, 1, 0, 30, tzinfo=timezone.utc),
    )
    assert restarted_manager.submit(request) == result
    assert backend.calls.count("start_once") == 1

    backend.fail_at = None
    backend.reconciliation_resolved = False
    reconciled = manager.reconcile_pending(request.request_id)
    assert reconciled.phase is ManagerPhase.RECONCILIATION_REQUIRED
    assert backend.calls.count("start_once") == 1
    assert backend.calls.count("reconcile") == 1
    assert store.get(request.request_id).pending_operation == "reconcile"


@pytest.mark.parametrize(
    "now",
    [
        datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc),
        datetime(2029, 12, 31, 23, 59, tzinfo=timezone.utc),
    ],
)
def test_expired_or_not_yet_issued_admission_denies_before_allocation(tmp_path, now) -> None:
    manager, store, backend, admission = _manager(tmp_path, now=now)

    with pytest.raises(ManagerDenied, match="admission"):
        manager.submit(_request(admission))

    assert backend.calls == []
    assert store.get("request-1") is None


def test_expiry_after_reservation_denies_before_allocation(tmp_path) -> None:
    times = iter(
        (
            datetime(2030, 1, 1, 0, 59, tzinfo=timezone.utc),
            datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc),
        )
    )
    manager, store, backend, admission = _manager(tmp_path, now=lambda: next(times))

    result = manager.submit(_request(admission))

    assert result.phase is ManagerPhase.DENIED
    assert backend.calls == []
    assert store.get("request-1").state["allocation_operation_key"].startswith("alloc-")


def test_clock_rechecked_after_claim_read_and_allocate_intent(tmp_path) -> None:
    admission = _admission()
    authority = FakeAdmissionAuthority(admission)
    current = {"now": datetime(2030, 1, 1, 0, 30, tzinfo=timezone.utc)}
    authority.on_get_claim = lambda: current.update(
        now=datetime(2030, 1, 1, 0, 59, tzinfo=timezone.utc)
    )
    clock_calls = {"count": 0}

    def clock():
        clock_calls["count"] += 1
        if clock_calls["count"] == 3:
            return datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc)
        return current["now"]

    manager, store, backend, _ = _manager(
        tmp_path, admission=admission, authority=authority, now=clock
    )

    result = manager.submit(_request(admission))

    assert clock_calls["count"] == 3
    assert result.phase is ManagerPhase.DENIED
    assert backend.calls == []
    assert store.get("request-1").phase is ManagerPhase.DENIED


def test_claimed_admission_denies_before_allocation(tmp_path) -> None:
    admission = _admission()
    authority = FakeAdmissionAuthority(admission)
    authority.claimed = Claim(
        admission.admission_id,
        "executor-claimed",
        "2030-01-01T00:20:00Z",
        admission.execution_deadline,
    )
    manager, store, backend, _ = _manager(tmp_path, admission=admission, authority=authority)

    with pytest.raises(ManagerDenied, match="already been claimed"):
        manager.submit(_request(admission))

    assert backend.calls == []
    assert store.get("request-1") is None


def test_unique_manager_reservation_blocks_second_request_id(tmp_path) -> None:
    manager, store, backend, admission = _manager(tmp_path)
    backend.fail_at = "allocate"
    first = _request(admission)

    result = manager.submit(first)
    assert result.phase is ManagerPhase.RECONCILIATION_REQUIRED
    assert store.get(first.request_id) is not None

    with pytest.raises(ManagerAdmissionReserved):
        manager.submit(_request(admission, request_id="request-2"))

    assert backend.calls.count("allocate") == 1
    assert store.get("request-2") is None


def test_sqlite_race_reserves_admission_for_only_one_request(tmp_path) -> None:
    store = SQLiteManagerStateStore(tmp_path / "reservation-race.sqlite3")
    admission = _admission()

    def reserve(request_id: str):
        digest = _canonical_digest({"request_id": request_id})
        key = "alloc-" + _digest(request_id.encode("ascii"))
        return store.begin_request(request_id, digest, admission.admission_id, key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reserve, request_id) for request_id in ("request-a", "request-b")]
    successes = 0
    conflicts = 0
    for future in futures:
        try:
            _, created = future.result()
            successes += int(created)
        except ManagerAdmissionReserved:
            conflicts += 1

    assert successes == 1
    assert conflicts == 1
    assert sum(store.get(request_id) is not None for request_id in ("request-a", "request-b")) == 1


def test_recovery_and_cleanup_deny_during_active_phase(tmp_path) -> None:
    manager, store, backend, admission = _manager(tmp_path)
    request = _request(admission)
    store.begin_request(
        request.request_id,
        _canonical_digest(request.payload_mapping()),
        request.admission_id,
        "alloc-" + "a" * 64,
    )

    with pytest.raises(ManagerDenied, match="RECONCILIATION_REQUIRED"):
        manager.reconcile_pending(request.request_id)
    with pytest.raises(ManagerDenied, match="RECONCILIATION_REQUIRED"):
        manager.cleanup(request.request_id)

    assert backend.calls == []


def test_ambiguous_start_allows_explicit_idempotent_known_allocation_cleanup(tmp_path) -> None:
    manager, store, backend, admission = _manager(tmp_path)
    backend.fail_at = "start_once"
    request = _request(admission)
    pending = manager.submit(request)
    operation_key = store.get(request.request_id).state["allocation_operation_key"]
    assert pending.phase is ManagerPhase.RECONCILIATION_REQUIRED
    assert store.get(request.request_id).state["allocation"]["allocation_id"] == "allocation-1"

    backend.fail_at = None
    backend.reconciliation_absent = False
    reconciled = manager.reconcile_pending(request.request_id)
    assert reconciled.phase is ManagerPhase.RECONCILIATION_REQUIRED
    assert reconciled.pending_operation == "cleanup_required"
    backend.reconciliation_absent = True
    cleaned = manager.cleanup(request.request_id)
    assert cleaned.phase is ManagerPhase.CLEANUP_RECONCILED
    calls = tuple(backend.calls)
    assert backend.calls.count("start_once") == 1
    assert backend.calls.count("stop_allocation_quiesce") == 1
    assert backend.calls.count("destroy") == 1
    assert backend.reconcile_calls[-1] == (
        request.request_id,
        operation_key,
        "allocation-1",
    )
    cleanup_stop_index = backend.events.index("stop_allocation_quiesce")
    cleanup_verify_index = backend.events.index("verify_quiescence", cleanup_stop_index + 1)
    cleanup_destroy_index = backend.events.index("destroy", cleanup_verify_index + 1)
    assert cleanup_stop_index < cleanup_verify_index < cleanup_destroy_index

    assert manager.cleanup(request.request_id) == cleaned
    assert manager.submit(request) == cleaned
    assert tuple(backend.calls) == calls


def test_unknown_allocation_reconciliation_uses_exact_persisted_operation_key(tmp_path) -> None:
    verifier = FakeEvidenceVerifier()
    verifier.reconciliation_key_override = "alloc-wrong-key"
    manager, store, backend, admission = _manager(tmp_path, verifier=verifier)
    backend.fail_at = "allocate"
    request = _request(admission)
    pending = manager.submit(request)
    operation_key = store.get(request.request_id).state["allocation_operation_key"]

    backend.fail_at = None
    reconciled = manager.reconcile_pending(request.request_id)

    assert pending.pending_operation == "allocate"
    assert backend.reconcile_calls == [(request.request_id, operation_key, None)]
    assert reconciled.phase is ManagerPhase.RECONCILIATION_REQUIRED
    assert reconciled.pending_operation == "reconcile"
    with pytest.raises(ManagerDenied, match="verified present-allocation"):
        manager.cleanup(request.request_id)
    assert "stop_allocation_quiesce" not in backend.calls
    assert "destroy" not in backend.calls


def test_absence_alone_does_not_terminalize_reconciliation(tmp_path) -> None:
    verifier = FakeEvidenceVerifier()
    verifier.operation_settled_reconciliation = False
    manager, store, backend, admission = _manager(tmp_path, verifier=verifier)
    backend.fail_at = "allocate"
    request = _request(admission)
    manager.submit(request)
    backend.fail_at = None

    result = manager.reconcile_pending(request.request_id)

    assert result.phase is ManagerPhase.RECONCILIATION_REQUIRED
    assert result.pending_operation == "reconcile"
    assert store.get(request.request_id).phase is ManagerPhase.RECONCILIATION_REQUIRED
    assert "recovered_allocation_id" not in store.get(request.request_id).state


@pytest.mark.parametrize(
    "proof_field",
    [
        "operation_settled_reconciliation",
        "operation_fenced_reconciliation",
    ],
)
def test_present_allocation_without_all_proofs_cannot_enable_cleanup(
    tmp_path, proof_field: str
) -> None:
    verifier = FakeEvidenceVerifier()
    setattr(verifier, proof_field, False)
    manager, store, backend, admission = _manager(tmp_path, verifier=verifier)
    backend.fail_at = "allocate"
    request = _request(admission)
    manager.submit(request)
    backend.fail_at = None
    backend.reconciliation_absent = False
    backend.force_reconciliation_allocation_id = True
    backend.reconciliation_allocation_id = "allocation-found"

    result = manager.reconcile_pending(request.request_id)

    assert result.phase is ManagerPhase.RECONCILIATION_REQUIRED
    assert result.pending_operation == "reconcile"
    assert "recovered_allocation_id" not in store.get(request.request_id).state
    with pytest.raises(ManagerDenied, match="verified present-allocation"):
        manager.cleanup(request.request_id)
    assert "stop_allocation_quiesce" not in backend.calls
    assert "destroy" not in backend.calls


def test_present_settled_fenced_allocation_can_be_quiesced_before_destroy(tmp_path) -> None:
    verifier = FakeEvidenceVerifier()
    verifier.whole_domain_quiescent_reconciliation = False
    manager, store, backend, admission = _manager(tmp_path, verifier=verifier)
    backend.fail_at = "allocate"
    request = _request(admission)
    manager.submit(request)
    backend.fail_at = None
    backend.reconciliation_absent = False
    backend.force_reconciliation_allocation_id = True
    backend.reconciliation_allocation_id = "allocation-found"

    reconciled = manager.reconcile_pending(request.request_id)
    assert reconciled.pending_operation == "cleanup_required"
    proof = store.get(request.request_id).state["verified_reconciliation"]
    assert proof["operation_settled"] is True
    assert proof["operation_fenced"] is True
    assert proof["whole_domain_quiescent"] is False

    backend.reconciliation_absent = True
    cleaned = manager.cleanup(request.request_id)

    assert cleaned.phase is ManagerPhase.CLEANUP_RECONCILED
    stop_index = backend.events.index("stop_allocation_quiesce")
    verify_index = backend.events.index("verify_quiescence", stop_index + 1)
    destroy_index = backend.events.index("destroy", verify_index + 1)
    assert stop_index < verify_index < destroy_index


def test_known_allocation_reconciliation_rejects_none_identity(tmp_path) -> None:
    manager, store, backend, admission = _manager(tmp_path)
    backend.fail_at = "start_once"
    request = _request(admission)
    manager.submit(request)
    backend.fail_at = None
    backend.force_reconciliation_allocation_id = True
    backend.reconciliation_allocation_id = None

    result = manager.reconcile_pending(request.request_id)

    assert backend.reconcile_calls[-1][2] == "allocation-1"
    assert result.phase is ManagerPhase.RECONCILIATION_REQUIRED
    assert result.pending_operation == "reconcile"


def test_known_allocation_reconciliation_rejects_mismatched_identity(tmp_path) -> None:
    verifier = FakeEvidenceVerifier()
    verifier.reconciliation_allocation_override_set = True
    verifier.reconciliation_allocation_override = "allocation-other"
    manager, _, backend, admission = _manager(tmp_path, verifier=verifier)
    backend.fail_at = "start_once"
    request = _request(admission)
    manager.submit(request)
    backend.fail_at = None

    result = manager.reconcile_pending(request.request_id)

    assert result.phase is ManagerPhase.RECONCILIATION_REQUIRED
    assert result.pending_operation == "reconcile"


@pytest.mark.parametrize("replacement_id", [None, "allocation-other"])
def test_recovered_allocation_identity_cannot_change_on_later_reconciliation(
    tmp_path, replacement_id: str | None
) -> None:
    manager, store, backend, admission = _manager(tmp_path)
    backend.fail_at = "allocate"
    request = _request(admission)
    manager.submit(request)
    backend.fail_at = None
    backend.reconciliation_absent = False
    backend.force_reconciliation_allocation_id = True
    backend.reconciliation_allocation_id = "allocation-recovered"

    discovered = manager.reconcile_pending(request.request_id)
    assert discovered.phase is ManagerPhase.RECONCILIATION_REQUIRED
    assert discovered.pending_operation == "cleanup_required"
    assert store.get(request.request_id).state["recovered_allocation_id"] == "allocation-recovered"

    backend.reconciliation_allocation_id = replacement_id
    rejected = manager.reconcile_pending(request.request_id)

    assert rejected.phase is ManagerPhase.RECONCILIATION_REQUIRED
    assert rejected.pending_operation == "reconcile"
    assert store.get(request.request_id).state["recovered_allocation_id"] == "allocation-recovered"


@pytest.mark.parametrize(
    ("active_phase", "pending_operation"),
    [
        (ManagerPhase.ALLOCATING, "allocate"),
        (ManagerPhase.STARTING, "start_once"),
    ],
)
def test_crash_intent_replay_and_recovery_stay_unresolved(
    tmp_path, active_phase: ManagerPhase, pending_operation: str
) -> None:
    manager, store, backend, admission = _manager(tmp_path)
    request = _request(admission)
    digest = _canonical_digest(request.payload_mapping())
    record, created = store.begin_request(
        request.request_id,
        digest,
        admission.admission_id,
        "alloc-" + "b" * 64,
    )
    assert created

    if active_phase is ManagerPhase.STARTING:
        intent = store.record_intent(
            request.request_id,
            digest,
            record.revision,
            "allocate",
            ManagerPhase.ALLOCATING,
        )
        record = store.record_result(
            request.request_id,
            digest,
            intent.revision,
            "allocate",
            ManagerPhase.ALLOCATED,
            WorkloadOutcome.UNDETERMINED,
            "authenticate",
            {
                "allocation": {
                    "allocation_id": "allocation-1",
                    "environment_id": admission.environment_id,
                    "image_digest": admission.image_digest,
                }
            },
        )
        for operation, phase, next_operation, state_patch in (
            ("authenticate", ManagerPhase.AUTHENTICATING, "claim", {}),
            (
                "claim",
                ManagerPhase.CLAIMING,
                "start_once",
                {
                    "claim": Claim(
                        admission.admission_id,
                        request.executor_id,
                        "2030-01-01T00:30:00Z",
                        admission.execution_deadline,
                    ).to_mapping()
                },
            ),
        ):
            intent = store.record_intent(
                request.request_id, digest, record.revision, operation, phase
            )
            record = store.record_result(
                request.request_id,
                digest,
                intent.revision,
                operation,
                ManagerPhase.AUTHENTICATED if operation == "authenticate" else ManagerPhase.CLAIMED,
                WorkloadOutcome.UNDETERMINED,
                next_operation,
                state_patch,
            )

    record = store.record_intent(
        request.request_id,
        digest,
        record.revision,
        pending_operation,
        active_phase,
    )
    restarted_manager = DeterministicExecutorManager(
        state_store=store,
        backend=backend,
        environment_authenticator=FakeEnvironmentAuthenticator(),
        admission_authority=FakeAdmissionAuthority(admission),
        evidence_verifier=FakeEvidenceVerifier(),
        utc_clock=lambda: datetime(2030, 1, 1, 0, 30, tzinfo=timezone.utc),
    )

    replay = restarted_manager.submit(request)
    assert replay.phase is active_phase
    assert replay.pending_operation == pending_operation
    with pytest.raises(ManagerDenied, match="RECONCILIATION_REQUIRED"):
        restarted_manager.reconcile_pending(request.request_id)
    with pytest.raises(ManagerDenied, match="RECONCILIATION_REQUIRED"):
        restarted_manager.cleanup(request.request_id)

    persisted = store.get(request.request_id)
    assert persisted is not None
    assert persisted.phase is active_phase
    assert persisted.pending_operation == pending_operation
    assert backend.calls == []


@pytest.mark.parametrize(
    ("port", "field", "pending_operation"),
    [
        ("verifier", "whole_domain", "verify_quiescence"),
        ("verifier", "authenticated_quiescence", "verify_quiescence"),
        ("verifier", "destroyed", "verify_destroy"),
        ("verifier", "authenticated_destroy", "verify_destroy"),
        ("verifier", "resolved_reconciliation", "reconcile"),
        ("verifier", "operation_settled_reconciliation", "reconcile"),
        ("verifier", "operation_fenced_reconciliation", "reconcile"),
        ("verifier", "whole_domain_quiescent_reconciliation", "reconcile"),
        ("verifier", "allocation_absent_reconciliation", "reconcile"),
        ("verifier", "authenticated_reconciliation", "reconcile"),
    ],
)
def test_truthy_string_verification_flags_fail_closed(
    tmp_path, port: str, field: str, pending_operation: str
) -> None:
    verifier = FakeEvidenceVerifier()
    setattr(verifier, field, "true")
    manager, _, backend, admission = _manager(tmp_path, verifier=verifier)

    result = manager.submit(_request(admission))

    assert result.phase is ManagerPhase.RECONCILIATION_REQUIRED
    assert result.pending_operation == pending_operation
    if pending_operation == "verify_quiescence":
        assert "collect_snapshot" not in backend.calls
    if pending_operation == "verify_destroy":
        assert "reconcile" not in backend.calls


def test_missing_ports_deny_before_journal_or_backend_effect(tmp_path) -> None:
    admission = _admission()
    store = SQLiteManagerStateStore(tmp_path / "manager.sqlite3")
    backend = FakeBackend()
    backend.store = store
    manager = DeterministicExecutorManager(
        state_store=store,
        backend=backend,
        environment_authenticator=None,
        admission_authority=FakeAdmissionAuthority(admission),
        evidence_verifier=FakeEvidenceVerifier(),
        utc_clock=lambda: datetime(2030, 1, 1, 0, 30, tzinfo=timezone.utc),
    )

    with pytest.raises(ManagerDenied, match="ports are required"):
        manager.submit(_request(admission))
    assert backend.calls == []
    assert store.get("request-1") is None


def test_state_store_compare_and_swap_rejects_stale_writer(tmp_path) -> None:
    store = SQLiteManagerStateStore(tmp_path / "cas.sqlite3")
    request = _request(_admission())
    digest = _canonical_digest(request.payload_mapping())
    initial, created = store.begin_request(
        request.request_id, digest, request.admission_id, "alloc-" + "a" * 64
    )
    assert created
    intent = store.record_intent(
        request.request_id,
        digest,
        initial.revision,
        "allocate",
        ManagerPhase.ALLOCATING,
    )
    with pytest.raises(ManagerStateConflict, match="compare-and-swap"):
        store.record_intent(
            request.request_id,
            digest,
            initial.revision,
            "allocate",
            ManagerPhase.ALLOCATING,
        )
    assert store.get(request.request_id) == intent


def test_missing_state_store_denies_before_backend_effect(tmp_path) -> None:
    admission = _admission()
    backend = FakeBackend()
    absent_store_manager = DeterministicExecutorManager(
        state_store=None,
        backend=backend,
        environment_authenticator=FakeEnvironmentAuthenticator(),
        admission_authority=FakeAdmissionAuthority(admission),
        evidence_verifier=FakeEvidenceVerifier(),
        utc_clock=lambda: datetime(2030, 1, 1, 0, 30, tzinfo=timezone.utc),
    )
    with pytest.raises(ManagerDenied, match="state store"):
        absent_store_manager.submit(_request(admission))
    assert backend.calls == []


def test_unverified_whole_domain_quiescence_prevents_collection(tmp_path) -> None:
    verifier = FakeEvidenceVerifier()
    verifier.whole_domain = False
    manager, _, backend, admission = _manager(tmp_path, verifier=verifier)

    result = manager.submit(_request(admission))
    assert result.phase is ManagerPhase.RECONCILIATION_REQUIRED
    assert result.pending_operation == "verify_quiescence"
    assert "collect_snapshot" not in backend.calls
    assert "destroy" not in backend.calls


@pytest.mark.parametrize(
    ("file_type", "mutate"), [("symlink", False), ("regular", True), ("device", False)]
)
def test_unsafe_or_mutated_output_never_produces_snapshot_or_destroys(
    tmp_path, file_type: str, mutate: bool
) -> None:
    backend = FakeBackend()
    backend.output_type = file_type
    backend.mutate_output = mutate
    manager, store, _, admission = _manager(tmp_path, backend=backend)

    result = manager.submit(_request(admission))
    assert result.phase is ManagerPhase.RECONCILIATION_REQUIRED
    assert result.pending_operation == "collect_snapshot"
    assert result.snapshot_digest is None
    assert "snapshot_bytes_b64" not in store.get("request-1").state
    assert "destroy" not in backend.calls
