from __future__ import annotations

import re
import sys
from pathlib import Path

WORKFLOW_ROOT = Path(".github/workflows")
SELF_HOSTED_SELECTORS = {
    "[self-hosted, fast]",
    "[self-hosted, docker]",
    "[self-hosted, macos]",
    "[self-hosted, macOS, ARM64]",
    "[self-hosted, backtester]",
}
GITHUB_HOSTED_SELECTORS = {
    "ubuntu-latest",
    "macos-latest",
}
ALLOWED_SELECTORS = SELF_HOSTED_SELECTORS | GITHUB_HOSTED_SELECTORS
BACKTESTER_WORKFLOWS = {
    Path(".github/workflows/backtest-engine-heavy.yml"),
    Path(".github/workflows/canonical-strategy-v2-backtest.yml"),
    Path(".github/workflows/heavy-backtest.yml"),
    Path(".github/workflows/historical-data-persistent-registration-smoke.yml"),
    Path(".github/workflows/historical-data-persistent-registration-smoke-v2.yml"),
    Path(".github/workflows/market-data-external-corpus-drive-acceptance.yml"),
    Path(".github/workflows/market-data-external-corpus-registration-ci.yml"),
    Path(".github/workflows/market-data-ingestion-io-benchmark.yml"),
    Path(".github/workflows/physical-mac-acceptance.yml"),
    Path(".github/workflows/pr287-local-btc-probe.yml"),
    Path(".github/workflows/signal-bot-heavy-audit.yml"),
    Path(".github/workflows/strategy-factory-soak.yml"),
}
FORBIDDEN_SELECTOR_FRAGMENTS = {
    "windows-latest",
    "[self-hosted, backtest]",
    "[self-hosted, postgresql]",
    "[self-hosted, redis]",
    "[self-hosted, linux]",
    "[self-hosted, x64]",
    "[self-hosted, arm64]",
    "[self-hosted, vds]",
}
RUNS_ON_RE = re.compile(r"^\s*runs-on:\s*(?P<selector>.+?)\s*$", re.MULTILINE)
JOB_USES_RE = re.compile(r"^ {4}uses:\s*.+?\s*$", re.MULTILINE)
MAX_PARALLEL_ONE_RE = re.compile(r"^\s*max-parallel:\s*1\s*$", re.MULTILINE)
BARE_SELF_HOSTED = {"self-hosted", "[self-hosted]"}


def workflow_files(workflow_root: Path = WORKFLOW_ROOT) -> list[Path]:
    return sorted(
        path
        for pattern in ("*.yml", "*.yaml")
        for path in workflow_root.glob(pattern)
        if path.is_file()
    )


def _normalized_repository_path(path: Path, workflow_root: Path) -> Path:
    return Path(".github/workflows") / path.relative_to(workflow_root)


def audit(workflow_root: Path = WORKFLOW_ROOT) -> list[str]:
    errors: list[str] = []
    files = workflow_files(workflow_root)
    if not files:
        return [f"no workflow files found under {workflow_root}"]

    for path in files:
        repository_path = _normalized_repository_path(path, workflow_root)
        text = path.read_text(encoding="utf-8")
        selectors = [match.group("selector").strip() for match in RUNS_ON_RE.finditer(text)]
        if not selectors:
            if JOB_USES_RE.search(text):
                continue
            errors.append(f"{repository_path}: no runs-on selector found")
            continue

        for selector in selectors:
            normalized = selector.lower()
            if normalized in BARE_SELF_HOSTED:
                errors.append(
                    f"{repository_path}: bare self-hosted selector is forbidden: {selector!r}"
                )
                continue
            forbidden = (
                []
                if selector in ALLOWED_SELECTORS
                else sorted(
                    fragment for fragment in FORBIDDEN_SELECTOR_FRAGMENTS if fragment in normalized
                )
            )
            if forbidden:
                errors.append(
                    f"{repository_path}: forbidden runner selector fragment(s): "
                    f"{', '.join(forbidden)}"
                )
            if selector not in ALLOWED_SELECTORS:
                allowed = ", ".join(sorted(ALLOWED_SELECTORS))
                errors.append(
                    f"{repository_path}: invalid runs-on selector {selector!r}; "
                    f"allowed selectors: {allowed}"
                )

        uses_backtester = "[self-hosted, backtester]" in selectors
        if uses_backtester and repository_path not in BACKTESTER_WORKFLOWS:
            errors.append(
                f"{repository_path}: backtester role is not allowlisted; prove that the "
                "workflow executes historical strategy backtests, extended quantitative "
                "audits or long quantitative soaks and update BACKTESTER_WORKFLOWS"
            )
        if repository_path in BACKTESTER_WORKFLOWS and not uses_backtester:
            errors.append(
                f"{repository_path}: allowlisted quantitative workflow does not use "
                "[self-hosted, backtester]"
            )

        if MAX_PARALLEL_ONE_RE.search(text):
            errors.append(
                f"{repository_path}: max-parallel: 1 prevents distribution across free "
                "matching runners; document a correctness requirement before using it"
            )

    return errors


def main() -> int:
    errors = audit()
    if errors:
        print("Runner routing policy violations:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Runner routing policy audit passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
