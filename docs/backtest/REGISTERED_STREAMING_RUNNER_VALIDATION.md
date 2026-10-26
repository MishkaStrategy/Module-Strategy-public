# Registered Streaming Runner Validation

Source commit `3cbe71c0fde5a0596d84c51b141f84991540db19` passed the final disposable integration
certification on GitHub-hosted Ubuntu in workflow run `30695496418`.

The successful `certify-and-clean` job completed every fail-closed step before publishing
cleanup commit `dd42fd8e6bd5e74c07f588fd1f4def9e9fef6ced`:

- CPython 3.12: clean install, `pip check`, compile and the complete configured
  registered loader, cursor, runner and recovery runtime suite;
- CPython 3.13: the same runtime suite plus Ruff, format check, strict MyPy and
  the 1,000-row legacy/streaming benchmark equivalence gate;
- registered streaming checkpoint replay uses bounded deterministic prefix replay,
  verifies the committed runtime digest and reopens Parquet at the exact next-row
  physical cursor;
- file sources and `legacy_materialized` registered sources retain the unchanged
  legacy replay route;
- all temporary diagnostic and certification workflows were removed before the
  cleanup commit was pushed.

A subsequent standard `Fast checks` run exposed repository-wide Ruff format drift in
eight files inherited from the bounded Backtest branch. Disposable formatter run
`30700920926` applied Ruff `0.16.1`, required `ruff format --check .`, `ruff check .`,
and `git diff --check` to pass, then removed its own workflow and published formatted
code commit `71a985febadeafe9aff1b72e680b96e26ce40d5f`.

The Backtest quality gate then exposed integration coverage below the repository-wide
90% threshold even though 210 runtime tests, Ruff, strict MyPy, CLI, memory smoke and
wheel validation were green. A dedicated fail-closed contract package was added for
registered loader, cursor, recovery and result-publication error paths. Disposable
fixer run `30701564590` verified all 17 focused contracts and removed its own workflow
before publishing commit `d323bc8b44809d6091230f3333d331c92bb29e4a`.

The first coverage diagnostic completed 227 tests and raised combined branch coverage
from 86% to 89.338%. A second bounded-scalability package covers validation and error
contracts for record buffers, performance configuration, compact equity retention and
the memory guard. Disposable fixer run `30701873298` verified all 32 focused contracts,
corrected the hybrid retention expectation, removed its own workflow and published
commit `71f2d4c01298bf2ba03924405b2945e0dd1fcfcb`.

Final coverage gate run `30701981782` passed on CPython 3.13. It required both focused
packages to pass compile, Ruff lint and formatting, then executed **242 Backtest tests**
under branch coverage with `coverage report --fail-under=90`. The result was exactly
**90% combined branch coverage**. Both temporary coverage workflows were removed in
commits `73d6b3a9d9e1a42120561376e03d49244a05fbf7` and
`20108740f55c0881aff8ea57476b8538f8d1ce96`.

The first complete standard CI pass on head `fc613bcec9afbc6147abcab05ebf968d6840b630`
confirmed Backtest 3.12/3.13, the historical-data bridge, Fast checks, actionlint and the
main component workflows. Core-only and Wave repository compatibility exposed one real
integration boundary: an already exhausted resumed registered cursor attempted to import
the optional market-data ingestion backend before returning `StopIteration`. Disposable
fixer run `30707248675` moved the physical end-of-source check ahead of optional imports,
passed compile, Ruff lint, Ruff formatting and the exact Core-only regression test, then
removed its workflow and published production fix commit
`f5f2919081f7521fed036425674145b2b03d25b4`.

Other failures in that standard pass were infrastructure-only: one uv build cache returned
a malformed Mach-O interpreter, one uv-managed Python 3.12 installation lacked the stdlib
`hmac` module, and one PyPI request ended with a TLS handshake EOF. These failures occurred
outside project runtime assertions and are re-evaluated by the clean-head CI rerun.

Workflow runs attached directly to GitHub Actions bot-authored cleanup commits can have
conclusion `action_required` with zero jobs. This is a GitHub approval/trigger boundary,
not a test failure; connector-authored evidence commits trigger the normal repository
workflows on the same production code tree.
