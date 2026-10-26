from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from market_data_ingestion.enums import DataType
from market_data_ingestion.spool import RecordSpool


def _replace_payload(spool: RecordSpool, row: dict[str, object]) -> None:
    payload = json.dumps(
        row,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    spool._connection.execute(
        "UPDATE records SET payload = ?, identity = ?",
        (payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()),
    )
    spool._connection.commit()


def _add_extra_field(row: dict[str, object]) -> None:
    row["unexpected"] = "value"


def _use_semantically_noncanonical_bool(row: dict[str, object]) -> None:
    row["is_closed"] = "1"


@pytest.mark.parametrize(
    "mutate",
    [_add_extra_field, _use_semantically_noncanonical_bool],
)
def test_spool_rejects_noncanonical_semantic_roundtrip(
    tmp_path: Path,
    five_minute_records,
    mutate: Callable[[dict[str, object]], None],
) -> None:
    spool = RecordSpool(tmp_path / "semantic-roundtrip.sqlite3", reset=True)
    try:
        spool.append("acquired", (five_minute_records[0],))
        payload = spool._connection.execute("SELECT payload FROM records").fetchone()[0]
        decoded = json.loads(payload)
        assert isinstance(decoded, dict)
        row: dict[str, object] = decoded
        mutate(row)
        _replace_payload(spool, row)

        with pytest.raises(Exception) as captured:
            tuple(spool.iter_sorted("acquired", DataType.KLINES, "5m"))

        assert getattr(captured.value, "code", None) == "SPOOL_METADATA_CORRUPT"
    finally:
        spool.close(remove=True)
