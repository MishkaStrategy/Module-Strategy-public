# Control UI Performance Baseline

Status: measured on Draft PR #371. This document records observed performance evidence and defines when optimization is justified. It is not a claim that every runtime path has been benchmarked.

## Principles

Performance work must be evidence-driven. Do not add memoization, code splitting, caching, concurrency or lazy-loading solely because they appear faster in theory.

A performance change should have:

1. a reproducible baseline;
2. a user-visible or operational problem, or a defined regression threshold;
3. a focused measurement after the change;
4. correctness, security and owner-boundary tests unchanged or strengthened.

Browser bundle size and local Control Plane latency are separate surfaces and must not be conflated.

## Measured Web Console baseline

Source: exact-head `market-data-web-console-ci` run `31236320766`, job `93049363811`, head `515ad6f598a7bd3f0d06417a5838a3efc056b9b5`.

The production Vite build completed successfully without a chunk-size warning.

| Asset | Raw | gzip |
| --- | ---: | ---: |
| JavaScript `dist/assets/index-DLJz4NV3.js` | 403.68 kB | 119.79 kB |
| CSS `dist/assets/index-BySMY-3m.css` | 21.53 kB | 4.38 kB |

The same CI run also passed:

- exact locked dependency installation;
- production dependency audit;
- browser security boundary;
- strict TypeScript;
- full Vitest suite;
- production build.

This measurement includes the accepted System Resources Dashboard static slice.

## Current interpretation

The measured build does not justify speculative code splitting or dependency churn by itself:

- Vite emitted no bundle-size warning;
- no measured navigation/render latency regression has been demonstrated;
- no accepted user workflow is blocked by bundle size;
- forcing new chunks would add loading/error states and testing surface without evidence of benefit.

Therefore the current decision is **observe, do not optimize speculatively**.

## Regression policy

Bundle size is a diagnostic signal, not the sole release criterion.

A focused optimization investigation should be opened when one or more of the following is observed on comparable production builds:

- main JavaScript gzip size grows by more than 20% from this baseline (approximately 144 kB gzip) without an intentional feature justification;
- main CSS gzip size grows by more than 25% from this baseline (approximately 5.5 kB gzip) without an intentional feature justification;
- Vite starts emitting a chunk-size warning;
- measured cold navigation, interaction or rendering latency becomes operationally material;
- a dependency adds substantial code to routes that do not use it;
- memory/CPU measurements from the accepted System Resources projection show a reproducible Control Plane regression attributable to UI/API behavior.

These are investigation thresholds, not permission to bypass functional/security gates.

## What is not yet measured

The following must not be presented as established performance facts until a reproducible measurement exists:

- browser first-contentful paint or Core Web Vitals;
- route-level render/interaction latency;
- `modular-control` cold-start distribution;
- Control Plane request latency distribution;
- System Resources provider overhead under sustained polling;
- long-running Dashboard memory behavior;
- remote-network performance (the product remains loopback/local-only in this scope).

If these become relevant, add a focused benchmark or E2E measurement rather than estimating them from bundle size.

## System Resources polling

The System Resources Dashboard currently performs a bounded read every 10 seconds through the same reader-authenticated Control Plane client.

The server provider samples CPU for 50 ms per request. This is an implementation parameter, not yet a measured sustained-overhead result. If polling overhead becomes a concern, measure CPU/request latency first; do not introduce caching or a second metrics state machine without evidence.

## Security blocker is not a performance problem

Issue #384 (`nanoid` GHSA-2v37-7h3g-55p8) is closed for the current candidate: its lockfile contains `nanoid@3.3.18`, and the full development dependency audit reports zero vulnerabilities. Real-browser E2E remains a separate, unrun acceptance gate; this baseline does not claim browser performance measurements.

Do not trade security for faster acceptance:

- do not suppress the audit;
- do not force an incompatible dependency major;
- do not use a third-party repack solely to remove the finding.

The static production build baseline remains valid evidence, but real-browser performance should be measured only after the security gate can proceed.

## Next measurement opportunities

Prioritize measurements only when they answer a concrete decision:

- route-level browser timing if navigation becomes visibly slow;
- CLI cold-start timing if SSH/recovery use feels delayed;
- Control Plane resource/latency soak if 10-second System polling shows measurable overhead;
- bundle attribution if the main JS gzip threshold is crossed.

Record the exact head SHA, environment, commands/workflow and raw result for every future baseline so comparisons remain reproducible.
