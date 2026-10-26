# Breakout–Return deep optimization report

The authoritative baseline, 30-round engineering log, 15 intermediate audits, benchmark evidence, final independent audit, limitations and verdict are recorded in:

- `DEEP_OPTIMIZATION_BASELINE.md`;
- `DEEP_OPTIMIZATION_FINAL_AUDIT.md`;
- `CORE_CHANGE_REQUEST.md`.

Final local module evidence for the public hardening implementation:

- 135 Breakout–Return tests passed;
- Ruff passed;
- strict mypy passed;
- compileall passed;
- package coverage 91% with a 90% gate;
- wheel build and clean virtualenv installation passed;
- entry-point, `py.typed`, public import and real BacktestEngine smoke passed;
- 50,000-event median 3.651361 s;
- 1,000,000-event workload with midpoint restart 19.158167 s;
- peak module-path memory 6,611 bytes in the 50,000-event traced workload;
- checkpoint remained below 1 KiB in the recorded workloads.

Final GitHub Actions evidence is not yet available: current repository workflows terminate before creating any job steps across multiple unrelated workflows. PR #18 therefore remains Draft and the final verdict remains **conditionally ready**, not fully integration-ready.
