# New-root workflow boundary

No executable workflow is enabled in this local snapshot. The 101 original
definitions are preserved byte-for-byte in `../legacy-workflows/` for review.
Their source references, old commit guards and acceptance records belong to the
private project history; they are not CI evidence for this new root.

Do not move them back or connect shared/home runners as part of publication.
The selected Option 3 boundary needs an external protected controller, disposable
isolated executors, a separate publisher, authenticated execution receipts, and
separate Linux deployment / Darwin acceptance / backtester roles. Actual owners,
configuration and trust sources are still missing. CI enablement requires review
of that concrete boundary and separate authorization.

Existing workflow-specific tests and audit scripts still refer to the old paths.
They remain migration work, not passing gates. Source-level software checks that
do not discover workflows can run locally. See `../../PUBLICATION_STATUS.md`.
