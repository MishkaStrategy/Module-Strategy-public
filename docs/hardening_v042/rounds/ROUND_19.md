# Round 19 — explicit crash-tail recovery

## Baseline

A process crash can leave the final JSONL event record partially written. The durable event store previously had only two unsafe choices: reject the complete log forever or silently weaken validation. Internal and newline-terminated corruption must never be hidden.

## Change

- default reopen remains fail-closed;
- optional `recover_truncated_tail=True` is an explicit operational policy;
- recovery is allowed only for an invalid final record that does not end with a newline;
- the file is truncated back to the last validated byte boundary;
- optional `fsync` also covers the truncation;
- a `recovered_truncated_tail` evidence flag records that recovery occurred;
- valid unterminated JSON remains valid and is not classified as recovery;
- internal or newline-terminated corruption always fails.

## Result

Crash recovery is narrow, observable and deterministic. It removes only a provably incomplete final append and never repairs or skips committed corruption.
