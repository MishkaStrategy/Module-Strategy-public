# Security model

Implemented controls:

- no API key is requested for supported public Binance history;
- exact exchange/market/instrument resolution, with Spot and Futures identities isolated;
- path-segment validation, root containment and symlink rejection;
- atomic temporary writes, directory fsync and restrictive file modes;
- immutable destination conflict detection;
- contextual raw IDs so equal bytes from different streams cannot collide;
- raw payload checksum plus strict companion-metadata verification;
- ZIP traversal, member-count, expanded-size and compression-ratio limits;
- malformed/truncated archive and REST framing rejection;
- bounded response size, retries, timeout, `Retry-After`, token bucket and circuit breaker;
- redacted HTTP query strings and secret-key filtering in structured log context;
- strict canonical JSON; no pickle, dynamic class loading or downloaded-code execution;
- SQLite spool schema/version verification and payload/index identity cross-check;
- active/stale lock ownership checks;
- structured disk-full and permission-denied errors with temporary-file cleanup;
- deep registered-dataset verification before export;
- source metadata re-resolution before registration to detect rule changes mid-run.

Remaining release gates:

- Bandit or equivalent security linter;
- dependency vulnerability audit and SBOM;
- repository secret scanning;
- archive/property fuzzing through Hypothesis;
- real official-network adversarial acceptance;
- PyArrow-specific malformed-file and resource-limit tests.
