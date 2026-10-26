from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from .canonical import canonical_sha256
from .models import SpecificationError


class FinalTestAccessError(RuntimeError):
    """Raised when the untouched test gate is bypassed or reused."""


@dataclass(frozen=True, slots=True)
class FinalTestGrant:
    grant_id: str
    experiment_id: str
    specification_sha256: str
    split_fingerprint: str
    candidate_configuration_hashes: tuple[str, ...]
    issued_at: datetime
    consumed_at: datetime | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        candidates = tuple(sorted(set(self.candidate_configuration_hashes)))
        if not candidates:
            raise SpecificationError("final test grant requires frozen candidates")
        object.__setattr__(self, "candidate_configuration_hashes", candidates)
        for name in ("issued_at", "consumed_at"):
            value = getattr(self, name)
            if value is not None:
                if value.tzinfo is None or value.utcoffset() is None:
                    raise SpecificationError(f"{name} must be timezone-aware")
                object.__setattr__(self, name, value.astimezone(UTC))
        if self.schema_version != 1:
            raise SpecificationError("unsupported final test grant schema")

    @classmethod
    def issue(
        cls,
        *,
        experiment_id: str,
        specification_sha256: str,
        split_fingerprint: str,
        candidate_configuration_hashes: Sequence[str],
        issued_at: datetime,
    ) -> FinalTestGrant:
        candidates = tuple(sorted(set(candidate_configuration_hashes)))
        digest = canonical_sha256(
            {
                "experiment_id": experiment_id,
                "specification_sha256": specification_sha256,
                "split_fingerprint": split_fingerprint,
                "candidate_configuration_hashes": candidates,
            }
        )
        return cls(
            grant_id=f"final-grant-{digest[:24]}",
            experiment_id=experiment_id,
            specification_sha256=specification_sha256,
            split_fingerprint=split_fingerprint,
            candidate_configuration_hashes=candidates,
            issued_at=issued_at,
        )
