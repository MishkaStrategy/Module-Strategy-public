from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from modular_backtest.event_artifacts import (
    SHARDED_EVENT_ENCODING,
    shard_large_gzip_event_stream,
)
from modular_backtest.funding import FundingEvent, FundingLineageEntry, RegisteredFundingSource
from modular_backtest.funding_application import (
    FundingApplicationError,
    collect_funding_runtime_evidence,
)
from modular_backtest.futures_execution import FUNDING_RUNTIME_MODEL_VERSION


def _source() -> RegisteredFundingSource:
    start = datetime(2026, 2, 1, tzinfo=UTC)
    event = FundingEvent(
        ordinal=0,
        timestamp=start,
        symbol="BTCUSDT",
        rate=Decimal("0.001"),
        mark_price=Decimal("100"),
    )
    return RegisteredFundingSource(
        dataset_id="compressed-funding-test",
        dataset_version=1,
        dataset_hash="a" * 64,
        symbol="BTCUSDT",
        coverage_start_inclusive=start,
        coverage_end_exclusive=start + timedelta(hours=1),
        event_count=1,
        source_record_count=1,
        lineage=(
            FundingLineageEntry(
                artifact_id="owner",
                source_kind="official_rest",
                checksum_sha256="b" * 64,
                record_count=1,
                start=start,
                end=start + timedelta(hours=1),
            ),
        ),
        events=(event,),
    )


def _funding_row(source: RegisteredFundingSource) -> dict[str, object]:
    identity = source.identity_sha256()
    event = source.events[0]
    return {
        "event_type": "portfolio.funding",
        "source": "backtest_funding",
        "payload": {
            "funding": {
                "model_version": FUNDING_RUNTIME_MODEL_VERSION,
                "source_identity_sha256": identity,
                "ordinal": 0,
                "event_key": event.key(identity),
                "cashflow": "-1.25",
            }
        },
    }


def test_collect_funding_evidence_reads_lossless_gzip_stream(tmp_path: Path) -> None:
    source = _source()
    path = tmp_path / "events.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps({"event_type": "run.started"}, sort_keys=True) + "\n")
        handle.write(json.dumps(_funding_row(source), sort_keys=True) + "\n")

    evidence = dict(
        collect_funding_runtime_evidence(tmp_path, source, application_id="compressed-run")
    )

    assert evidence["journaled_settlement_count"] == 1
    assert evidence["cumulative_journaled_cashflow"] == "-1.25"
    assert evidence["event_stream_encoding"] == "gzip"


def test_collect_funding_evidence_reads_verified_sharded_gzip_stream(tmp_path: Path) -> None:
    source = _source()
    rows = (
        json.dumps({"event_type": "run.started"}, sort_keys=True)
        + "\n"
        + json.dumps(_funding_row(source), sort_keys=True)
        + "\n"
    )
    canonical = rows.encode("utf-8")
    path = tmp_path / "events.jsonl.gz"
    with gzip.open(path, "wb", compresslevel=1) as handle:
        handle.write(canonical)
    physical_bytes = path.stat().st_size
    (tmp_path / "journal.json").write_text(
        json.dumps(
            {
                "schema_version": "3",
                "event_stream": {
                    "path": "events.jsonl.gz",
                    "encoding": "gzip",
                    "count": 2,
                    "bytes": physical_bytes,
                    "logical_bytes": len(canonical),
                    "semantic_sha256": "c" * 64,
                    "counts_by_type": {"run.started": 1, "portfolio.funding": 1},
                    "retained_window": 2,
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    shard_large_gzip_event_stream(
        tmp_path,
        max_file_bytes=physical_bytes,
        target_part_bytes=max(1, physical_bytes // 3),
    )

    evidence = dict(
        collect_funding_runtime_evidence(tmp_path, source, application_id="sharded-run")
    )

    assert evidence["journaled_settlement_count"] == 1
    assert evidence["cumulative_journaled_cashflow"] == "-1.25"
    assert evidence["event_stream_encoding"] == SHARDED_EVENT_ENCODING


def test_collect_funding_evidence_rejects_ambiguous_plain_and_gzip_streams(
    tmp_path: Path,
) -> None:
    source = _source()
    (tmp_path / "events.jsonl").write_text("", encoding="utf-8")
    with gzip.open(tmp_path / "events.jsonl.gz", "wt", encoding="utf-8"):
        pass

    with pytest.raises(FundingApplicationError, match="exactly one"):
        collect_funding_runtime_evidence(tmp_path, source, application_id="ambiguous-run")
