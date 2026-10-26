# BTCUSDT multi-year lifecycle acceptance — package 0.4.0

## Result

The public, unauthenticated Binance USD-M BTCUSDT 5m lifecycle acceptance passed.
It covered immutable creation, append-only update, deliberate partition damage,
quarantine and repair, process interruption, restart/resume from immutable raw evidence,
and deep integrity validation.

No private exchange credentials, signed endpoints, account access or live trading were used.

The machine-readable record is
`reports/lifecycle-acceptance-btcusdt-0-4.json`.

## Immutable v1 and append-only v2

GitHub Actions run `30369425360` executed the update and repair sequence.

The initial immutable dataset contained:

- dataset `binance_usdt-m-perpetual_btcusdt_5m`, version 1;
- range `2019-09-08T17:55:00Z` through `2026-07-19T07:00:00Z` exclusive;
- 721,597 traded 5m klines;
- validation status `VALID`;
- deep integrity `ok`, with 251 partitions, 96 raw artifacts and 1,022,264 rows
  checked across traded and derived streams plus metadata;
- elapsed time 269 seconds.

The update requested only the range after the registered v1 end and published immutable
version 2:

- unchanged dataset identity and start;
- new end `2026-07-27T19:20:00Z` exclusive;
- 724,049 traded 5m klines;
- validation status `VALID`;
- v1 manifest checksum unchanged;
- deep integrity `ok`, with 254 partitions, 101 raw artifacts and 1,025,737 rows checked;
- elapsed time 8 seconds.

This demonstrates append-only acquisition and version publication without mutation of the
registered parent dataset.

## Tamper, quarantine and repair v3

The acceptance deliberately appended invalid bytes to the newly published v2 July 2026
Parquet partition. The targeted partition contained 2,452 klines covering
`2026-07-19T07:00:00Z` through `2026-07-27T19:20:00Z`.

The repair operation:

- detected the checksum mismatch;
- quarantined the damaged partition;
- preserved v1 and v2 manifests byte-for-byte;
- published version 3;
- returned status `REPAIRED` with no unresolved gaps;
- repaired exactly the damaged range;
- passed deep integrity with 254 partitions, 101 raw artifacts and 1,025,737 rows checked;
- completed in 9 seconds.

## Restart and resume from immutable raw

Exact-head GitHub Actions run `30372890842` executed the restart/resume gate at commit
`cb92ab6797e70a498e9cea5dc0e9968fd8622b76`.

The first process was terminated after durable progress. Its checkpoint recorded:

- state `DOWNLOADING`;
- 4 completed chunks;
- 90 pending chunks;
- immutable hashes for the raw payload and metadata files already published;
- process exit code 143 after the controlled termination.

The separate resume command then:

- exited with code 0;
- restored 4 confirmed chunks from immutable raw evidence;
- preserved every pre-interruption raw hash;
- completed all 94 planned chunks with zero pending chunks;
- produced the expected terminal state `COMPLETED_WITH_WARNINGS` and the single warning
  `resuming from checkpoint state DOWNLOADING`;
- registered a `VALID` dataset containing 721,597 traded 5m klines;
- passed deep integrity with 85 partitions, 96 raw artifacts and 721,599 total rows checked,
  including the two metadata records;
- completed the resume stage in 383 seconds.

The terminal checkpoint also recorded `COMPLETED_WITH_WARNINGS`, matching the canonical
state-machine contract when a successful run carries an explicit resume warning.

## Evidence identity

Update/repair artifact:

- run: `30369425360`;
- artifact: `market-data-ingestion-lifecycle-30369425360`;
- digest: `sha256:073bc3f2bccb2120a9b4a4420e5d1d313a2178446af58493f3c2a5d47008edf8`.

Restart/resume artifact:

- run: `30372890842`;
- artifact: `market-data-ingestion-resume-30372890842`;
- digest: `sha256:35cf6f15addd2ad7786f80af6f42c2eb7369739f15b6bbe54ab865afbce79d6a`.

## Acceptance conclusion

The multi-year real update/resume/repair release gate is passed. The remaining gate for
Draft PR #221 is human review and explicitly authorized history-preserving integration into
`integration/modular-strategy-v0.6.0-rc`. This report does not authorize merge, release,
package publication, deployment, private exchange access or live trading.
