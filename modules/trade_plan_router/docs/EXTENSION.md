# Extension guide

## Adding a routing policy

1. Add a typed enum value and configuration validation.
2. Implement the policy behind the existing resolver/evaluator boundary rather
   than adding an order-dependent conditional in the module adapter.
3. Include the policy in canonical configuration hashing.
4. Define deterministic tie-breaking for complete equality.
5. Add unit, golden, property/permutation, and replay tests.
6. Update conflict/configuration documentation and migration notes.

## Adding an upstream schema

Create an explicit versioned normalizer. Do not mutate or guess missing upstream
business decisions. Validate feature schema against payload schema, envelope
timestamps, ownership/linkage, input hash, and expiry. Unknown fields must follow
the configured strict/lenient policy. Add tests against the real local producer
source distribution when available.

## Adding a plan action or intent

Update the typed action/intent model, composer, invariant validator, serializer
exact schema, Module API feature contract, golden registry, executor contract,
and backward-compatibility documentation together. Define whether the action
creates exposure, reduces exposure, needs protection/risk reservation, targets
a position, or needs cancellation/replacement sequencing.

## Determinism requirements

Never use unordered iteration, random UUIDs, system time, implicit local timezone,
filesystem order, network reads, or global mutable state in routing semantics.
New audit-only fields must not change `content_hash` or `plan_id`. New semantic
fields must change them and require a schema/migration decision.

## Release evidence

Every confirmed defect needs a regression test. Re-run branch coverage,
security/type/path/text audits, golden registry, deterministic replay, clean
wheel lifecycle, and clean-source validation before accepting an extension.
