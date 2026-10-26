from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from scripts.external_admission_ledger import (
    AdmissionAlreadyClaimed,
    AdmissionLedger,
    NetworkPolicy,
    ResourcePolicy,
)
from scripts.protected_admission_controller import (
    AuthenticatedProviderObservation,
    AuthenticatedExecutorObservation,
    AdmissionIssueResult,
    ControllerAdmissionError,
    MAX_CANDIDATE_BYTES,
    MAX_GRANT_BYTES,
    ProtectedAdmissionController,
    ProtectedControllerPolicy,
    TrustedControllerInputs,
)
from tests.test_fixed_workload_planner import _identity, _inputs


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
KEY = b"controller-test-key-material-at-least-32-bytes"


class Loader:
    def __init__(self, policy: ProtectedControllerPolicy | None = None) -> None:
        candidate, profile, inventory = _inputs()
        self.bundle = TrustedControllerInputs(
            policy=policy or _policy(),
            candidate=candidate,
            profile=profile,
            inventory=inventory,
            material_id="release-material-v1",
            material_digest="9" * 64,
        )
        self.bundle.inventory["resource_policies"][0]["sha256"] = (
            self.bundle.policy.resource_policy_digest
        )
        self.bundle.inventory["network_policies"][0]["sha256"] = (
            self.bundle.policy.network_policy_digest
        )
        for workload in self.bundle.inventory["workloads"]:
            workload["resource_policy"]["sha256"] = self.bundle.policy.resource_policy_digest
            workload["network_policy"]["sha256"] = self.bundle.policy.network_policy_digest
        _identity(self.bundle.inventory, "inventory_id")
        self.source_sha = candidate["source"]["commit"]

    def load_for_source(self, source_sha: str) -> TrustedControllerInputs:
        if source_sha != self.source_sha:
            raise ValueError("unknown frozen source")
        return self.bundle


class Provider:
    def __init__(self, facts: Any = None) -> None:
        self.facts = facts or _facts()

    def authenticate_current_run(self) -> AuthenticatedProviderObservation:
        return self.facts


class ExecutorAuthenticator:
    def __init__(self) -> None:
        self.calls = 0

    def authenticate_executor(
        self, *, credential: str, admission_id: str, grant_digest: str
    ) -> AuthenticatedExecutorObservation:
        self.calls += 1
        if credential != "synthetic-test-credential":
            raise ValueError("invalid synthetic test credential")
        return AuthenticatedExecutorObservation(
            executor_id="synthetic-executor",
            admission_id=admission_id,
            grant_digest=grant_digest,
            authentication_evidence_digest="b" * 64,
        )


def _policy(**changes: Any) -> ProtectedControllerPolicy:
    resource = ResourcePolicy(2000, 4_294_967_296, 10_737_418_240, 128, 3600)
    network = NetworkPolicy("disabled", ())
    values = {
        "schema_version": 1,
        "policy_id": "protected-release-v1",
        "repository": "https://github.com/MishkaStrategy/Module-Strategy.git",
        "provider_id": "github-actions",
        "workflow_id": "release-controller.yml",
        "workflow_digest": "c" * 64,
        "event": "workflow_dispatch",
        "ref": "refs/heads/integration/modular-strategy-full-rc",
        "audience": "module-strategy-release-controller",
        "grant_ttl_seconds": 300,
        "resource_policy_id": "fixed-resource-v1",
        "resource_policy_digest": hashlib.sha256(
            json.dumps(resource.to_mapping(), sort_keys=True, separators=(",", ":")).encode("ascii")
        ).hexdigest(),
        "network_policy_id": "network-disabled-v1",
        "network_policy_digest": hashlib.sha256(
            json.dumps(network.to_mapping(), sort_keys=True, separators=(",", ":")).encode("ascii")
        ).hexdigest(),
        "resource_policy": resource,
        "network_policy": network,
    }
    values.update(changes)
    return ProtectedControllerPolicy(**values)


def _facts(**changes: Any) -> AuthenticatedProviderObservation:
    values = {
        "provider_id": "github-actions",
        "run_id": "run-123",
        "attempt": 1,
        "repository": "https://github.com/MishkaStrategy/Module-Strategy.git",
        "source_sha": "d" * 40,
        "workflow_id": "release-controller.yml",
        "workflow_digest": "c" * 64,
        "event": "workflow_dispatch",
        "ref": "refs/heads/integration/modular-strategy-full-rc",
        "audience": "module-strategy-release-controller",
        "authentication_evidence_digest": "a" * 64,
    }
    values.update(changes)
    return AuthenticatedProviderObservation(**values)


def _controller(
    tmp_path: Path,
    *,
    facts: Any = None,
    loader: Any = None,
    clock: Any = None,
    executor_authenticator: Any = None,
) -> ProtectedAdmissionController:
    return ProtectedAdmissionController(
        trusted_loader=loader if loader is not None else Loader(),
        provider_authenticator=Provider(facts),
        ledger=AdmissionLedger(tmp_path / "ledger.sqlite3"),
        grant_signing_key=KEY,
        executor_authenticator=(executor_authenticator or ExecutorAuthenticator()),
        utc_clock=clock or (lambda: NOW),
    )


def _issue(controller: ProtectedAdmissionController) -> Any:
    return controller.issue()


def test_issue_binds_trusted_policy_inventory_material_and_full_provider_context(
    tmp_path: Path,
) -> None:
    result = _issue(_controller(tmp_path))
    assert len(result.admissions) == 3
    assert {row.workload_id for row in result.admissions} == {
        "paper-smoke",
        "darwin-reconnect",
        "backtest-parity",
    }
    assert result.dispatch_authorized is False
    assert result.external_trust_established is False
    assert result.acceptance_eligible is False


def test_issue_result_cannot_be_constructed_with_authority_flags_enabled(
    tmp_path: Path,
) -> None:
    result = _issue(_controller(tmp_path))
    for field in (
        "external_trust_established",
        "acceptance_eligible",
        "dispatch_authorized",
    ):
        with pytest.raises(TypeError):
            AdmissionIssueResult(
                plan=result.plan,
                admissions=result.admissions,
                **{field: True},
            )


@pytest.mark.parametrize(
    "field,value", [("audience", "forged"), ("event", "pull_request"), ("ref", "refs/heads/main")]
)
def test_wrong_provider_policy_context_denies_before_ledger_write(
    tmp_path: Path, field: str, value: str
) -> None:
    controller = _controller(tmp_path, facts=_facts(**{field: value}))
    with pytest.raises(ControllerAdmissionError, match="does not match policy"):
        _issue(controller)
    ledger = AdmissionLedger(tmp_path / "ledger.sqlite3")
    assert ledger.get("11111111-2222-4333-8444-555555555555") is None


def test_missing_trusted_configuration_or_unauthenticated_observation_denies(
    tmp_path: Path,
) -> None:
    ledger = AdmissionLedger(tmp_path / "empty.sqlite3")
    missing = ProtectedAdmissionController(
        trusted_loader=None,
        provider_authenticator=Provider(),
        ledger=ledger,
        grant_signing_key=KEY,
    )
    with pytest.raises(ControllerAdmissionError, match="configuration are required"):
        _issue(missing)
    unauthenticated = _controller(tmp_path / "bad", facts={"audience": "trusted"})
    with pytest.raises(ControllerAdmissionError, match="authenticated observation"):
        _issue(unauthenticated)


def test_oversized_trusted_input_and_grant_are_rejected(tmp_path: Path) -> None:
    loader = Loader()
    loader.bundle.candidate["oversized"] = "x" * (MAX_CANDIDATE_BYTES + 1)
    with pytest.raises(ControllerAdmissionError, match="candidate exceeds"):
        _issue(_controller(tmp_path, loader=loader))
    with sqlite3.connect(tmp_path / "ledger.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM protected_issuance").fetchone()[0] == 0

    controller = _controller(tmp_path / "grant-bound")
    with pytest.raises(ControllerAdmissionError, match="grant is empty or exceeds"):
        controller.claim(
            admission_id="not-a-real-admission",
            grant="g" * (MAX_GRANT_BYTES + 1),
            credential="synthetic-test-credential",
        )


def test_loader_candidate_source_substitution_and_policy_substitution_deny(tmp_path: Path) -> None:
    loader = Loader()
    loader.bundle.candidate["source"]["commit"] = "e" * 40
    _identity(loader.bundle.candidate, "candidate_id")
    with pytest.raises(ControllerAdmissionError, match="candidate source"):
        _issue(_controller(tmp_path, loader=loader))

    loader = Loader()
    loader.bundle.inventory["resource_policies"][0]["sha256"] = "0" * 64
    for workload in loader.bundle.inventory["workloads"]:
        workload["resource_policy"]["sha256"] = "0" * 64
    _identity(loader.bundle.inventory, "inventory_id")
    with pytest.raises(ControllerAdmissionError, match="protected policy"):
        _issue(_controller(tmp_path / "policy", loader=loader))


def test_planner_blockers_and_linux_darwin_substitution_deny(tmp_path: Path) -> None:
    loader = Loader()
    loader.bundle.profile["acceptance_requirements"].pop(1)
    _identity(loader.bundle.profile, "profile_id")
    with pytest.raises(ControllerAdmissionError, match="planner produced blockers"):
        _issue(_controller(tmp_path, loader=loader))
    with sqlite3.connect(tmp_path / "ledger.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM protected_issuance").fetchone()[0] == 0

    loader = Loader()
    loader.bundle.inventory["workloads"][1]["environment_id"] = "paper-vds-linux"
    _identity(loader.bundle.inventory, "inventory_id")
    with pytest.raises(ControllerAdmissionError, match="planning failed"):
        _issue(_controller(tmp_path / "platform", loader=loader))

    loader = Loader()
    loader.bundle.inventory["workloads"][0]["required_outputs"] = []
    _identity(loader.bundle.inventory, "inventory_id")
    with pytest.raises(ControllerAdmissionError, match="planning failed"):
        _issue(_controller(tmp_path / "outputs", loader=loader))


def test_duplicate_concurrent_issuance_returns_exact_replay(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    barrier = threading.Barrier(2)

    def attempt() -> Any:
        barrier.wait(timeout=5)
        try:
            return _issue(controller)
        except ControllerAdmissionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert all(result is not None for result in results)
    assert results[0].admissions == results[1].admissions


def test_same_run_attempt_returns_exact_durable_grant_bundle(tmp_path: Path) -> None:
    clock_value = [NOW]
    loader = Loader()
    controller = _controller(tmp_path, loader=loader, clock=lambda: clock_value[0])
    first = _issue(controller)
    clock_value[0] = datetime(2030, 1, 1, 0, 1, tzinfo=timezone.utc)
    controller._provider.facts = _facts(authentication_evidence_digest="f" * 64)
    replay = _issue(controller)
    assert replay.admissions == first.admissions
    controller.claim(
        admission_id=first.admissions[0].admission_id,
        grant=first.admissions[0].grant,
        credential="synthetic-test-credential",
    )


def test_same_run_attempt_with_different_content_is_a_collision(tmp_path: Path) -> None:
    loader = Loader()
    controller = _controller(tmp_path, loader=loader)
    _issue(controller)
    loader.bundle = TrustedControllerInputs(
        policy=loader.bundle.policy,
        candidate=loader.bundle.candidate,
        profile=loader.bundle.profile,
        inventory=loader.bundle.inventory,
        material_id=loader.bundle.material_id,
        material_digest="6" * 64,
    )
    with pytest.raises(
        ControllerAdmissionError, match="conflicts with its immutable admission bundle"
    ):
        _issue(controller)


def test_batch_persistence_failure_rolls_back_every_admission_and_reservation(
    tmp_path: Path,
) -> None:
    controller = _controller(tmp_path)
    database = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TRIGGER fail_second_admission BEFORE INSERT ON admissions
               WHEN (SELECT COUNT(*) FROM admissions) >= 1
               BEGIN SELECT RAISE(ABORT, 'injected batch failure'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected batch failure"):
        _issue(controller)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM admissions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM protected_issuance").fetchone()[0] == 0
        connection.execute("DROP TRIGGER fail_second_admission")
    # The rolled-back reservation permits a clean retry; no partial grant set survived.
    assert len(_issue(controller).admissions) == 3


def test_signed_grant_validation_precedes_atomic_one_use_claim(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    result = _issue(controller)
    issued = result.admissions[0]
    wrong_audience = _controller(tmp_path / "audience", facts=_facts(audience="other"))
    # A correctly signed token still cannot be consumed from a different authenticated audience.
    with pytest.raises(ControllerAdmissionError, match="audience does not match grant"):
        wrong_audience.claim(
            admission_id=issued.admission_id,
            grant=issued.grant,
            credential="synthetic-test-credential",
        )

    encoded, bearer = issued.grant.rsplit(".", 1)
    envelope = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    signature = envelope["signature"]
    envelope["signature"] = ("A" if signature[0] != "A" else "B") + signature[1:]
    forged = (
        base64.urlsafe_b64encode(
            json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        )
        .decode()
        .rstrip("=")
        + "."
        + bearer
    )
    with pytest.raises(ControllerAdmissionError, match="signature"):
        controller.claim(
            admission_id=issued.admission_id, grant=forged, credential="synthetic-test-credential"
        )
    assert AdmissionLedger(tmp_path / "ledger.sqlite3").get_claim(issued.admission_id) is None

    with pytest.raises(ControllerAdmissionError, match="executor identity"):
        controller.claim(
            admission_id=issued.admission_id,
            grant=issued.grant,
            credential="synthetic-test-credential",
            executor_id="caller-invented-id",
        )
    with pytest.raises(ControllerAdmissionError, match="executor authentication failed"):
        controller.claim(
            admission_id=issued.admission_id,
            grant=issued.grant,
            credential="invalid-credential",
        )
    assert AdmissionLedger(tmp_path / "ledger.sqlite3").get_claim(issued.admission_id) is None

    controller.claim(
        admission_id=issued.admission_id, grant=issued.grant, credential="synthetic-test-credential"
    )
    with pytest.raises(AdmissionAlreadyClaimed):
        controller.claim(
            admission_id=issued.admission_id,
            grant=issued.grant,
            credential="synthetic-test-credential",
        )


def test_concurrent_authenticated_claim_has_one_winner(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    issued = _issue(controller).admissions[0]
    barrier = threading.Barrier(2)

    def attempt(executor: str) -> str:
        barrier.wait(timeout=5)
        try:
            controller.claim(
                admission_id=issued.admission_id,
                grant=issued.grant,
                credential="synthetic-test-credential",
            )
            return "claimed"
        except AdmissionAlreadyClaimed:
            return "replay_rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ("executor-a", "executor-b")))
    assert sorted(outcomes) == ["claimed", "replay_rejected"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("attempt", 2),
        ("run_id", "other-run"),
        ("source_sha", "e" * 40),
        ("provider_id", "other-provider"),
    ],
)
def test_expired_grant_and_provider_source_run_attempt_mismatch_deny(
    tmp_path: Path, field: str, value: Any
) -> None:
    controller = _controller(tmp_path)
    issued = _issue(controller).admissions[0]
    controller._clock = lambda: datetime(2030, 1, 1, 0, 6, tzinfo=timezone.utc)
    with pytest.raises(ControllerAdmissionError, match="expired"):
        controller.claim(
            admission_id=issued.admission_id,
            grant=issued.grant,
            credential="synthetic-test-credential",
        )
    mismatch = _controller(tmp_path / "mismatch", facts=_facts(**{field: value}))
    with pytest.raises(ControllerAdmissionError, match="provider run does not match"):
        mismatch.claim(
            admission_id=issued.admission_id,
            grant=issued.grant,
            credential="synthetic-test-credential",
        )


def test_executor_authentication_delay_cannot_extend_grant_ttl(tmp_path: Path) -> None:
    current_time = [NOW]

    class AdvancingExecutorAuthenticator(ExecutorAuthenticator):
        def authenticate_executor(
            self, *, credential: str, admission_id: str, grant_digest: str
        ) -> AuthenticatedExecutorObservation:
            identity = super().authenticate_executor(
                credential=credential, admission_id=admission_id, grant_digest=grant_digest
            )
            current_time[0] = datetime(2030, 1, 1, 0, 5, tzinfo=timezone.utc)
            return identity

    controller = _controller(
        tmp_path,
        clock=lambda: current_time[0],
        executor_authenticator=AdvancingExecutorAuthenticator(),
    )
    issued = _issue(controller).admissions[0]
    with pytest.raises(ControllerAdmissionError, match="grant expired"):
        controller.claim(
            admission_id=issued.admission_id,
            grant=issued.grant,
            credential="synthetic-test-credential",
        )
    assert AdmissionLedger(tmp_path / "ledger.sqlite3").get_claim(issued.admission_id) is None


def test_sqlite_write_lock_wait_cannot_extend_grant_ttl(tmp_path: Path) -> None:
    current_time = [NOW]
    begin_attempted = threading.Event()

    database = tmp_path / "ledger.sqlite3"
    controller = _controller(
        tmp_path,
        clock=lambda: current_time[0],
    )
    issued = _issue(controller).admissions[0]
    lock_connection = sqlite3.connect(database, timeout=5)
    lock_connection.execute("BEGIN IMMEDIATE")
    ledger_connect = controller._ledger._connect

    def traced_connect() -> sqlite3.Connection:
        connection = ledger_connect()

        def trace_statement(statement: str) -> None:
            if statement.strip().upper() == "BEGIN IMMEDIATE":
                # This runs at SQLite's BEGIN attempt while the test still owns
                # the write lock, so the ledger's clock must be read afterward.
                current_time[0] = datetime(2030, 1, 1, 0, 5, tzinfo=timezone.utc)
                begin_attempted.set()

        connection.set_trace_callback(trace_statement)
        return connection

    controller._ledger._connect = traced_connect
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            controller.claim,
            admission_id=issued.admission_id,
            grant=issued.grant,
            credential="synthetic-test-credential",
        )
        assert begin_attempted.wait(timeout=5)
        lock_connection.rollback()
        lock_connection.close()
        with pytest.raises(ControllerAdmissionError, match="grant expired"):
            future.result(timeout=5)
    assert AdmissionLedger(database).get_claim(issued.admission_id) is None


def test_cross_repository_grant_replay_is_rejected(tmp_path: Path) -> None:
    issuer = _controller(tmp_path)
    issued = _issue(issuer).admissions[0]
    encoded, bearer = issued.grant.split(".", 1)
    envelope = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    envelope["payload"]["repository"] = "https://github.com/attacker/repository.git"
    signed_payload = json.dumps(
        envelope["payload"], sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    envelope["signature"] = (
        base64.urlsafe_b64encode(hmac.new(KEY, signed_payload, hashlib.sha256).digest())
        .decode("ascii")
        .rstrip("=")
    )
    forged = (
        base64.urlsafe_b64encode(
            json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("ascii")
        )
        .decode("ascii")
        .rstrip("=")
        + "."
        + bearer
    )
    with pytest.raises(ControllerAdmissionError, match="does not match grant"):
        issuer.claim(
            admission_id=issued.admission_id,
            grant=forged,
            credential="synthetic-test-credential",
        )
    assert AdmissionLedger(tmp_path / "ledger.sqlite3").get_claim(issued.admission_id) is None


def test_claim_requires_explicit_executor_authenticator(tmp_path: Path) -> None:
    ledger = AdmissionLedger(tmp_path / "ledger.sqlite3")
    controller = ProtectedAdmissionController(
        trusted_loader=Loader(),
        provider_authenticator=Provider(),
        ledger=ledger,
        grant_signing_key=KEY,
        executor_authenticator=None,
        utc_clock=lambda: NOW,
    )
    issued = _issue(controller).admissions[0]
    with pytest.raises(ControllerAdmissionError, match="executor authenticators"):
        controller.claim(
            admission_id=issued.admission_id,
            grant=issued.grant,
            credential="synthetic-test-credential",
        )
    assert ledger.get_claim(issued.admission_id) is None


@pytest.mark.parametrize(
    "malformed",
    ["x" * 20, "%%%..token", "e30.token.extra", "e30." + "a" * 42],
)
def test_malformed_bounded_grant_is_denied_without_claim(tmp_path: Path, malformed: str) -> None:
    controller = _controller(tmp_path)
    issued = _issue(controller).admissions[0]
    with pytest.raises(ControllerAdmissionError):
        controller.claim(
            admission_id=issued.admission_id,
            grant=malformed,
            credential="synthetic-test-credential",
        )
    assert AdmissionLedger(tmp_path / "ledger.sqlite3").get_claim(issued.admission_id) is None


def test_duplicate_json_fields_in_grant_are_rejected(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    issued = _issue(controller).admissions[0]
    encoded, bearer = issued.grant.split(".", 1)
    envelope = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    duplicate_field_envelope = (
        '{"payload":'
        + json.dumps(envelope["payload"], sort_keys=True, separators=(",", ":"))
        + ',"signature":'
        + json.dumps(envelope["signature"])
        + ',"signature":'
        + json.dumps(envelope["signature"])
        + "}"
    )
    malformed = (
        base64.urlsafe_b64encode(duplicate_field_envelope.encode("ascii"))
        .decode("ascii")
        .rstrip("=")
        + "."
        + bearer
    )
    with pytest.raises(ControllerAdmissionError, match="duplicate JSON field"):
        controller.claim(
            admission_id=issued.admission_id,
            grant=malformed,
            credential="synthetic-test-credential",
        )
    assert AdmissionLedger(tmp_path / "ledger.sqlite3").get_claim(issued.admission_id) is None


def test_policy_mapping_is_closed_and_versioned() -> None:
    mapping = _policy().to_mapping()
    assert ProtectedControllerPolicy.from_mapping(mapping) == _policy()
    with pytest.raises(ControllerAdmissionError, match="missing or unknown"):
        ProtectedControllerPolicy.from_mapping({**mapping, "candidate_override": True})
    with pytest.raises(ControllerAdmissionError, match="schema_version"):
        ProtectedControllerPolicy.from_mapping({**mapping, "schema_version": 2})


def test_policy_json_schema_is_closed_and_enforces_bounds() -> None:
    schema_path = Path(__file__).parents[1] / "schemas" / "protected_controller_policy.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    valid_policy = _policy().to_mapping()
    assert validator.is_valid(valid_policy)
    assert not validator.is_valid({**valid_policy, "candidate_override": True})
    assert not validator.is_valid({**valid_policy, "grant_ttl_seconds": 3601})


def test_claim_rejects_weak_grant_key_before_authentication_or_mutation(tmp_path: Path) -> None:
    authenticator = ExecutorAuthenticator()
    controller = _controller(tmp_path, executor_authenticator=authenticator)
    issued = _issue(controller).admissions[0]
    controller._key = b"weak-key"
    with pytest.raises(ControllerAdmissionError, match="authenticators and grant verification key"):
        controller.claim(
            admission_id=issued.admission_id,
            grant=issued.grant,
            credential="synthetic-test-credential",
        )
    assert authenticator.calls == 0
    assert AdmissionLedger(tmp_path / "ledger.sqlite3").get_claim(issued.admission_id) is None
