from __future__ import annotations

import re
import sys
from pathlib import Path

WORKFLOW_ROOT = Path(".github/workflows")
RUNS_ON = re.compile(r"^\s{4}runs-on:\s*(?P<selector>.+?)\s*$", re.MULTILINE)
DOCKER_SELECTORS = {"[self-hosted, docker]", "ubuntu-latest"}
MACOS_SELECTORS = {"[self-hosted, macos]", "macos-latest"}
DOCKER_DEPENDENCY = re.compile(
    r"""(?ixm)
    ^[ ]{4}(?:container|services):(?:\s|$)
    |^\s+(?:-\s+)?uses:\s*['"]?(?:docker/|docker://)
    |(?:^|[\s;&|()])(?:sudo\s+)?docker(?:-compose)?(?:\s|$)
    |\bDOCKER_(?:BUILDKIT|HOST|TLS_VERIFY|CERT_PATH|CONTEXT)\b
    """
)
MACOS_DEPENDENCY = re.compile(
    r"""(?ixm)
    (?:^|[\s;&|()])(?:xcodebuild|xcrun|codesign|osascript|plutil|swift)(?:\s|$)
    |(?:^|[\s;&|()])security\s+(?:find-|create-|delete-|unlock-|set-)
    |/Applications/Xcode\.app
    |\bMACOSX_DEPLOYMENT_TARGET\b
    """
)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _jobs(text: str) -> list[tuple[str, str]]:
    lines = text.splitlines()
    try:
        start = lines.index("jobs:") + 1
    except ValueError:
        return []

    result: list[tuple[str, str]] = []
    name: str | None = None
    block: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and _indent(line) == 0:
            break
        match = re.fullmatch(r"  ([A-Za-z0-9_.-]+):(?:\s*#.*)?", line)
        if match:
            if name is not None:
                result.append((name, "\n".join(block)))
            name, block = match.group(1), [line]
        elif name is not None:
            block.append(line)
    if name is not None:
        result.append((name, "\n".join(block)))
    return result


def _selector(block: str) -> str | None:
    match = RUNS_ON.search(block)
    return match.group("selector").strip() if match else None


def audit(repo: Path) -> list[str]:
    errors: list[str] = []
    root = repo / WORKFLOW_ROOT
    for pattern in ("*.yml", "*.yaml"):
        for path in sorted(root.glob(pattern)):
            if not path.is_file():
                continue
            relative = path.relative_to(repo)
            for job_name, block in _jobs(path.read_text(encoding="utf-8")):
                selector = _selector(block)
                if selector is None:
                    continue
                needs_docker = DOCKER_DEPENDENCY.search(block) is not None
                needs_macos = MACOS_DEPENDENCY.search(block) is not None
                if needs_docker and selector not in DOCKER_SELECTORS:
                    allowed = ", ".join(sorted(DOCKER_SELECTORS))
                    errors.append(
                        f"{relative}:{job_name}: Docker/container dependency requires "
                        f"one of {allowed}, found {selector!r}"
                    )
                if needs_macos and selector not in MACOS_SELECTORS:
                    allowed = ", ".join(sorted(MACOS_SELECTORS))
                    errors.append(
                        f"{relative}:{job_name}: macOS-specific dependency requires "
                        f"one of {allowed}, found {selector!r}"
                    )
                if needs_docker and needs_macos:
                    errors.append(
                        f"{relative}:{job_name}: combines Docker and macOS dependencies; "
                        "split the incompatible topology into separate jobs"
                    )
    return errors


def main() -> int:
    repo = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    errors = audit(repo)
    if errors:
        print("Environment dependency routing violations:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Environment dependency routing policy passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
