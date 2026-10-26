from __future__ import annotations

import base64
import hashlib
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from dataclasses import replace
import uuid
from collections.abc import Callable, Mapping
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from stat import S_ISREG

from scripts.external_admission_ledger import (
    Admission,
    AdmissionLedger,
    NetworkPolicy,
    ResourcePolicy,
)
import scripts.external_execution_publisher as publisher_module
from scripts.external_execution_publisher import LocalExecutionPublisher, PublicationError
from scripts.verify_external_execution_receipt import MAX_RECEIPT_BYTES, canonical_json_bytes
from scripts.candidate_evidence_bridge import (
    EvidenceBridgeError,
    FixedWorkloadMapping,
    OutputEvidenceRequirement,
    verify_candidate_evidence_publication,
)


def _readonly_file_state(
    root: Path, databases: tuple[Path, ...]
) -> tuple[tuple[tuple[str, int, int], ...], dict[str, tuple[int, int]]]:
    sidecar_paths = {
        Path(f"{database}{suffix}") for database in databases for suffix in ("-wal", "-shm")
    }
    logical_files = tuple(
        sorted(
            (path.relative_to(root).as_posix(), path.stat().st_size, path.stat().st_mtime_ns)
            for path in root.rglob("*")
            if path.is_file() and path not in sidecar_paths
        )
    )
    sidecars: dict[str, tuple[int, int]] = {}
    for path in sorted(sidecar_paths):
        if not path.exists():
            continue
        if path.is_symlink() or not S_ISREG(path.lstat().st_mode):
            raise AssertionError(f"SQLite coordination sidecar is not a regular file: {path}")
        info = path.stat()
        sidecars[path.relative_to(root).as_posix()] = (info.st_size, info.st_mtime_ns)
    return logical_files, sidecars


class ExternalExecutionPublisherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("openssl") is None:
            raise unittest.SkipTest("OpenSSL CLI is required")

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.keys = self.root / "trusted"
        self.keys.mkdir()
        self.private_key = self.root / "controller.pem"
        subprocess.run(
            ["openssl", "genpkey", "-algorithm", "Ed25519", "-out", str(self.private_key)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "openssl",
                "pkey",
                "-in",
                str(self.private_key),
                "-pubout",
                "-out",
                str(self.keys / "controller.pub.pem"),
            ],
            check=True,
            capture_output=True,
        )
        self.ledger = AdmissionLedger(self.root / "admissions.sqlite3")
        self.output_bytes = b"trusted artifact bytes\n"
        output_digest = hashlib.sha256(self.output_bytes).hexdigest()
        self.candidate = {
            "source": {
                "repository": "https://github.com/MishkaStrategy/Module-Strategy.git",
                "commit": "b" * 40,
            },
            "components": [{"id": "strategy-core"}],
            "artifacts": [{"component": "strategy-core", "file": {"sha256": output_digest}}],
        }
        self.candidate["candidate_id"] = hashlib.sha256(
            canonical_json_bytes(self.candidate)
        ).hexdigest()
        self.profile = {
            "target_environments": ["linux-vds"],
            "python_runtimes": [{"id": "python312-linux-x86_64"}],
        }
        self.profile["acceptance_requirements"] = [
            {"gate": "native-smoke", "component_id": "strategy-core", "environment": "linux-vds"}
        ]
        self.profile["profile_id"] = hashlib.sha256(canonical_json_bytes(self.profile)).hexdigest()
        self.admission = Admission(
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
            candidate_id=self.candidate["candidate_id"],
            candidate_digest=hashlib.sha256(canonical_json_bytes(self.candidate)).hexdigest(),
            profile_id=self.profile["profile_id"],
            profile_digest=hashlib.sha256(canonical_json_bytes(self.profile)).hexdigest(),
            material_id="release-materials-v1",
            material_digest="3" * 64,
            required_output_paths=("artifacts/result.json",),
            resource_policy=ResourcePolicy(2000, 4_294_967_296, 10_737_418_240, 256, 3600),
            network_policy=NetworkPolicy("disabled", ()),
            issued_at="2030-01-01T00:00:00Z",
            expires_at="2030-01-01T00:30:00Z",
            execution_deadline="2030-01-01T02:00:00Z",
            publication_deadline="2030-01-01T03:00:00Z",
        )
        self.ledger.issue(self.admission)
        self.ledger.claim(
            self.admission.admission_id,
            self.admission.grant_digest,
            "executor-1",
            "2030-01-01T00:01:00Z",
        )
        self.publisher = LocalExecutionPublisher(
            self.ledger,
            self.root / "outbox.sqlite3",
            self.root / "evidence-store",
            self.keys,
        )
        self.stage = self.root / "stage"
        (self.stage / "artifacts").mkdir(parents=True)
        (self.stage / "artifacts/result.json").write_bytes(self.output_bytes)
        self.payload = self._payload()
        self.receipt_path = self._write_receipt(self.payload)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _payload(self) -> dict:
        output_digest = hashlib.sha256(self.output_bytes).hexdigest()
        return {
            "admission_id": self.admission.admission_id,
            "repository": self.admission.repository,
            "source_sha": self.admission.source_sha,
            "workflow_digest": self.admission.workflow_digest,
            "runtime_id": self.admission.runtime_id,
            "workload_id": self.admission.workload_id,
            "workload_digest": self.admission.workload_digest,
            "material_digest": self.admission.material_digest,
            "policy_id": self.admission.policy_id,
            "policy_digest": self.admission.policy_digest,
            "environment_id": self.admission.environment_id,
            "admission_digest": self._admission_digest(),
            "observed_provider": {
                "provider_id": self.admission.provider_id,
                "run_id": self.admission.provider_run_id,
                "attempt": self.admission.provider_attempt,
            },
            "observed_resource_policy_digest": self._mapping_digest(
                self.admission.resource_policy.to_mapping()
            ),
            "observed_network_policy_digest": self._mapping_digest(
                self.admission.network_policy.to_mapping()
            ),
            "executor": {
                "executor_id": "executor-1",
                "ephemeral": True,
                "image_digest": self.admission.image_digest,
                "image_os": self.admission.image_os,
                "image_arch": self.admission.image_arch,
                "observed_os": "linux",
                "observed_arch": "x86_64",
                "created_at": "2030-01-01T00:00:30Z",
            },
            "started_at": "2030-01-01T00:02:00Z",
            "ended_at": "2030-01-01T00:03:00Z",
            "destroyed_at": "2030-01-01T00:04:00Z",
            "outcome": "passed",
            "exit_code": 0,
            "outputs": [
                {
                    "path": "artifacts/result.json",
                    "sha256": output_digest,
                    "size_bytes": len(self.output_bytes),
                }
            ],
        }

    def _mapping_digest(self, value: dict) -> str:
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
                "ascii"
            )
        ).hexdigest()

    def _admission_digest(self) -> str:
        return self._mapping_digest(self.admission.to_mapping())

    def _write_receipt(self, payload: dict, path: Path | None = None) -> Path:
        data = canonical_json_bytes(
            {"schema_version": 1, "key_id": "controller", "payload": payload}
        )
        payload_path = self.root / "signed-input.json"
        signature_path = self.root / "signature.bin"
        payload_path.write_bytes(data)
        subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-sign",
                "-inkey",
                str(self.private_key),
                "-rawin",
                "-in",
                str(payload_path),
                "-out",
                str(signature_path),
            ],
            check=True,
            capture_output=True,
        )
        envelope = {
            "schema_version": 1,
            "key_id": "controller",
            "payload": payload,
            "signature": base64.b64encode(signature_path.read_bytes()).decode("ascii"),
        }
        output = path or self.root / "receipt.json"
        output.write_text(json.dumps(envelope), encoding="utf-8")
        return output

    def _prepare(
        self,
        *,
        receipt_path: Path | None = None,
        staged_outputs: Path | None = None,
        now: Callable[[], str] | None = None,
    ) -> str:
        return self.publisher.prepare(
            self.admission.admission_id,
            self.receipt_path if receipt_path is None else receipt_path,
            self.stage if staged_outputs is None else staged_outputs,
            (lambda: "2030-01-01T00:05:00Z") if now is None else now,
        )

    def _evidence(self, **changes: object) -> dict:
        evidence = {
            "candidate_id": self.candidate["candidate_id"],
            "profile_id": self.profile["profile_id"],
            "source_commit": self.admission.source_sha,
            "component_id": "strategy-core",
            "artifact_sha256": hashlib.sha256(self.output_bytes).hexdigest(),
            "gate": "native-smoke",
            "environment": "linux-vds",
            "runtime_id": self.admission.runtime_id,
            "run_id": self.admission.provider_run_id,
            "attempt": self.admission.provider_attempt,
            "outcome": "passed",
            "evidence_sha256": "0" * 64,
        }
        evidence.update(changes)
        body = {key: value for key, value in evidence.items() if key != "evidence_sha256"}
        evidence["evidence_sha256"] = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
        return evidence

    def _workload_mapping(self) -> FixedWorkloadMapping:
        return FixedWorkloadMapping(
            workload_id=self.admission.workload_id,
            workload_digest=self.admission.workload_digest,
            policy_id=self.admission.policy_id,
            policy_digest=self.admission.policy_digest,
            material_id=self.admission.material_id,
            material_digest=self.admission.material_digest,
            environment_id=self.admission.environment_id,
            target_environment="linux-vds",
            runtime_id=self.admission.runtime_id,
            required_outputs=(
                OutputEvidenceRequirement(
                    output_path="artifacts/result.json",
                    gate="native-smoke",
                    component_id="strategy-core",
                    artifact_sha256=hashlib.sha256(self.output_bytes).hexdigest(),
                ),
            ),
        )

    def _publish_v3_for_profile(
        self, acceptance_requirements: object
    ) -> tuple[LocalExecutionPublisher, str]:
        profile = {key: value for key, value in self.profile.items() if key != "profile_id"}
        profile["acceptance_requirements"] = acceptance_requirements
        profile["profile_id"] = hashlib.sha256(canonical_json_bytes(profile)).hexdigest()
        self.profile = profile
        suffix = profile["profile_id"][:12]
        self.admission = replace(
            self.admission,
            admission_id=str(uuid.UUID(hex=profile["profile_id"][:32])),
            profile_id=profile["profile_id"],
            profile_digest=hashlib.sha256(canonical_json_bytes(profile)).hexdigest(),
        )
        ledger = AdmissionLedger(self.root / f"v3-admissions-{suffix}.sqlite3")
        ledger.issue(self.admission)
        ledger.claim(
            self.admission.admission_id,
            self.admission.grant_digest,
            "executor-1",
            "2030-01-01T00:01:00Z",
        )
        publisher = LocalExecutionPublisher(
            ledger,
            self.root / f"v3-outbox-{suffix}.sqlite3",
            self.root / f"v3-evidence-store-{suffix}",
            self.keys,
        )
        receipt_path = self._write_receipt(self._payload(), self.root / f"v3-receipt-{suffix}.json")
        publication_id = publisher.prepare(
            self.admission.admission_id,
            receipt_path,
            self.stage,
            lambda: "2030-01-01T00:05:00Z",
        )
        publisher.commit(publication_id)
        return publisher, publication_id

    def test_prepare_commit_writes_content_addressed_bundle_idempotently(self) -> None:
        publication_id = self._prepare()
        self.assertEqual(self._prepare(), publication_id)
        first = self.publisher.commit(publication_id)
        self.assertTrue(first.is_file())
        self.assertEqual(self.publisher.commit(publication_id), first)
        bundle = json.loads(first.read_text(encoding="ascii"))
        self.assertEqual(bundle["admission_id"], self.admission.admission_id)
        self.assertEqual(
            bundle["outputs"][0]["sha256"], hashlib.sha256(self.output_bytes).hexdigest()
        )
        self.assertTrue(
            (self.root / "evidence-store" / "blobs" / bundle["outputs"][0]["sha256"]).is_file()
        )
        receipt_digest = hashlib.sha256(self.receipt_path.read_bytes()).hexdigest()
        stored_receipt = self.root / "evidence-store" / "receipts" / f"{receipt_digest}.json"
        self.assertEqual(stored_receipt.read_bytes(), self.receipt_path.read_bytes())

    def test_committed_snapshot_and_evidence_bridge_are_read_only_and_never_grant_acceptance(
        self,
    ) -> None:
        publication_id = self._prepare()
        self.publisher.commit(publication_id)

        databases = (Path(self.ledger.database), Path(self.publisher.outbox_database))
        before_files, before_sidecars = _readonly_file_state(self.root, databases)
        snapshot = self.publisher.read_committed(publication_id)
        report = verify_candidate_evidence_publication(
            self.publisher,
            publication_id,
            self.candidate,
            self.profile,
            [self._evidence()],
            self._workload_mapping(),
        )
        self.assertEqual(snapshot.outputs[0].content, self.output_bytes)
        self.assertTrue(report.bindings_valid)
        self.assertTrue(report.receipt_signature_verified_under_configured_key)
        self.assertFalse(report.external_trust_established)
        self.assertFalse(report.acceptance_eligible)
        after_files, after_sidecars = _readonly_file_state(self.root, databases)
        self.assertEqual(before_files, after_files)
        allowed_sidecars = {
            Path(f"{database}{suffix}").relative_to(self.root).as_posix()
            for database in databases
            for suffix in ("-wal", "-shm")
        }
        changed_sidecars = {
            path
            for path in before_sidecars.keys() | after_sidecars.keys()
            if before_sidecars.get(path) != after_sidecars.get(path)
        }
        self.assertLessEqual(changed_sidecars, allowed_sidecars)

    def test_v3_evidence_bridge_requires_exact_declared_acceptance_tuple(self) -> None:
        publisher, publication_id = self._publish_v3_for_profile(
            [{"gate": "native-smoke", "component_id": "strategy-core", "environment": "linux-vds"}]
        )
        report = verify_candidate_evidence_publication(
            publisher,
            publication_id,
            self.candidate,
            self.profile,
            [self._evidence()],
            self._workload_mapping(),
            manifest_schema_version=3,
        )
        self.assertTrue(report.bindings_valid)
        self.assertFalse(report.external_trust_established)
        self.assertFalse(report.acceptance_eligible)

        changed_requirements = (
            ("undeclared", "strategy-core", "linux-vds"),
            ("native-smoke", "other-component", "linux-vds"),
            ("native-smoke", "strategy-core", "darwin-acceptance"),
        )
        for gate, component_id, environment in changed_requirements:
            mapping = self._workload_mapping()
            mapping = FixedWorkloadMapping(
                **{
                    **mapping.__dict__,
                    "target_environment": environment,
                    "required_outputs": (
                        OutputEvidenceRequirement(
                            "artifacts/result.json",
                            gate,
                            component_id,
                            hashlib.sha256(self.output_bytes).hexdigest(),
                        ),
                    ),
                }
            )
            with self.subTest(gate=gate, component_id=component_id, environment=environment):
                with self.assertRaisesRegex(
                    EvidenceBridgeError, "declared v3 acceptance requirement"
                ):
                    verify_candidate_evidence_publication(
                        publisher,
                        publication_id,
                        self.candidate,
                        self.profile,
                        [self._evidence()],
                        mapping,
                        manifest_schema_version=3,
                    )

    def test_v3_evidence_bridge_rejects_missing_or_malformed_acceptance_requirements(self) -> None:
        for requirements in (
            None,
            [],
            [{"gate": "native-smoke", "component_id": "strategy-core"}],
        ):
            with self.subTest(requirements=requirements):
                publisher, publication_id = self._publish_v3_for_profile(requirements)
                with self.assertRaisesRegex(EvidenceBridgeError, "acceptance requirement"):
                    verify_candidate_evidence_publication(
                        publisher,
                        publication_id,
                        self.candidate,
                        self.profile,
                        [self._evidence()],
                        self._workload_mapping(),
                        manifest_schema_version=3,
                    )

    def test_v2_evidence_bridge_keeps_legacy_acceptance_requirement_behavior(self) -> None:
        publisher, publication_id = self._publish_v3_for_profile([])
        report = verify_candidate_evidence_publication(
            publisher,
            publication_id,
            self.candidate,
            self.profile,
            [self._evidence()],
            self._workload_mapping(),
        )
        self.assertTrue(report.bindings_valid)
        self.assertFalse(report.external_trust_established)
        self.assertFalse(report.acceptance_eligible)

    def test_prepared_publication_is_not_read_or_recovered_by_evidence_bridge(self) -> None:
        publication_id = self._prepare()
        database = Path(self.publisher.outbox_database)
        before_files, before_sidecars = _readonly_file_state(self.root, (database,))
        with self.assertRaisesRegex(PublicationError, "not committed"):
            self.publisher.read_committed(publication_id)
        after_files, after_sidecars = _readonly_file_state(self.root, (database,))
        self.assertEqual(before_files, after_files)
        allowed_sidecars = {
            Path(f"{database}{suffix}").relative_to(self.root).as_posix()
            for suffix in ("-wal", "-shm")
        }
        changed_sidecars = {
            path
            for path in before_sidecars.keys() | after_sidecars.keys()
            if before_sidecars.get(path) != after_sidecars.get(path)
        }
        self.assertLessEqual(changed_sidecars, allowed_sidecars)
        self.assertFalse((self.root / "evidence-store" / "bundles").exists())

    def test_read_committed_rejects_tampered_content_addressed_bundle_and_blob(self) -> None:
        publication_id = self._prepare()
        bundle_path = self.publisher.commit(publication_id)
        bundle_path.write_bytes(b"tampered bundle")
        with self.assertRaisesRegex(
            PublicationError, "size does not match|bundle or receipt differs"
        ):
            self.publisher.read_committed(publication_id)
        connection = self.publisher._connect_readonly()
        try:
            bundle_row = connection.execute(
                "SELECT bundle_json FROM publications WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
            canonical_bundle = bytes(bundle_row["bundle_json"])
        finally:
            connection.close()
        bundle_path.write_bytes(canonical_bundle)
        digest = hashlib.sha256(self.output_bytes).hexdigest()
        (self.root / "evidence-store" / "blobs" / digest).write_bytes(b"X" * len(self.output_bytes))
        with self.assertRaisesRegex(PublicationError, "content-addressed output"):
            self.publisher.read_committed(publication_id)

    def test_read_committed_does_not_recreate_a_missing_admission_database(self) -> None:
        publication_id = self._prepare()
        self.publisher.commit(publication_id)
        database = Path(self.ledger.database)
        database.unlink()
        for suffix in ("-wal", "-shm"):
            Path(f"{database}{suffix}").unlink(missing_ok=True)
        with self.assertRaisesRegex(PublicationError, "read-only admission snapshot failed"):
            self.publisher.read_committed(publication_id)
        self.assertFalse(database.exists())

    def test_read_committed_uses_consistent_snapshot_during_active_writers(self) -> None:
        publication_id = self._prepare()
        self.publisher.commit(publication_id)
        outbox = sqlite3.connect(self.publisher.outbox_database, isolation_level=None)
        admissions = sqlite3.connect(self.ledger.database, isolation_level=None)
        try:
            outbox.execute("BEGIN IMMEDIATE")
            outbox.execute(
                "UPDATE publications SET status='PREPARED' WHERE publication_id = ?",
                (publication_id,),
            )
            admissions.execute("BEGIN IMMEDIATE")
            admissions.execute(
                "UPDATE admissions SET executor_id='uncommitted-writer' WHERE admission_id = ?",
                (self.admission.admission_id,),
            )
            wal_files = tuple(
                Path(f"{database}-wal")
                for database in (self.publisher.outbox_database, self.ledger.database)
            )
            wal_canaries = tuple(path.read_bytes() for path in wal_files)

            snapshot = self.publisher.read_committed(publication_id)

            self.assertEqual(snapshot.outputs[0].content, self.output_bytes)
            self.assertTrue(outbox.in_transaction)
            self.assertTrue(admissions.in_transaction)
            self.assertEqual(
                outbox.execute(
                    "SELECT status FROM publications WHERE publication_id = ?",
                    (publication_id,),
                ).fetchone()[0],
                "PREPARED",
            )
            self.assertEqual(
                admissions.execute(
                    "SELECT executor_id FROM admissions WHERE admission_id = ?",
                    (self.admission.admission_id,),
                ).fetchone()[0],
                "uncommitted-writer",
            )
            self.assertEqual(tuple(path.read_bytes() for path in wal_files), wal_canaries)
        finally:
            if outbox.in_transaction:
                outbox.rollback()
            if admissions.in_transaction:
                admissions.rollback()
            outbox.close()
            admissions.close()

        after_rollback = self.publisher.read_committed(publication_id)
        self.assertEqual(after_rollback.claim.executor_id, "executor-1")

    def test_read_committed_preserves_invalid_wal_canary_and_reads_checkpointed_state(self) -> None:
        publication_id = self._prepare()
        self.publisher.commit(publication_id)
        sidecar = Path(f"{self.publisher.outbox_database}-wal")
        canary = b"damaged-wal-canary"
        sidecar.write_bytes(canary)

        snapshot = self.publisher.read_committed(publication_id)

        self.assertEqual(snapshot.publication_id, publication_id)
        self.assertEqual(snapshot.outputs[0].content, self.output_bytes)
        self.assertEqual(sidecar.read_bytes(), canary)
        sidecar.unlink()

    def test_evidence_bridge_rejects_changed_candidate_profile_duplicate_or_missing_rows(
        self,
    ) -> None:
        publication_id = self._prepare()
        self.publisher.commit(publication_id)
        evidence = self._evidence()
        with self.assertRaisesRegex(EvidenceBridgeError, "candidate/profile/source identity"):
            changed = dict(self.candidate)
            changed["components"] = [{"id": "other"}]
            verify_candidate_evidence_publication(
                self.publisher,
                publication_id,
                changed,
                self.profile,
                [evidence],
                self._workload_mapping(),
            )
        with self.assertRaisesRegex(EvidenceBridgeError, "profile"):
            changed_profile = dict(self.profile)
            changed_profile["target_environments"] = ["other-environment"]
            verify_candidate_evidence_publication(
                self.publisher,
                publication_id,
                self.candidate,
                changed_profile,
                [evidence],
                self._workload_mapping(),
            )
        with self.assertRaisesRegex(EvidenceBridgeError, "duplicate, or conflicting"):
            verify_candidate_evidence_publication(
                self.publisher,
                publication_id,
                self.candidate,
                self.profile,
                [evidence, evidence],
                self._workload_mapping(),
            )
        with self.assertRaisesRegex(EvidenceBridgeError, "absent, duplicate, or conflicting"):
            verify_candidate_evidence_publication(
                self.publisher,
                publication_id,
                self.candidate,
                self.profile,
                [],
                self._workload_mapping(),
            )

    def test_evidence_bridge_rejects_wrong_runtime_even_when_evidence_self_hash_is_recomputed(
        self,
    ) -> None:
        publication_id = self._prepare()
        self.publisher.commit(publication_id)
        wrong_runtime = self._evidence(runtime_id="cpython-3.13.1")
        with self.assertRaisesRegex(EvidenceBridgeError, "runtime"):
            verify_candidate_evidence_publication(
                self.publisher,
                publication_id,
                self.candidate,
                self.profile,
                [wrong_runtime],
                self._workload_mapping(),
            )

    def test_evidence_bridge_rejects_mixed_and_subclass_output_requirements(self) -> None:
        publication_id = self._prepare()
        self.publisher.commit(publication_id)
        mapping = self._workload_mapping()
        valid = mapping.required_outputs[0]

        class ExtendedRequirement(OutputEvidenceRequirement):
            pass

        invalid_mappings = (
            FixedWorkloadMapping(
                **{**mapping.__dict__, "required_outputs": (valid, "malformed requirement")}
            ),
            FixedWorkloadMapping(
                **{
                    **mapping.__dict__,
                    "required_outputs": (
                        valid,
                        ExtendedRequirement(
                            valid.output_path,
                            valid.gate,
                            valid.component_id,
                            valid.artifact_sha256,
                        ),
                    ),
                }
            ),
        )
        for invalid_mapping in invalid_mappings:
            with self.subTest(required_outputs=invalid_mapping.required_outputs):
                with self.assertRaisesRegex(EvidenceBridgeError, "invalid output requirement"):
                    verify_candidate_evidence_publication(
                        self.publisher,
                        publication_id,
                        self.candidate,
                        self.profile,
                        [self._evidence()],
                        invalid_mapping,
                    )

    def test_candidate_profile_and_evidence_are_snapshotted_before_validation(self) -> None:
        publication_id = self._prepare()
        self.publisher.commit(publication_id)

        class ReadOnceMapping(Mapping):
            def __init__(self, value: dict) -> None:
                self.value = value
                self.reads: dict[str, int] = {}

            def __getitem__(self, key: str) -> object:
                count = self.reads.get(key, 0) + 1
                self.reads[key] = count
                if count > 1:
                    raise AssertionError(f"input was reread after snapshot: {key}")
                return self.value[key]

            def __iter__(self):
                return iter(self.value)

            def __len__(self) -> int:
                return len(self.value)

        candidate = ReadOnceMapping(dict(self.candidate))
        profile = ReadOnceMapping(dict(self.profile))
        evidence = ReadOnceMapping(self._evidence())
        report = verify_candidate_evidence_publication(
            self.publisher,
            publication_id,
            candidate,
            profile,
            [evidence],
            self._workload_mapping(),
        )
        self.assertTrue(report.bindings_valid)

    def test_candidate_evidence_bridge_rejects_nonpassing_receipt_and_wrong_output_mapping(
        self,
    ) -> None:
        publication_id = self._prepare()
        self.publisher.commit(publication_id)
        mapping = self._workload_mapping()
        wrong_output = FixedWorkloadMapping(
            **{
                **mapping.__dict__,
                "required_outputs": (
                    OutputEvidenceRequirement(
                        "artifacts/result.json", "native-smoke", "strategy-core", "9" * 64
                    ),
                ),
            }
        )
        with self.assertRaisesRegex(EvidenceBridgeError, "output digest"):
            verify_candidate_evidence_publication(
                self.publisher,
                publication_id,
                self.candidate,
                self.profile,
                [self._evidence()],
                wrong_output,
            )

        payload = self._payload()
        payload["outcome"] = "failed"
        payload["exit_code"] = 1
        failed_receipt = self._write_receipt(payload, self.root / "failed-receipt.json")
        other_publisher = LocalExecutionPublisher(
            self.ledger, self.root / "second-outbox.sqlite3", self.root / "second-store", self.keys
        )
        failed_id = other_publisher.prepare(
            self.admission.admission_id,
            failed_receipt,
            self.stage,
            lambda: "2030-01-01T00:05:00Z",
        )
        other_publisher.commit(failed_id)
        with self.assertRaisesRegex(EvidenceBridgeError, "non-passing receipt"):
            verify_candidate_evidence_publication(
                other_publisher,
                failed_id,
                self.candidate,
                self.profile,
                [self._evidence()],
                self._workload_mapping(),
            )

    def test_expired_admission_cannot_create_a_new_publication(self) -> None:
        publisher = LocalExecutionPublisher(
            self.ledger,
            self.root / "expired-outbox.sqlite3",
            self.root / "expired-evidence",
            self.keys,
        )
        with self.assertRaisesRegex(PublicationError, "publication deadline expired"):
            publisher.prepare(
                self.admission.admission_id,
                self.receipt_path,
                self.stage,
                lambda: "2030-01-01T03:00:00Z",
            )

    def test_prepare_requires_callable_clock_at_runtime(self) -> None:
        with self.assertRaisesRegex(PublicationError, "callable clock"):
            getattr(self.publisher, "prepare")(
                self.admission.admission_id,
                self.receipt_path,
                self.stage,
                "2030-01-01T00:05:00Z",
            )

    def test_new_reservation_rechecks_clock_after_validation(self) -> None:
        clock_values = (
            "2030-01-01T02:59:59Z",
            "2030-01-01T03:00:00Z",
        )
        clock_reads = 0

        def clock() -> str:
            nonlocal clock_reads
            value = clock_values[clock_reads]
            clock_reads += 1
            return value

        with self.assertRaisesRegex(PublicationError, "publication deadline expired"):
            self._prepare(now=clock)

        self.assertEqual(clock_reads, 2)
        with sqlite3.connect(self.root / "outbox.sqlite3") as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM publications").fetchone()[0], 0
            )

    def test_prepared_retry_after_deadline_is_recovery_not_new_authorization(self) -> None:
        publication_id = self._prepare()
        self.assertEqual(
            self._prepare(now=lambda: "2030-01-01T04:00:00Z"),
            publication_id,
        )
        self.publisher.commit(publication_id)
        self.assertEqual(
            self.ledger.get(self.admission.admission_id).publication_deadline,
            "2030-01-01T03:00:00Z",
        )

    def test_receipt_is_bounded_and_no_follow(self) -> None:
        symlink_path = self.root / "receipt-symlink.json"
        symlink_path.symlink_to(self.receipt_path)
        with self.assertRaisesRegex(PublicationError, "receipt verification failed"):
            self._prepare(receipt_path=symlink_path)
        oversized = self.root / "oversized-receipt.json"
        oversized.write_bytes(b"x" * (MAX_RECEIPT_BYTES + 1))
        with self.assertRaisesRegex(PublicationError, "receipt verification failed"):
            self._prepare(receipt_path=oversized)

    def test_verification_uses_and_stores_the_same_receipt_snapshot(self) -> None:
        original_bytes = self.receipt_path.read_bytes()
        original_verify = publisher_module.verify_receipt_bytes

        def replace_path_after_snapshot(snapshot: bytes, *args: object, **kwargs: object) -> dict:
            self.receipt_path.write_bytes(b"replacement after snapshot")
            return original_verify(snapshot, *args, **kwargs)

        with patch.object(publisher_module, "verify_receipt_bytes", replace_path_after_snapshot):
            publication_id = self._prepare()
        bundle = self.publisher.commit(publication_id)
        stored = (
            self.root
            / "evidence-store"
            / "receipts"
            / f"{hashlib.sha256(original_bytes).hexdigest()}.json"
        )
        self.assertTrue(bundle.is_file())
        self.assertEqual(stored.read_bytes(), original_bytes)

    def test_mismatched_claim_executor_is_rejected(self) -> None:
        connection = self.ledger._connect()
        try:
            connection.execute(
                "UPDATE admissions SET executor_id = 'different-executor' WHERE admission_id = ?",
                (self.admission.admission_id,),
            )
        finally:
            connection.close()
        with self.assertRaisesRegex(PublicationError, "persisted claim"):
            self._prepare()

    def test_prepare_rejects_persisted_claim_deadline_mismatch_before_reservation(self) -> None:
        connection = self.ledger._connect()
        try:
            connection.execute(
                "UPDATE admissions SET execution_deadline = ? WHERE admission_id = ?",
                ("2030-01-01T02:00:01Z", self.admission.admission_id),
            )
        finally:
            connection.close()

        with self.assertRaisesRegex(PublicationError, "persisted claim deadline"):
            self._prepare()

        with sqlite3.connect(self.root / "outbox.sqlite3") as connection:
            reservation_count = connection.execute("SELECT COUNT(*) FROM publications").fetchone()[
                0
            ]
        self.assertEqual(reservation_count, 0)

    def test_prepare_rejects_receipt_started_before_claim_time(self) -> None:
        payload = self._payload()
        payload["started_at"] = "2030-01-01T00:00:59Z"
        receipt_path = self._write_receipt(payload, self.root / "started-before-claim.json")

        with self.assertRaisesRegex(PublicationError, "outside the claimed admission deadline"):
            self._prepare(receipt_path=receipt_path)

        with sqlite3.connect(self.root / "outbox.sqlite3") as connection:
            reservation_count = connection.execute("SELECT COUNT(*) FROM publications").fetchone()[
                0
            ]
        self.assertEqual(reservation_count, 0)

    def test_commit_rejects_claim_time_after_signed_receipt_start_before_store_write(self) -> None:
        publication_id = self._prepare()
        connection = self.ledger._connect()
        try:
            connection.execute(
                "UPDATE admissions SET claimed_at = ? WHERE admission_id = ?",
                ("2030-01-01T00:02:01Z", self.admission.admission_id),
            )
        finally:
            connection.close()

        with self.assertRaisesRegex(PublicationError, "outside the claimed admission deadline"):
            self.publisher.commit(publication_id)

        self.assertFalse((self.root / "evidence-store" / "blobs").exists())
        self.assertFalse((self.root / "evidence-store" / "bundles").exists())
        self.assertFalse((self.root / "evidence-store" / "receipts").exists())
        with sqlite3.connect(self.root / "outbox.sqlite3") as connection:
            status = connection.execute(
                "SELECT status FROM publications WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()[0]
        self.assertEqual(status, "PREPARED")

    def test_read_committed_rejects_changed_claim_without_repair_or_evidence_mutation(self) -> None:
        publication_id = self._prepare()
        self.publisher.commit(publication_id)
        evidence_store = self.root / "evidence-store"

        def evidence_files() -> dict[str, bytes]:
            return {
                path.relative_to(evidence_store).as_posix(): path.read_bytes()
                for path in sorted(evidence_store.rglob("*"))
                if path.is_file()
            }

        committed_evidence = evidence_files()
        self.assertTrue(committed_evidence)
        connection = self.ledger._connect()
        try:
            connection.execute(
                "UPDATE admissions SET claimed_at = ? WHERE admission_id = ?",
                ("2030-01-01T00:02:01Z", self.admission.admission_id),
            )
        finally:
            connection.close()

        with self.assertRaisesRegex(PublicationError, "stored receipt lifecycle"):
            self.publisher.read_committed(publication_id)

        self.assertEqual(evidence_files(), committed_evidence)
        with sqlite3.connect(self.root / "outbox.sqlite3") as connection:
            status = connection.execute(
                "SELECT status FROM publications WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()[0]
        self.assertEqual(status, "COMMITTED")

    def test_read_committed_rejects_tampered_stored_receipt_without_repair(self) -> None:
        publication_id = self._prepare()
        self.publisher.commit(publication_id)
        evidence_store = self.root / "evidence-store"
        receipt_digest = hashlib.sha256(self.receipt_path.read_bytes()).hexdigest()
        stored_receipt = evidence_store / "receipts" / f"{receipt_digest}.json"

        def evidence_files() -> dict[str, bytes]:
            return {
                path.relative_to(evidence_store).as_posix(): path.read_bytes()
                for path in sorted(evidence_store.rglob("*"))
                if path.is_file()
            }

        before_tamper = evidence_files()
        self.assertIn(stored_receipt.relative_to(evidence_store).as_posix(), before_tamper)
        tampered_bytes = b"X" * len(before_tamper["receipts/" + stored_receipt.name])
        stored_receipt.write_bytes(tampered_bytes)
        after_tamper = evidence_files()

        with self.assertRaisesRegex(
            PublicationError, "content-addressed bundle or receipt differs from outbox bytes"
        ):
            self.publisher.read_committed(publication_id)

        self.assertEqual(evidence_files(), after_tamper)
        self.assertEqual(
            {
                path: content
                for path, content in after_tamper.items()
                if path != "receipts/" + stored_receipt.name
            },
            {
                path: content
                for path, content in before_tamper.items()
                if path != "receipts/" + stored_receipt.name
            },
        )
        with sqlite3.connect(self.root / "outbox.sqlite3") as connection:
            status = connection.execute(
                "SELECT status FROM publications WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()[0]
        self.assertEqual(status, "COMMITTED")

    def test_provider_and_policy_observation_mismatch_is_rejected(self) -> None:
        for field, value in (
            ("observed_provider", {"provider_id": "other", "run_id": "run-12345", "attempt": 1}),
            ("observed_resource_policy_digest", "0" * 64),
            ("observed_network_policy_digest", "0" * 64),
        ):
            with self.subTest(field=field):
                payload = self._payload()
                payload[field] = value
                with self.assertRaises(PublicationError):
                    self._prepare(
                        receipt_path=self._write_receipt(payload, self.root / f"{field}.json")
                    )

    def test_symlink_extra_path_collision_and_changed_output_fail_closed(self) -> None:
        (self.stage / "artifacts/result.json").unlink()
        (self.stage / "artifacts/result.json").symlink_to(self.root / "outside")
        with self.assertRaisesRegex(PublicationError, "symlink"):
            self._prepare()
        (self.stage / "artifacts/result.json").unlink()
        (self.stage / "artifacts/result.json").write_bytes(b"x" * len(self.output_bytes))
        with self.assertRaisesRegex(PublicationError, "do not match"):
            self._prepare()
        (self.stage / "artifacts/result.json").write_bytes(b"changed")
        with self.assertRaisesRegex(PublicationError, "size does not match"):
            self._prepare()
        (self.stage / "artifacts/result.json").write_bytes(self.output_bytes)
        (self.stage / "artifacts/extra.txt").write_text("extra", encoding="ascii")
        with self.assertRaisesRegex(PublicationError, "inventory differs"):
            self._prepare()
        collision = self._payload()
        collision["outputs"] = [
            *collision["outputs"],
            {"path": "Artifacts/RESULT.JSON", "sha256": "0" * 64, "size_bytes": 0},
        ]
        with self.assertRaises(PublicationError):
            self._prepare(receipt_path=self._write_receipt(collision, self.root / "collision.json"))

    def test_staged_enumeration_bounds_files_directories_and_empty_directories(self) -> None:
        (self.stage / "empty-directory").mkdir()
        with patch.object(publisher_module, "MAX_STAGED_ENTRIES", 2):
            with self.assertRaisesRegex(PublicationError, "maximum enumerated entry count"):
                self._prepare()

    def test_empty_unexpected_directory_is_not_silently_ignored(self) -> None:
        (self.stage / "unexpected-empty").mkdir()
        with self.assertRaisesRegex(PublicationError, "extra_directories"):
            self._prepare()

    def test_replay_is_idempotent_only_for_identical_receipt_and_bundle(self) -> None:
        barrier = Barrier(2)

        def prepare_same() -> str:
            barrier.wait(timeout=5)
            return self._prepare()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: prepare_same(), range(2)))
        publication_id = results[0]
        self.assertEqual(results, [publication_id, publication_id])
        alternate = self.root / "whitespace-receipt.json"
        alternate.write_text(self.receipt_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaisesRegex(PublicationError, "conflicting receipt or bundle"):
            self._prepare(receipt_path=alternate)

    def test_independent_publishers_race_conflicting_bundles_with_one_atomic_winner(self) -> None:
        second_output = b"different valid artifact bytes\n"
        second_stage = self.root / "stage-second"
        (second_stage / "artifacts").mkdir(parents=True)
        (second_stage / "artifacts/result.json").write_bytes(second_output)

        second_payload = json.loads(json.dumps(self.payload))
        second_payload["outputs"] = [
            {
                "path": "artifacts/result.json",
                "sha256": hashlib.sha256(second_output).hexdigest(),
                "size_bytes": len(second_output),
            }
        ]
        second_receipt = self._write_receipt(second_payload, self.root / "second-receipt.json")

        second_ledger = AdmissionLedger(self.root / "admissions.sqlite3")
        publisher_a = LocalExecutionPublisher(
            self.ledger,
            self.root / "outbox.sqlite3",
            self.root / "evidence-store",
            self.keys,
        )
        publisher_b = LocalExecutionPublisher(
            second_ledger,
            self.root / "outbox.sqlite3",
            self.root / "evidence-store",
            self.keys,
        )
        barrier = Barrier(2)

        def prepare(
            publisher: LocalExecutionPublisher, receipt_path: Path, stage: Path
        ) -> tuple[str, str]:
            barrier.wait(timeout=5)
            try:
                return (
                    "prepared",
                    publisher.prepare(
                        self.admission.admission_id,
                        receipt_path,
                        stage,
                        lambda: "2030-01-01T00:05:00Z",
                    ),
                )
            except PublicationError as exc:
                return ("conflict", str(exc))

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda args: prepare(*args),
                    (
                        (publisher_a, self.receipt_path, self.stage),
                        (publisher_b, second_receipt, second_stage),
                    ),
                )
            )

        winners = [value for status, value in results if status == "prepared"]
        conflicts = [value for status, value in results if status == "conflict"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertIn("conflicting receipt or bundle", conflicts[0])

        with sqlite3.connect(self.root / "outbox.sqlite3") as connection:
            publications = connection.execute(
                "SELECT publication_id, admission_id, status, receipt_digest FROM publications"
            ).fetchall()
            receipts = connection.execute(
                "SELECT publication_id, digest, data FROM publication_receipts"
            ).fetchall()
            blobs = connection.execute(
                "SELECT publication_id, digest, data FROM publication_blobs"
            ).fetchall()

        self.assertEqual(len(publications), 1)
        publication_id, admission_id, status, receipt_digest = publications[0]
        self.assertEqual(publication_id, winners[0])
        self.assertEqual(admission_id, self.admission.admission_id)
        self.assertEqual(status, "PREPARED")
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0][0], publication_id)
        self.assertEqual(receipts[0][1], receipt_digest)
        self.assertEqual(hashlib.sha256(receipts[0][2]).hexdigest(), receipt_digest)
        self.assertEqual(len(blobs), 1)
        self.assertEqual(blobs[0][0], publication_id)
        self.assertIn(
            blobs[0][2],
            {self.output_bytes, second_output},
        )
        winning_payload = json.loads(receipts[0][2])["payload"]
        self.assertEqual(winning_payload["outputs"][0]["sha256"], blobs[0][1])
        expected_receipts = {self.receipt_path.read_bytes(), second_receipt.read_bytes()}
        self.assertIn(receipts[0][2], expected_receipts)

    def test_commit_retry_recovers_after_bundle_write_before_status_update(self) -> None:
        publication_id = self._prepare()
        original = self.publisher._mark_committed
        calls = 0

        def fail_once(value: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("simulated interruption")
            original(value)

        self.publisher._mark_committed = fail_once  # type: ignore[method-assign]
        with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
            self.publisher.commit(publication_id)
        first_retry = self.publisher.commit(publication_id)
        self.assertEqual(
            first_retry.read_bytes(), self.publisher.commit(publication_id).read_bytes()
        )

    def test_commit_rejects_missing_outbox_blob_before_any_store_write(self) -> None:
        publication_id = self._prepare()
        with sqlite3.connect(self.root / "outbox.sqlite3") as connection:
            connection.execute(
                "DELETE FROM publication_blobs WHERE publication_id = ?", (publication_id,)
            )
        with self.assertRaisesRegex(PublicationError, "do not match the bundle output inventory"):
            self.publisher.commit(publication_id)
        self.assertFalse((self.root / "evidence-store" / "blobs").exists())
        self.assertFalse((self.root / "evidence-store" / "bundles").exists())
        self.assertFalse((self.root / "evidence-store" / "receipts").exists())

    def test_committed_recovery_validates_and_restores_missing_content(self) -> None:
        publication_id = self._prepare()
        bundle_path = self.publisher.commit(publication_id)
        bundle = json.loads(bundle_path.read_text(encoding="ascii"))
        digest = bundle["outputs"][0]["sha256"]
        receipt_digest = bundle["receipt_sha256"]
        (self.root / "evidence-store" / "blobs" / digest).unlink()
        (self.root / "evidence-store" / "receipts" / f"{receipt_digest}.json").unlink()
        bundle_path.unlink()
        restored = self.publisher.commit(publication_id)
        self.assertTrue(restored.is_file())
        self.assertEqual(
            (self.root / "evidence-store" / "blobs" / digest).read_bytes(), self.output_bytes
        )
        self.assertEqual(
            (self.root / "evidence-store" / "receipts" / f"{receipt_digest}.json").read_bytes(),
            self.receipt_path.read_bytes(),
        )

    def test_evidence_directories_are_synced_before_committed_status(self) -> None:
        publication_id = self._prepare()
        original_fsync = publisher_module._fsync_directory
        synced: set[Path] = set()
        original_mark = self.publisher._mark_committed

        def record_fsync(path: Path) -> None:
            original_fsync(path)
            synced.add(path)

        def check_then_mark(value: str) -> None:
            required = {
                self.root / "evidence-store" / "blobs",
                self.root / "evidence-store" / "receipts",
                self.root / "evidence-store" / "bundles",
            }
            self.assertTrue(required <= synced)
            original_mark(value)

        with patch.object(publisher_module, "_fsync_directory", record_fsync):
            self.publisher._mark_committed = check_then_mark  # type: ignore[method-assign]
            self.publisher.commit(publication_id)

    def test_staged_read_is_bounded_to_signed_size_plus_one(self) -> None:
        original_read = publisher_module.os.read
        appended = False

        def append_before_read(descriptor: int, count: int) -> bytes:
            nonlocal appended
            if not appended and publisher_module.os.fstat(descriptor).st_size == len(
                self.output_bytes
            ):
                with (self.stage / "artifacts/result.json").open("ab") as handle:
                    handle.write(b"x")
                appended = True
            return original_read(descriptor, count)

        with patch.object(publisher_module.os, "read", append_before_read):
            with self.assertRaisesRegex(PublicationError, "exceeds its signed size"):
                self._prepare()
        self.assertTrue(appended)

    def test_execution_after_admission_deadline_is_rejected(self) -> None:
        payload = self._payload()
        payload["destroyed_at"] = "2030-01-01T02:00:01Z"
        with self.assertRaisesRegex(PublicationError, "outside the claimed admission deadline"):
            self._prepare(
                receipt_path=self._write_receipt(payload, self.root / "late.json"),
                now=lambda: "2030-01-01T02:01:00Z",
            )


if __name__ == "__main__":
    unittest.main()
