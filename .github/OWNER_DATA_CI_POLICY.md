# Owner-data CI execution boundary

This boundary describes only the patched integration revision containing
these workflow definitions. It does not update `main` or other refs that still
carry older workflow definitions, and it makes no global guarantee about
manual dispatch on those refs. No standing authority for owner-data work is
created by this change.

Automatic pull-request and integration-push workflows must remain useful for
synthetic tests and read-only contracts, but they must not access the original
owner data store, call an exchange API, materialize owner data, run a
full-corpus backtest, or publish owner-derived evidence.

The canonical strategy workflow now runs its existing deterministic and
benchmark test surface automatically on pull requests. Its owner-data job is
retained for review behind a failed prerequisite: the manual-only authority
diagnostic exits before checkout or data access, and the native job requires
that job to succeed. Pull-request and push events skip the diagnostic and also
skip the native job; manual dispatch fails the prerequisite, so the native job
is skipped by GitHub's default dependency-success condition. The funding
workflow runs its read-only `--mode check` contract and temporary-fixture test
suite on pull requests and integration pushes; its materialization job has the
same failed prerequisite. The historical registration workflow keeps the
synthetic registration/configuration tests on pull requests and integration
pushes, with its original-corpus registration and backtest behind the same
failed prerequisite. Workflow dispatch runs a short diagnostic that fails
before checkout or data access. A dispatch button, input, branch name, workflow
environment, or caller-provided hash is not authorization.

These guards are temporary fail-closed boundaries, not approval of any owner
operation. They may be replaced only after a separately reviewed,
operation-specific protocol exists for fresh explicit authorization, exact
source and dataset identity, owner-root verification, one-shot replay
prevention, and the approved isolated data route. The existing MS5A search
grant does not authorize canonical backtests, funding materialization, or
original-corpus registration.

Automatic-path policy tests protect these distinctions and use only synthetic
fixtures. Do not connect to the original owner registry or invoke original-root
registration as part of CI policy validation.
