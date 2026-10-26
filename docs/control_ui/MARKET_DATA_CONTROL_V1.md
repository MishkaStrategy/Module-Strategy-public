# Market Data Control v1 — sanitized Dataset read bridge

Status: implemented on Draft PR #371; exact-head CI required before acceptance.

## Ownership

Historical Market Data Ingestion, Dataset Registry and its native HTTP API remain authoritative for dataset registration, storage, validation and durable jobs.

The unified Control Plane does not open the Registry database or storage directly and does not duplicate Dataset validation rules. It uses the existing loopback native API with the server-owned Market Data reader token.

## Control Plane API

Reader-authenticated routes:

```text
GET /api/control/v1/market-data/datasets
GET /api/control/v1/market-data/datasets/{dataset_id}
GET /api/control/v1/market-data/datasets/{dataset_id}/validation
```

The corresponding terminal commands are:

```text
modular-control dataset list
modular-control dataset show DATASET_ID
modular-control dataset validate DATASET_ID [--deep]
```

No Dataset mutation endpoint is added by this slice.

## Sanitization boundary

Native Dataset responses contain fields that are valid for the owner application but unsafe or unnecessary for the project-wide Control Plane, including storage/manifest locations and free-form issue/lineage text.

The Control Plane deliberately omits:

- `manifest_uri`;
- `storage_uri`;
- raw `unresolved_issues`;
- raw validation `issues`;
- raw lineage entries.

It exposes only immutable identity and bounded factual metadata:

- dataset ID/version/content hash;
- exchange/market/instrument;
- data types/timeframes;
- UTC range;
- Dataset schema/descriptor versions;
- validation status;
- registered timestamp;
- safe consumer identities;
- unresolved issue count and lineage count.

Validation projection exposes only status, deep/shallow mode and factual counters (`partitions_checked`, `raw_artifacts_checked`, `rows_checked`, `issue_count`).

All projected textual identities are bounded path-free tokens. Unexpected path-like values fail closed instead of being displayed.

## Transport safety

- Market Data base URL remains loopback-only and pathless.
- Native reader credentials remain server-side and are not returned by Control Plane.
- Proxy environment variables are ignored.
- Redirects are not followed.
- Catalog responses are bounded to 1 MiB.
- HTTP error bodies are never included in Control Plane errors.
- Upstream 404 becomes a typed Dataset-not-found Control Plane error.
- pagination, requested version, dataset identity and deep-validation mode are checked for response consistency.

## Browser/runtime truth

This bridge is a server-side projection of the owner API. The browser and terminal never become Dataset truth and do not cache critical Registry state as authority.
