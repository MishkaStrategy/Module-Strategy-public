from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from market_data_ingestion.enums import DataType
from market_data_ingestion.spool import RecordSpool


def test_pending_rowid_state_is_bounded_for_unpromoted_input(
    tmp_path: Path,
    five_minute_records,
) -> None:
    spool = RecordSpool(tmp_path / "bounded.sqlite3", reset=True, batch_size=2)
    try:
        repeated = (five_minute_records[0] for _ in range(5_000))
        assert spool.append("acquired", repeated) == 5_000
        assert sum(1 for _ in spool.iter_sorted("acquired", DataType.KLINES, "5m")) == 5_000

        retained_rowids = sum(len(items) for items in spool._pending_rowids.values())
        assert len(spool._pending_rowid_order) == spool.pending_rowid_limit
        assert retained_rowids <= spool.pending_rowid_limit
    finally:
        spool.close(remove=True)


def test_evicted_rowid_falls_back_to_full_validated_payload(
    tmp_path: Path,
    five_minute_records,
) -> None:
    spool = RecordSpool(tmp_path / "fallback.sqlite3", reset=True, batch_size=2)
    try:
        seed = five_minute_records[0]
        records = tuple(
            replace(
                seed,
                open_time=seed.open_time + timedelta(minutes=5 * index),
                close_time=seed.close_time + timedelta(minutes=5 * index),
            )
            for index in range(spool.pending_rowid_limit + 1)
        )
        assert spool.append("acquired", records) == len(records)
        acquired = tuple(spool.iter_sorted("acquired", DataType.KLINES, "5m"))

        assert spool.append("validated", (acquired[0],)) == 1
        assert spool.count("validated", DataType.KLINES, "5m") == 1
        assert spool._connection.execute(
            "SELECT COUNT(*) FROM records WHERE stage = 'validated'"
        ).fetchone() == (1,)
    finally:
        spool.close(remove=True)
