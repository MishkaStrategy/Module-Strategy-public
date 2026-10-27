# Source origin and evidence migration

This local repository starts a new Git history from selected source in the
private Module-Strategy project. The original repository, old history,
conversations, workflow logs and private execution evidence remain separate.
Source selection and file hashes are recorded in a private provenance manifest.
No old Git object database, remote configuration or runner registration is copied.

The local snapshot retains 26 Python distributions, the Web Console and the
three strategy families. It also includes the opt-in effective-configuration
contract, its Core manifest binding and durable-job resume checks, and the
server-side recovery backup-target binding.

Local software verification during preparation covered 91 Recovery API tests,
343 effective-configuration tests and 129 Web Console tests. The Web Console
build passed. All 26 Python distributions were constructed offline. Risk Sizing
owns its runtime files and retains its original Core dependency range; Core
excludes that subtree from its wheel. The source tree uses an internal relative
link to the single canonical implementation, while the risk sdist contains
regular source files and builds independently outside the monorepo.

The selected 26-wheel Python 3.12 profile was installed offline in an isolated
environment. All 20 module entrypoints loaded, dependency checks passed, and
there were no overlapping installed paths or console scripts. Removing and
reinstalling Risk Sizing and reinstalling Core preserved the intended ownership.
These checks do not establish six resolved release locks, Python 3.13
compatibility, Linux/Darwin acceptance or release readiness.

Historical source revisions in registries describe source provenance. They are
not proof that the bytes in this new Git root were executed. Old acceptance
records, data receipts and grants must not be relabelled with this repository's
commit. Future evidence must identify its actual source, inputs, environment
and authority. Missing or historical evidence does not pass a current gate.

See `PUBLICATION_STATUS.md`, `ROADMAP.md` and `RELEASE_CHECKLIST.md`.
