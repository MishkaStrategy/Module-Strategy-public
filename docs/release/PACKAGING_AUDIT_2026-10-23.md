# Local Python packaging preflight — 2026-10-23

**Scope:** candidate-local, offline diagnostic for the existing root, Market Data Ingestion, and Control Plane package builds. This record is not a release acceptance, trusted execution receipt, selected support profile, or authority to install/deploy the full product.

The source worktree was dirty at `127c483659fbb3acdfc90ba39ecb3af2b4c69616`; each build read a temporary copy of `pyproject.toml`, optional packaging metadata files, and `src/`. The build interpreter was CPython 3.12.13, with setuptools 84.0.0 and uv 0.11.32. Two independent temporary copies per package were built with `uv build --no-build-isolation --offline --wheel --sdist`, `SOURCE_DATE_EPOCH=1792713600`, and `PYTHONHASHSEED=0`. No network was used, and build outputs were temporary.

| Package | Source-input tree SHA-256 | Wheel SHA-256 (both runs) | Normalized sdist SHA-256 (both runs) |
| --- | --- | --- | --- |
| `modular-strategy-core` | `236aa7e9b3166c33317af0b52d2d1c5f051c3b5975f2ab04b8139cacb7632c02` | `db14bbbc8811309c3189e647b041b4c6fe9ef1fe04b8eeb2f312b8088338f50b` | `a959e269bd190e67b954868fe40aef3e689926d58b30c9278723f1a9dd5e7c7f` |
| `modular-market-data-ingestion` | `4bb9e28916569b5b48d5a534cfe322e28b21402fc6f3e24f9a93a52c35cec166` | `cdd9d62960bc9c44e38122f428119bee9ca7696a56474b2886cb820d09194d3b` | `fb4c85e2ae27a6906277e9569719c2933b83efd646787995d75d9e5390210e8d` |
| `modular-control-plane` | `1a2c1f2a637566cbaf4785be8475e301b93e675b4f540996909991af07ac232a` | `089c73132c812637580c45baa5692e80f0f11804be8fa2b264f6a104f8a719c6` | `c5f931d4c903ccde3d1331287854a700f16229853f67a636ca05e0a92bd44513` |

The direct setuptools sdist outputs were not byte-identical. For the three packages, their tar member names and file contents matched across runs, while only tar metadata timestamps differed (20, 14, and 13 members respectively). Running the existing [`scripts/normalize_sdist.py`](../../scripts/normalize_sdist.py) on each temporary sdist made the two final sdist hashes identical. The wheel hashes matched without normalization. The normalizer's focused test suite passed: 7 tests; Ruff lint and formatting checks passed.

The current worktree contains exactly three `uv.lock` files: the root, Market Data Ingestion, and Control Plane projects. `uv lock --check --offline` passed for all three (96, 65, and 34 resolved packages). Earlier temporary clean-environment checks installed `modular-strategy-core[backtest]` and `modular-control-plane[interface]` under Python 3.12 and 3.13, then passed `pip check`, import smoke, and CLI `--help`. Market Data Ingestion's lock resolution passed, but both offline interface/parquet clean installs stopped because `fastapi==0.161.0` was not present in the local wheel cache; no network fallback was attempted. This cache miss is not evidence that an online installation or the product itself fails.

These three component checks do not make a 26-distribution release profile. The complete runtime/build lock set, exact supported interpreter patches, dependency-index policy, Signal Bot profile placement, and candidate-bound independent reconstruction receipts remain unresolved. The two successful component installs and normalized artifact checks are local diagnostics only. No CI, owner-data workload, PAPER, soak, deployment, publication, merge, main write, or release action occurred.
