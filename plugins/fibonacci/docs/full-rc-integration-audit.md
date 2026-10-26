# Fibonacci full-RC integration audit

Status: candidate validation in progress.

## Exact lineage

- source PR: `#243`;
- source head: `a1eb108a79ce1fe65db7da813c49d986366a4274`;
- isolated candidate branch: `integration/fibonacci-premerge`;
- ordinary source merge: `cdfecc77bf7e04d935a4d21108afcec3744e4441`;
- shared-preflight workflow convergence: `c697c4744e6d0f37cdd3366ddcbecca119925c04`;
- canonical formatting and Core-compatibility transaction: `be92595c68abc633975384b437c722c2ed484dfb`.

The full-RC branch is not moved until the final candidate matrix is green.

## Accepted ownership boundary

The source change is additive and limited to:

- `plugins/fibonacci/**`;
- `.github/workflows/fibonacci-module-ci.yml`.

The plugin is an analytical feature producer. It does not own execution, risk,
orders, fills, positions or portfolio state.

## Semantic audit

The Core adapter:

- rejects future or incomplete higher-timeframe candles;
- processes only candles whose close time is visible at the current completed bar;
- resets active state and publishes feature deletions after a data gap;
- installs a cursor barrier so pre-gap candles cannot be replayed as new evidence;
- rejects regressed timeframe series;
- snapshots and restores engines, feature IDs, completed-wave IDs and gap barriers;
- republishes restored feature state deterministically;
- publishes only `Feature` and `FeatureDelete` values;
- marks active, completed and short-setup features with `execution: none`;
- never produces an `ExecutableTradePlan`, order, trade, position or risk mutation.

The integration compatibility test uses frozen Core `0.4.2` and verifies the
required module API, registry planning, feature publication without trades and
checkpoint state round-trip behavior.

## Replay evidence boundary

The retained regime evidence declares:

- schema `fibonacci-cycle4-regime-evidence/v2`;
- package version `0.4.0`;
- exact BTCUSDT, ETHUSDT and SOLUSDT row counts and SHA-256 values;
- zero null timestamps and duplicate open times;
- strictly increasing timestamps and valid OHLCV contracts;
- zero orders and zero trades;
- no active setup at the retained final cutoff;
- no PnL or profitability result;
- no exact retained standalone walk-forward cutoff manifest.

The raw datasets, caches and built wheel are not committed to GitHub. The report
must not be interpreted as independently reproduced profitability evidence.

## Candidate quality contract

Permanent CI must pass on Python 3.12 and 3.13 using the shared runner preflight:

- compileall;
- Ruff lint and format check;
- strict MyPy;
- branch-aware coverage of at least 90%;
- immutable replay assertions;
- root and plugin wheel build plus Twine verification;
- clean-wheel dependency and entry-point smoke;
- repository Fast, Core, actionlint, runner-routing and environment policies.

No merge to `main`, release, package publication, deployment or live execution is
authorized by this audit.
