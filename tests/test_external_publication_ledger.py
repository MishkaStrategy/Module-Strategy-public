from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, fields
from pathlib import Path
from unittest.mock import patch

from scripts.external_publication_ledger import (
    LocalPublicationLedger,
    PublicationLedgerError,
    VerifiedPublicationAuthorization,
)
from scripts.external_execution_publisher import PublicationError
import scripts.external_publication_ledger as publication_module
from scripts.external_publication_receipt import (
    PublicationReceiptError,
    canonical_json_bytes,
    validate_payload,
    verify_receipt,
)
from tests.test_external_execution_publisher import ExternalExecutionPublisherTests


class _FixtureAuthorizationVerifier:
    """Test-only allowlist standing in for an external authenticated verifier."""

    def __init__(self) -> None:
        self.records: dict[str, VerifiedPublicationAuthorization] = {}

    def verify(self, authorization: object) -> VerifiedPublicationAuthorization:
        if not isinstance(authorization, str) or authorization not in self.records:
            raise ValueError("unknown synthetic authorization")
        return self.records[authorization]


class LocalPublicationLedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("openssl") is None:
            raise unittest.SkipTest("OpenSSL CLI is required")

    def setUp(self) -> None:
        self.fixture = ExternalExecutionPublisherTests(
            "test_prepare_commit_writes_content_addressed_bundle_idempotently"
        )
        self.fixture.setUp()
        self.execution_id = self.fixture._prepare()
        self.fixture.publisher.commit(self.execution_id)
        self.authorization_verifier = _FixtureAuthorizationVerifier()
        self.publications = LocalPublicationLedger(
            self.fixture.root / "publications.sqlite3",
            (self.fixture.root / "publication-store").resolve(),
            store_id="test-store",
            namespace="test-namespace",
            trusted_publisher_keys={"local-test": "controller"},
            publisher_audience="test-publisher-audience",
            authorization_verifier=self.authorization_verifier,
        )
        self.authorization_verifier.records["valid"] = self._verified_authorization()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _publish(self, **changes: object) -> bytes:
        args = {
            "authorization": "valid",
            "publisher_id": "local-test",
            "key_id": "controller",
            "private_key": self.fixture.private_key,
            "trusted_signers": self.fixture.keys,
        }
        args.update(changes)
        return self.publications.publish(self.fixture.publisher, self.execution_id, **args)

    def _verified_authorization(self, **changes: object) -> VerifiedPublicationAuthorization:
        snapshot = self.fixture.publisher.read_committed(self.execution_id)
        bundle_digest = hashlib.sha256(snapshot.bundle_bytes).hexdigest()
        inventory = tuple(
            (item.path, item.sha256, item.size_bytes)
            for item in sorted(snapshot.outputs, key=lambda item: item.path)
        )
        inventory_bytes = json.dumps(
            [
                {"path": path, "sha256": digest, "size_bytes": size}
                for path, digest, size in inventory
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        values: dict[str, object] = {
            "authorization_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "authorization_digest": "1" * 64,
            "actor": "local-test",
            "publisher_policy_digest": "2" * 64,
            "audience": "test-publisher-audience",
            "nonce": "synthetic-one-shot-nonce-1",
            "admission_id": snapshot.admission.admission_id,
            "admission_digest": publication_module.admission_digest(snapshot.admission),
            "execution_publication_id": snapshot.publication_id,
            "execution_receipt_digest": hashlib.sha256(snapshot.receipt_bytes).hexdigest(),
            "output_inventory": inventory,
            "output_inventory_digest": hashlib.sha256(inventory_bytes).hexdigest(),
            "destination_kind": "local_staging",
            "store_id": "test-store",
            "store_root": str(self.publications.store_root),
            "namespace": "test-namespace",
            "object_key": f"objects/sha256/{bundle_digest}",
            "object_id": f"sha256:{bundle_digest}",
            "object_version": bundle_digest,
            "expires_at": "2999-01-01T00:00:00Z",
        }
        values.update(changes)
        return VerifiedPublicationAuthorization(**values)  # type: ignore[arg-type]

    def _register_authorization(self, token: str, **changes: object) -> str:
        self.authorization_verifier.records[token] = self._verified_authorization(**changes)
        return token

    def _verify(self, receipt: bytes, **kwargs: object) -> dict:
        payload = json.loads(receipt)["payload"]
        binding = {
            name: payload[name]
            for name in (
                "publication_id",
                "authorization_claim",
                "admission",
                "execution",
                "publisher",
                "destination",
                "artifact",
            )
        }
        binding.update(kwargs.pop("expected_binding", {}))
        return verify_receipt(
            receipt,
            trusted_signers=kwargs.pop("trusted_signers", self.fixture.keys),
            trusted_publisher_keys=kwargs.pop(
                "trusted_publisher_keys", {"local-test": "controller"}
            ),
            expected_binding=binding,
            **kwargs,
        )

    def test_publish_is_signed_local_and_identical_retry_returns_same_receipt(self) -> None:
        receipt = self._publish()
        payload = self._verify(receipt)
        self.assertEqual(payload["destination"]["kind"], "local_staging")
        path = self.fixture.root / "publication-store" / payload["destination"]["object_key"]
        bundle = self.fixture.publisher.read_committed(self.execution_id).bundle_bytes
        self.assertEqual(path.read_bytes(), bundle)
        self.assertEqual(payload["artifact"]["sha256"], hashlib.sha256(bundle).hexdigest())
        self.assertEqual(self._publish(), receipt)
        with self.assertRaises(PublicationReceiptError):
            self._verify(receipt, expected_destination_kind="github_release")

    def test_missing_verifier_fails_before_construction_side_effects(self) -> None:
        database = self.fixture.root / "missing-verifier" / "ledger.sqlite3"
        store = self.fixture.root / "missing-verifier" / "store"
        with self.assertRaisesRegex(PublicationLedgerError, "verifier is required"):
            LocalPublicationLedger(
                database,
                store,
                store_id="test-store",
                namespace="test-namespace",
                trusted_publisher_keys={"local-test": "controller"},
                publisher_audience="test-publisher-audience",
            )
        self.assertFalse(database.parent.exists())
        self.assertFalse(store.exists())

    def test_missing_verifier_at_publication_fails_before_reservation_or_write(self) -> None:
        self.publications._authorization_verifier = None
        with self.assertRaisesRegex(PublicationLedgerError, "verifier is not configured"):
            self._publish()
        with self.publications._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM publications").fetchone()[0], 0)
        self.assertEqual(list(self.publications.store_root.iterdir()), [])

    def test_verifier_rejects_unknown_authorization_before_reservation_or_write(self) -> None:
        with self.assertRaisesRegex(PublicationLedgerError, "verification failed"):
            self._publish(authorization="not-issued")
        with self.publications._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM publications").fetchone()[0], 0)
        self.assertEqual(list(self.publications.store_root.iterdir()), [])

    def test_authorization_must_match_publisher_execution_inventory_and_destination(self) -> None:
        mismatches = {
            "actor": "other-publisher",
            "audience": "other-audience",
            "admission_id": "22222222-3333-4444-8555-666666666666",
            "execution_receipt_digest": "3" * 64,
            "output_inventory": (("other.txt", "4" * 64, 3),),
            "output_inventory_digest": "5" * 64,
            "destination_kind": "remote_upload",
            "store_id": "other-store",
            "store_root": "/other/root",
            "namespace": "other-namespace",
        }
        for index, (field, value) in enumerate(mismatches.items()):
            with self.subTest(field=field):
                token = f"mismatch-{index}"
                self._register_authorization(token, **{field: value})
                with self.assertRaises(PublicationLedgerError):
                    self._publish(authorization=token)
        with self.publications._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM publications").fetchone()[0], 0)
        self.assertEqual(list(self.publications.store_root.iterdir()), [])

    def test_expired_authorization_fails_before_reservation_or_write(self) -> None:
        token = self._register_authorization("expired", expires_at="2000-01-01T00:00:00Z")
        with self.assertRaisesRegex(PublicationLedgerError, "has expired"):
            self._publish(authorization=token)
        with self.publications._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM publications").fetchone()[0], 0)
        self.assertEqual(list(self.publications.store_root.iterdir()), [])

    def test_authorization_expiring_at_reservation_boundary_rolls_back_before_lease(self) -> None:
        before_expiry = "2998-12-31T23:59:59Z"
        after_expiry = "3000-01-01T00:00:00Z"
        with (
            patch.object(
                publication_module, "_now", side_effect=[before_expiry, after_expiry]
            ),
            patch.object(publication_module, "_write_store_object") as write_object,
        ):
            with self.assertRaisesRegex(PublicationLedgerError, "has expired"):
                self._publish()

        with self.publications._connect() as db:
            rows = db.execute("SELECT * FROM publications").fetchall()
        self.assertEqual(rows, [])
        self.assertEqual(list(self.publications.store_root.iterdir()), [])
        write_object.assert_not_called()

    def test_expiry_during_reservation_reclaim_rolls_back_reconciliation_and_lease(self) -> None:
        with patch.object(
            publication_module,
            "sign_receipt",
            side_effect=PublicationReceiptError("simulated interrupted publication"),
        ):
            with self.assertRaisesRegex(PublicationLedgerError, "could not be verified"):
                self._publish()

        with self.publications._connect() as db:
            original = db.execute("SELECT * FROM publications").fetchone()
        self.assertIsNotNone(original)
        original_state = tuple(original)
        self.assertEqual(original["status"], "UNCERTAIN")
        object_path = self.publications.store_root / original["object_key"]
        original_object = object_path.read_bytes()

        with (
            patch.object(
                publication_module,
                "_now",
                side_effect=["2998-12-31T23:59:59Z", "3000-01-01T00:00:00Z"],
            ),
            patch.object(publication_module, "_write_store_object") as write_object,
        ):
            with self.assertRaisesRegex(PublicationLedgerError, "has expired"):
                self._publish()

        with self.publications._connect() as db:
            after = db.execute("SELECT * FROM publications").fetchone()
        self.assertEqual(tuple(after), original_state)
        self.assertEqual(object_path.read_bytes(), original_object)
        write_object.assert_not_called()

    def test_fresh_authorization_replay_for_same_admission_is_rejected(self) -> None:
        self._publish()
        with self.assertRaises(PublicationLedgerError):
            self._publish(
                authorization=self._register_authorization(
                    "fresh-auth",
                    authorization_id="bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
                    authorization_digest="3" * 64,
                    nonce="synthetic-one-shot-nonce-2",
                )
            )

    def test_reused_authorization_nonce_conflicts_with_existing_reservation(self) -> None:
        self._publish()
        token = self._register_authorization(
            "reused-nonce",
            authorization_id="bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
            authorization_digest="3" * 64,
        )
        with self.assertRaisesRegex(PublicationLedgerError, "conflicting input"):
            self._publish(authorization=token)

    def test_candidate_destination_override_is_rejected(self) -> None:
        with self.assertRaises(PublicationLedgerError):
            self._publish(destination_kind="github_release")

    def test_missing_authenticated_publisher_policy_digest_is_rejected(self) -> None:
        values = asdict(self._verified_authorization())
        del values["publisher_policy_digest"]
        with self.assertRaises(TypeError):
            VerifiedPublicationAuthorization(**values)

        incomplete = object.__new__(VerifiedPublicationAuthorization)
        complete = self._verified_authorization()
        for descriptor in fields(VerifiedPublicationAuthorization):
            if descriptor.name != "publisher_policy_digest":
                object.__setattr__(incomplete, descriptor.name, getattr(complete, descriptor.name))
        self.authorization_verifier.records["missing-policy-digest"] = incomplete
        with self.assertRaisesRegex(
            PublicationLedgerError, "verified publisher policy digest is invalid"
        ):
            self._publish(
                authorization="missing-policy-digest",
                private_key=self.fixture.root / "must-not-read.pem",
            )
        with self.assertRaises(TypeError):
            self._publish(publisher_policy_digest="2" * 64)
        with self.publications._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM publications").fetchone()[0], 0)
        self.assertEqual(list(self.publications.store_root.iterdir()), [])

    def test_malformed_authenticated_publisher_policy_digest_fails_before_side_effects(
        self,
    ) -> None:
        for index, value in enumerate(("not-a-digest", "A" * 64, "0" * 63)):
            with self.subTest(value=value):
                token = f"malformed-policy-{index}"
                self._register_authorization(token, publisher_policy_digest=value)
                with self.assertRaisesRegex(
                    PublicationLedgerError, "verified publisher policy digest is invalid"
                ):
                    self._publish(
                        authorization=token,
                        private_key=self.fixture.root / "must-not-read.pem",
                    )
                with self.publications._connect() as db:
                    self.assertEqual(
                        db.execute("SELECT COUNT(*) FROM publications").fetchone()[0], 0
                    )
                self.assertEqual(list(self.publications.store_root.iterdir()), [])

    def test_verified_publisher_policy_digest_binds_receipt_and_idempotency(self) -> None:
        token = self._register_authorization("verified-policy", publisher_policy_digest="5" * 64)
        receipt = self._publish(authorization=token)
        self.assertEqual(self._verify(receipt)["publisher"]["policy_digest"], "5" * 64)

        self._register_authorization(
            token,
            publisher_policy_digest="6" * 64,
        )
        with self.assertRaisesRegex(PublicationLedgerError, "conflicting input"):
            self._publish(authorization=token)

    def test_missing_signer_trust_does_not_create_receipt(self) -> None:
        with self.assertRaises(PublicationLedgerError):
            self._publish(key_id="unknown")

    def test_execution_publication_substitution_is_rejected_by_source_reader(self) -> None:
        with self.assertRaises(PublicationError):
            self.publications.publish(
                self.fixture.publisher,
                "0" * 64,
                authorization="valid",
                publisher_id="local-test",
                key_id="controller",
                private_key=self.fixture.private_key,
                trusted_signers=self.fixture.keys,
            )

    def test_tampered_signed_receipt_and_unknown_signer_are_rejected(self) -> None:
        receipt = self._publish()
        tampered = receipt.replace(b"local-test", b"local-test-x")
        with self.assertRaises(PublicationReceiptError):
            self._verify(tampered)
        with self.assertRaises(PublicationReceiptError):
            self._verify(receipt, trusted_signers=self.fixture.root / "missing-trust")

    def test_concurrent_identical_reservation_never_creates_multiple_rows(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self._publish) for _ in range(2)]
        results = [future.result() for future in futures if future.exception() is None]
        self.assertGreaterEqual(len(results), 1)
        self.assertTrue(all(result == results[0] for result in results))
        with self.publications._connect() as db:
            row = db.execute("SELECT COUNT(*), status, receipt FROM publications").fetchone()
            self.assertEqual(row[0], 1)
            self.assertEqual(row["status"], "COMMITTED")
            self.assertEqual(bytes(row["receipt"]), results[0])

    def test_expired_lease_takeover_fences_stale_writer_at_final_commit(self) -> None:
        entered_sign = threading.Event()
        resume_stale = threading.Event()
        original_sign = publication_module.sign_receipt
        calls = 0
        lock = threading.Lock()

        def pause_first_sign(*args: object, **kwargs: object) -> bytes:
            nonlocal calls
            with lock:
                calls += 1
                first = calls == 1
            if first:
                entered_sign.set()
                if not resume_stale.wait(timeout=10):
                    raise TimeoutError("test did not release stale signer")
            return original_sign(*args, **kwargs)

        with patch.object(publication_module, "sign_receipt", pause_first_sign):
            with ThreadPoolExecutor(max_workers=2) as executor:
                stale = executor.submit(self._publish)
                self.assertTrue(entered_sign.wait(timeout=10))
                with self.publications._connect() as db:
                    db.execute("UPDATE publications SET lease_until='2000-01-01T00:00:00Z'")
                current = executor.submit(self._publish)
                winner = current.result(timeout=10)
                resume_stale.set()
                with self.assertRaises(PublicationLedgerError):
                    stale.result(timeout=10)
        with self.publications._connect() as db:
            row = db.execute("SELECT status, receipt FROM publications").fetchone()
        self.assertEqual(row["status"], "COMMITTED")
        self.assertEqual(bytes(row["receipt"]), winner)

    def test_signer_material_change_is_rejected_on_cached_retry(self) -> None:
        self._publish()
        alternate_private = self.fixture.root / "alternate-publisher.pem"
        subprocess.run(
            ["openssl", "genpkey", "-algorithm", "Ed25519", "-out", str(alternate_private)],
            check=True,
            capture_output=True,
        )
        with self.assertRaisesRegex(PublicationLedgerError, "conflicting input"):
            self._publish(private_key=alternate_private)

    def test_readback_mismatch_marks_uncertain_then_reconciles_original_object(self) -> None:
        original = publication_module._read_store_object
        reads = 0

        def corrupt_store_read(root: Path, key: str) -> bytes:
            nonlocal reads
            reads += 1
            if reads == 1:
                return b"different"
            return original(root, key)

        with patch.object(publication_module, "_read_store_object", corrupt_store_read):
            with self.assertRaises(PublicationLedgerError):
                self._publish()
        with self.publications._connect() as db:
            self.assertEqual(
                db.execute("SELECT status FROM publications").fetchone()[0], "UNCERTAIN"
            )
        receipt = self._publish()
        self.assertEqual(
            self._verify(receipt)["outcome"],
            "committed",
        )

    def test_changed_root_and_corrupted_cached_receipt_fail_closed(self) -> None:
        receipt = self._publish()
        other_root = LocalPublicationLedger(
            self.fixture.root / "other.sqlite3",
            (self.fixture.root / "other-store").resolve(),
            store_id="test-store",
            namespace="test-namespace",
            trusted_publisher_keys={"local-test": "controller"},
            publisher_audience="test-publisher-audience",
            authorization_verifier=self.authorization_verifier,
        )
        other_root.database = self.publications.database
        with self.assertRaises(PublicationLedgerError):
            other_root.publish(
                self.fixture.publisher,
                self.execution_id,
                authorization="valid",
                publisher_id="local-test",
                key_id="controller",
                private_key=self.fixture.private_key,
                trusted_signers=self.fixture.keys,
            )
        with self.publications._connect() as db:
            db.execute("UPDATE publications SET receipt=?", (b"corrupt",))
        with self.assertRaises(PublicationLedgerError):
            self._publish()
        self.assertTrue(receipt)

    def test_committed_row_without_receipt_cannot_be_resigned(self) -> None:
        self._publish()
        with self.publications._connect() as db:
            db.execute("UPDATE publications SET receipt=NULL")
        with self.assertRaisesRegex(PublicationLedgerError, "missing its immutable receipt"):
            self._publish()

    def test_conflicting_existing_object_fails_closed_and_persists_uncertain_state(self) -> None:
        self._publish()
        with self.publications._connect() as db:
            row = db.execute("SELECT object_key FROM publications").fetchone()
            db.execute(
                "UPDATE publications SET status='UNCERTAIN', lease_until='2000-01-01T00:00:00Z'"
            )
        target = self.publications.store_root / row["object_key"]
        target.write_bytes(b"conflicting object bytes")
        with self.assertRaisesRegex(PublicationLedgerError, "conflicts with reserved identity"):
            self._publish()
        with self.publications._connect() as db:
            self.assertEqual(
                db.execute("SELECT status FROM publications").fetchone()[0], "UNCERTAIN"
            )

    def test_delayed_crash_recovery_keeps_monotone_ledger_timestamps(self) -> None:
        self._publish()
        with self.publications._connect() as db:
            db.execute(
                "UPDATE publications SET status='IN_FLIGHT', receipt=NULL, owner_token='crashed', "
                "lease_until='2000-01-01T00:00:00Z'"
            )
        recovered = self._publish()
        timestamps = self._verify(recovered)["timestamps"]
        ordered = [
            timestamps[key]
            for key in ("reserved_at", "in_flight_at", "verified_at", "committed_at")
        ]
        self.assertEqual(ordered, sorted(ordered))

    def test_parent_symlink_escape_is_rejected(self) -> None:
        escape = self.fixture.root / "outside"
        escape.mkdir()
        (self.fixture.root / "publication-store" / "objects").symlink_to(
            escape, target_is_directory=True
        )
        with self.assertRaises(PublicationLedgerError):
            self._publish()
        self.assertEqual(list(escape.iterdir()), [])

    def test_closed_expected_binding_float_version_and_bundle_artifact_match(self) -> None:
        receipt = self._publish()
        decoded = json.loads(receipt)
        with self.assertRaises(PublicationReceiptError):
            self._verify(
                receipt,
                expected_binding={
                    "publisher": {"publisher_id": "other", "policy_digest": "2" * 64}
                },
            )
        decoded["schema_version"] = 1.0
        with self.assertRaises(PublicationReceiptError):
            payload = decoded["payload"]
            binding = {
                name: payload[name]
                for name in (
                    "publication_id",
                    "authorization_claim",
                    "admission",
                    "execution",
                    "publisher",
                    "destination",
                    "artifact",
                )
            }
            verify_receipt(
                canonical_json_bytes(decoded),
                trusted_signers=self.fixture.keys,
                expected_binding=binding,
                trusted_publisher_keys={"local-test": "controller"},
            )
        payload = json.loads(receipt)["payload"]
        for field, value in (
            ("size_bytes", payload["artifact"]["size_bytes"] + 1),
            ("sha256", "f" * 64),
        ):
            altered = json.loads(receipt)["payload"]
            altered["artifact"][field] = value
            with self.assertRaises(PublicationReceiptError):
                validate_payload(altered)
        with self.assertRaises(PublicationReceiptError):
            self._verify(
                receipt,
                expected_binding={
                    "destination": {
                        **payload["destination"],
                        "store_root": "/wrong/staging/root",
                    }
                },
            )
        with self.assertRaises(PublicationReceiptError):
            self._verify(receipt, trusted_publisher_keys={"local-test": "other-key"})

    def test_signing_failure_recovery_reuses_reserved_identity_and_timestamp(self) -> None:
        with patch.object(
            publication_module,
            "sign_receipt",
            side_effect=PublicationReceiptError("simulated signing crash"),
        ):
            with self.assertRaises(PublicationLedgerError):
                self._publish()
        with self.publications._connect() as db:
            original = db.execute("SELECT publication_id, created_at FROM publications").fetchone()
            db.execute("UPDATE publications SET lease_until='2000-01-01T00:00:00Z'")
        receipt = self._publish()
        payload = self._verify(receipt)
        self.assertEqual(payload["publication_id"], original["publication_id"])
        self.assertEqual(payload["timestamps"]["reserved_at"], original["created_at"])

    def test_partial_temp_write_never_exposes_partial_final_object_and_recovers(self) -> None:
        original_write = os.write
        calls = 0

        def fail_after_partial(fd: int, data: bytes | memoryview) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                return original_write(fd, data[:16])
            raise OSError("simulated process/write failure")

        with patch.object(publication_module.os, "write", fail_after_partial):
            with self.assertRaises(PublicationLedgerError):
                self._publish()
        with self.publications._connect() as db:
            row = db.execute("SELECT object_key, status FROM publications").fetchone()
        self.assertEqual(row["status"], "UNCERTAIN")
        with self.assertRaises(FileNotFoundError):
            publication_module._read_store_object(self.publications.store_root, row["object_key"])
        receipt = self._publish()
        with self.publications._connect() as db:
            expected_id = db.execute("SELECT publication_id FROM publications").fetchone()[0]
        self.assertEqual(self._verify(receipt)["publication_id"], expected_id)


if __name__ == "__main__":
    unittest.main()
