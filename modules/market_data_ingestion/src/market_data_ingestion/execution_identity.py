from __future__ import annotations

import re
from dataclasses import dataclass

from .adapters.base import ExchangeAdapter
from .canonical import sha256_canonical
from .contracts import MarketDataRequest
from .version import NORMALIZATION_VERSION, VALIDATION_POLICY_VERSION

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class IngestionExecutionIdentity:
    run_id: str
    execution_hash: str
    adapter_id: str
    adapter_version: str

    def __post_init__(self) -> None:
        if _SHA256.fullmatch(self.execution_hash) is None:
            raise ValueError("execution_hash must be lowercase SHA-256")
        if self.run_id != f"mdi:{self.execution_hash[:24]}":
            raise ValueError("run_id must be derived from execution_hash")
        if not self.adapter_id.strip() or not self.adapter_version.strip():
            raise ValueError("adapter identity must be non-empty")


def ingestion_execution_identity(
    request: MarketDataRequest,
    adapter: ExchangeAdapter,
    *,
    format_name: str = "parquet",
    compression: str = "zstd",
    reject_unresolved_required_gaps: bool = True,
    concurrency: int = 1,
    max_records_in_memory: int = 5_000_000,
) -> IngestionExecutionIdentity:
    """Return the deterministic identity used by the ingestion pipeline.

    The interface layer consumes this backend-owned helper and a parity test verifies it
    against the actual `IngestionRunResult.run_id`. The pipeline implementation remains
    authoritative until it is mechanically refactored to call this helper directly.
    """

    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency <= 0:
        raise ValueError("concurrency must be a positive integer")
    if (
        not isinstance(max_records_in_memory, int)
        or isinstance(max_records_in_memory, bool)
        or max_records_in_memory <= 0
    ):
        raise ValueError("max_records_in_memory must be a positive integer")
    execution_hash = sha256_canonical(
        {
            "request": request,
            "adapter_id": adapter.exchange_id,
            "adapter_version": adapter.adapter_version,
            "normalization_version": NORMALIZATION_VERSION,
            "validation_policy_version": VALIDATION_POLICY_VERSION,
            "storage_format": format_name,
            "compression": compression,
            "reject_unresolved_required_gaps": reject_unresolved_required_gaps,
            "concurrency": concurrency,
            "max_records_in_memory": max_records_in_memory,
        }
    )
    return IngestionExecutionIdentity(
        run_id=f"mdi:{execution_hash[:24]}",
        execution_hash=execution_hash,
        adapter_id=adapter.exchange_id,
        adapter_version=adapter.adapter_version,
    )
