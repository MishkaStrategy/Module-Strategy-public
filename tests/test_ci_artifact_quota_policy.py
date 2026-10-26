from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FAST_WORKFLOW = ROOT / ".github" / "workflows" / "fast-checks.yml"
CORE_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RESEARCH_FAST_WORKFLOW = ROOT / ".github" / "workflows" / "research-runner-ci.yml"
RESEARCH_RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "research-runner-release.yml"
RESEARCH_HEAVY_WORKFLOW = ROOT / ".github" / "workflows" / "research-runner-heavy.yml"
RESEARCH_SECURITY_WORKFLOW = ROOT / ".github" / "workflows" / "research-runner-security.yml"
RESEARCH_VALIDATION_WORKFLOW = ROOT / ".github" / "workflows" / "research-runner-validation.yml"
TEST_ARCHITECTURE_WORKFLOW = ROOT / ".github" / "workflows" / "test-architecture-validation.yml"
MARKET_DATA_EXPORT_WORKFLOW = ROOT / ".github" / "workflows" / "market-data-export-artifacts-ci.yml"
MARKET_DATA_PARITY_WORKFLOW = ROOT / ".github" / "workflows" / "market-data-cli-parity-audit-ci.yml"
MARKET_DATA_MIGRATION_WORKFLOW = (
    ROOT / ".github" / "workflows" / "market-data-download-config-migration-ci.yml"
)
MARKET_DATA_SECURITY_WORKFLOW = (
    ROOT / ".github" / "workflows" / "market-data-control-plane-security-ci.yml"
)
MARKET_DATA_INGESTION_WORKFLOW = ROOT / ".github" / "workflows" / "market-data-ingestion-ci.yml"
STRATEGY_FACTORY_WORKFLOW = ROOT / ".github" / "workflows" / "strategy-factory-ci.yml"
TRADING_RUNNER_WORKFLOW = ROOT / ".github" / "workflows" / "trading-runner-ci.yml"
V06_ACCEPTANCE_WORKFLOW = ROOT / ".github" / "workflows" / "integration-ci.yml"
RELEASE_PUBLISH_ACTION_FILE = (
    ROOT / ".github" / "actions" / "publish-release-evidence" / "action.yml"
)
RECOVERY_RUNBOOK = ROOT / "docs" / "operations" / "STORAGE_PRESSURE_MANUAL_RECOVERY.md"
POSTMORTEM = ROOT / "docs" / "audits" / "MIMIC_ACTIONS_QUOTA_POSTMORTEM_2026-08-10.md"

UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
RELEASE_PUBLISH_ACTION = "uses: ./.github/actions/publish-release-evidence"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _step(text: str, start_marker: str, next_marker: str | None = None) -> str:
    start = text.index(start_marker)
    if next_marker is None:
        return text[start:]
    end = text.index(next_marker, start + len(start_marker))
    return text[start:end]


def _assert_blocking_release_publication(
    path: Path,
    *,
    manifest_marker: str,
    publish_marker: str,
    gate_marker: str,
) -> None:
    text = _read(path)
    assert manifest_marker in text
    assert "artifact transport: omitted" not in text
    assert "not required by the GAP-001 correctness contract" not in text
    assert "permissions:\n  contents: write" in text

    publish = _step(text, publish_marker, gate_marker)
    gate = _step(text, gate_marker)

    assert RELEASE_PUBLISH_ACTION in publish
    assert UPLOAD_ACTION not in publish
    assert "id: evidence_publish" in publish
    assert "commit_sha: ${{ env.EXPECTED_HEAD }}" in publish
    assert 'draft: "true"' in publish
    assert "continue-on-error:" not in publish
    assert 'test "${{ steps.evidence_publish.outcome }}" = success' in gate


def _assert_failure_only_org_retained_upload(block: str) -> None:
    assert UPLOAD_ACTION in block
    assert "failure()" in block
    assert "cancelled()" in block
    assert "continue-on-error: true" in block
    assert "retention-days:" not in block


def test_fast_and_core_repair_diagnostics_are_failure_only_nonblocking() -> None:
    fast = _read(FAST_WORKFLOW)
    fast_upload = _step(fast, "      - name: Upload Ruff lint and repair evidence")
    assert "steps.ruff_lint.outcome == 'failure'" in fast_upload
    assert "        continue-on-error: true" in fast_upload
    assert UPLOAD_ACTION in fast_upload

    core = _read(CORE_WORKFLOW)
    core_upload = _step(core, "      - name: Upload Core CI diagnostics")
    assert "        if: failure()" in core_upload
    assert "        continue-on-error: true" in core_upload
    assert UPLOAD_ACTION in core_upload


def test_specialized_research_validation_follows_release_evidence_policy() -> None:
    _assert_blocking_release_publication(
        RESEARCH_RELEASE_WORKFLOW,
        manifest_marker="      - name: Record closed Research Runner release inventory",
        publish_marker="      - name: Publish exact Research Runner release evidence",
        gate_marker="      - name: Enforce Research Runner release publication gate",
    )
    release = _read(RESEARCH_RELEASE_WORKFLOW)
    assert UPLOAD_ACTION not in release
    assert "      - name: Checkout Release evidence publisher" in release
    assert "      - name: Assemble exact Research Runner release evidence" in release
    assert "asset_name: research-runner-release-py${{ matrix.python-version }}.tar.gz" in release

    _assert_blocking_release_publication(
        RESEARCH_HEAVY_WORKFLOW,
        manifest_marker="      - name: Record closed Research Runner Heavy evidence inventory",
        publish_marker="      - name: Publish exact Research Runner Heavy evidence",
        gate_marker="      - name: Enforce Research Runner Heavy evidence publication gate",
    )
    heavy = _read(RESEARCH_HEAVY_WORKFLOW)
    assert UPLOAD_ACTION not in heavy
    assert "      - name: Checkout Release evidence publisher" in heavy
    assert "asset_name: research-runner-heavy.tar.gz" in heavy

    _assert_blocking_release_publication(
        RESEARCH_SECURITY_WORKFLOW,
        manifest_marker="      - name: Sanitize and verify security evidence",
        publish_marker="      - name: Publish exact Research Runner security evidence",
        gate_marker="      - name: Enforce Research Runner security publication gate",
    )
    security = _read(RESEARCH_SECURITY_WORKFLOW)
    assert UPLOAD_ACTION not in security
    assert "      - name: Checkout Release evidence publisher" in security
    assert "      - name: Enforce security gate" in security
    assert "asset_name: research-runner-security-py3.13.tar.gz" in security

    dispatcher = _read(RESEARCH_VALIDATION_WORKFLOW)
    assert "permissions:\n  contents: write" in dispatcher
    for label in ("heavy-validation", "release-validation", "security-validation"):
        assert label in dispatcher


def test_research_fast_follows_release_evidence_policy() -> None:
    _assert_blocking_release_publication(
        RESEARCH_FAST_WORKFLOW,
        manifest_marker="      - name: Record Research Runner fast evidence identity",
        publish_marker="      - name: Publish exact Research Runner fast evidence",
        gate_marker="      - name: Enforce Research Runner fast publication gate",
    )
    research = _read(RESEARCH_FAST_WORKFLOW)
    diagnostics = _step(
        research,
        "      - name: Upload Research Runner fast failure diagnostics",
        "  full:",
    )
    _assert_failure_only_org_retained_upload(diagnostics)
    assert "      - name: Checkout Release evidence publisher" in research
    assert "sparse-checkout: .github/actions/publish-release-evidence" in research
    assert "asset_name: research-runner-fast-py3.12.tar.gz" in research


def test_quota_remediated_acceptance_surfaces_publish_release_evidence() -> None:
    _assert_blocking_release_publication(
        TEST_ARCHITECTURE_WORKFLOW,
        manifest_marker="      - name: Record test architecture evidence identity",
        publish_marker="      - name: Publish exact test architecture evidence",
        gate_marker="      - name: Enforce test architecture publication gate",
    )
    _assert_blocking_release_publication(
        MARKET_DATA_EXPORT_WORKFLOW,
        manifest_marker="      - name: Record export evidence identity",
        publish_marker="      - name: Publish exact Market Data export evidence",
        gate_marker="      - name: Enforce Market Data export publication gate",
    )
    _assert_blocking_release_publication(
        MARKET_DATA_PARITY_WORKFLOW,
        manifest_marker="      - name: Record parity evidence identity",
        publish_marker="      - name: Publish exact Market Data CLI parity evidence",
        gate_marker="      - name: Enforce Market Data CLI parity publication gate",
    )
    _assert_blocking_release_publication(
        MARKET_DATA_MIGRATION_WORKFLOW,
        manifest_marker="      - name: Record migration evidence identity",
        publish_marker="      - name: Publish exact Market Data migration evidence",
        gate_marker="      - name: Enforce Market Data migration publication gate",
    )
    _assert_blocking_release_publication(
        STRATEGY_FACTORY_WORKFLOW,
        manifest_marker="      - name: Record Strategy Factory evidence identity",
        publish_marker="      - name: Publish exact Strategy Factory evidence",
        gate_marker="      - name: Enforce Strategy Factory publication gate",
    )


def test_trading_and_v06_acceptance_follow_release_evidence_policy() -> None:
    _assert_blocking_release_publication(
        TRADING_RUNNER_WORKFLOW,
        manifest_marker="      - name: Record Trading Runner evidence identity",
        publish_marker="      - name: Publish exact Trading Runner evidence",
        gate_marker="      - name: Enforce Trading Runner publication gate",
    )
    trading = _read(TRADING_RUNNER_WORKFLOW)
    trading_diagnostics = _step(
        trading,
        "      - name: Upload Trading Runner failure diagnostics",
    )
    _assert_failure_only_org_retained_upload(trading_diagnostics)
    assert "asset_name: trading-runner-${{ matrix.python-version }}.tar.gz" in trading

    _assert_blocking_release_publication(
        V06_ACCEPTANCE_WORKFLOW,
        manifest_marker="      - name: Record sanitized evidence hashes",
        publish_marker="      - name: Publish exact v0.6 acceptance evidence",
        gate_marker="      - name: Enforce v0.6 acceptance publication gate",
    )
    acceptance = _read(V06_ACCEPTANCE_WORKFLOW)
    acceptance_diagnostics = _step(
        acceptance,
        "      - name: Upload v0.6 acceptance failure diagnostics",
    )
    _assert_failure_only_org_retained_upload(acceptance_diagnostics)
    assert "      - name: Assemble exact acceptance release evidence" in acceptance
    assert (
        "asset_name: v0-6-executable-acceptance-${{ matrix.python-version }}.tar.gz" in acceptance
    )


def test_market_data_security_and_storage_pressure_follow_release_policy() -> None:
    security = _read(MARKET_DATA_SECURITY_WORKFLOW)
    assert "permissions:\n  contents: write" in security
    assert "EXPECTED_HEAD: ${{ github.event.pull_request.head.sha || github.sha }}" in security
    assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in security
    assert "      - name: Verify exact security candidate" in security
    _assert_blocking_release_publication(
        MARKET_DATA_SECURITY_WORKFLOW,
        manifest_marker="      - name: Record control-plane security evidence identity",
        publish_marker="      - name: Publish exact control-plane security evidence",
        gate_marker="      - name: Enforce control-plane security publication gate",
    )
    security_diagnostics = _step(
        security,
        "      - name: Upload control-plane security failure diagnostics",
    )
    _assert_failure_only_org_retained_upload(security_diagnostics)

    ingestion = _read(MARKET_DATA_INGESTION_WORKFLOW)
    assert "permissions:\n  contents: write" in ingestion
    assert "EXPECTED_HEAD: ${{ github.event.pull_request.head.sha || github.sha }}" in ingestion
    assert ingestion.count("ref: ${{ github.event.pull_request.head.sha || github.sha }}") == 2
    assert "      - name: Verify exact interface candidate" in ingestion
    assert "      - name: Verify exact quality candidate" in ingestion

    interface_diagnostics = _step(
        ingestion,
        "      - name: Upload interface failure diagnostics",
        "  quality-package:",
    )
    quality_diagnostics = _step(
        ingestion,
        "      - name: Upload quality failure diagnostics",
    )
    _assert_failure_only_org_retained_upload(interface_diagnostics)
    _assert_failure_only_org_retained_upload(quality_diagnostics)

    storage_identity = _step(
        ingestion,
        "      - name: Record storage-pressure evidence identity",
        "      - name: Publish exact Market Data storage-pressure evidence",
    )
    storage_publish = _step(
        ingestion,
        "      - name: Publish exact Market Data storage-pressure evidence",
        "      - name: Enforce Market Data storage-pressure publication gate",
    )
    storage_gate = _step(
        ingestion,
        "      - name: Enforce Market Data storage-pressure publication gate",
        "      - name: Upload quality failure diagnostics",
    )

    assert "market-data-control-plane-storage-*.json" in storage_identity
    assert "shared-jobs-storage-pressure-*.json" in storage_identity
    assert 'handle.write("present=false\\n")' in storage_identity
    assert 'handle.write("present=true\\n")' in storage_identity
    assert RELEASE_PUBLISH_ACTION in storage_publish
    assert UPLOAD_ACTION not in storage_publish
    assert "id: storage_evidence_publish" in storage_publish
    assert "steps.storage_evidence.outputs.present == 'true'" in storage_publish
    assert "commit_sha: ${{ env.EXPECTED_HEAD }}" in storage_publish
    assert 'draft: "true"' in storage_publish
    assert "continue-on-error:" not in storage_publish
    assert "steps.storage_evidence.outputs.present == 'true'" in storage_gate
    assert 'test "${{ steps.storage_evidence_publish.outcome }}" = success' in storage_gate


def test_release_publisher_is_exact_commit_bound_and_non_clobbering() -> None:
    publisher = _read(RELEASE_PUBLISH_ACTION_FILE)

    assert 'tag="ci-evidence-${EVIDENCE_COMMIT_SHA}"' in publisher
    assert 'expected_release_name="CI evidence ${EVIDENCE_COMMIT_SHA}"' in publisher
    assert (
        'expected_release_body="Canonical immutable evidence for '
        '${GITHUB_REPOSITORY}@${EVIDENCE_COMMIT_SHA}."' in publisher
    )
    assert "if draft != 'true':" in publisher
    assert "payload['target_commitish'] = commit_sha" in publisher
    assert 'target_commitish="$(python3 -c' in publisher
    assert (
        '"${target_commitish}" != "${EVIDENCE_COMMIT_SHA}" && '
        '"${target_commitish}" != "${default_branch}"' in publisher
    )
    assert "Existing draft evidence release target mismatch" in publisher
    assert "Existing published evidence release is not bound" in publisher
    assert "Refusing to replace differing immutable asset" in publisher
    assert "Concurrent immutable asset differs" in publisher
    assert 'digest_name="${effective_asset_name}.sha256"' in publisher
    assert 'upload_immutable "${prepared_asset}" "${effective_asset_name}"' in publisher
    assert 'upload_immutable "${digest_path}" "${digest_name}"' in publisher
    assert "--clobber" not in publisher


def test_quota_policy_has_no_global_artifact_failure_suppression() -> None:
    workflow_root = ROOT / ".github" / "workflows"
    forbidden_global_markers = (
        "ACTIONS_IGNORE_ARTIFACT",
        "IGNORE_ARTIFACT_QUOTA",
        "ALLOW_ARTIFACT_QUOTA_FAILURE",
    )

    hits: list[str] = []
    for path in workflow_root.glob("*.yml"):
        text = _read(path)
        for marker in forbidden_global_markers:
            if marker in text:
                hits.append(f"{path.name}:{marker}")

    assert hits == []


def test_quota_recovery_uses_read_only_inventory_and_exact_head_retry() -> None:
    runbook = _read(RECOVERY_RUNBOOK)

    assert "Inventory Actions artifacts read-only." in runbook
    assert "successful accepted exact-head evidence" in runbook
    assert "If the fresh batch digest differs from the reviewed digest" in runbook
    assert "STOP before the" in runbook and "first DELETE" in runbook
    assert "`actions: read` / `contents: read`" in runbook
    assert (
        "previously failed evidence upload/job on the **unchanged exact producer head**" in runbook
    )
    assert "All substantive gates must remain unchanged and green." in runbook
    assert (
        "Do not replace immutable evidence with mutable comments/logs merely to bypass" in runbook
    )


def test_postmortem_adopts_release_backed_organization_storage_policy() -> None:
    postmortem = _read(POSTMORTEM)

    assert "PR #611 (`CI: adopt organization storage policy`)" in postmortem
    assert "GitHub Actions artifacts are temporary only" in postmortem
    assert ".github/actions/publish-release-evidence" in postmortem
    assert "earlier recommendation to raise organization Actions retention" in postmortem
    assert "explicitly withdrawn" in postmortem
    assert "recommended first recovery value: **14 days**" not in postmortem
