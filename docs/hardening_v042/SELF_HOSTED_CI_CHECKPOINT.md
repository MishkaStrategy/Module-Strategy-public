# Self-hosted CI checkpoint after round 10

A dedicated macOS ARM64 workflow was added without disabling the existing GitHub-hosted Linux compatibility workflows.

Requested runner labels:

`self-hosted`, `macOS`, `ARM64`, `module-strategy`

The current GitHub connector does not expose runner inventory. Label availability will therefore be determined from the first workflow state after push:

- running/completed: labels matched an online runner;
- queued: runner offline or labels do not match; local evidence remains authoritative but is not reported as GitHub CI.

The workflow includes concurrency cancellation, a 120-minute timeout, runner diagnostics, full tests, recovery/replay contracts, wheel/sdist clean install, security/SBOM and deterministic performance evidence. Commands are macOS-compatible and use `shasum -a 256` rather than GNU-only `sha256sum`.
