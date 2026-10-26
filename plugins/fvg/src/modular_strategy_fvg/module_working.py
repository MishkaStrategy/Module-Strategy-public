from __future__ import annotations

from dataclasses import dataclass

from .module_shared import *  # noqa: F401,F403


@dataclass(slots=True)
class _FvgWorkingState:
    zones: dict[str, FvgZone]
    lifecycle: dict[str, FvgZoneLifecycle]
    candidates: dict[str, FvgPreCandidate]
    pre_evaluated: dict[tuple[str, str], datetime]
    last: dict[tuple[str, str], datetime]
    fingerprints: dict[tuple[str, str], str | None]
    history_fingerprints: dict[tuple[str, str], tuple[tuple[datetime, str], ...]]
    history_references: dict[tuple[str, str], tuple[_HistoryReference, ...]]
    signatures: dict[tuple[str, str], _BarSignature | None]
    barriers: dict[tuple[str, str], datetime]
    bound_symbol: str | None
    republish_pending: bool
