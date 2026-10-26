"""Official Binance canonical Variant 2 backtest entry point."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ._common import (
    REGISTERED_DATASET_HASH,
    discover_drive_parquet,
    load_5m,
    sha256_file,
)
from ._portfolio import run_portfolio, summarize
from ._signals import build_signals
from .registered_canonical_dataset import (
    EXPECTED_PARTITIONS,
    OFFICIAL_REST_CONTENT_SHA256,
    OFFICIAL_REST_PARQUET_SHA256,
    REGISTERED_DATASET_ID,
    REGISTERED_DATASET_VERSION,
    REGISTERED_SOURCE,
    REGISTERED_SOURCE_URI,
)


def _validate_registered_provenance(value: dict[str, object]) -> None:
    if value.get("api_attempt_status") != "HTTP_451_RESTRICTED_LOCATION":
        raise RuntimeError("registered canonical fallback lacks exact HTTP 451 cause")
    if value.get("content_sha256") != OFFICIAL_REST_CONTENT_SHA256:
        raise RuntimeError("registered canonical content digest mismatch")
    if value.get("parquet_sha256") != OFFICIAL_REST_PARQUET_SHA256:
        raise RuntimeError("registered canonical Parquet digest mismatch")
    if value.get("official_rest_content_sha256") != OFFICIAL_REST_CONTENT_SHA256:
        raise RuntimeError("registered fallback official REST reference mismatch")
    if value.get("official_content_parity") is not True:
        raise RuntimeError("registered fallback did not prove official content parity")
    registered = value.get("registered_dataset")
    if not isinstance(registered, dict):
        raise RuntimeError("registered fallback dataset evidence is missing")
    exact = {
        "consumer": "modular_research_runner",
        "dataset_hash": REGISTERED_DATASET_HASH,
        "dataset_id": REGISTERED_DATASET_ID,
        "dataset_version": REGISTERED_DATASET_VERSION,
        "partition_count": EXPECTED_PARTITIONS,
        "row_count": 721_597,
        "source_uri": REGISTERED_SOURCE_URI,
        "validation_status": "VALID",
    }
    for key, expected in exact.items():
        if registered.get(key) != expected:
            raise RuntimeError(f"registered fallback {key} mismatch")


def _load_provenance(path: Path, input_path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("dataset provenance must be a JSON object")
    endpoint = value.get("endpoint")
    if endpoint != {
        "base_url": "https://fapi.binance.com",
        "method": "GET",
        "path": "/fapi/v1/klines",
        "security": "NONE",
    }:
        raise RuntimeError("dataset endpoint provenance mismatch")
    source = value.get("source")
    if source not in {
        "OFFICIAL_BINANCE_USDTM_REST_API",
        "OFFICIAL_BINANCE_PUBLIC_DATA_ARCHIVE",
        REGISTERED_SOURCE,
    }:
        raise RuntimeError("dataset source is not an approved canonical source")
    parquet_sha = value.get("parquet_sha256")
    if not isinstance(parquet_sha, str) or parquet_sha != sha256_file(input_path):
        raise RuntimeError("dataset parquet provenance mismatch")
    comparison = value.get("drive_comparison_status")
    parity = value.get("drive_content_parity")
    if comparison == "MATCH" and parity is not True:
        raise RuntimeError("matched Drive comparison must declare parity")
    if comparison == "NOT_REQUESTED" and parity is not None:
        raise RuntimeError("omitted Drive comparison must not declare parity")
    if comparison not in {"MATCH", "NOT_REQUESTED"}:
        raise RuntimeError("dataset Drive comparison status is invalid")
    if value.get("row_count") != 721_597:
        raise RuntimeError("dataset provenance row count mismatch")
    if source == REGISTERED_SOURCE:
        _validate_registered_provenance(value)
    return value


def run(
    input_path: Path,
    output_dir: Path,
    *,
    generated_from_commit: str,
    provenance_path: Path,
) -> dict[str, object]:
    provenance = _load_provenance(provenance_path, input_path)
    if provenance.get("generated_from_commit") != generated_from_commit:
        raise RuntimeError("dataset provenance commit mismatch")
    frame = load_5m(input_path, expected_sha256=str(provenance["parquet_sha256"]))
    signals = build_signals(frame)
    ledger, counts = run_portfolio(frame, signals)
    return summarize(
        ledger,
        signal_counts=counts,
        output_dir=output_dir,
        input_path=input_path,
        row_count=len(frame),
        generated_from_commit=generated_from_commit,
        dataset_provenance=provenance,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--generated-from-commit", required=True)
    parser.add_argument("--dataset-provenance", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.input:
        input_path = arguments.input.resolve(strict=True)
    else:
        input_path = discover_drive_parquet()
    summary = run(
        input_path,
        arguments.output.resolve(),
        generated_from_commit=arguments.generated_from_commit,
        provenance_path=arguments.dataset_provenance.resolve(strict=True),
    )
    print(json.dumps(summary, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
