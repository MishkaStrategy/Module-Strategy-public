from __future__ import annotations

import hashlib
import json
from pathlib import Path


def test_schema_catalog_is_deterministic_and_references_unique_schemas() -> None:
    root = Path(__file__).parents[1] / "schemas"
    catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    expected_hash = catalog.pop("catalog_hash")
    payload = json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(payload).hexdigest() == expected_hash
    paths = [item["path"] for item in catalog["schemas"]]
    ids = [item["id"] for item in catalog["schemas"]]
    assert paths == sorted(paths)
    assert len(paths) == len(set(paths)) == len(set(ids))
    for item in catalog["schemas"]:
        document = json.loads((root / item["path"]).read_text(encoding="utf-8"))
        assert document["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert document["$id"] == item["id"]
        assert document["type"] == "object"
        assert document["additionalProperties"] is False
