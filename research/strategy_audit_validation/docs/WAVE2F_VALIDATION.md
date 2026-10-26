# Wave 2F validation record

## Original governance validation

The original Wave 2F checkpoint validated:

- compileall;
- 70 governance/composition/benchmark tests;
- 96% focused branch coverage for the seven governance modules;
- deterministic identity and replay;
- restart/recovery;
- corruption detection;
- detached approval and actor separation;
- immutable evidence checksum verification.

The original governance SLO baseline and workload remain unchanged.

## Variant 2 canonical validation

Merged strategy-contract PR #316 supplied:

- `fvg_rt_final_15m@2.0.0`;
- `frozen_1h_short@1.0.0`;
- `frozen_4h_short@1.0.0`;
- the v2 contract registry;
- the canonical composition;
- the contract-bound runtime adapter;
- exact candidate and composition identity;
- completed-candle, UTC and no-look-ahead enforcement;
- checkpoint/restart identity fencing.

The accepted strategy-contract head
`5fc3a8739b3d7d6dca99fbe45703a2201797bc44` passed the substantive Strategy Audit,
Strategy Factory, Core, fast-check, runner-policy, soak and full-corpus backtest
workflows recorded in PR #316.

Focused contract tests prove:

- `load_composition(..., require_canonical=True)` succeeds;
- the composition hash is
  `51659c6ed582f2843357bd33fbff35059f9a535dd27f05a585a9e7dfa78365c9`;
- the candidate ID is
  `strategy-candidate-51659c6ed582f2843357bd33`;
- repeated loads are deterministic;
- version, contract completeness and execution-order drift are rejected or produce a
  distinct identity;
- the historical `INCOMPLETE` candidate remains retained only as fail-closed evidence.

## Semantic digest correction

Independent review of exact-head run `30813361192` proved that all ledger bytes,
trade counts and metrics were unchanged, but the previous digest changed solely when
`generated_from_commit` changed. That run is not accepted as final semantic evidence.

The benchmark now declares the versioned contract
`canonical-strategy-v2-result-v1`. Commit SHA remains in `summary.json` as provenance,
but is excluded from the semantic payload together with the digest field itself.
Regression tests prove that:

- changing only `generated_from_commit` leaves the digest unchanged;
- changing result metrics changes the digest;
- adding the digest to the summary does not make it self-referential.

The stable expected digest for the accepted ledger/result bytes is
`63a2c102601cec5a613ff3749754d31955d6513c654c73cd3dd7e09bbc5cefdb`.

## Downstream handoff validation

`test_wave2f_variant2_handoff.py` binds the machine-readable Wave 2F resolution and
Stream A handoff to the exact canonical composition, contract registry, ledger hashes
and versioned semantic digest. It also recomputes every SHA-256 entry in
`reports/strategy-governance-wave2f/evidence-checksums.json`.

The exact-head GitHub Actions result for the final governance handoff commit must be
treated as authoritative. Intermediate metadata commits marked `[skip ci]` are not
independent validation checkpoints.

## Semantic boundaries

The governance handoff does not change trading rules or economic results. It promotes
the already owner-authorized Variant 2 identity from a dedicated strategy-contract
workstream into the Wave 2F consumption boundary. Detached production approval, live
trading, private credentials, release and deployment remain out of scope.
