# Completeness round 3 — operational and recovery paths

## Scope

Durable event logs, state repositories, corrupted/oversized state, path safety and checkpoint integrity.

## Confirmed defects and fixes

- Absolute state keys could escape the configured state directory: fixed with resolved-path containment checks.
- Nested in-memory state remained mutable through caller-owned objects: deep-freeze added.
- Durable state and event logs had no configurable size ceiling: conservative defaults added.
- JSONL append validated by mutating memory before persistence: preflight validation now occurs before disk write.
- Existing oversized or corrupted JSONL rows now fail with line-specific errors.
- Checkpoint payloads had no persisted integrity check: optional SHA-256 integrity field added.
- Legacy checkpoint payloads without a checksum remain readable.

## Compatibility

Repository and checkpoint public protocols are unchanged. Existing state is readable; new state receives additional integrity evidence.

## Verdict

Operational fail-closed behavior is materially stronger. Full exchange reconciliation remains a release blocker and is not claimed by this round.
