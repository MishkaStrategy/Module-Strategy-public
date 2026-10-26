# Control UI accessibility hardening v1

Status: focused Phase 10 accessibility slice for Draft PR #371.

## Scope

This slice hardens keyboard and assistive-technology navigation in the existing hash-routed Dashboard without changing Control Plane, owner, trading or mutation contracts.

Implemented boundaries:

- the first keyboard-focusable control is a visible-on-focus skip link that bypasses the long sidebar and focuses the existing `<main>` landmark;
- the skip-link click is intercepted so the Dashboard hash router is not replaced by a fragment-only route;
- the main landmark receives `tabindex=-1` only as a programmatic focus target and is not inserted into the normal tab order;
- client-side hash route changes transfer focus to the main landmark so keyboard/screen-reader context does not remain on the previous sidebar link;
- `document.title` follows the canonical `NAVIGATION` route labels, including nested Market Data routes, without exposing query-string state;
- existing global `:focus-visible` styling and `prefers-reduced-motion` behavior remain in force; the skip-link transition is also disabled for reduced-motion users.

## Verification

Required acceptance for this slice:

1. strict TypeScript succeeds;
2. Vitest covers hash normalization, canonical route labels and document-title derivation;
3. production build succeeds;
4. focused Research Playwright proves the skip link is first in keyboard order, becomes visible on focus, moves focus to `<main>` without changing the Research route, and SPA navigation updates both focus and document title;
5. the existing Research read-only/DB byte-identity/privacy assertions remain green after the accessibility assertions;
6. static browser security checks remain green.

The generic full Web Console browser workflow remains a separate, unrun gate. Issue #384 is closed for the current candidate: the full lockfile audit reports zero vulnerabilities. Keep browser acceptance distinct until the real-browser workflow has run.

## Non-claims

This slice does not claim complete WCAG conformance, formal screen-reader certification, color-contrast certification for every component state, or replacement of future manual accessibility review. No new accessibility runtime dependency or audit suppression is introduced.

## Safety

- no Live/private-exchange/order-placement capability;
- no new server/API route;
- no token persistence or additional browser storage;
- no arbitrary shell/file/log access;
- no owner mutation semantics;
- no duplication of Dashboard navigation state: route titles are derived from the existing canonical `NAVIGATION` structure.
