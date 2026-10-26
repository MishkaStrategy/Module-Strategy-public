# Core Change Request: version-aware module-state migration hook

## Status

Proposed for a future core-hardening branch. **No core change is included in this module PR.**

## Current contract

`restore_module_states()` in Modular Strategy Core validates that both:

- `ModuleState.module_version == ModuleManifest.version`;
- `ModuleState.schema_version == ModuleManifest.state_schema_version`.

Only after both exact comparisons succeed does the core call `module.restore_state(payload)`.

This is a safe default because an unknown implementation never receives incompatible state. It also means that a module cannot advertise a new semantic version or manifest state-schema version while migrating an older checkpoint inside its own versioned `restore_state()` implementation.

## Reproduced limitation

Breakout–Return now has an internal payload-v2 format with:

- configuration fingerprint;
- bounded diagnostic counters;
- canonical numeric duplicate signatures;
- stricter state invariants.

The module can safely parse and migrate its legacy payload-v1 state. However, changing `ModuleManifest.version` from `1.0.0` or `state_schema_version` from `1` would cause the core to reject the checkpoint before the migration code runs.

For this PR, the module keeps its existing public manifest version/schema and versions the payload internally. This preserves all existing core checkpoint restores and avoids an unapproved core change, but it prevents an honest manifest version bump for the hardening revision.

## Requested future capability

Add an optional, explicit module-owned compatibility decision before payload restore. One possible contract:

```python
class StrategyModule:
    def accepts_state_version(
        self,
        *,
        module_version: str,
        schema_version: str,
    ) -> bool: ...
```

The default implementation must retain current exact-match behavior. A module that opts in would receive the payload only after declaring the specific stored version/schema compatible.

An alternative is a separate `migrate_state(ModuleState) -> Mapping[str, object]` hook called only for explicitly declared source versions.

## Safety requirements

- exact matching remains the default;
- no wildcard acceptance;
- accepted source versions are deterministic and declared by the module;
- migration runs before any live state mutation;
- migration failure is atomic;
- checkpoint metadata remains immutable and auditable;
- the migrated payload is validated by the destination module schema;
- migration is covered by cross-version contract tests.

## Impact

Affected areas:

- `StrategyModule` optional API;
- `restore_module_states()`;
- checkpoint compatibility tests;
- Module API documentation.

Unaffected areas:

- event ordering;
- Feature Store;
- dependency planner;
- order/execution DTOs;
- current modules that do not opt in.

## Backward compatibility

The default path would remain byte-for-byte equivalent to the current exact version/schema comparison. Existing modules require no changes.

## Migration plan

1. Add the optional hook with exact-match default behavior.
2. Add contract tests for exact match, accepted migration, rejected migration, mutation atomicity and undeclared versions.
3. Let one audited external module exercise the hook.
4. Freeze the behavior as part of the eventual Module API stable release.

## Current module decision

Breakout–Return does not modify the core and does not depend on this request. It retains manifest `1.0.0` / state schema `1` and uses an internally versioned payload that accepts legacy payload `1` and emits payload `2`.
