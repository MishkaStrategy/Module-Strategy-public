from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.verify_external_execution_receipt import (
    REPOSITORY,
    ReceiptError,
    _load_expected_output_paths,
    _valid_output_path,
    canonical_json_bytes,
    main as verify_receipt_main,
    verify_receipt,
)


class ExternalExecutionReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("openssl") is None:
            raise unittest.SkipTest("OpenSSL CLI is required")

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.keys = self.root / "trusted"
        self.keys.mkdir()
        self.private = self.root / "controller.pem"
        subprocess.run(
            ["openssl", "genpkey", "-algorithm", "Ed25519", "-out", str(self.private)],
            check=True,
            capture_output=True,
        )
        self.publish_key("controller", self.private)
        self.expected = {
            "repository": REPOSITORY,
            "source_sha": "a" * 40,
            "admission_id": "11111111-2222-4333-8444-555555555555",
            "workflow_digest": "d" * 64,
            "runtime_id": "python312-linux-x86_64",
            "material_digest": "e" * 64,
            "policy_id": "release-policy-v1",
            "policy_digest": "b" * 64,
            "workload_id": "paper-validation",
            "workload_digest": "c" * 64,
            "environment_id": "linux-ci-protected",
            "image_digest": "f" * 64,
            "image_os": "linux",
            "image_arch": "x86_64",
            "admission_digest": "9" * 64,
            "provider_id": "provider-a",
            "provider_run_id": "run-12345",
            "provider_attempt": 1,
            "resource_policy_digest": "7" * 64,
            "network_policy_digest": "8" * 64,
        }
        self.required_outputs = ["artifacts/result.json"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def publish_key(self, key_id: str, private_key: Path) -> None:
        public = self.keys / f"{key_id}.pub.pem"
        subprocess.run(
            ["openssl", "pkey", "-in", str(private_key), "-pubout", "-out", str(public)],
            check=True,
            capture_output=True,
        )

    def payload(self) -> dict:
        return {
            "admission_id": self.expected["admission_id"],
            "repository": REPOSITORY,
            "source_sha": "a" * 40,
            "workflow_digest": self.expected["workflow_digest"],
            "runtime_id": self.expected["runtime_id"],
            "workload_id": "paper-validation",
            "workload_digest": "c" * 64,
            "material_digest": self.expected["material_digest"],
            "policy_id": "release-policy-v1",
            "policy_digest": "b" * 64,
            "environment_id": "linux-ci-protected",
            "admission_digest": self.expected["admission_digest"],
            "observed_provider": {"provider_id": "provider-a", "run_id": "run-12345", "attempt": 1},
            "observed_resource_policy_digest": self.expected["resource_policy_digest"],
            "observed_network_policy_digest": self.expected["network_policy_digest"],
            "executor": {
                "executor_id": "ephemeral-runner-9f8a",
                "ephemeral": True,
                "image_digest": self.expected["image_digest"],
                "image_os": self.expected["image_os"],
                "image_arch": self.expected["image_arch"],
                "observed_os": "linux",
                "observed_arch": "x86_64",
                "created_at": "2026-01-02T03:00:00Z",
            },
            "started_at": "2026-01-02T03:01:00Z",
            "ended_at": "2026-01-02T03:02:00Z",
            "destroyed_at": "2026-01-02T03:03:00Z",
            "outcome": "passed",
            "exit_code": 0,
            "outputs": [{"path": "artifacts/result.json", "sha256": "1" * 64, "size_bytes": 17}],
        }

    def sign(self, payload: dict, *, key_id: str = "controller", key: Path | None = None) -> dict:
        source_key = key or self.private
        data = self.root / "payload.json"
        sig = self.root / "signature.bin"
        unsigned = {"schema_version": 1, "key_id": key_id, "payload": payload}
        data.write_bytes(canonical_json_bytes(unsigned))
        subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-sign",
                "-inkey",
                str(source_key),
                "-rawin",
                "-in",
                str(data),
                "-out",
                str(sig),
            ],
            check=True,
            capture_output=True,
        )
        return {**unsigned, "signature": base64.b64encode(sig.read_bytes()).decode("ascii")}

    def write_envelope(self, envelope: dict) -> Path:
        path = self.root / "receipt.json"
        path.write_text(json.dumps(envelope), encoding="utf-8")
        return path

    def verify(self, path: Path) -> dict:
        return verify_receipt(path, self.keys, "openssl", self.expected, self.required_outputs)

    def test_real_openssl_ed25519_receipt_verifies(self) -> None:
        receipt = self.write_envelope(self.sign(self.payload()))
        self.assertEqual(self.verify(receipt)["outcome"], "passed")

    def test_cli_verifies_valid_receipt_and_rejects_binding_mismatch(self) -> None:
        receipt = self.write_envelope(self.sign(self.payload()))
        expected_outputs = self.root / "expected-outputs.json"
        expected_outputs.write_text(json.dumps(self.required_outputs), encoding="utf-8")
        cli_fields = (
            "repository",
            "source_sha",
            "admission_id",
            "admission_digest",
            "workflow_digest",
            "runtime_id",
            "material_digest",
            "policy_id",
            "policy_digest",
            "workload_id",
            "workload_digest",
            "environment_id",
            "image_digest",
            "image_os",
            "image_arch",
            "provider_id",
            "provider_run_id",
            "provider_attempt",
            "resource_policy_digest",
            "network_policy_digest",
        )
        arguments = [
            str(receipt),
            "--trusted-keys",
            str(self.keys),
            "--expected-outputs",
            str(expected_outputs),
        ]
        for field in cli_fields:
            arguments.extend((f"--{field.replace('_', '-')}", str(self.expected[field])))

        self.assertEqual(verify_receipt_main(arguments), 0)

        mismatch = arguments.copy()
        mismatch[mismatch.index("--provider-run-id") + 1] = "different-run"
        self.assertEqual(verify_receipt_main(mismatch), 1)

    def test_wrong_controller_key_pinned_by_publisher_fails(self) -> None:
        wrong = self.root / "wrong.pem"
        subprocess.run(
            ["openssl", "genpkey", "-algorithm", "Ed25519", "-out", str(wrong)],
            check=True,
            capture_output=True,
        )
        self.publish_key("other", wrong)
        receipt = self.write_envelope(self.sign(self.payload()))
        envelope = json.loads(receipt.read_text(encoding="utf-8"))
        envelope["key_id"] = "other"
        receipt = self.write_envelope(envelope)
        with self.assertRaisesRegex(ReceiptError, "signature verification failed"):
            self.verify(receipt)

    def test_key_id_is_in_signed_bytes(self) -> None:
        self.publish_key("alias", self.private)
        envelope = self.sign(self.payload())
        envelope["key_id"] = "alias"
        with self.assertRaisesRegex(ReceiptError, "signature verification failed"):
            self.verify(self.write_envelope(envelope))

    def test_non_ed25519_trusted_key_is_rejected(self) -> None:
        rsa_private = self.root / "rsa.pem"
        subprocess.run(
            [
                "openssl",
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
                "-out",
                str(rsa_private),
            ],
            check=True,
            capture_output=True,
        )
        self.publish_key("rsa", rsa_private)
        envelope = self.sign(self.payload())
        envelope["key_id"] = "rsa"
        with self.assertRaisesRegex(ReceiptError, "must be an Ed25519 public key"):
            self.verify(self.write_envelope(envelope))

    def test_non_spki_public_key_encoding_is_rejected(self) -> None:
        key_path = self.keys / "controller.pub.pem"
        key_bytes = key_path.read_bytes()
        key_path.write_bytes(
            key_bytes.replace(b"BEGIN PUBLIC KEY", b"BEGIN ED25519 PUBLIC KEY").replace(
                b"END PUBLIC KEY", b"END ED25519 PUBLIC KEY"
            )
        )
        with self.assertRaisesRegex(ReceiptError, "SubjectPublicKeyInfo PEM"):
            self.verify(self.write_envelope(self.sign(self.payload())))

    def test_schema_version_swap_is_rejected(self) -> None:
        envelope = self.sign(self.payload())
        envelope["schema_version"] = 2
        with self.assertRaisesRegex(ReceiptError, "unsupported schema_version"):
            self.verify(self.write_envelope(envelope))

    def test_changed_payload_fails_signature(self) -> None:
        envelope = self.sign(self.payload())
        envelope["payload"]["outputs"][0]["sha256"] = "2" * 64
        with self.assertRaisesRegex(ReceiptError, "signature verification failed"):
            self.verify(self.write_envelope(envelope))

    def test_expected_repository_source_policy_workload_environment_bindings(self) -> None:
        cases = (
            ("repository", "https://example.invalid/repo.git", "repository does not match"),
            ("source_sha", "9" * 40, "source SHA mismatch"),
            ("admission_id", "66666666-2222-4333-8444-555555555555", "admission_id mismatch"),
            ("workflow_digest", "9" * 64, "workflow_digest mismatch"),
            ("runtime_id", "other-runtime", "runtime_id mismatch"),
            ("material_digest", "9" * 64, "material_digest mismatch"),
            ("policy_id", "other-policy", "policy_id mismatch"),
            ("policy_digest", "9" * 64, "policy_digest mismatch"),
            ("workload_id", "other-workload", "workload_id mismatch"),
            ("workload_digest", "9" * 64, "workload_digest mismatch"),
            ("environment_id", "untrusted-env", "environment_id mismatch"),
            ("admission_digest", "0" * 64, "admission_digest mismatch"),
        )
        for field, wrong_value, message in cases:
            with self.subTest(field=field):
                payload = self.payload()
                payload[field] = wrong_value
                with self.assertRaisesRegex(ReceiptError, message):
                    self.verify(self.write_envelope(self.sign(payload)))

    def test_expected_admission_id_must_be_lowercase_canonical_uuid(self) -> None:
        expected = dict(self.expected)
        expected["admission_id"] = "ABCDEFAB-CDEF-4ABC-8DEF-ABCDEFABCDEF"
        with self.assertRaisesRegex(ReceiptError, "expected admission_id must be a UUID"):
            verify_receipt(
                self.write_envelope(self.sign(self.payload())),
                self.keys,
                "openssl",
                expected,
                self.required_outputs,
            )

        for field, wrong_value in (
            ("image_digest", "9" * 64),
            ("image_os", "darwin"),
            ("image_arch", "aarch64"),
        ):
            with self.subTest(field=f"executor.{field}"):
                payload = self.payload()
                payload["executor"][field] = wrong_value
                with self.assertRaisesRegex(ReceiptError, f"executor\\.{field} mismatch"):
                    self.verify(self.write_envelope(self.sign(payload)))

    def test_observed_provider_and_policy_digests_match_admission(self) -> None:
        cases = (
            ("provider_id", "provider-b"),
            ("run_id", "run-999"),
            ("attempt", 2),
        )
        for field, value in cases:
            with self.subTest(provider_field=field):
                payload = self.payload()
                payload["observed_provider"][field] = value
                with self.assertRaisesRegex(ReceiptError, "observed_provider"):
                    self.verify(self.write_envelope(self.sign(payload)))
        for field in ("observed_resource_policy_digest", "observed_network_policy_digest"):
            with self.subTest(digest=field):
                payload = self.payload()
                payload[field] = "0" * 64
                with self.assertRaisesRegex(ReceiptError, f"{field} mismatch"):
                    self.verify(self.write_envelope(self.sign(payload)))

    def test_wrong_platform_mapping_fails(self) -> None:
        payload = self.payload()
        payload["executor"]["observed_arch"] = "aarch64"
        with self.assertRaisesRegex(ReceiptError, "does not match"):
            self.verify(self.write_envelope(self.sign(payload)))

    def test_bad_lifecycle_and_terminal_outcome_fail(self) -> None:
        payload = self.payload()
        payload["destroyed_at"] = "2026-01-02T03:01:30Z"
        with self.assertRaisesRegex(ReceiptError, "timestamps"):
            self.verify(self.write_envelope(self.sign(payload)))

    def test_timestamps_require_canonical_utc_rfc3339(self) -> None:
        for value in (
            "2026-01-02T03:01:00+00:00",
            "2026-01-02T03:01:00-05:00",
            "2026-01-02 03:01:00Z",
            "２０２６-01-02T03:01:00Z",
        ):
            with self.subTest(value=value):
                payload = self.payload()
                payload["started_at"] = value
                with self.assertRaisesRegex(ReceiptError, "canonical UTC RFC 3339"):
                    self.verify(self.write_envelope(self.sign(payload)))
        payload = self.payload()
        payload["outcome"] = "passed"
        payload["exit_code"] = 3
        with self.assertRaisesRegex(ReceiptError, "outcome and exit_code"):
            self.verify(self.write_envelope(self.sign(payload)))

    def test_unknown_fields_and_duplicate_json_keys_fail(self) -> None:
        payload = self.payload()
        payload["candidate_claim"] = True
        with self.assertRaisesRegex(ReceiptError, "unknown candidate_claim"):
            self.verify(self.write_envelope(self.sign(payload)))
        path = self.root / "duplicate.json"
        path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
        with self.assertRaisesRegex(ReceiptError, "duplicate JSON key"):
            self.verify(path)

    def test_malformed_json_and_signature_fail(self) -> None:
        path = self.root / "malformed.json"
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(ReceiptError, "cannot load receipt"):
            self.verify(path)
        envelope = self.sign(self.payload())
        envelope["signature"] = "%%%"
        with self.assertRaisesRegex(ReceiptError, "valid base64"):
            self.verify(self.write_envelope(envelope))

    def test_output_row_and_aggregate_bounds_fail(self) -> None:
        payload = self.payload()
        payload["outputs"] = [{"path": "../escape", "sha256": "1" * 64, "size_bytes": 1}]
        with self.assertRaisesRegex(ReceiptError, "portable relative-path dialect"):
            self.verify(self.write_envelope(self.sign(payload)))
        for bad_path in (
            "C:/drive",
            "file:stream",
            "a\\b",
            "line\nbreak",
            "CON.txt",
            "COM¹.log",
            "name.",
            "a//b",
        ):
            with self.subTest(path=bad_path):
                payload = self.payload()
                payload["outputs"][0]["path"] = bad_path
                with self.assertRaisesRegex(ReceiptError, "portable relative-path dialect"):
                    self.verify(self.write_envelope(self.sign(payload)))
        payload = self.payload()
        payload["outputs"] = [
            {"path": "Result.json", "sha256": "1" * 64, "size_bytes": 1},
            {"path": "result.json", "sha256": "2" * 64, "size_bytes": 1},
        ]
        with self.assertRaisesRegex(ReceiptError, "case-insensitive path collision"):
            self.verify(self.write_envelope(self.sign(payload)))
        payload = self.payload()
        payload["outputs"] = [
            {"path": "A", "sha256": "1" * 64, "size_bytes": 1},
            {"path": "a/file", "sha256": "2" * 64, "size_bytes": 1},
        ]
        self.required_outputs = ["A", "a/file"]
        with self.assertRaisesRegex(ReceiptError, "file/directory prefix conflict"):
            self.verify(self.write_envelope(self.sign(payload)))
        self.required_outputs = ["artifacts/result.json"]
        payload = self.payload()
        payload["outputs"] = [
            {"path": f"out/{index}", "sha256": "1" * 64, "size_bytes": 1} for index in range(257)
        ]
        with self.assertRaisesRegex(ReceiptError, "at most 256"):
            self.verify(self.write_envelope(self.sign(payload)))
        payload = self.payload()
        payload["outputs"] = [
            {"path": f"out/{index}", "sha256": "1" * 64, "size_bytes": 1_073_741_824}
            for index in range(3)
        ]
        with self.assertRaisesRegex(ReceiptError, "total output size"):
            self.verify(self.write_envelope(self.sign(payload)))

    def test_passed_receipt_must_match_protected_output_inventory(self) -> None:
        payload = self.payload()
        payload["outputs"] = []
        with self.assertRaisesRegex(ReceiptError, "missing required outputs"):
            self.verify(self.write_envelope(self.sign(payload)))
        payload = self.payload()
        payload["outputs"].append({"path": "extra/output", "sha256": "2" * 64, "size_bytes": 1})
        with self.assertRaisesRegex(ReceiptError, "absent from the protected admission inventory"):
            self.verify(self.write_envelope(self.sign(payload)))
        payload = self.payload()
        payload["outputs"][0]["path"] = "artifacts/Result.json"
        with self.assertRaisesRegex(ReceiptError, "absent from the protected admission inventory"):
            self.verify(self.write_envelope(self.sign(payload)))

    def test_empty_passed_output_list_requires_explicit_empty_inventory(self) -> None:
        payload = self.payload()
        payload["outputs"] = []
        self.required_outputs = []
        receipt = self.write_envelope(self.sign(payload))
        self.assertEqual(self.verify(receipt)["outputs"], [])

    def test_expected_bindings_fail_closed(self) -> None:
        receipt = self.write_envelope(self.sign(self.payload()))
        with self.assertRaisesRegex(ReceiptError, "expected bindings: missing"):
            verify_receipt(
                receipt, self.keys, "openssl", {"repository": REPOSITORY}, self.required_outputs
            )
        unexpected = {**self.expected, "untrusted_override": "value"}
        with self.assertRaisesRegex(ReceiptError, "expected bindings: unknown"):
            verify_receipt(receipt, self.keys, "openssl", unexpected, self.required_outputs)

    def test_expected_output_inventory_is_a_strict_json_path_list(self) -> None:
        inventory = self.root / "expected-outputs.json"
        inventory.write_text('["artifacts/result.json"]', encoding="utf-8")
        self.assertEqual(_load_expected_output_paths(inventory), ["artifacts/result.json"])
        inventory.write_text('{"paths": ["artifacts/result.json"]}', encoding="utf-8")
        with self.assertRaisesRegex(ReceiptError, "must be a JSON array"):
            _load_expected_output_paths(inventory)
        inventory.write_text('["A", "a/file"]', encoding="utf-8")
        with self.assertRaisesRegex(ReceiptError, "file/directory prefix conflict"):
            _load_expected_output_paths(inventory)

    def test_schema_and_verifier_output_path_dialects_match(self) -> None:
        schema_path = (
            Path(__file__).parents[1] / "schemas" / "external-execution-receipt-v1.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        pattern = re.compile(schema["$defs"]["outputPath"]["pattern"])
        for path, valid in (
            ("artifacts/result.json", True),
            ("COM¹", False),
            ("COM².log", False),
            ("LPT³.data", False),
            ("C:/drive", False),
            ("file:stream", False),
            ("name.", False),
        ):
            with self.subTest(path=path):
                self.assertEqual(pattern.fullmatch(path) is not None, valid)
                self.assertEqual(_valid_output_path(path), valid)


if __name__ == "__main__":
    unittest.main()
