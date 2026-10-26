# v0.6 staging merge resolution

- full RC first parent: `70aa998554ea9858a67493f71246148b91532129`
- v0.6 staging second parent: `9b7cc5cac837f8d66c691117143e0dcb9cf2d8f8`
- merge base: `d6b610e30557ae48ed606d3259d99a5adaae0683`
- diagnostic run: `30466248836`
- diagnostic artifact SHA-256: `0d984bc8da249e1cb34e339af3aed4b567e923300c23a50b06cfde01bf9f1cf2`
- conflict count: 28

Governance conflicts used the full RC side: actionlint labels, shared Core CI and Signal Bot runner governance.

Product conflicts used the staging side: project README, package metadata, Core 0.4.2 contracts and timeframe tests.

The merge is strictly paper-only. No main/frozen-Core mutation, rebase, squash, force-push, release, deployment or live execution was performed.
