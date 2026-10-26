# Core 0.4.2 recovery checkpoint

This branch resumes the verified Core 0.4.2 hardening program from the last recoverable source checkpoint after round 10.

## Provenance

- stable integration base: `5e03b933207a99940244634f49a756d27888a5c3`;
- stable Core 0.4.1 hardening head: `362e11b5876fdab82bbc1f5183fc9630dcdc6187`;
- recovered source checkpoint: `43b54c004befffdb3ecf0aaa0bf4a3df6ebf3743`;
- recovery branch: `core/hardening-v0.4.2-recovery`;
- package version: `0.4.2`.

The temporary transport payload, materializer workflow, trigger and debug log were removed by a dedicated one-shot VDS workflow. Their source commit remains reachable for provenance, but none of those files belongs in the candidate diff.

## Continuation policy

The original final local Core 0.4.2 tree is not reachable from GitHub. Therefore the recovery proceeds from the last recoverable source checkpoint rather than pretending the unavailable final tree was published.

Work resumes with:

1. clean Python 3.12/3.13 baseline;
2. two additional completeness reviews, bringing the total to five;
3. rounds 11 through 30 and audits after every even round;
4. a new independent final audit;
5. full packaging, security, deterministic replay, recovery, benchmark and stress gates;
6. fast-forward publication to `core/hardening-v0.4.x` only after exact-head evidence is green.

No merge, force push, tag or release is authorized by this checkpoint.
