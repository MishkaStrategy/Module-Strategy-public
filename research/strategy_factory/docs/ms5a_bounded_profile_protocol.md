# MS5A bounded profile protocol

> **Current status — execution is hard-disabled by the #1190 STOP.** The
> execution entry points reject execution with
> `MS5A_PROFILE_EXECUTION_STOPPED_1190`; `--plan-only` is the only permitted CLI
> mode. This guard is a STOP enforcement mechanism, not authorization
> verification. A local template or caller-selected `claim_root` is not trusted
> one-shot authorization; the trusted grant authority and claim ledger are
> absent. The SQLite owner registry may write. No execution may resume without
> a new explicit owner decision and an independently verified trusted
> authority. The protocol detail below is historical/proposed design and does
> not establish that these authorization components exist or are trusted.

The selector in `strategy_factory.ms5a_budget_probe` validates the accepted
frozen request, Factory checkpoint, content hashes, dataset identity, and first
frozen native SEARCH job for each family. It is a read-only plan report: it
does not load market data, execute a Backtest, or write Factory/Research state.
A future execution requires a separate owner grant exactly bound to the
emitted source, request, dataset, runtime, plan hashes, and limits.

## Version 2 source binding

The frozen lineage remains `408d09c05435292d6995060a5d2fd0d4feb0f16e`:
`SOURCE_SHA`, the frozen request and checkpoint, selected plans, and their data
and parameter identities are unchanged. The exact diagnostic runtime
candidate is commit
`24501b6d7259c2b8d9eacf1ba2ddbebb94a07c55`, based on
`347825fc2496033dcc70c5ae3ffb667a7148c1d6` through follow-up commit
`01b03b70297a447cd32c7f61f3827a0e14db0f7d`. The identity
`MishkaStrategy/Module-Strategy#1407@03b3f83` names the upstream MDI repair;
it is distinct from the exact runtime candidate SHA. The candidate's pinned
runtime sources are `modules/market_data_ingestion/src/market_data_ingestion/integrity.py`
(`8d4141a0a6856bba7fd235f9eca54234809f0b715e450ac8f257fd7f8351a4c5`),
`modules/market_data_ingestion/src/market_data_ingestion/storage.py`
(`e6f97dfa1dba11400bda10610c04bcfc53c72e1ec7bb0d6a3275546e2f7d240f`), and
`research/strategy_factory/src/strategy_factory/multi_strategy_owner.py`
(`83002d75b2a96d2b6e9ef9e0c70d365d65d72e315ea9c472637bec4c8a4a3eed`).
The owner source adds optional diagnostics instrumentation and the synchronous
interface/factory; its semantics-neutral path remains available when diagnostics
are omitted. The diagnostic candidate is not thereby accepted as a general
runtime.

Protocol schema 2 (`ms5a-bounded-profile-v2-repaired-runtime`) binds both
source identities, the upstream repair ID, those exact three runtime-source
hashes, and the exact controller-source commit executing this protocol. After
merge, bind the byte-identical merged controller commit; an unrelated
integration-head SHA is not a substitute. A schema-1 authority or receipt is
rejected. The runtime checkout must be clean and at the pinned candidate SHA;
the executor checks all three source files are contained regular files with
approved hashes. Before consuming the claim, the parent verifies the owner
source digest and its version-1 diagnostics API through the AST contract gate.
The version and callable signatures are bound transitively by the exact owner
source digest and that gate; there is no separate version field in the authority.
Inside the child, the loaded `market_data_ingestion.integrity` and `.storage`
paths and digests are attested before owner resolution; the full existing
Strategy Factory runtime-module attestation remains after owner resolution.
The child removes controller source paths from import search and loads only the
manifested `ms5a_budget_probe` and executor source snapshots into a private
`_ms5a_controller.strategy_factory` namespace with an empty package path. Its
`canonical`, `errors`, `contracts`, and `contract_codec` names alias the
hash-bound modules from the verified runtime checkout, so private duplicate
contract types cannot cross into runtime APIs. The owner module is rejected if
already present in `sys.modules`; the child reads its exact contained source
bytes once, verifies the bound digest and diagnostic AST contract on that same
snapshot, then compiles and executes those bytes under the canonical runtime
module name. Failed execution removes only that partially initialized module.
Receipts keep source identity separate from `runtime_module_sha256`, which
records loaded-module attestation. On setup failure, only the allowlisted
phase, safe exception classes, and available helper attestation are reported;
no case or candidate metrics are asserted.

The planned families, prefix schedule, frozen plans/parameters, and redaction
rules remain unchanged. Resource bounds remain a 105-second child-native
budget, 120-second child watchdog, and 1,200-second outer timeout. Eight GiB is
the protocol memory maximum; the planned future grant caps effective cgroup
memory at 4 GiB, with zero swap, one CPU, one worker, and network disabled.
Any future execution requires a fresh human-approved one-shot authority bound
to this v2 contract;
previously consumed authority is not reusable. No diagnostic execution or
general-runtime acceptance is implied by this protocol update.

The selected cases are the first frozen plans for `breakout_return`, `fvg_rt`,
and `liquidity_sweep`; each first native job is `optimization_000` in SEARCH.
The proposed prefixes are 1,000, 2,000, and 4,000 5-minute bars. The selector
accepts only the known modules for each family and reports declarative
`manifest_history_complete` separately from runtime readiness. Each prefix
also includes `skip_until_ready` and `runtime_readiness: unverified`. The
generic engine skips a module for incomplete manifest history only when that
module sets `skip_until_ready`; the selector does not infer runtime readiness
from a manifest or simulate module branches.

The accepted FVG composite declares 200 bars on 15m, 1h, 4h, and 1d and sets
`skip_until_ready=False`. The proposed FVG prefixes are incomplete against
those aggregate manifest counts; this is not a conclusion that the strategy is
runtime-unready. Static source shows some branches can warm within the frozen
train span of 147.318 days: the long FVG and frozen 1h paths use a 4h EMA200,
which requires 200 four-hour bars (about 33.333 days). The daily EMA200 used by
daily short paths requires 200 days and cannot warm within that train span.
This is a branch-history observation only, not a product defect or verified
runtime-readiness claim. The selector does not establish actual signal output,
performance, or complete family readiness.

The bounded executor is `strategy_factory.ms5a_profile_executor`. Its
`--plan-only` mode rechecks the pinned repaired-runtime checkout and frozen lineage,
then emits the prefix-major schedule without loading market data or consuming a
grant. Execution mode requires a separate exact owner manifest, an exclusive
one-shot claim directory, registered data root, and empty scratch root. It
imports the production runner from the clean diagnostic candidate checkout, verifies
loaded runtime-module paths and hashes, and calls
`MS5ABacktestConfigFactory` and `_OwnerFundingBacktestRunner` through
`modular_backtest.run_backtest`. It does not initialize or write the original
Factory/Research stores.

Execution is prefix-major: all three families at 1,000 bars, then all three at
2,000, then all three at 4,000. It keeps the frozen parameters and seed and
requires the registered 5-minute prefix to contain exactly the requested
number of rows before calling Backtest. Cases run sequentially, with at most
one diagnostic child running at a time. A 105-second native deadline begins at child start (including
owner setup), with a 120-second hard process-group watchdog; the outer cap is
20 minutes including setup, and the RSS cap is 8 GiB. Concurrency is one. Stop
at the first failure or cap without retry or rescheduling. cProfile is dumped
for a caught Backtest failure. If the shared native deadline expires between
cases, the child returns a stopped receipt with the completed case hashes and
profiles accumulated so far; a process-level hard kill cannot guarantee
cleanup or a profile dump.

Execution must run inside an isolated Linux cgroup-v2 container with numeric
`memory.max` no greater than 8 GiB and `memory.swap.max=0`; the controller
rejects unsupported hosts and missing/unlimited caps before consuming the
grant. Before consuming the grant, the controller also verifies `/proc/1/exe`
and the NUL-delimited PID-1 command line: coreutils `timeout --signal=KILL Ns`
must directly wrap this execution module, with `N <= 1200`. This lets the
outer cap terminate the entire container cgroup, including an escaped process
group. The sanitized report records the attested timeout and cgroup limits.
Sampled RSS remains supplemental monitoring, not the memory enforcement
mechanism.

Hash the exact frozen input prefix and output tree, keep cProfile data in
scratch, and publish only sanitized statuses, durations, and hashes; never
include candidate metrics or trades, or treat probe output as candidate
evidence. Do not extend periods beyond frozen SEARCH or read HOLDOUT. Short
prefix timings characterize bounded startup/CPU cost only; they cannot
establish 96-case/24-hour feasibility.

A child setup failure may have no case or module evidence, but its receipt is
accepted only with a fixed allowlisted phase, dataset kind, error class, and up
to four cause-class names. Exception messages, filesystem paths, and payloads
are never included. The parent validates and preserves this metadata so a
wrapped owner-resolution failure remains diagnosable without exposing private
details.

Before native imports, the child appends a bounded allowlisted phase ledger to
the scratch root and fsyncs each record so the parent can retain breadcrumbs
after a hard timeout. The report labels these as `partial_diagnostic_evidence`
and exposes only `last_recorded_phase`; this is not a definitive failure phase,
child-terminal receipt, or case receipt. A hard stop can occur between a native
return and its next ledger append. The ledger distinguishes the three
`plan_build_started`/`plan_build_returned` pairs from the nine
`case_started`/`case_operation_returned` callback pairs and reserves capacity for
all 44 expected events, with four slots of headroom under the 48-event cap. One standard-library `faulthandler` snapshot
is scheduled at 90 seconds and cancelled on ordinary exit. The parent emits at
most 64 accepted-source-relative file/line/function frames and an opaque trace
hash; raw trace and cProfile files remain in scratch. cProfile wraps owner
resolution when it returns or raises normally; process termination cannot
guarantee either profiler dump. Ledger, trace, and profile inputs are size
bounded and symlink-rejected before the parent reads them.

Before launch, complete the configured Strategy Factory test and branch-coverage
gate without changing its 90% threshold, and inspect GitHub checks for the exact
controller head. Missing, skipped, or earlier-head checks do not prove a pass.
Record the controller commit, immutable container image identity, accepted
runtime identity, and effective cgroup and watchdog limits with the diagnostic
receipt. Environment preparation and plan-only checks do not consume the grant;
the exclusive claim marks the start of its single execution attempt.
