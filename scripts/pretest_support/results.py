from __future__ import annotations

import shutil
from collections.abc import Iterable
from pathlib import Path

from .models import PretestError

GENERATED_RESULT_ROOTS = frozenset(
    {
        "full-btc",
        "restart-replay",
        "smoke",
        "smoke-repeat",
    }
)


def reset_generated_results(workspace: Path, names: Iterable[str]) -> tuple[Path, ...]:
    """Remove only allowlisted result roots owned by the local pretest suite."""
    requested = tuple(dict.fromkeys(names))
    for name in requested:
        candidate = Path(name)
        if candidate.is_absolute() or candidate.name != name or name not in GENERATED_RESULT_ROOTS:
            raise PretestError(f"refusing to reset non-generated result root: {name!r}")

    workspace = Path(workspace)
    if workspace.name != "pretest" or workspace.parent.name != ".local-tests":
        raise PretestError("local pretest workspace must be .local-tests/pretest")
    if workspace.parent.is_symlink():
        raise PretestError("local pretest .local-tests parent must not be a symlink")
    if workspace.is_symlink():
        raise PretestError("local pretest workspace must not be a symlink")

    resolved_workspace = workspace.resolve()
    if resolved_workspace.name != "pretest" or resolved_workspace.parent.name != ".local-tests":
        raise PretestError("resolved local pretest workspace escaped .local-tests/pretest")

    results_root = workspace / "results"
    if results_root.is_symlink():
        raise PretestError("local pretest results root must not be a symlink")
    results_root.mkdir(parents=True, exist_ok=True)
    resolved_results_root = results_root.resolve()
    if resolved_results_root.parent != resolved_workspace:
        raise PretestError("local pretest results root escaped its workspace")

    targets: list[Path] = []
    for name in requested:
        target = results_root / name
        if target.is_symlink():
            raise PretestError(f"generated result root must not be a symlink: {name}")
        resolved_target = target.resolve(strict=False)
        if resolved_target.parent != resolved_results_root:
            raise PretestError(f"generated result root escaped results directory: {name}")
        if target.exists() and not target.is_dir():
            raise PretestError(f"generated result root is not a directory: {name}")
        targets.append(target)

    removed: list[Path] = []
    for target in targets:
        if target.exists():
            shutil.rmtree(target)
            removed.append(target)
    return tuple(removed)
