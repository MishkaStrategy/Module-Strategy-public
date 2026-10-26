# Local Test Evidence — Position & Exit Management 0.2.1

## Audited code

- Branch: `modules/position-exit-management-hardening`
- Audited code commit: `df58ca52779e0097624ecd9917c7a9d3d803708a`
- Remote integration base: `integration/modular-strategy-v0.5.0`
- Base commit: `5e03b933207a99940244634f49a756d27888a5c3`
- Local Python: `3.13.5`

## Final local gates

| Gate | Result |
|---|---|
| Module pytest | 223 passed |
| Repository pytest | 244 passed |
| Branch-aware module coverage | 92% (`927` statements, `402` branches) |
| Ruff format | 26 files formatted |
| Ruff lint | passed |
| strict mypy | passed, 6 source files |
| compileall | passed |
| Bandit | passed |
| detect-secrets | 0 findings |
| Core 0.4.0 wheel-only matrix | 223 passed, `pip check` clean |
| Core 0.4.1 wheel-only matrix | 223 passed, `pip check` clean |
| Wheel | built and member-verified |
| Source distribution | built, member-verified and rebuilt after unpack |
| Unpacked sdist suite | 222 standalone-applicable tests passed |
| Public import | passed |
| Entry point | passed |
| ModuleRegistry discovery/load | passed |
| Checkpoint/restore smoke | passed |
| Deterministic replay | passed across four hash seeds |

The standalone sdist intentionally excludes `test_workflow_contract.py`, because that test validates the repository-level `.github/workflows` file outside the Python distribution. The test remains mandatory and passed in the full repository checkout.

## Distribution checksums

- Core 0.4.0 wheel: `35c1bb1d2e46d828f14bfa148afde9e2b1622440c4c7d6dfb2243fa9f69f4c57`
- Core 0.4.1 wheel: `d76877d0f353f75505a4e30e5ad3fb2f61e3f6ab1fbec40e8577ab278c9c1cc2`
- Module 0.2.1 wheel: `2a5625a008c873fdbdc3b99d24ed9a74b1fd728681e1f0fe44b2e255b5f423c0`
- Module 0.2.1 sdist: `a2d481b0a17d77fba7be5b538d80838b26142325bd104344428d73b3e5e6ffb2`
- Wheel rebuilt from sdist: `5b238613ca44440c92366d56ef3d6fc28e04babb014f60e5889f1adbf5a60f4b`

## External/network-dependent gates

`pip-audit` was invoked in the clean Core 0.4.1 environment but could not query `pypi.org` because external DNS was unavailable. No vulnerability-free claim is made from that local run. The GitHub workflow retains the networked security gate where service access is available.

The global host `pip check` reports an unrelated pre-existing `moviepy`/`pillow` conflict. Both clean project environments returned `No broken requirements found` and are the authoritative package evidence.
