# Round 29 — downstream executor contract harness

## Goal
Prove that the existing public kernel contracts can carry the planned chain:

`Confirmed Setup → Entry Plan → Protection/Target → Risk Decision → Order → Fill → Position → Exit`

without adding production stubs or strategy-specific kernel branches.

## Finding
The first harness exposed a high-severity integration defect: automatic setup lifecycle transitions used the order-producing module as the setup owner. A downstream Risk module therefore could not submit an order for a setup created by an upstream analytical module under strict transition validation.

## Minimal fix
Automatic order/fill setup transitions now retain the original `SetupRecord.module` owner. The actual order-producing module is preserved in transition metadata as `order_source_module`.

No public contract changed.

## Evidence
- Scrambled probe modules are deterministically dependency-sorted.
- Same-bar immutable feature propagation completes the plan chain.
- Market order is not filled on the setup-confirmation bar.
- Setup, order, fill, position and close retain one correlation chain.
- Exit is scheduled through `ClosePositionRequest`, not by direct portfolio mutation.
