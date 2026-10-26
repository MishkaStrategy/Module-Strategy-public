# Recovery status

Current phase: five completeness reviews and rounds 11-13 complete; exact-head temporal verification.

Stable Core hardening branch remains at Core 0.4.1. The recovery branch contains the last recoverable Core 0.4.2 source checkpoint with transport-only files removed and a policy-compliant VDS baseline workflow.

Completeness review 4 is implemented and regression-protected:

- execution and reconciliation identity collections are copied into immutable tuples;
- caller-owned list mutation cannot alter stored DTO state;
- non-string identities fail closed;
- checkpoint integrity uses one canonical lowercase SHA-256 representation;
- pinned Ruff formatting, strict MyPy and targeted recovery tests passed before publication.

Completeness review 5 is implemented and regression-protected:

- state size limits require real positive integers or `None`;
- disk state reads use `lstat`, regular-file validation and opened-file inode comparison;
- symlinks, path swaps and non-regular files fail closed;
- reads are capped after open at `max_state_bytes + 1`;
- post-`fstat` growth is rejected;
- the dev toolchain explicitly includes `build` and `twine`;
- wheel, sdist, Twine metadata validation and clean-wheel smoke passed before publication.

Completed hardening continuation:

- round 11: strict timeframe parameter types, duplicate/out-of-order rejection, stream identity isolation and correct reset semantics;
- round 12: actual Core-generated HTF close timestamps, 23/25-hour DST semantics, overlap rejection and close-aware run hashing;
- intermediate audit 6: temporal, look-ahead and compatibility scope complete;
- round 13: all timeframe aggregators preflight an event before any buffer, cursor or history mutation.

The semantic commits were published through normal fast-forward Git refs. This user-authored checkpoint triggers executable exact-head PR workflows because GitHub does not create workflow runs from connector-authored Git-object ref updates.

The branch is not ready for promotion until rounds 14-30, the remaining intermediate audits, exact-head CI, heavy evidence and the independent final audit are complete.
