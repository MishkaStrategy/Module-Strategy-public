# Round 15 — strict centralized validation

## Baseline

The shared numeric validator converted arbitrary values with `float(value)`, so numeric strings could cross financial boundaries. The nonblank validator reported non-string identifiers as blank-value errors instead of deterministic type violations.

## Change

- accepted numeric runtime types are explicitly `int`, `float` and `Decimal`;
- booleans, strings, bytes, complex values and arbitrary coercible objects fail closed;
- finite `Decimal` values remain supported for financial callers;
- non-finite decimal and floating-point values are rejected;
- nonblank identifiers distinguish type errors from blank-string errors;
- UTC datetime validation remains explicit and regression-protected.

## Result

Financial and identity validation no longer depends on implicit Python coercion. Invalid inputs fail at the common boundary with stable exception classes and messages.
