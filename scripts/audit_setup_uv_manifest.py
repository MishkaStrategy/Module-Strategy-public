from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACTION_ROOTS = (ROOT / ".github" / "workflows", ROOT / ".github" / "actions")
SETUP_UV_RE = re.compile(
    r"^(?P<indent>\s*)(?P<dash>-\s+)?uses:\s*astral-sh/setup-uv@(?P<ref>[^\s#]+)"
)
IMMUTABLE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^\s*version:\s*['\"]?(?P<version>\d+\.\d+\.\d+)['\"]?\s*(?:#.*)?$")
MANIFEST_RE = re.compile(
    r"^\s*manifest-file:\s*['\"]?data:application/x-ndjson,",
)


@dataclass(frozen=True)
class SetupUvUse:
    path: Path
    line_number: int
    action_ref: str
    block: tuple[str, ...]


def _iter_setup_uv_uses(path: Path) -> list[SetupUvUse]:
    lines = path.read_text(encoding="utf-8").splitlines()
    uses: list[SetupUvUse] = []
    for index, line in enumerate(lines):
        match = SETUP_UV_RE.match(line)
        if match is None:
            continue

        uses_indent = len(match.group("indent"))
        step_indent = uses_indent if match.group("dash") else max(0, uses_indent - 2)
        block = [line]
        for candidate in lines[index + 1 :]:
            stripped = candidate.lstrip()
            indent = len(candidate) - len(stripped)
            if stripped.startswith("- ") and indent <= step_indent:
                break
            if stripped and indent < step_indent:
                break
            block.append(candidate)

        uses.append(
            SetupUvUse(
                path=path,
                line_number=index + 1,
                action_ref=match.group("ref"),
                block=tuple(block),
            )
        )
    return uses


def setup_uv_uses(root: Path = ROOT) -> list[SetupUvUse]:
    uses: list[SetupUvUse] = []
    for action_root in (root / ".github" / "workflows", root / ".github" / "actions"):
        for pattern in ("**/*.yml", "**/*.yaml"):
            for path in sorted(action_root.glob(pattern)):
                uses.extend(_iter_setup_uv_uses(path))
    return uses


def audit(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    for use in setup_uv_uses(root):
        location = f"{use.path.relative_to(root)}:{use.line_number}"
        if IMMUTABLE_SHA_RE.fullmatch(use.action_ref) is None:
            errors.append(
                f"{location}: setup-uv action ref {use.action_ref!r} is not an immutable 40-character SHA"
            )

        version = next(
            (match.group("version") for line in use.block if (match := VERSION_RE.match(line))),
            None,
        )
        if version is None:
            errors.append(f"{location}: direct setup-uv step must pin a literal semantic version")

        if not any(MANIFEST_RE.match(line) for line in use.block):
            errors.append(
                f"{location}: direct setup-uv step must embed a data:application/x-ndjson manifest"
            )
    return errors


def main() -> int:
    errors = audit()
    if errors:
        print("setup-uv manifest policy violations:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    uses = setup_uv_uses()
    print(f"setup-uv manifest policy passed for {len(uses)} direct Action use(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
