# Checkpoint, resume and cancel

Each run has two distinct immutable/recoverable artifacts:

1. a resume specification containing the canonical request, execution identity, adapter
   version, storage settings, quality policy, concurrency, memory bound and optional
   parent dataset;
2. a checkpoint containing state plus completed and pending chunk IDs.

Recovery rules:

1. a completed request returns its registered descriptor without downloading;
2. an incomplete request validates the resume specification and checkpoint;
3. completed chunks are resolved through raw descriptors and SHA-256;
4. the adapter reconstructs typed records from immutable raw bytes;
5. only missing chunks call the source;
6. a retryable source failure moves the run to `PAUSED` and persists progress;
7. a cancellation marker is checked during execution;
8. `market-data resume` clears a prior cancellation marker and continues the exact
   execution envelope;
9. `market-data cancel` rejects terminal runs and records `CANCELLED` for paused runs.

Fail-closed conditions include:

- corrupt JSON or unsupported resume/checkpoint version;
- request/execution hash mismatch;
- adapter-version mismatch;
- missing or changed raw metadata;
- raw checksum mismatch;
- same request ID with different canonical content;
- unsafe checkpoint, raw or spool path.

Concurrent downloads preserve deterministic publication order. Successful chunks from a
partially failed batch are checkpointed and reused by resume.
