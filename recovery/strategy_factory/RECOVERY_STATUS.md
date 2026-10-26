# Strategy Factory deterministic recovery checkpoint

Status: **source recovery complete; release gate still fail-closed**.

## Recovery result

The originally referenced archive `strategy_factory_transport_c371da91.tar.gz` was not found. Recovery nevertheless reached an independently verified exact source checkpoint from the retained 59-part Base64/XZ transport:

1. remove the extra `U` at the end of transport `part-05`;
2. replace Base64 character `t` with `T` at normalized offset `88148`;
3. verify the recorded normalized-Base64 and XZ SHA-256 identities;
4. safely extract exactly 80 regular files, rejecting links and unsafe paths;
5. apply only complete, independently parseable post-archive delta blocks;
6. replace provisional reconstructions with the recovered bytes;
7. verify final source hashes, compile, tests and integration gates before materialization.

The full source, tests, Risk Request Composer boundary and compatibility regressions are now materialized in branch `research/automatic-strategy-builder`. Temporary transport fragments, recovery workflows and diagnostic logs have been removed.

## Verified recovered surface

- Strategy Factory: 589 tests pass on Python 3.12 and 3.13;
- Risk Request Composer: 6 tests pass on Python 3.12 and 3.13;
- acceptance scenarios: 25/25 pass;
- branch-aware coverage: 91%;
- Ruff, strict MyPy, Bandit, tracked-source safety, wheel builds and clean-wheel smoke pass;
- fault injection, bounded performance/memory and 400-check extended restart soak pass;
- the publication path reaches `REGISTERED`; identical replay reaches `DUPLICATE`.

## Remaining safety boundary

Recovery completion does not make the component release-ready. Draft PR #218 remains paper-only and blocked by issue #250, paper-runtime admission with the real independent auditor, intended-topology staging soak and final independent release audit.

No merge, release, deployment, live trading, tag, force-push, rebase or auto-merge is authorized.
