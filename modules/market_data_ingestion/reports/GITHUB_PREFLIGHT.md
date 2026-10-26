# GitHub preflight — Historical Market Data Ingestion 0.4.0

## Canonical base

- Repository: `mishkacher/Module-Strategy`
- Component branch: `data/historical-market-data-ingestion`
- Exact base SHA: `9b7cc5cac837f8d66c691117143e0dcb9cf2d8f8`
- Frozen Core ancestor: `cc7eb494c27eb0fc48dd16026b704bade9f6de3e`
- Integration method: ordinary history-preserving commits; no force-push or rebase

## Local gate

```text
python -m compileall -q src tests scripts
python scripts/static_audit.py --output reports/static-audit-github-preflight.json
PYTHONWARNINGS='error::ResourceWarning' pytest -q
```

Result:

- 207 tests collected and passed;
- static audit: 0 findings;
- Python: 3.13.5;
- CSV/Fake vertical slice: passed;
- deep manifest/partition/raw integrity: passed;
- streaming, update, backfill, resume, cancel and repair tests: passed.

## Environment-limited gates

The local internal package index returned HTTP 503 while resolving Ruff, MyPy,
Hypothesis, PyArrow, build and Twine. These gates are therefore not claimed locally and
are configured in the component GitHub Actions workflow for Python 3.12 and 3.13.

Real Binance acceptance is not claimed locally because Binance host DNS resolution failed.
A separate manual public-market-data acceptance workflow is included; it requires no API
key and does not enable private or live trading operations.
