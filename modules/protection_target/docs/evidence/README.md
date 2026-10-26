# Evidence retention policy

This directory retains compact, reviewable evidence needed to reproduce the Protection & Target
1.1.1 audit: benchmark summaries, coverage text, test summaries, deterministic replay and stress
results, security summaries, import/entry-point smoke and distribution checksums.

Generated wheels, source distributions, JUnit XML, full coverage JSON, cProfile dumps, full SBOMs and
verbose build logs are intentionally not committed. The self-hosted workflow uploads those files as
14-day GitHub Actions artifacts for each audited commit.
