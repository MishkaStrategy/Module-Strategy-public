# Historical Dataset Lineage

Status: **ACTIVE — EXACT-HEAD PATH HARDENING ACCEPTANCE**

This phase imports no raw market data into GitHub. The external Parquet corpus
remains outside Git. GitHub contains only code, strict source specifications
without credentials, hashes and sanitized validation or registration evidence.

## Baseline corpus

- exchange: Binance;
- market: USDT-M perpetual futures;
- data type: traded-price klines;
- symbols: BTCUSDT, ETHUSDT, BNBUSDT, XRPUSDT, SOLUSDT, DOGEUSDT,
  ADAUSDT, TRXUSDT, LINKUSDT and AVAXUSDT;
- timeframes: 5m, 10m, 15m, 1h and 4h;
- expected files: 50;
- authoritative summary SHA-256:
  `a5f763fed5912cf0a988f50ac163d1b47f2506d46412aec120cd56a4eaf43893`.

`XAUUSDT` is an independent extension and must not be mixed into the baseline
manifest or version sequence.

## Verification contract

`market-data verify-external-corpus` accepts a strict JSON spec containing local
file paths, persistence-safe source URIs, summary row counts and UTC bounds. It
fails closed on:

- missing or extra symbol/timeframe matrix entries;
- unsafe paths or source URIs;
- file-size or expected SHA-256 mismatch;
- Parquet schema drift;
- row-count or inclusive-range arithmetic mismatch;
- non-UTC timestamps or timeframe misalignment;
- invalid close-time semantics;
- non-finite or invalid OHLCV values;
- negative volume/trade values;
- gaps, duplicates, reordered timestamps or changed bounds.

Path validation is performed on the requested path before canonicalization.
Final-component symlinks are rejected for corpus Parquet files, external corpus
specs and Drive indexes. Manifest, inventory and registration-evidence writers
also reject symlink destinations before atomic replacement and do not touch the
symlink target. Regression tests cover every protected input and output boundary.

The verifier reads Parquet in bounded batches and emits a content-addressed
corpus manifest containing source URI, SHA-256, byte size, rows, UTC bounds,
continuity counts and schema fingerprints for every file. `verified_at` is audit
metadata and is excluded from `corpus_hash`.

Exact-head run `30526439374` passed the complete 50-file audit for commit
`1c1a355be8eeb5c39c0bb13f0c0c10398f354714`. Its sanitized acceptance evidence
records:

- status: `PASS`;
- corpus hash:
  `6d32c075f5adf3623679edd49c20ee8ee8758d3ea0e75b7f06613593e065c44e`;
- 50 files, 10 symbols and 5 timeframes;
- 615,395,637 source bytes;
- 12,968,018 rows;
- zero gap events and zero duplicate events;
- no raw market data uploaded to GitHub.

## Registration boundary

External registration is a normalization and publication operation, not a
metadata-only declaration. The source files use the verified external
12-column Binance Kline schema, while registered partitions use the canonical
normalized `KlineRecord` schema. The registration audit therefore:

1. repeats exact-byte verification immediately before import;
2. stores each original source Parquet as immutable local raw lineage through
   `LocalStorage.write_raw`;
3. normalizes records in bounded batches and publishes monthly immutable
   Parquet partitions through `LocalStorage.write_kline_partition`;
4. creates one `DatasetManifest v2` and `DatasetDescriptor` per symbol;
5. registers each descriptor through `SQLiteDatasetRegistry`;
6. performs deep registered-dataset integrity verification before acceptance;
7. emits only sanitized dataset IDs, hashes, versions, coverage and counts.

The registration CI uses temporary runner storage and deletes raw, normalized,
manifest and SQLite bytes after validation. It proves the canonical registration
path without establishing a persistent production registry or uploading market
bytes to GitHub. Persistent registration remains a separate controlled action
after the Draft PR is accepted.

No parallel registry, hidden copy-on-write state, live exchange access, private
credentials or order path is allowed.

## Verified implementation checkpoint

Exact self-removing transaction run `30522537020` validated the first verifier
slice on Python 3.12 before publication:

- exact branch and last-commit fence;
- shared runner preflight and isolated environment;
- canonical Ruff lint and formatting;
- strict MyPy for the verifier and CLI integration;
- focused PyArrow contract tests and CLI smoke;
- actionlint for the permanent Python 3.12/3.13 workflow.

The self-cleaned product commit is
`5c0b73f4eeedcc9ca4e7b4de77f792c1020f6166`. The temporary transaction workflow
and helper are absent from that tree. The candidate remains Draft and unmerged
while the registration implementation and its exact-head acceptance workflow
are validated.
