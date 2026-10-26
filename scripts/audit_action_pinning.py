from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / ".github" / "action-pinning-baseline.json"
ACTION_ROOTS = (ROOT / ".github" / "workflows", ROOT / ".github" / "actions")
USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*([^\s#]+)", re.MULTILINE)
IMMUTABLE_SHA = re.compile(r"^[0-9a-f]{40}$")


def mutable_uses() -> Counter[str]:
    refs: Counter[str] = Counter()
    for root in ACTION_ROOTS:
        for pattern in ("**/*.yml", "**/*.yaml"):
            for path in sorted(root.glob(pattern)):
                for use in USES_RE.findall(path.read_text(encoding="utf-8")):
                    if use.startswith("./"):
                        continue
                    ref = use.rsplit("@", 1)[-1]
                    if not IMMUTABLE_SHA.fullmatch(ref):
                        refs[use] += 1
    return refs


def audit() -> list[str]:
    payload = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    allowed = Counter(payload["allowed_counts"])
    current = mutable_uses()
    errors: list[str] = []
    for use, count in sorted(current.items()):
        if count > allowed[use]:
            errors.append(
                f"mutable Action ref {use!r} occurs {count} time(s); baseline allows {allowed[use]}"
            )
    return errors


def main() -> int:
    errors = audit()
    if errors:
        print("Immutable Action policy violations:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    refs = mutable_uses()
    print(
        f"Immutable Action policy passed; grandfathered mutable refs remaining: {sum(refs.values())}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
