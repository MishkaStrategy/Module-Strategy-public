# Core 0.4.2 post-release hardening status

This document is a repository-local status anchor for project chats. The canonical cross-chat activity ledger is GitHub issue #103; the active stacked review is Draft PR #96.

## Source-of-truth rules

- Project chats and project libraries are checked first; GitHub is checked second.
- An artifact absent from both sources is treated as not created.
- The project does not use Work mode.
- No direct writes to `main`, force-push, merge, tag, release, live-trading activation, secrets change or branch-protection change are authorized by this workstream.

## Branch topology

- Integration base: `integration/modular-strategy-v0.5.0` at `5e03b933207a99940244634f49a756d27888a5c3`.
- Stable Core candidate: `core/hardening-v0.4.x` at `f4a7c9eac018df1bac9bd35a032d0c153ad26f1f` (Draft PR #13).
- Active stacked branch: `core/post-release-hardening-v0.4.2` (Draft PR #96).

## Last fully validated checkpoint

Commit `5c0a0c42433012e375c5559f7eff886b4f3ebf0f` passed `core-v042-quality-ci` run `29989107537` on Python 3.12 and 3.13:

- 635 Core/root tests per interpreter;
- 84 Session Range tests per interpreter;
- 80 Market Regime tests per interpreter;
- 70 FVG tests per interpreter;
- 869 passed per interpreter, 1,738 across the matrix;
- failures/errors/skips: 0/0/0;
- 86% branch coverage (`4,477` statements, `479` missed, `1,632` branches, `330` partial);
- compile, Ruff, strict MyPy, build, Twine, wheel smoke and normalized-sdist smoke passed;
- wheel checksum `5a7b50f155fd9b44d9d21c481857aa24fb7f6910e45bd1630b37c2625cd7441b`;
- normalized-sdist checksum `b8b97d9b1b0c97356f4adb26682c9f63d4b0199b1905bcd1168a8d1dfbc193fe`;
- distribution checksums matched across Python versions.

Validated contracts include feed/replay input hardening, pre-storage error redaction, serialization security boundaries, strict Metrics aggregate validation and fail-closed ExchangeRulesAdapter public boundaries.

## Frozen integration-validation candidate

Source-and-test head before this status anchor: `845764f81ab0a2991dec09ca15dd58468f4fa395`.

The candidate contains the following unpromoted packages and must pass one complete Python 3.12/3.13 pull-request matrix before any of them are treated as validated.

### MarketView accessor boundaries

- exact integer `history()` length and boolean `include_current` validation;
- canonical nonblank timeframe validation for `history()` and `current_for()`;
- preserved empty-view, unknown-timeframe and nonpositive-length behavior;
- 20 focused boundary cases.

### Checkpoint construction, persistence and decoding

- static arguments validated before any module snapshot side effect;
- UTC-normalized deterministic checkpoint identity includes run, kernel, bar, data, configuration, module, schema and captured state;
- save rejects unsupported schemas and mismatched integrity before repository mutation;
- load reports missing required root/module-state fields explicitly;
- integrity type validation and verified round-trip coverage.

### State repository boundaries

- disk state uses strict JSON decoding and rejects duplicate keys and NaN/Infinity;
- in-memory load validates keys consistently with save/delete;
- disk and memory repositories return detached immutable evidence.

### Durable event-log recovery

- event JSONL uses strict JSON decoding;
- duplicate keys and nonfinite constants fail closed;
- crash-tail truncation is restricted to syntactically incomplete final records;
- semantically corrupt or ambiguous final records are preserved and surfaced, not silently deleted.

### RunManifest evidence invariants

- direct DTO construction validates identity, timeframe and positive bar count;
- manifests and parameters are typed and aligned;
- nested module parameters are detached and immutable;
- `run_id` must agree with kernel/data/config/module hashes;
- bar collections reject non-Candle elements and mixed symbol/timeframe evidence;
- focused regression coverage in `tests/test_post_release_run_manifest_boundaries.py`.

A dedicated Draft integration-validation PR is used only to obtain observable pull-request CI. It does not authorize merge or release.

## Known synchronization correction

The historical local checkpoint `d97c51a59750bf4f768d1a3944fb2dabca41a1cf` is not present in GitHub, and no matching source archive or Git bundle was found in the project library. Its old 2,210-test statement is historical local evidence only, not reproducible evidence for the current branch.
