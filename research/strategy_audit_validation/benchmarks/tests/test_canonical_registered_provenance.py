from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PANDAS_AVAILABLE = importlib.util.find_spec("pandas") is not None
pytestmark = pytest.mark.skipif(
    not _PANDAS_AVAILABLE,
    reason="canonical provenance validation requires the benchmark environment",
)

if _PANDAS_AVAILABLE:
    _BENCHMARK_ROOT = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(_BENCHMARK_ROOT))
    from benchmarks import canonical_strategy_v2 as canonical
    from benchmarks import registered_canonical_dataset as registered
else:
    canonical = registered = None


def _provenance(input_path: Path) -> dict[str, object]:
    assert canonical is not None and registered is not None
    return {
        "api_attempt_status": "HTTP_451_RESTRICTED_LOCATION",
        "content_sha256": registered.OFFICIAL_REST_CONTENT_SHA256,
        "drive_comparison_status": "NOT_REQUESTED",
        "drive_content_parity": None,
        "endpoint": {
            "base_url": "https://fapi.binance.com",
            "method": "GET",
            "path": "/fapi/v1/klines",
            "security": "NONE",
        },
        "generated_from_commit": "a" * 40,
        "official_content_parity": True,
        "official_rest_content_sha256": registered.OFFICIAL_REST_CONTENT_SHA256,
        "parquet_sha256": canonical.sha256_file(input_path),
        "registered_dataset": {
            "consumer": "modular_research_runner",
            "dataset_hash": canonical.REGISTERED_DATASET_HASH,
            "dataset_id": registered.REGISTERED_DATASET_ID,
            "dataset_version": registered.REGISTERED_DATASET_VERSION,
            "partition_count": registered.EXPECTED_PARTITIONS,
            "row_count": 721_597,
            "source_uri": registered.REGISTERED_SOURCE_URI,
            "validation_status": "VALID",
        },
        "retained_drive_raw_sha256": (
            "2b8a20ef09e1c292abbbee4a6963a1cedb7904cfc4340c9de72d0d2cd0b87ed1"
        ),
        "row_count": 721_597,
        "source": registered.REGISTERED_SOURCE,
    }


def _write(tmp_path: Path, payload: dict[str, object]) -> tuple[Path, Path]:
    input_path = tmp_path / "input.parquet"
    input_path.write_bytes(b"registered-canonical")
    payload["parquet_sha256"] = canonical.sha256_file(input_path) if canonical else ""
    provenance = tmp_path / "provenance.json"
    provenance.write_text(json.dumps(payload), encoding="utf-8")
    return input_path, provenance


def _bind_synthetic_parquet(
    monkeypatch: pytest.MonkeyPatch,
    input_path: Path,
) -> None:
    assert canonical is not None
    monkeypatch.setattr(
        canonical,
        "OFFICIAL_REST_PARQUET_SHA256",
        canonical.sha256_file(input_path),
    )


def test_registered_canonical_provenance_is_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert canonical is not None
    seed = tmp_path / "seed"
    seed.write_bytes(b"registered-canonical")
    payload = _provenance(seed)
    input_path, provenance = _write(tmp_path, payload)
    _bind_synthetic_parquet(monkeypatch, input_path)

    observed = canonical._load_provenance(provenance, input_path)

    assert observed["source"] == registered.REGISTERED_SOURCE


def test_registered_canonical_provenance_rejects_dataset_hash_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert canonical is not None
    seed = tmp_path / "seed"
    seed.write_bytes(b"registered-canonical")
    payload = _provenance(seed)
    evidence = payload["registered_dataset"]
    assert isinstance(evidence, dict)
    evidence["dataset_hash"] = "0" * 64
    input_path, provenance = _write(tmp_path, payload)
    _bind_synthetic_parquet(monkeypatch, input_path)

    with pytest.raises(RuntimeError, match="dataset_hash mismatch"):
        canonical._load_provenance(provenance, input_path)


def test_registered_canonical_provenance_rejects_official_content_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert canonical is not None
    seed = tmp_path / "seed"
    seed.write_bytes(b"registered-canonical")
    payload = _provenance(seed)
    payload["content_sha256"] = "0" * 64
    input_path, provenance = _write(tmp_path, payload)
    _bind_synthetic_parquet(monkeypatch, input_path)

    with pytest.raises(RuntimeError, match="content digest mismatch"):
        canonical._load_provenance(provenance, input_path)


def test_registered_canonical_provenance_rejects_partition_count_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert canonical is not None and registered is not None
    seed = tmp_path / "seed"
    seed.write_bytes(b"registered-canonical")
    payload = _provenance(seed)
    evidence = payload["registered_dataset"]
    assert isinstance(evidence, dict)
    evidence["partition_count"] = registered.EXPECTED_PARTITIONS - 1
    input_path, provenance = _write(tmp_path, payload)
    _bind_synthetic_parquet(monkeypatch, input_path)

    with pytest.raises(RuntimeError, match="partition_count mismatch"):
        canonical._load_provenance(provenance, input_path)


def test_registered_canonical_provenance_rejects_accepted_parquet_drift(
    tmp_path: Path,
) -> None:
    assert canonical is not None
    seed = tmp_path / "seed"
    seed.write_bytes(b"registered-canonical")
    payload = _provenance(seed)
    input_path, provenance = _write(tmp_path, payload)

    with pytest.raises(RuntimeError, match="Parquet digest mismatch"):
        canonical._load_provenance(provenance, input_path)
