# Control UI Security Hardening v1

Status: reproducible security baseline for Draft PR #371 / Issue #397. This document records current enforced boundaries and known blockers. It does not claim full browser security acceptance; dependency audit, unit tests, typecheck and build are tracked separately from real browser execution.

## 1. Threat boundary

The Control UI is a local/PAPER operator surface. It must not become a general remote-administration, shell, filesystem, secret-management or Live-trading interface.

Primary threats addressed by the current design:

- browser credential persistence/leakage;
- ambient browser cookies or authenticated-response caching becoming a second credential/state channel;
- host-path/environment/command-line disclosure;
- arbitrary command/file execution;
- accidental remote bind/proxy traversal;
- raw owner response/exception leakage;
- duplicate business state machines and unsafe read side effects;
- Live/private-exchange capability appearing through a UI/control shortcut;
- dependency vulnerabilities hidden to obtain a green browser run.

## 2. Control Plane server boundaries

### Loopback-only

Control API bind validation accepts only loopback forms. Owner HTTP adapters used by the Control Plane similarly validate loopback base URLs, reject URL credentials, path prefixes/query fragments where forbidden and avoid following redirects.

Market Data HTTP reads set `trust_env=False`, preventing ambient proxy environment from silently changing the owner boundary.

Remote bind/TLS/reverse-proxy support is not part of this accepted scope.

### PAPER-only / no Live control

The Control Plane contract reports fixed `PAPER` mode. The focused CI source boundary rejects known dangerous capability markers such as:

```text
subprocess.
os.system(
os.popen(
pty.spawn(
shell=True
shell = True
private_api_key
private_exchange_key
real_order_placement
```

This scan is not a substitute for code review, but it is a deterministic regression gate for the local Control Plane package.

Paper lifecycle mutations remain unavailable until owner contract #381 is accepted.

### No arbitrary command or file API

The Control Plane does not expose a shell/script endpoint. Logs audit #391 also explicitly rejects arbitrary file paths, `tail`, shell execution and direct owner-database reads as a unified Logs implementation.

## 3. Lightweight import / side-effect boundary

`control-plane-foundation-ci` imports the base `modular_control_plane` package and fails if heavy owner/server modules leak into that import boundary.

The forbidden-import proof includes:

- FastAPI/uvicorn;
- Market Data owner packages;
- Backtest engine;
- Research Runner;
- Results Registry;
- Trading Runner;
- Paper Execution;
- Signal Bot;
- Strategy Audit / Strategy Factory;
- `psutil`.

System Resources therefore uses `psutil` only in the server `interface` path; importing the shared contract/client package does not inspect the host.

Read-only owner adapters must not initialize/migrate persistence, claim leases or start owner workers merely because the Control UI imports or reads them.

The isolated-wheel gate additionally proves that the default FastAPI application can be composed without optional project owners installed. Backtest owner absence is exercised as a real clean-environment case and must fail lazily with the fixed `BACKTEST_DEPENDENCY_UNAVAILABLE` error rather than an import-time crash or raw `ImportError` leakage.

## 4. Bounded and privacy-minimized contracts

### Dataset projection

The Dataset Control projection omits:

- native manifest URI;
- native storage URI;
- raw validation/unresolved issue text;
- raw lineage/path payloads.

Projected textual identities are bounded path-free tokens. Slash, backslash, NUL/surrounding whitespace and unsafe native identity values fail closed. Projection validation exceptions are converted to fixed safe error messages without echoing the unsafe owner value.

Catalog HTTP responses are bounded to 1 MiB. Market Data health preserves its 64 KiB bound.

### System Resources projection

The System Resources API exposes only bounded aggregates:

- CPU/logical CPU count;
- RAM/disk usage;
- uptime;
- aggregate process count;
- RSS/thread count of the current Control Plane process.

It intentionally has no hostname, username, env, cwd, argv/cmdline, filesystem/mount path, network inventory or per-process identity/list fields.

The browser validator is strict on the System Resources key set and rejects unknown/privacy-expanding fields such as `hostname`.

## 5. Error redaction

Owner/transport errors must not use raw response bodies or exception messages as user-visible errors.

Server errors use typed safe fields. `technical_details` is internal diagnostic data and is not retained by browser client errors.

The terminal client exposes safe fields such as code/message/action/status code and never renders bearer values through client representation or normal API errors.

Security tests cover examples where native response bodies/paths/secrets must not appear in projected errors.

## 6. Browser credential, cookie and cache boundary

Reader/operator/Market Data tokens are React in-memory values only.

The Web Console static CI security step fails if Control UI source contains:

```text
localStorage
sessionStorage
document.cookie
indexedDB
dangerouslySetInnerHTML
eval(
credentials: "same-origin"
credentials: "include"
```

Equivalent single-quoted credential modes are rejected by the same gate. The full E2E workflow carries the same source boundary and enforces it during browser acceptance.

All Control/Market Data/Backtest/Strategy/Research/Evidence/Paper browser HTTP clients explicitly use `credentials: "omit"` and `cache: "no-store"`. Native Market Data SSE uses the same policy. Bearer headers are therefore the only browser authentication channel accepted by these clients; ambient cookies are not sent.

The Control FastAPI application applies `Cache-Control: no-store` to successful and error responses as defense in depth. The isolated-wheel System Resources smoke asserts this header on a real successful response.

The browser security gate also rejects the external-bind literal `0.0.0.0` in browser/Vite source and requires the local Vite proxy configuration to contain `127.0.0.1`.

Reloading the application may therefore require re-authentication; persistence is intentionally not added as a convenience feature.

## 7. Dependency security gates

### Python

The focused Control Plane acceptance installs the complete development/interface surface, runs `uv pip check`, compile, Ruff format/lint, strict MyPy, tests, package build and installed-wheel smoke.

`control-plane-clean-install-ci` separately builds the Control Plane wheel, creates fresh Python 3.12/3.13 environments, installs only the intended `[interface]` wheel surface, runs `pip check`, proves the lightweight import boundary, composes the default server, exercises System Resources/privacy/no-store behavior, proves lazy actionable owner absence and executes installed CLI/API entrypoints.

A future dedicated Python vulnerability scanner may be added only if it is deterministic in the project runner environment. Do not claim a `pip-audit`/Bandit CI gate that is not actually present in the live workflow.

### Web production dependencies

`market-data-web-console-ci` installs the exact lockfile and runs:

```bash
npm audit --omit=dev --audit-level=high
```

Substantive audit failures are not retried as network noise. Only recognizable audit-registry/network transport failures receive bounded retries.

### Web full dependency tree / real browser gate

The real `market-data-web-console-e2e` performs a full high-severity dependency audit before browser execution. The previous #384 / NanoID blocker is closed: the candidate lockfile contains `nanoid@3.3.18`, outside the advisory's `<3.3.17` vulnerable range.

Current full-tree findings were verified against the official advisories and resolved with compatible upstream patches:

- `vite@8.1.5` was affected by four `launch-editor` advisories; the official `GHSA-587h-7wmj-h5pm` marks `8.1.6` patched. The direct dependency is now `vite@8.1.6`.
- `@vitest/mocker@4.1.11` was affected by `GHSA-qxg6-89mg-rqv8` and `GHSA-xp26-3454-mjgp`; both advisories mark `4.1.12` patched. `vitest@4.1.12` selects this matching mocker release.
- The full `npm audit --package-lock-only --json` on this lockfile reports zero vulnerabilities.

Evidence: [Vite GHSA-587h-7wmj-h5pm](https://github.com/advisories/GHSA-587h-7wmj-h5pm), [Vitest GHSA-qxg6-89mg-rqv8](https://github.com/vitest-dev/vitest/security/advisories/GHSA-qxg6-89mg-rqv8), and [Vitest GHSA-xp26-3454-mjgp](https://github.com/vitest-dev/vitest/security/advisories/GHSA-xp26-3454-mjgp). This resolves the dependency-audit blocker; it does not by itself claim that real browser E2E has run or passed.

Security policy:

- do not suppress/ignore advisories or use `npm audit fix --force`;
- keep dependencies on official compatible release lines and rerun the full audit, TypeScript, Vitest and production build after dependency updates.

A green production-only audit is not equivalent to a green full dependency-tree browser acceptance.

## 8. Authentication scope separation

Control reader and operator scopes are distinct.

- reader token protects read-only Control API surfaces;
- operator token is required only for accepted mutation surfaces such as enabled Backtest mutations;
- Market Data reader credential remains server-side and separate from the Control reader credential;
- Signal Bot health does not expose Telegram bot token/chat ID.

Do not reuse one browser token field to silently broaden privilege.

## 9. Owner-boundary security

Security includes state ownership, not only secret redaction.

The Control Plane must not:

- construct a second Trading Runner/Paper Execution owner;
- create an Acceptance state machine because workflow soak exists (#382);
- open/migrate Research state for monitoring;
- rewrite Strategy Factory checkpoints/governance state;
- treat Evidence as mutable runtime logs;
- open Signal Bot SQLite or introspect Telegram client objects to obtain health;
- invent a durable multi-owner event journal before #391.

Fail-closed/unavailable UI is preferred over a duplicated state machine that can disagree with the owner.

## 10. CI/platform failures vs code security failures

Always inspect exact job steps/logs.

Known unrelated platform/account conditions include:

- GitHub Actions artifact-storage quota can fail `actions/upload-artifact` after permanent functional/security steps have passed; reported to HQ #373;
- top-level jobs can fail before steps materialize; `steps: null` is not a repository-code result.

Artifact retention is governed centrally by the integration branch policy; Control UI workflows must not reintroduce local `retention-days` overrides merely to work around quota conditions.

These conditions must not be mislabeled as green security checks, but they also must not be used to falsely attribute a repository-code vulnerability.

The focused Control Plane acceptance workflows intentionally do not require artifact upload for their core pass/fail result.

## 11. Security review checklist for future Control UI changes

Before accepting a new read surface:

- identify the authoritative owner;
- prove reads are side-effect free;
- define a bounded versioned contract;
- remove raw paths/free-form secret-bearing payloads;
- validate resource identity/cursor/pagination inputs;
- bound response size/timeouts;
- use reader auth;
- project fixed safe errors;
- add owner/mock and, where meaningful, real read smoke;
- verify base imports remain lightweight.

Before accepting a mutation surface:

- owner-native mutation must already exist;
- PAPER-only boundary;
- idempotency key/duplicate behavior;
- actor attribution/audit;
- concurrency/fencing semantics;
- timeout/retry/recovery behavior;
- typed safe error states;
- no browser-as-runtime-truth;
- exact-head mutation E2E.

Before accepting browser UI:

- no credential persistence;
- no ambient cookie credential mode;
- no authenticated HTTP caching;
- no unsafe HTML/dynamic code primitive;
- AbortSignal/cancellation for async reads;
- strict contract validation;
- technical-details redaction;
- TypeScript/Vitest/build green;
- full dependency audit and real browser E2E green before claiming full browser acceptance.

## 12. Current security status

The reproducible static/backend baseline requires the current PR exact head to pass:

- `control-plane-foundation-ci` on Python 3.12 and 3.13;
- `control-plane-clean-install-ci` on Python 3.12 and 3.13;
- `market-data-web-console-ci` through production audit, browser security boundary, strict TypeScript, Vitest and build.

The canonical run IDs and exact accepted head belong in PR #371 / issue evidence, not hard-coded here, so this document does not become stale after a docs-only synchronization commit.

Not accepted / still blocked:

- full real-browser Web Console acceptance;
- Paper lifecycle mutations #381;
- durable Acceptance owner #382;
- Signal Bot test-notification mutation #383;
- unified Logs event owner #391.

Security status must be reported with these distinctions instead of a blanket "secure" or "fully green" claim.
