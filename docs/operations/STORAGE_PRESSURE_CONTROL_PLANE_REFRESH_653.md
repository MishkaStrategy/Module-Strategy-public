# GAP-013 Market Data control-plane storage proof refresh

Issue: #653  
Parent: #540 / #390 / #373  
Accepted owner proof: #603 / PR #604  
Canonical merge: `2a4286a288341fd395789d34a6ffd5c279706b4f`  
Accepted Market Data test tree: `9830209ebb7df137b604feba676bdb21992ff1bb`  
Accepted ingestion run: `31500006636`

This checkpoint updates the canonical storage-pressure inventory after the application/control-plane SQLite owner proof was accepted. It is separate from the existing Market Data registry/publication-admission tier because the persistence and failure semantics are different.

The accepted proof covers the real application JobStore/lease/repair-confirmation database, the hardened control-plane token/audit database, and the production-selected shared `jobs.sqlite3` lifecycle. Representative measurements use scales `1 / 100 / 1000` and 3 repeats on Python 3.12 and 3.13. The proof measures DB/WAL/SHM/live/settled footprints, checks reopen/integrity, and induces actual bounded `SQLITE_FULL` through proof-only `PRAGMA max_page_count` without filling the host disk.

At scale 1000, conservative component-wise maxima across the accepted Python matrices are:

- operational JobStore/control-plane operational surface: DB `1,077,248 B`, WAL `4,157,112 B`, SHM `32,768 B`, max observed live total `5,267,128 B`, settled DB `1,089,536 B`, repeat live-total range `16,432 B`, max-live bytes/item `5,268 B`;
- hardened token/audit surface: DB `360,448 B`, WAL `4,132,392 B`, SHM `32,768 B`, max observed live total `4,525,608 B`, settled DB `417,792 B`, repeat range `0 B`, max-live bytes/item `4,526 B`;
- shared `jobs.sqlite3`: durable jobs/events/leases/repair-confirmations `1000 / 2000 / 0 / 1000`, DB max `1,724,416 B`, WAL max `4,169,472 B`, SHM `32,768 B`, max observed live total `5,914,344 B`, settled DB max `1,748,992 B`, repeat live-total range max `12,360 B`, max-live bytes/item `5,915 B`.

Component-wise maxima may come from different repeat/matrix observations and must not be summed into a synthetic footprint. `max_live_bytes_per_item` is total live footprint divided by scale, not a marginal growth slope.

Boundary evidence SHA-256 is `3bdac93a5776851322d52b5b366af21de0e86f35b8aa1fc52b7ce482a64d2774`. The bounded fault proof preserves transaction atomicity, committed generation and HMAC audit-chain identity, passes integrity/reopen, and recovers after restoring only test capacity.

No host free-space threshold is inferred from these measurements. The canonical policy therefore remains fail-closed for this owner surface:

- physical high-water: `null`;
- physical critical: `null`;
- physical status: `UNKNOWN_NO_OWNER_FREE_SPACE_ADMISSION_THRESHOLD`;
- deletion authority: `NONE`.

No runtime/store semantics, retention/deletion, runner policy, deployment, release or Live capability is introduced by this refresh.
