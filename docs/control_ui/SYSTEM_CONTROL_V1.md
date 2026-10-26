# System Control v1 — resources and fail-closed capacity

Status: implemented on Draft PR #371 with the System Capacity extension tracked by #693 / Draft PR #698. Exact-head CI is required before capacity acceptance.

## Ownership

System Resources is a read-only operational projection owned by the local Control Plane process. System Capacity is a separate read-only projection of the canonical GAP-013 storage-pressure policy owned by #390 and consumed through the same Control Plane/System Resources surface.

Neither projection is a second trading, research, market-data, acceptance, cleanup or backup state machine. Browser state is never authoritative.

## Resource reader API

```text
GET /api/control/v1/system/resources
```

The stable v1 route returns:

- UTC collection timestamp;
- aggregate CPU utilization and logical CPU count;
- RAM total/used/available bytes and utilization percentage;
- disk total/used/available bytes and utilization percentage for the server-selected root volume;
- operating-system uptime in seconds;
- aggregate process count;
- RSS bytes and thread count for the current Control Plane process only.

This contract is intentionally unchanged by #693.

## Capacity reader APIs

```text
GET /api/control/v1/system/capacity
GET /api/control/v1/system/capacity/artifacts
```

Both use normal reader authentication. They are available only when the trusted server process attaches the canonical policy through an absolute server-side `MODULAR_CONTROL_STORAGE_PRESSURE_POLICY_PATH`. The browser cannot supply or override that path.

A missing policy returns typed `NOT_ATTACHED`; malformed, stale or semantically unsafe policy data returns typed `UNAVAILABLE`. Unknown capacity remains unknown rather than being guessed.

### Owner/local capacity projection

`/system/capacity` projects only allowlisted semantics from `docs/operations/STORAGE_PRESSURE_THRESHOLDS.json`:

- accepted self-hosted runner free-disk high/critical watermarks for the named runner roles;
- System Acceptance owner DB/payload bounds and accepted measurements;
- Market Data next-publication admission semantics and recovery reserve;
- Market Data application/control-plane representative storage measurements, explicitly non-aggregatable;
- Research platform accepted storage measurements;
- Core/Paper/Trading `UNKNOWN_DO_NOT_PRUNE` state while no concrete persistent runtime backend is selected.

The route explicitly reports that no universal local-host physical watermark is available. Current disk free space from `/system/resources` is an observation only and is not converted into a time-to-full or global high/critical forecast.

### External artifact quota projection

`/system/capacity/artifacts` keeps GitHub Actions artifact quota separate from local physical disk. It exposes only:

- `PARTIAL_EXTERNAL_QUOTA` status;
- proactive usage status `UNKNOWN_NO_ACCEPTED_USAGE_SOURCE`;
- accepted critical signal `CREATE_ARTIFACT_QUOTA_FAILURE`;
- bounded maintenance safety ceiling (count/bytes/rate-limit reserve);
- `local_disk_watermark_applicable=false`;
- `operator_cleanup_available=false`.

The maintenance ceiling is not a quota watermark. Control UI exposes no artifact-delete/prune mutation.

## Canonical fail-closed invariants

The provider rejects policy data that would change these accepted semantics, including:

- a different capacity forecast owner;
- duplicate-monitor permission;
- treating unknown as deletion permission;
- owner deletion authority other than `NONE` where the canonical owner tier carries that field;
- partial physical thresholds (one bound known while the other is unknown);
- stale owner-proof statuses;
- treating Market Data component maxima as a synthetic aggregate;
- treating Market Data next-publication admission as a universal disk watermark;
- treating the external artifact maintenance ceiling as proactive quota usage or a quota watermark;
- external artifact cleanup authority.

Policy files are size-bounded regular files and are opened no-follow where the platform supports it. Raw policy contents, source paths and rationale text are never projected.

## Privacy boundary

The System contracts intentionally contain no fields for:

- hostname or machine name;
- username/user identity;
- environment variables or secrets;
- current working directory;
- command line / argv;
- individual PID/process identities or process lists;
- filesystem or mount paths;
- owner database paths;
- raw market data;
- network-interface or address inventory;
- GitHub artifact IDs, deletion endpoints or maintenance batch identities.

Provider exceptions are converted to typed safe Control API failures. Raw exception messages and configured policy paths are not returned to clients.

## Terminal surface

```text
modular-control system resources [--json]
modular-control system capacity [--json]
modular-control system artifact-capacity [--json]
```

`system capacity` reports unknown physical thresholds as `UNKNOWN` and deletion authority as `NONE`. `system artifact-capacity` reports unknown proactive quota usage and explicitly states that Control UI cleanup is unavailable.

## Dashboard surface

`#/system/resources` reads resource metrics, owner/local capacity and external artifact quota independently. Failure of one capacity source does not blank CPU/RAM/disk metrics or silently convert another source into replacement truth.

The Dashboard labels runner watermarks as CI-runner policy, not local-host forecast; owner measurements as non-aggregatable; Market Data publication admission as operation-specific; and external artifact quota as external to local disk.

## Dependency boundary

`psutil` remains a server-side resource-provider dependency. Capacity policy projection uses standard-library/Pydantic code and does not make the canonical repository JSON part of the clean interface wheel. A clean installed wheel therefore starts without a repository checkout; capacity remains `NOT_ATTACHED` until deployment explicitly supplies a trusted policy file.

## Non-goals

This surface does not expose:

- pruning/deletion or cleanup commands;
- backup/restore or System Recovery controls;
- automatic storage reclamation;
- time-to-full prediction without an accepted owner growth model;
- arbitrary process inspection;
- process termination/signals;
- filesystem browsing;
- shell commands;
- log-file paths/tailing;
- remote machine monitoring;
- resource tuning;
- Live/private-exchange controls.
