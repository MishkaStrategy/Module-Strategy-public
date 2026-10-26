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
build passed. All 26 Python distributions were constructed offline; Risk Sizing
requires the existing staging recipe recorded in the inactive
`.github/legacy-workflows/risk-sizing-ci.yml`. Its direct standalone descriptor
still references monorepo source and is not independently buildable from its
unmodified sdist. These checks do not establish clean installation, Python 3.13
compatibility, six target locks, Linux/Darwin acceptance or release readiness.

Historical source revisions in registries describe source provenance. They are
not proof that the bytes in this new Git root were executed. Old acceptance
records, data receipts and grants must not be relabelled with this repository's
commit. Future evidence must identify its actual source, inputs, environment
and authority. Missing or historical evidence does not pass a current gate.

See `PUBLICATION_STATUS.md`, `ROADMAP.md` and `RELEASE_CHECKLIST.md`.
