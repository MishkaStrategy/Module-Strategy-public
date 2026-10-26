import csv
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "physical-mac-acceptance.yml"
SCRIPT = ROOT / "scripts" / "run_physical_mac_historical_acceptance.sh"
SUMMARY_VALIDATOR = ROOT / "scripts" / "verify_physical_historical_summary.py"
RUNNER_POLICY = ROOT / "scripts" / "audit_runner_routing.py"
EXPECTED_HEAD = "a" * 40
EXPECTED_BASE = "b" * 40


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _valid_summary() -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "PASS",
        "candidate_sha": EXPECTED_HEAD,
        "full_rc_sha": EXPECTED_BASE,
        "host_os": "Darwin",
        "host_arch": "arm64",
        "canonical_prepare": True,
        "canonical_smoke_compare_restart_replay": True,
        "canonical_full_historical": True,
        "paper_only": True,
        "live_trading_authorized": False,
        "final_24h_soak_complete": False,
        "raw_market_data_uploaded": False,
    }


def _validate_summary(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SUMMARY_VALIDATOR),
            "--summary",
            str(path),
            "--expected-head",
            EXPECTED_HEAD,
            "--expected-base",
            EXPECTED_BASE,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)


def test_physical_acceptance_is_scoped_to_accepted_control_ui_parent() -> None:
    text = _workflow()
    assert "types: [opened, reopened, synchronize]" in text
    assert "integration/modular-strategy-full-rc" in text
    assert "control-ui/unified-control-plane-foundation" in text
    assert "github.event.pull_request.head.repo.full_name == github.repository" in text
    assert "ready_for_review" not in text
    assert "converted_to_draft" not in text


def test_physical_acceptance_uses_only_approved_capability_selectors() -> None:
    text = _workflow()
    assert text.count("runs-on: [self-hosted, backtester]") == 1
    assert text.count("runs-on: [self-hosted, fast]") == 3
    assert "runs-on: [self-hosted]" not in text
    assert "macOS, ARM64" not in text
    assert "self-hosted, macos," not in text


def test_physical_acceptance_is_explicitly_allowlisted_for_backtester() -> None:
    policy = RUNNER_POLICY.read_text(encoding="utf-8")
    assert 'Path(".github/workflows/physical-mac-acceptance.yml"),' in policy


def test_physical_acceptance_pins_exact_candidate_and_merge_base() -> None:
    text = _workflow()
    script = _script()
    assert "github.event.pull_request.head.sha || inputs.expected_sha" in text
    assert "github.event.pull_request.base.sha || inputs.expected_base_sha" in text
    assert "fetch-depth: 0" in text
    assert '[ "$(git rev-parse HEAD)" = "$EXPECTED_HEAD" ]' in script
    assert 'git merge-base "$EXPECTED_BASE" "$EXPECTED_HEAD"' in script
    assert 'git merge-base --is-ancestor "$BASELINE_SHA" "$EXPECTED_HEAD"' in script


def test_historical_job_requires_physical_apple_silicon_and_canonical_suite() -> None:
    script = _script()
    assert '[ "$(uname -s)" = "Darwin" ]' in script
    assert '[ "$(uname -m)" = "arm64" ]' in script
    assert 'CANONICAL_REL="BTCUSDT/BTCUSDT_5m.parquet"' in script
    assert "run_canonical prepare" in script
    assert "run_canonical smoke" in script
    assert "run_canonical full" in script
    assert "scripts/run_local_test_suite.sh" in script


def test_historical_helper_nounset_scope_and_exit_sentinel_are_fail_closed() -> None:
    script = _script()
    assert 'local mode=$1 log="$PRIVATE_LOG_DIR/$mode.log" status' not in script
    assert 'local mode=$1\n  local log="$PRIVATE_LOG_DIR/$mode.log"\n  local status' in script
    assert "local exit_status=$?" in script
    assert "trap - EXIT" in script
    assert "scripts/verify_physical_historical_summary.py" in script
    assert "exit_status=97" in script
    assert 'exit "$exit_status"' in script


def test_historical_evidence_stays_outside_worktree_until_canonical_suite_passes() -> None:
    script = _script()
    assert 'RUN_ROOT="$RUNNER_TEMP/module-strategy-physical-' in script
    assert 'EVIDENCE_DIR="$RUN_ROOT/evidence/historical"' in script
    assert (
        'PUBLISH_EVIDENCE_DIR="${PHYSICAL_EVIDENCE_DIR:-$GITHUB_WORKSPACE/.physical-acceptance/historical}"'
        in script
    )
    assert '[ "$EVIDENCE_DIR" != "$PUBLISH_EVIDENCE_DIR" ]' in script
    assert '[ ! -e "$PUBLISH_EVIDENCE_DIR" ]' in script
    assert 'cp "$EVIDENCE_DIR/$name" "$PUBLISH_EVIDENCE_DIR/$name"' in script
    prepare_index = script.index("run_canonical prepare")
    smoke_index = script.index("run_canonical smoke")
    full_index = script.index("run_canonical full")
    publish_index = script.index('mkdir -p "$PUBLISH_EVIDENCE_DIR"')
    assert prepare_index < smoke_index < full_index < publish_index


def test_historical_helper_preserves_canonical_failure_without_nounset_false_green(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    corpus = home / "Google Drive" / "My Drive" / "binance_usdtm_futures_history"
    btc_dir = corpus / "BTCUSDT"
    btc_dir.mkdir(parents=True)
    (btc_dir / "BTCUSDT_5m.parquet").write_bytes(b"synthetic-parquet-placeholder")

    fieldnames = [
        "status",
        "symbol",
        "timeframe",
        "rows",
        "gap_events",
        "estimated_missing_5m_rows",
        "first_open_time",
        "last_open_time",
    ]
    with (corpus / "download_summary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(
            {
                "status": "ok",
                "symbol": "BTCUSDT",
                "timeframe": "5m",
                "rows": "721597",
                "gap_events": "0",
                "estimated_missing_5m_rows": "0",
                "first_open_time": "2019-09-08 17:55:00+00:00",
                "last_open_time": "2026-07-19 06:55:00+00:00",
            }
        )
        for index in range(49):
            writer.writerow(
                {
                    "status": "ok",
                    "symbol": f"SYNTH{index:02d}",
                    "timeframe": "1h",
                    "rows": "1",
                    "gap_events": "0",
                    "estimated_missing_5m_rows": "0",
                    "first_open_time": "2026-01-01 00:00:00+00:00",
                    "last_open_time": "2026-01-01 00:00:00+00:00",
                }
            )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "git",
        """#!/bin/sh
case "$1" in
  rev-parse) printf '%s\\n' "$EXPECTED_HEAD" ;;
  status) exit 0 ;;
  cat-file) exit 0 ;;
  merge-base)
    if [ "${2:-}" = "--is-ancestor" ]; then
      exit 0
    fi
    printf '%s\\n' "$EXPECTED_BASE"
    ;;
  *) exit 2 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "uname",
        """#!/bin/sh
case "${1:-}" in
  -s) printf 'Darwin\\n' ;;
  -m) printf 'arm64\\n' ;;
  *) exit 2 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "bash",
        """#!/bin/sh
printf 'synthetic canonical failure\\n' >&2
exit 42
""",
    )

    runner_temp = tmp_path / "runner-temp"
    evidence = tmp_path / "evidence"
    runner_temp.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "HOME": str(home),
            "EXPECTED_HEAD": EXPECTED_HEAD,
            "EXPECTED_BASE": EXPECTED_BASE,
            "GITHUB_WORKSPACE": str(ROOT),
            "RUNNER_TEMP": str(runner_temp),
            "PHYSICAL_EVIDENCE_DIR": str(evidence),
            "GITHUB_RUN_ID": "synthetic",
            "GITHUB_RUN_ATTEMPT": "1",
        }
    )
    result = subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "canonical local suite: prepare" in combined
    assert "canonical local suite failed in mode=prepare" in combined
    assert "unbound variable" not in combined
    assert not evidence.exists()
    assert not (evidence / "summary.json").exists()


def test_historical_pass_sentinel_accepts_only_complete_exact_candidate(tmp_path: Path) -> None:
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(_valid_summary()), encoding="utf-8")
    result = _validate_summary(path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "historical PASS sentinel validation passed" in result.stdout


def test_historical_pass_sentinel_rejects_missing_and_incomplete_results(tmp_path: Path) -> None:
    missing = _validate_summary(tmp_path / "missing.json")
    assert missing.returncode != 0

    path = tmp_path / "summary.json"
    payload = _valid_summary()
    payload["canonical_smoke_compare_restart_replay"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")
    incomplete = _validate_summary(path)
    assert incomplete.returncode != 0
    assert "canonical_smoke_compare_restart_replay" in incomplete.stdout


def test_historical_pass_sentinel_rejects_wrong_candidate_and_live_authorization(
    tmp_path: Path,
) -> None:
    path = tmp_path / "summary.json"
    payload = _valid_summary()
    payload["candidate_sha"] = "c" * 40
    path.write_text(json.dumps(payload), encoding="utf-8")
    wrong_candidate = _validate_summary(path)
    assert wrong_candidate.returncode != 0
    assert "candidate SHA mismatch" in wrong_candidate.stdout

    payload = _valid_summary()
    payload["live_trading_authorized"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    live = _validate_summary(path)
    assert live.returncode != 0
    assert "live_trading_authorized" in live.stdout


def test_historical_artifacts_are_sanitized_and_never_include_raw_market_data() -> None:
    text = _workflow()
    script = _script()
    assert "path: .physical-acceptance/historical" in text
    assert 'raw_market_data_uploaded": False' in script
    assert '".parquet", ".csv", ".db", ".sqlite", ".sqlite3"' in script
    assert "private absolute path leaked into evidence" in script
    assert "${{ runner.temp }}/module-strategy" not in text


def test_paper_runtime_remains_loopback_and_live_execution_is_not_authorized() -> None:
    text = _workflow()
    assert "127.0.0.1" in text
    assert 'MODULAR_CONTROL_PAPER_E2E: "1"' in text
    assert 'assert value["mode"] == "PAPER"' in text
    assert 'assert value["remote_bind_allowed"] is False' in text
    assert '"live_trading_authorized": False' in text
    assert '"final_24h_soak_complete": False' in text


def test_physical_acceptance_workflow_cannot_mutate_repository_or_release() -> None:
    text = (_workflow() + "\n" + _script()).lower()
    forbidden = (
        "git push",
        "git reset --hard",
        "gh pr merge",
        "git checkout main",
        "git switch main",
        "release create",
        "gh release",
        "actions/create-deployment",
        "repos/{owner}/{repo}/deployments",
    )
    for token in forbidden:
        assert token not in text
