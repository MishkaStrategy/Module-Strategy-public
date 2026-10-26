from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLISH_ACTION = ROOT / ".github" / "actions" / "publish-release-evidence" / "action.yml"


def test_differing_same_sha_evidence_is_preserved_as_run_scoped_asset() -> None:
    text = PUBLISH_ACTION.read_text(encoding="utf-8")

    assert 'effective_asset_name="${asset_name}"' in text
    assert 'if upload_immutable "${prepared_asset}" "${effective_asset_name}"; then' in text
    assert 'existing_asset_id="$(find_asset_id "${asset_name}" 2>/dev/null)"' not in text
    assert 'if cmp -s "${source_file}" "${existing_directory}/${upload_name}"; then' in text
    assert 'effective_asset_name="$(run_scoped_asset_name "${asset_name}")"' in text
    assert "archive_upload_status=$?" in text
    assert (
        'echo "Preserving the differing immutable asset and switching once to ${effective_asset_name}"'
        in text
    )
    assert '"${GITHUB_RUN_ID}" "${GITHUB_RUN_ATTEMPT}"' in text
    assert 'upload_immutable "${prepared_asset}" "${effective_asset_name}"' in text
    assert 'printf \'%s  %s\\n\' "${digest}" "${effective_asset_name}"' in text


def test_run_scoped_fallback_does_not_weaken_non_clobber() -> None:
    text = PUBLISH_ACTION.read_text(encoding="utf-8")

    assert "Refusing to replace differing immutable asset" in text
    assert "Concurrent immutable asset differs" in text
    assert "--request DELETE" not in text
    assert "DELETE " not in text
    assert 'target_commitish="$(python3 -c' in text
    assert 'expected_release_name="CI evidence ${EVIDENCE_COMMIT_SHA}"' in text
    assert (
        '"${target_commitish}" != "${EVIDENCE_COMMIT_SHA}" && '
        '"${target_commitish}" != "${default_branch}"' in text
    )
    assert "Existing draft evidence release target mismatch" in text
    assert "Existing published evidence release is not bound" in text
