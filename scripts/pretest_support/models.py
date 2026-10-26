from __future__ import annotations

import hashlib
import math
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Sequence

BASE_SHA = "3962fe5116c05ca9858bc9bcaf03a6e0d84c9e66"
STEP = timedelta(minutes=5)


class PretestError(RuntimeError):
    """A blocking local readiness condition."""


@dataclass(frozen=True, slots=True)
class Finding:
    level: str
    check: str
    detail: str


@dataclass(frozen=True, slots=True)
class DataFacts:
    path: str
    format: str
    sha256: str
    rows: int
    first: str
    last: str
    gaps: int
    timestamp_column: str
    columns: dict[str, str]


class Reporter:
    def __init__(self) -> None:
        self.findings: list[Finding] = []

    def add(self, level: str, check: str, detail: str) -> None:
        self.findings.append(Finding(level, check, detail))
        print(f"[{level.upper()}] {check}: {detail}")

    def passed(self, check: str, detail: str) -> None:
        self.add("pass", check, detail)

    def warn(self, check: str, detail: str) -> None:
        self.add("warn", check, detail)

    def fail(self, check: str, detail: str) -> None:
        self.add("fail", check, detail)

    @property
    def failures(self) -> int:
        return sum(row.level == "fail" for row in self.findings)

    @property
    def warnings(self) -> int:
        return sum(row.level == "warn" for row in self.findings)


def run(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def timestamp(raw: object) -> datetime:
    if isinstance(raw, bool):
        raise PretestError("timestamp must not be boolean")
    if isinstance(raw, datetime):
        value = raw
    elif isinstance(raw, int | float) or (isinstance(raw, str) and raw.strip().isdigit()):
        number_value = int(raw)
        seconds = number_value / 1000 if number_value > 10_000_000_000 else number_value
        value = datetime.fromtimestamp(seconds, tz=UTC)
    elif isinstance(raw, str):
        value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    else:
        raise PretestError(f"unsupported timestamp: {raw!r}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise PretestError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def number(raw: object, name: str) -> float:
    if isinstance(raw, bool) or raw is None:
        raise PretestError(f"{name} is not numeric")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise PretestError(f"{name} is not numeric") from exc
    if not math.isfinite(value):
        raise PretestError(f"{name} is not finite")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
