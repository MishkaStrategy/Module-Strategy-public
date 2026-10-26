# Breakout–Return final hardening checkpoint

Date: 2026-07-21
Module: Breakout–Return
Base branch: `integration/modular-strategy-v0.5.0`
Base commit: `5e03b933207a99940244634f49a756d27888a5c3`
Working branch: `modules/breakout-return-v1.1.0-hardening`
Draft PR: #18

## Final identities

- semantic head: `0d81bd6367181d323b79874badb4cb91d2a5b5c3`;
- exact tested evidence head: `88ab6a3a76987c54cdb34bc0094e5cd49a4149d6`;
- final documentation-only tip: `e249d7bc9e59302e34f475d004ae3679485e5778`;
- exact self-hosted evidence run: `29845670271`.

The branch tip differs from the exact tested evidence head only in this checkpoint Markdown file. No executable source, tests, workflow or packaging configuration changed after the exact run.

## Exact evidence

Python 3.12:

- 131 Breakout–Return tests passed;
- failures/errors/skips: 0;
- statement coverage 90.856031% (91% displayed), gate 90%;
- Ruff, strict mypy, compileall and public import passed;
- wheel, sdist, clean install, `pip check`, entry point, `py.typed` and checkpoint smoke passed.

Python 3.13:

- 131 Breakout–Return tests passed;
- full repository suite: 312 passed;
- failures/errors/skips: 0;
- statement coverage 90.856031% (91% displayed), gate 90%;
- Ruff, strict mypy, compileall and public import passed;
- wheel, sdist, clean install, `pip check`, entry point, `py.typed` and checkpoint smoke passed;
- Bandit passed;
- detect-secrets: zero findings;
- pip-audit: 70 dependencies, zero known vulnerabilities;
- CycloneDX SBOM: 74 components;
- benchmark and evidence artifacts published.

Artifact digests:

- Python 3.12: `sha256:8d7cefc9140b0a5cf5bbeb35b1d18de9cbc3f2ea9c3a62241c7465bab9d48238`;
- Python 3.13: `sha256:9508756b344b1632c8f8e5f26e60e0b75c34937c7990e5637bb580ce7516cfdd`.

## Closed defects

1. High: legacy active payload-v1 migration survives payload-v2 second restart.
2. Medium: invalid public datetime/reference inputs fail closed.
3. Medium: non-mapping restore input raises typed `BreakoutReturnStateError`.
4. Medium: optional `reference_feature_source_module` prevents foreign producer replacement.
5. Medium lifecycle: producer disappearance invalidates under `invalidate`; `keep_snapshot` retains the intended frozen setup and ignores foreign replacement.
6. CI: self-hosted macOS ARM64 execution with isolated Python 3.12/3.13.
7. CI: full repository checks install actual external module entry points.

## Scope and safety

- no Core source changes;
- no other module source changes;
- no direct integration commit;
- no force push;
- no merge;
- no stable tag or release;
- PR remains Draft.

## Verdict

**READY FOR INTEGRATION REVIEW.**

No critical, high or medium Breakout–Return defect remains open. Exact run `29845670271` covers all executable and test content at the final branch; the branch tip adds only this evidence Markdown synchronization. Merge requires explicit user authorization.
