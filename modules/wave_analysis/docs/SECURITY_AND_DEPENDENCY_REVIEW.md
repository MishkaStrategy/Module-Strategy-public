# Security and Dependency Review

- No secrets, tokens, credentials, network endpoints, subprocess execution, dynamic code evaluation, or writable external paths are used by the module.
- Runtime dependency is limited to `modular-strategy-core>=0.4,<0.5`.
- Security audit input contains only `PyYAML>=6.0`, inherited from Core configuration loading.
- Configuration and checkpoint inputs are validated fail-closed with bounded collections.
- Decimal values must be finite; timestamps must be UTC-aware; bool values are rejected where exact integers are required.
- Unknown checkpoint fields and inconsistent identities are rejected.
- Persistent Feature IDs are exact and bounded; gap invalidation removes stale facts.
- Same-instance runtime access is serialized to avoid checkpoint/read races.

CI runs `pip check`, `pip-audit`, and a conservative text secret-marker scan. No security guarantee replaces repository secret-scanning and dependency policy.
