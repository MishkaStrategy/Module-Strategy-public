# BTCUSDT registered dataset acceptance

## Verdict

`REGISTRATION IDENTITY AND REGISTERED BACKTEST CONSUMPTION PROVEN — ORIGINATING API ENDPOINT AND STRATEGY CONTROL LEDGERS STILL REQUIRED`

This report extends the BTCUSDT Drive byte/content attestation with accepted repository registration and Backtest consumption evidence. It does not make the strategy composition canonical or executable.

## Parent dataset attestation

- Attestation ID: `btcusdt-drive-history-20260719-v1`
- Canonical SHA-256: `e3cc285ba9fe4e7a50879ae52660f380d7177bb35a20f13c264f993cb6975a91`
- Parent report: `docs/strategy-contracts/BTCUSDT_DRIVE_DATASET_ATTESTATION.md`

## Repository market declaration

Canonical repository index:

- path: `modules/market_data_ingestion/configs/market_data/binance_usdtm_baseline_drive_index.json`
- Git blob OID: `4a79dc860d1a582cfc3a2e54847467b9b02cf327`
- exchange ID: `binance`
- market type: `usdt_m_perpetual`
- Drive folder ID: `1mm_1z1alqfTUF_8stXoaccCQergs6Sk_`
- summary file ID: `1nIsjXMI64hPLVu2SpoLbfE9BaRVU_WBs`
- summary SHA-256: `a5f763fed5912cf0a988f50ac163d1b47f2506d46412aec120cd56a4eaf43893`

This is authoritative repository evidence that the accepted corpus is classified as Binance USDT-M perpetual. The index does not record the originating Binance API endpoint URI. Therefore market classification is proven, while endpoint-level lineage remains open.

## Accepted registration workflow

- Workflow: `historical-data-persistent-registration-smoke-v2`
- Run ID: `30589784503`
- Job ID: `91029327057`
- Exact head: `656c3439edc1e17a6dfeab46cb68246a3e4cb4f8`
- Conclusion: `success`
- Artifact ID: `8777966748`
- Artifact name: `historical-data-persistent-registration-v2-30589784503`
- Artifact ZIP size: `1623` bytes
- Artifact ZIP SHA-256: `e418e858a3a23b25f55b72dafb1d897105529e147511cfb5bfc5738a7229a00a`
- `registration.json` size: `5868` bytes
- `registration.json` SHA-256: `315ee90d4907648e39af50614f1dd3c2c1f41792b8773293264e9fd337f3bbb4`
- `smoke.json` size: `656` bytes
- `smoke.json` SHA-256: `8cc4655a06a0c66f1d243d7ed1fb35dd4ef813e72af31b246f782d3ef3dd3c54`

The workflow fenced both the checked-out commit and the live integration branch head before executing the acceptance surface.

## Corpus registration

- Status: `PASS`
- Corpus hash: `6d32c075f5adf3623679edd49c20ee8ee8758d3ea0e75b7f06613593e065c44e`
- Registration version: `1`
- Registered at: `2026-07-30T14:33:58.385428+00:00`
- Dataset count: `10`
- Total rows: `12,968,018`
- Partition count: `3,865`
- Raw artifact count: `50`
- Reused datasets: `0`
- Raw market data uploaded to Actions artifact: `false`

The workflow used transient local storage and removed it after uploading sanitized evidence. It proves the deterministic registration identity and acceptance behavior, not continued existence of that particular SQLite/storage instance.

## Registered BTCUSDT identity

- Dataset ID: `binance_usdt-m-perpetual_btcusdt_5m`
- Version: `1`
- Dataset hash: `49ee948c7c22dc3c552bf6a96d2fdce9c5bba97662a694ae98ab85fc521c04f5`
- Instrument ID: `binance:usdt_m_perpetual:btcusdt`
- Start: `2019-09-08T17:55:00+00:00`
- End: `2026-07-19T07:00:00+00:00`
- Timeframes: `5m`, `10m`, `15m`, `1h`, `4h`
- Partitions: `415`
- Raw artifacts: `5`
- Validation status: `VALID`
- Reused: `false`

The accepted 10m Drive file was also downloaded in the current recovery workstream:

- Drive file ID: `1I2SCZiSniyZyLSFkU5KW8HCqHwVfjORo`
- Size: `19,862,961` bytes
- Rows from download summary: `360,798`
- SHA-256: `570cb34a910ae36d7cf74d2a68fb93048f2c4f3e86abd262f298e9a8919cdef3`

## Registered Backtest smoke

- Status: `PASS`
- Source URI: `dataset://binance_usdt-m-perpetual_btcusdt_5m/v1`
- Dataset hash: `49ee948c7c22dc3c552bf6a96d2fdce9c5bba97662a694ae98ab85fc521c04f5`
- Rows/bars processed: `576`
- First timestamp: `2024-01-01T00:00:00+00:00`
- Last timestamp: `2024-01-02T23:55:00+00:00`
- Gaps: `0`
- Duplicates: `0`
- Partitions read: `83`
- Run ID: `run-a278b8c34066c7a8`
- Raw market data uploaded: `false`
- Local storage path uploaded: `false`
- Restart verification in this smoke: `false`

This proves exact dataset selection, hash verification, registered streaming, and Backtest consumption. It does not reproduce the named canonical strategy.

## Machine-readable evidence

- Evidence ID: `btcusdt-persistent-registration-acceptance-v1`
- Path: `research/strategy_audit_validation/evidence/btcusdt-persistent-registration-acceptance-v1.json`
- Canonical SHA-256: `016e397c3067777a927cdd012e6fc12bfa1056c81504d4c2653d9a6e58a3c573`

Public API:

- `load_persistent_registration_evidence`
- `verify_registration_repository_sources`

The validator accepts only the immutable canonical evidence payload and verifies the current repository index Git blob, BTC file matrix, market declaration, summary identity, and parent dataset attestation.

## STRATEGY-312-05 progress

Now proven:

1. BTCUSDT dataset bytes and HTF derivation consistency;
2. repository market classification `binance / usdt_m_perpetual`;
3. accepted corpus hash;
4. stable registered dataset ID, version, and dataset hash;
5. exact registered source URI;
6. deep validation status `VALID`;
7. registered Backtest consumption.

Still required:

1. originating API endpoint URI or equivalent immutable endpoint-level source lineage;
2. 283-trade Final 15m ledger and SHA-256;
3. 91-trade Frozen 1H ledger and SHA-256;
4. 21-trade Frozen 4H ledger and SHA-256;
5. combined 395-trade ledger and SHA-256;
6. pre-portfolio signal counts;
7. execution-contract SHA-256;
8. fees, funding, slippage, and final-equity controls;
9. combined semantic digest and exact generating commit.

## Safety boundary

This acceptance does not:

- define the missing Frozen 4H Short rules;
- approve Frozen 1H residual conventions;
- define cross-component arbitration;
- approve runtime strategy adapters;
- prove funding, mark-price, index-price, or liquidation data;
- authorize merge, release, deployment, credentials, production mutation, or live trading.
