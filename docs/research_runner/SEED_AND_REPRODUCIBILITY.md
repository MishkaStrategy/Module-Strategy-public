# Seed and Reproducibility Contract

The root seed is never incremented by task order. Child seeds are derived from a
canonical SHA-256 payload containing the root seed and semantic labels.

Recommended hierarchy:

```text
root seed
  -> experiment label
  -> stage label
  -> run/configuration label
  -> scenario label
  -> simulation index
```

A worker receives an already-derived run seed. The number of workers, assignment
order, retry order and resume order are deliberately absent from seed derivation.

Random search first generates the valid finite population, then selects unique
indices with an explicit seed. The sampled configurations are immutable and can be
persisted before backtests start.

Deterministic identity includes:

- research specification hash;
- Backtest Engine version;
- metric schema version;
- Module API version;
- configuration hash;
- split ID;
- scenario ID;
- seed;
- repetition index.

Dirty-tree release experiments remain forbidden. A future exploratory dirty-tree
mode must retain a patch artifact and its SHA-256 before it can claim limited
reproducibility.
