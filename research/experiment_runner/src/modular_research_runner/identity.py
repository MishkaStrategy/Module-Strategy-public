from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .canonical import canonical_sha256
from .models import ResearchSpecification, SpecificationError


def _short(prefix: str, digest: str, length: int = 24) -> str:
    return f"{prefix}-{digest[:length]}"


@dataclass(frozen=True, slots=True)
class ExperimentIdentityInputs:
    specification: ResearchSpecification
    backtest_engine_version: str
    metric_schema_version: str
    module_api_version: str

    def experiment_id(self) -> str:
        digest = canonical_sha256(
            {
                "research_specification_sha256": self.specification.fingerprint,
                "backtest_engine_version": self.backtest_engine_version,
                "metric_schema_version": self.metric_schema_version,
                "module_api_version": self.module_api_version,
            }
        )
        return _short("exp", digest)


def configuration_id(parameters: object) -> str:
    return _short("cfg", canonical_sha256(parameters))


def run_id(
    *,
    experiment_id: str,
    stage_id: str,
    split_id: str,
    configuration_sha256: str,
    scenario_id: str,
    seed: int,
    repetition_index: int,
) -> str:
    if repetition_index < 0:
        raise SpecificationError("repetition_index must be non-negative")
    digest = canonical_sha256(
        {
            "experiment_id": experiment_id,
            "stage_id": stage_id,
            "split_id": split_id,
            "configuration_sha256": configuration_sha256,
            "scenario_id": scenario_id,
            "seed": seed,
            "repetition_index": repetition_index,
        }
    )
    return _short("run", digest)


def derive_seed(root_seed: int, *labels: str) -> int:
    if not isinstance(root_seed, int) or isinstance(root_seed, bool):
        raise SpecificationError("root seed must be an integer")
    if any(not isinstance(label, str) or not label for label in labels):
        raise SpecificationError("seed labels must be non-empty strings")
    payload = canonical_sha256({"root_seed": root_seed, "labels": labels}).encode("ascii")
    digest = hashlib.sha256(payload).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)
