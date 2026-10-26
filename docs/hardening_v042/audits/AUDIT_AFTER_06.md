# Intermediate audit after rounds 05–06

## Decision

**Continue.** Event and checkpoint input hardening is fail-closed, backward-compatible for valid persisted data, and clean under full regression/static gates.

## Changes

- Event sequence is an exact positive integer; optional identities are nonblank; payload is a mapping.
- Checkpoint index is an exact integer; module state payload is a mapping.
- Checkpoint schema declarations are canonical strings and persisted schema types are not coerced.

## Contracts and compatibility

Public field order, defaults, imports, Module API and schema value `1` remain unchanged. Existing valid event logs/checkpoints load identically. Malformed persisted data fails before module mutation.

## Evidence

- Full repository suite: **341 passed**.
- Coverage: `TOTAL                                3258    382   1054    246    85%`.
- Component Ruff: pass.
- strict mypy: pass across 25 core files.
- Test peak RSS: **73,416 KiB**.
- Event-loop median: **1.2318 s**; deterministic=True.
- Multi-timeframe median: **1.5497 s**; peak RSS **78,960 KiB**; checkpoint **1173 bytes**.

## Determinism / no-look-ahead / recovery

No bar visibility or event timing changed. Canonical IDs and schema types reduce replay ambiguity. Checkpoint corruption, atomic rollback and replay tests pass.

## GitHub workflow status

Published v0.4.1 workflows are green. Candidate v0.4.2 remains local pending a controlled push and self-hosted runner verification.

## Residual risks

Checkpoint nested row/container shape validation will receive additional adversarial coverage in checkpoint rounds 13–14.
