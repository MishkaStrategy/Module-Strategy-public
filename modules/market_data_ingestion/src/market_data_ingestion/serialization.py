from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .canonical import primitive


def json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    if pretty:
        text = json.dumps(
            primitive(value), sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False
        )
    else:
        text = json.dumps(
            primitive(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    return (text + "\n").encode("utf-8")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(
            handle,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
