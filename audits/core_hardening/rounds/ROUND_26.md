# Round 26 — self-verifying evidence bundle

`BacktestResult.export_directory` now emits:
- deterministic replay verification;
- environment/dependency fingerprint;
- SHA-256 checksums for every evidence artifact.

The run identity remains based on kernel/data/config/modules, not host environment, so research identity stays portable while environment evidence is retained separately.
