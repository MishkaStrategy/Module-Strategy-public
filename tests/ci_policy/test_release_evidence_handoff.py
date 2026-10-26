from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from scripts.release_evidence_handoff import (
    MAX_JOB_OUTPUT_CHARS,
    build_archive,
    decode_archive,
    encode_archive,
)


def test_long_run_handoff_workflows_are_non_dispatching_and_token_bounded() -> None:
    publish = Path(".github/workflows/publish-release-evidence-handoff.yml").read_text(
        encoding="utf-8"
    )
    smoke = Path(".github/workflows/long-run-evidence-handoff-smoke.yml").read_text(
        encoding="utf-8"
    )
    wrapper = Path(".github/actions/publish-release-evidence-handoff/action.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_call:" in publish
    assert "workflow_dispatch:" not in publish
    assert "issue_comment:" not in publish
    assert "repository_dispatch:" not in publish
    assert "runs-on: [self-hosted, fast]" in publish
    assert "contents: write" in publish
    assert "secrets:" not in publish
    assert "actions/upload-artifact" not in publish
    assert "actions/download-artifact" not in publish
    assert "github.token" not in publish.lower()
    assert "publish-release-evidence-handoff" in publish

    assert "pull_request:" in smoke
    assert "issue_comment:" not in smoke
    assert "workflow_dispatch:" not in smoke
    assert "runs-on: [self-hosted, backtester]" not in smoke
    assert smoke.count("runs-on: [self-hosted, fast]") == 2
    assert "actions/upload-artifact" not in smoke
    assert "actions/download-artifact" not in smoke

    assert "publish-release-evidence" in wrapper
    assert "HANDOFF_PAYLOAD" in wrapper
    assert "HANDOFF_SHA256" in wrapper

    prepare = Path(".github/actions/prepare-release-evidence-handoff/action.yml").read_text(
        encoding="utf-8"
    )
    assert "sanitize_ci_evidence.py" in prepare


def test_handoff_transport_keeps_safety_margin_below_github_output_limit() -> None:
    source = Path("scripts/release_evidence_handoff.py").read_text(encoding="utf-8")
    assert "MAX_JOB_OUTPUT_CHARS = 48_000" in source
    assert "GITHUB_OUTPUT" in source
    assert "actions/upload-artifact" not in source


def _archive_entries(archive: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        return {
            member.name: bundle.extractfile(member).read()
            for member in bundle.getmembers()
            if member.isfile()
        }


def test_handoff_archive_is_deterministic_and_round_trips(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "search-summary.json").write_text(
        '{"phase":"search"}\n',
        encoding="utf-8",
    )
    (evidence / "SHA256SUMS.txt").write_text(
        "abc  search-summary.json\n",
        encoding="utf-8",
    )

    first = build_archive([evidence])
    (evidence / "search-summary.json").touch()
    second = build_archive([evidence])

    assert first == second
    payload, digest = encode_archive(first)
    assert len(payload) < MAX_JOB_OUTPUT_CHARS
    assert decode_archive(payload, digest) == first
    assert _archive_entries(first) == {
        "SHA256SUMS.txt": b"abc  search-summary.json\n",
        "search-summary.json": b'{"phase":"search"}\n',
    }


def test_handoff_rejects_digest_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "evidence.json"
    source.write_text("{}\n", encoding="utf-8")
    payload, _ = encode_archive(build_archive([source]))

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        decode_archive(payload, "0" * 64)


def test_handoff_rejects_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(ValueError, match="symlink"):
        build_archive([link])


def test_handoff_fails_closed_above_job_output_bound() -> None:
    oversized = b"x" * ((MAX_JOB_OUTPUT_CHARS * 3 // 4) + 1)
    with pytest.raises(ValueError, match="exceeds bounded"):
        encode_archive(oversized)
