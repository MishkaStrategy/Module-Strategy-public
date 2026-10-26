from __future__ import annotations

import json
from pathlib import Path

from scripts.audit_artifact_retention import (
    UploadStep,
    classify_file,
    load_residual_debt_registry,
    requires_residual_debt,
    upload_identity,
    validate_residual_debt,
    yaml_files,
)


ROOT = Path(__file__).resolve().parents[2]
READINESS_WORKFLOW = ROOT / ".github/workflows/full-rc-release-readiness-ci.yml"
PERSISTENT_WORKFLOW = (
    ROOT / ".github/workflows/historical-data-persistent-registration-smoke-v2.yml"
)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "workflow.yml"
    path.write_text(body, encoding="utf-8")
    return path


def _registry_entry(upload: UploadStep) -> dict[str, object]:
    identity = upload_identity(upload)
    return {
        "path": identity.path,
        "step_name": identity.step_name,
        "artifact_name": identity.artifact_name,
        "condition": identity.condition,
        "classification": identity.classification,
        "owner": "test owner",
        "disposition": "test-disposition",
        "reference": "#771",
    }


def _write_registry(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps({"schema_version": 1, "entries": entries}, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _assert_write_token_is_scoped_to_head_fence(text: str, marker: str) -> None:
    job_prefix = text[: text.index("    steps:")]
    assert "GH_TOKEN:" not in job_prefix
    verify = text[text.index(marker) :]
    assert "env:\n          GH_TOKEN: ${{ github.token }}\n        shell: bash" in verify


def _assert_canonical_full_rc_push_activation(text: str) -> None:
    assert "on:\n  push:\n    branches:\n      - integration/modular-strategy-full-rc" in text
    assert "github.event_name == 'push'" in text
    assert (
        "EXPECTED_SHA: ${{ github.event_name == 'push' && github.sha || github.event.pull_request.head.sha || inputs.expected_sha }}"
        in text
    )
    assert (
        "CANDIDATE_BRANCH: ${{ github.event_name == 'push' && github.ref_name || github.event.pull_request.head.ref || inputs.candidate_branch }}"
        in text
    )
    assert "ref: ${{ env.EXPECTED_SHA }}" in text


def test_classifies_one_line_failure_cancelled_nonblocking(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """jobs:
  test:
    steps:
      - name: diagnostics
        if: failure() || cancelled()
        continue-on-error: true
        uses: actions/upload-artifact@deadbeef
        with:
          path: reports
""",
    )

    uploads, errors = classify_file(path)

    assert errors == []
    assert len(uploads) == 1
    upload = uploads[0]
    assert upload.condition == "failure() || cancelled()"
    assert upload.has_failure
    assert upload.has_cancelled
    assert upload.continue_on_error
    assert upload.classification == "failure-cancelled-condition-nonblocking"


def test_classifies_folded_failure_cancelled_condition(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """jobs:
  test:
    steps:
      - name: diagnostics
        if: >-
          ${{ failure()
              || cancelled() }}
        continue-on-error: true
        uses: actions/upload-artifact@deadbeef
""",
    )

    uploads, errors = classify_file(path)

    assert errors == []
    assert len(uploads) == 1
    upload = uploads[0]
    assert upload.condition == "${{ failure() || cancelled() }}"
    assert upload.has_failure
    assert upload.has_cancelled
    assert upload.continue_on_error
    assert upload.classification == "failure-cancelled-condition-nonblocking"


def test_classifies_unconditional_and_success_uploads_as_blocking(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """jobs:
  test:
    steps:
      - uses: actions/upload-artifact@deadbeef
      - name: accepted evidence
        if: success()
        uses: actions/upload-artifact@deadbeef
""",
    )

    uploads, errors = classify_file(path)

    assert errors == []
    assert [upload.classification for upload in uploads] == [
        "unconditional-blocking",
        "success-condition-blocking",
    ]
    assert [upload.line for upload in uploads] == sorted(upload.line for upload in uploads)


def test_missing_continue_on_error_keeps_failure_upload_blocking(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """jobs:
  test:
    steps:
      - if: failure()
        uses: actions/upload-artifact@deadbeef
""",
    )

    uploads, errors = classify_file(path)

    assert errors == []
    assert uploads[0].classification == "failure-condition-blocking"
    assert not uploads[0].continue_on_error


def test_nonliteral_continue_on_error_is_not_treated_as_nonblocking(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """jobs:
  test:
    steps:
      - if: failure() || cancelled()
        continue-on-error: ${{ true }}
        uses: actions/upload-artifact@deadbeef
""",
    )

    uploads, errors = classify_file(path)

    assert errors == []
    assert uploads[0].classification == "failure-cancelled-condition-blocking"
    assert not uploads[0].continue_on_error


def test_one_day_retention_is_allowed_for_failure_diagnostics(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """jobs:
  test:
    steps:
      - if: failure() || cancelled()
        continue-on-error: true
        uses: actions/upload-artifact@deadbeef
        with:
          path: reports
          retention-days: 1
""",
    )

    uploads, errors = classify_file(path)

    assert len(uploads) == 1
    assert errors == []


def test_quoted_one_day_retention_is_allowed(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """jobs:
  test:
    steps:
      - if: failure() || cancelled()
        continue-on-error: true
        uses: actions/upload-artifact@deadbeef
        with:
          path: reports
          retention-days: "1"
""",
    )

    uploads, errors = classify_file(path)

    assert len(uploads) == 1
    assert errors == []


def test_non_one_day_retention_remains_fail_closed(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """jobs:
  test:
    steps:
      - if: failure() || cancelled()
        continue-on-error: true
        uses: actions/upload-artifact@deadbeef
        with:
          path: reports
          retention-days: 2
""",
    )

    uploads, errors = classify_file(path)

    assert len(uploads) == 1
    assert len(errors) == 1
    assert "explicit retention-days must be exactly 1" in errors[0]


def test_other_condition_is_reported_without_claiming_diagnostic_safety(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """jobs:
  test:
    steps:
      - if: always() && steps.prepare.outcome == 'success'
        continue-on-error: true
        uses: actions/upload-artifact@deadbeef
""",
    )

    uploads, errors = classify_file(path)

    assert errors == []
    assert uploads[0].classification == "other-condition-nonblocking"
    assert not uploads[0].has_failure
    assert not uploads[0].has_cancelled


def test_unnamed_blocking_upload_uses_artifact_name_as_stable_identity(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        """jobs:
  test:
    steps:
      - uses: actions/upload-artifact@deadbeef
        with:
          name: entry-model-performance
          path: reports
""",
    )
    uploads, errors = classify_file(path)
    assert errors == []
    assert len(uploads) == 1
    upload = uploads[0]
    assert upload.name is None
    assert upload.artifact_name == "entry-model-performance"
    registry = _write_registry(tmp_path, [_registry_entry(upload)])
    assert validate_residual_debt(uploads, registry) == []


def test_repository_residual_debt_registry_is_exact() -> None:
    uploads: list[UploadStep] = []
    retention_errors: list[str] = []
    for path in yaml_files():
        file_uploads, file_errors = classify_file(path)
        uploads.extend(file_uploads)
        retention_errors.extend(file_errors)

    registered, registry_errors = load_residual_debt_registry()

    assert retention_errors == []
    assert registry_errors == []
    assert len(registered) == 18
    assert sum(requires_residual_debt(upload) for upload in uploads) == 18
    assert validate_residual_debt(uploads) == []


def test_new_blocking_upload_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """jobs:
  test:
    steps:
      - name: new accepted evidence
        uses: actions/upload-artifact@deadbeef
        with:
          name: new-evidence
          path: reports
""",
    )
    uploads, _ = classify_file(path)
    registry = _write_registry(tmp_path, [])

    errors = validate_residual_debt(uploads, registry)

    assert any("unregistered blocking artifact upload" in error for error in errors)


def test_missing_registered_debt_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """jobs:
  test:
    steps:
      - name: accepted evidence
        uses: actions/upload-artifact@deadbeef
""",
    )
    uploads, _ = classify_file(path)
    registry = _write_registry(tmp_path, [_registry_entry(uploads[0])])

    errors = validate_residual_debt([], registry)

    assert any(
        "registered residual artifact debt is missing or changed" in error for error in errors
    )


def test_same_blocking_count_cannot_hide_identity_substitution(tmp_path: Path) -> None:
    original_path = _write(
        tmp_path,
        """jobs:
  test:
    steps:
      - name: old accepted evidence
        uses: actions/upload-artifact@deadbeef
""",
    )
    original, _ = classify_file(original_path)
    registry = _write_registry(tmp_path, [_registry_entry(original[0])])
    replacement_path = tmp_path / "workflow.yml"
    replacement_path.write_text(
        """jobs:
  test:
    steps:
      - name: new accepted evidence
        uses: actions/upload-artifact@deadbeef
""",
        encoding="utf-8",
    )
    replacement, _ = classify_file(replacement_path)

    errors = validate_residual_debt(replacement, registry)

    assert sum("unregistered blocking artifact upload" in error for error in errors) == 1
    assert (
        sum("registered residual artifact debt is missing or changed" in error for error in errors)
        == 1
    )


def test_duplicate_registry_identity_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """jobs:
  test:
    steps:
      - name: accepted evidence
        uses: actions/upload-artifact@deadbeef
""",
    )
    uploads, _ = classify_file(path)
    entry = _registry_entry(uploads[0])
    registry = _write_registry(tmp_path, [entry, dict(entry)])

    _, errors = load_residual_debt_registry(registry)

    assert any("duplicate identity" in error for error in errors)


def test_registry_rejects_failure_diagnostic_as_residual_success_debt(
    tmp_path: Path,
) -> None:
    registry = _write_registry(
        tmp_path,
        [
            {
                "path": ".github/workflows/example.yml",
                "step_name": "Failure diagnostics",
                "artifact_name": None,
                "condition": "failure()",
                "classification": "failure-condition-blocking",
                "owner": "test owner",
                "disposition": "diagnostic",
                "reference": "#771",
            }
        ],
    )

    _, errors = load_residual_debt_registry(registry)

    assert any("failure/cancelled diagnostics cannot be registered" in error for error in errors)


def test_blocking_upload_without_step_or_artifact_name_is_rejected(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        """jobs:
  test:
    steps:
      - uses: actions/upload-artifact@deadbeef
        with:
          path: reports
""",
    )
    uploads, _ = classify_file(path)
    registry = _write_registry(tmp_path, [])

    errors = validate_residual_debt(uploads, registry)

    assert any("stable identity is required" in error for error in errors)


def test_full_rc_readiness_success_evidence_is_release_backed() -> None:
    text = READINESS_WORKFLOW.read_text(encoding="utf-8")
    uploads, errors = classify_file(READINESS_WORKFLOW)

    assert errors == []
    assert len(uploads) == 1
    assert uploads[0].classification == "failure-cancelled-condition-nonblocking"
    assert "permissions:\n  contents: write" in text
    _assert_canonical_full_rc_push_activation(text)
    _assert_write_token_is_scoped_to_head_fence(
        text,
        "      - name: Verify exact requested commit and live full-RC head",
    )
    assert "id: evidence_validation" in text
    assert 'expected_files = {"readiness.json", "SHA256SUMS"}' in text
    assert "uses: ./.github/actions/publish-release-evidence" in text
    assert "asset_name: full-rc-release-readiness.tar.gz" in text
    assert "commit_sha: ${{ env.EXPECTED_SHA }}" in text
    assert "Enforce release-readiness publication gate" in text
    assert "steps.evidence_validation.outcome == 'success'" in text
    assert "continue-on-error: true" in text
    assert "Cleanup release-readiness evidence" in text
    assert "Upload sanitized release-readiness evidence" not in text


def test_persistent_registration_success_evidence_is_release_backed_and_bounded() -> None:
    text = PERSISTENT_WORKFLOW.read_text(encoding="utf-8")
    uploads, errors = classify_file(PERSISTENT_WORKFLOW)

    assert errors == []
    assert len(uploads) == 1
    assert uploads[0].classification == "failure-cancelled-condition-nonblocking"
    assert "permissions:\n  contents: read\n" in text
    owner_job = text[text.index("  register-and-smoke:\n") :]
    owner_header = owner_job.split("    steps:\n", 1)[0]
    assert "    permissions:\n      contents: write\n" in owner_header
    assert text.count("      contents: write") == 1
    _assert_canonical_full_rc_push_activation(text)
    _assert_write_token_is_scoped_to_head_fence(
        text,
        "      - name: Verify exact requested commit and live branch head",
    )
    assert "id: evidence_validation" in text
    assert 'expected_files = {"registration.json", "smoke.json"}' in text
    assert "64 * 1024" in text
    assert "credential = re.compile" in text
    assert 'os.environ.get("GITHUB_WORKSPACE")' in text
    assert 'os.environ.get("RUNNER_TEMP")' in text
    assert 'os.environ.get("HOME")' in text
    assert "uses: ./.github/actions/publish-release-evidence" in text
    assert "asset_name: historical-data-persistent-registration-v2.tar.gz" in text
    assert "commit_sha: ${{ env.EXPECTED_SHA }}" in text
    assert "Enforce persistent acceptance publication gate" in text
    assert "steps.evidence_validation.outcome == 'success'" in text
    assert "continue-on-error: true" in text
    assert '"$RUNNER_TEMP/persistent-acceptance"' in text
    assert "Upload sanitized persistent acceptance evidence" not in text
