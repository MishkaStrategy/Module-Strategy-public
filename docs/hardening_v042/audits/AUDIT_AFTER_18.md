# Intermediate audit after round 18

## Scope

Event identity, append ordering, durable write semantics, reopen validation, file-system safety, bounded memory, restart behavior and deterministic cursor reads.

## Findings and resolution

- whole-file event-log loading was unbounded; replaced by bounded streaming;
- operational booleans and coercible sequence strings crossed the boundary; rejected explicitly;
- symlink/path-swap and non-regular log targets were not fully excluded; fail-closed open validation added;
- concurrent in-process append validation and publication could race; serialized by `RLock`;
- total durable log growth was unconstrained; an explicit ceiling now prevents uncontrolled reopen/append growth.

## Result

The event-store persistence boundary is deterministic, bounded and safe for checkpoint/restart evidence. No known critical or substantial issue remains in this audited persistence scope.
