from __future__ import annotations

import tempfile
import threading
import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from scripts.external_admission_ledger import (
    Admission,
    AdmissionAlreadyClaimed,
    AdmissionConflict,
    AdmissionError,
    AdmissionExpired,
    AdmissionLedger,
    NetworkPolicy,
    ResourcePolicy,
)


class ExternalAdmissionLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "admissions.sqlite3"
        self.ledger = AdmissionLedger(self.database)
        self.record = Admission(
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
            material_id="release-materials-v1",
            material_digest="3" * 64,
            required_output_paths=("artifacts/result.json", "artifacts/summary.txt"),
            resource_policy=ResourcePolicy(
                cpu_millis=2000,
                memory_bytes=4_294_967_296,
                storage_bytes=10_737_418_240,
                process_limit=256,
                execution_timeout_seconds=3600,
            ),
            network_policy=NetworkPolicy(mode="disabled", allowed_destinations=()),
            issued_at="2030-01-01T00:00:00Z",
            expires_at="2030-01-01T01:00:00Z",
            execution_deadline="2030-01-01T02:00:00Z",
            publication_deadline="2030-01-01T03:00:00Z",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_issue_is_immutable_and_idempotent_for_identical_record(self) -> None:
        self.assertTrue(self.ledger.issue(self.record))
        self.assertFalse(self.ledger.issue(self.record))
        self.assertEqual(self.ledger.get(self.record.admission_id), self.record)

    def test_issue_rejects_conflicting_record_with_same_admission_id(self) -> None:
        self.ledger.issue(self.record)
        conflict = replace(self.record, grant_digest="9" * 64)
        with self.assertRaises(AdmissionConflict):
            self.ledger.issue(conflict)

    def test_issue_rejects_second_admission_id_for_same_grant_digest(self) -> None:
        self.ledger.issue(self.record)
        second = replace(self.record, admission_id="22222222-2222-4333-8444-555555555555")

        with self.assertRaisesRegex(AdmissionConflict, "grant_digest already issued"):
            self.ledger.issue(second)

        self.assertEqual(self.ledger.get(self.record.admission_id), self.record)
        self.assertIsNone(self.ledger.get(second.admission_id))

    def test_concurrent_issue_race_for_same_grant_digest_has_one_winner(self) -> None:
        records = (
            self.record,
            replace(self.record, admission_id="22222222-2222-4333-8444-555555555555"),
        )
        barrier = threading.Barrier(2)

        def attempt(record: Admission) -> str:
            barrier.wait(timeout=5)
            try:
                return "issued" if self.ledger.issue(record) else "identical_retry"
            except AdmissionConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, records))

        self.assertEqual(results.count("issued"), 1)
        self.assertEqual(results.count("conflict"), 1)
        stored = [self.ledger.get(record.admission_id) for record in records]
        self.assertEqual(sum(record is not None for record in stored), 1)

    def test_initialization_fails_closed_for_legacy_duplicate_grant_digests(self) -> None:
        legacy_database = Path(self.temp.name) / "legacy-admissions.sqlite3"
        connection = sqlite3.connect(legacy_database)
        try:
            connection.execute(
                """CREATE TABLE admissions (
                    admission_id TEXT PRIMARY KEY,
                    record_json BLOB NOT NULL,
                    record_sha256 TEXT NOT NULL,
                    grant_digest TEXT NOT NULL,
                    issued_at_utc_us INTEGER NOT NULL,
                    expires_at_utc_us INTEGER NOT NULL,
                    execution_deadline TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('ISSUED', 'CLAIMED')),
                    executor_id TEXT,
                    claimed_at TEXT,
                    CHECK ((status = 'ISSUED' AND executor_id IS NULL AND claimed_at IS NULL)
                        OR (status = 'CLAIMED' AND executor_id IS NOT NULL AND claimed_at IS NOT NULL))
                )"""
            )
            shared_digest = "a" * 64
            connection.executemany(
                """INSERT INTO admissions (
                    admission_id, record_json, record_sha256, grant_digest,
                    issued_at_utc_us, expires_at_utc_us, execution_deadline,
                    status, executor_id, claimed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ISSUED', NULL, NULL)""",
                (
                    (
                        "11111111-2222-4333-8444-555555555555",
                        b"{}",
                        "1" * 64,
                        shared_digest,
                        1,
                        2,
                        "2030-01-01T02:00:00Z",
                    ),
                    (
                        "22222222-2222-4333-8444-555555555555",
                        b"{}",
                        "2" * 64,
                        shared_digest,
                        1,
                        2,
                        "2030-01-01T02:00:00Z",
                    ),
                ),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(AdmissionError, "existing admissions reuse grant_digest"):
            AdmissionLedger(legacy_database)

    def test_admission_repository_must_be_canonical(self) -> None:
        with self.assertRaisesRegex(AdmissionError, "canonical Module-Strategy URI"):
            replace(self.record, repository="https://example.invalid/other.git")

    def test_admission_id_must_be_lowercase_canonical_uuid(self) -> None:
        with self.assertRaisesRegex(AdmissionError, "canonical UUID"):
            replace(self.record, admission_id="ABCDEFAB-CDEF-4ABC-8DEF-ABCDEFABCDEF")

    def test_publication_deadline_is_immutable_and_after_execution_deadline(self) -> None:
        self.assertTrue(self.ledger.issue(self.record))
        self.assertEqual(
            self.ledger.get(self.record.admission_id).publication_deadline,
            self.record.publication_deadline,
        )
        with self.assertRaisesRegex(AdmissionError, "publication_deadline"):
            replace(self.record, publication_deadline="2030-01-01T01:59:59Z")

    def test_expired_admission_cannot_be_claimed(self) -> None:
        self.ledger.issue(self.record)
        with self.assertRaises(AdmissionExpired):
            self.ledger.claim(
                self.record.admission_id,
                self.record.grant_digest,
                "executor-1",
                "2030-01-01T01:00:00Z",
            )
        self.assertIsNotNone(self.ledger.get(self.record.admission_id))

    def test_claim_before_issuance_is_rejected(self) -> None:
        self.ledger.issue(self.record)
        with self.assertRaisesRegex(AdmissionError, "earlier than admission issued_at"):
            self.ledger.claim(
                self.record.admission_id,
                self.record.grant_digest,
                "executor-early",
                "2029-12-31T23:59:59Z",
            )

    def test_fractional_timestamp_expiry_uses_instant_order(self) -> None:
        record = replace(self.record, expires_at="2030-01-01T00:00:00.11Z")
        self.ledger.issue(record)
        claim = self.ledger.claim(
            record.admission_id,
            record.grant_digest,
            "executor-fractional",
            "2030-01-01T00:00:00.1Z",
        )
        self.assertEqual(claim.executor_id, "executor-fractional")

    def test_claim_is_one_use_and_replay_requires_new_admission(self) -> None:
        self.ledger.issue(self.record)
        claim = self.ledger.claim(
            self.record.admission_id,
            self.record.grant_digest,
            "executor-1",
            "2030-01-01T00:15:00Z",
        )
        self.assertEqual(claim.executor_id, "executor-1")
        self.assertEqual(claim.execution_deadline, self.record.execution_deadline)
        self.assertEqual(self.ledger.get_claim(self.record.admission_id), claim)
        with self.assertRaises(AdmissionAlreadyClaimed):
            self.ledger.claim(
                self.record.admission_id,
                self.record.grant_digest,
                "executor-2",
                "2030-01-01T00:16:00Z",
            )

    def test_concurrent_claim_race_has_exactly_one_winner(self) -> None:
        self.ledger.issue(self.record)
        barrier = threading.Barrier(2)

        def attempt(executor_id: str) -> str:
            barrier.wait(timeout=5)
            try:
                self.ledger.claim(
                    self.record.admission_id,
                    self.record.grant_digest,
                    executor_id,
                    "2030-01-01T00:30:00Z",
                )
                return "won"
            except AdmissionAlreadyClaimed:
                return "lost"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ("executor-a", "executor-b")))
        self.assertEqual(results.count("won"), 1)
        self.assertEqual(results.count("lost"), 1)

    def test_grant_mismatch_and_closed_record_shape_fail(self) -> None:
        self.ledger.issue(self.record)
        with self.assertRaises(AdmissionError):
            self.ledger.claim(
                self.record.admission_id, "0" * 64, "executor-1", "2030-01-01T00:10:00Z"
            )
        mapping = self.record.to_mapping()
        mapping["candidate_override"] = "candidate-controlled"
        with self.assertRaisesRegex(AdmissionError, "unknown candidate_override"):
            Admission.from_mapping(mapping)
        resource = self.record.resource_policy.to_mapping()
        resource["unbounded"] = True
        mapping = self.record.to_mapping()
        mapping["resource_policy"] = resource
        with self.assertRaisesRegex(AdmissionError, "unknown unbounded"):
            Admission.from_mapping(mapping)

    def test_policy_and_output_identity_are_closed(self) -> None:
        with self.assertRaisesRegex(AdmissionError, "allowlist network policy"):
            NetworkPolicy(mode="allowlist", allowed_destinations=())
        with self.assertRaisesRegex(AdmissionError, "file/directory prefix conflict"):
            replace(self.record, required_output_paths=("A", "a/result.txt"))


if __name__ == "__main__":
    unittest.main()
