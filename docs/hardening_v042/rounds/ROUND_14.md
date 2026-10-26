# Round 14 — strict configuration object boundaries

## Baseline

`EngineConfig` accepted arbitrary objects in its nested `fees`, `execution`, `risk` and `data` fields. Policy fields in execution and data configuration also relied on membership comparisons before explicit type validation, producing late or incidental errors for malformed inputs.

## Change

- nested configuration fields require their declared dataclass types;
- execution policy selectors require strings before value validation;
- data policy and timestamp semantics require strings;
- regression tests cover malformed mappings, sequences, booleans and integers.

## Result

Configuration errors now fail at construction with deterministic, field-specific `TypeError` messages rather than reaching the runtime engine.
