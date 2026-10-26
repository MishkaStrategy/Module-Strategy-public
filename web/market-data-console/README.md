# Market Data Console

Local-only React console for the Historical Market Data Ingestion application API.

## Current scope

- API version and health;
- immutable Dataset Registry list and exact-version details;
- artifact metadata lookup and authenticated ZIP download;
- durable job list with pagination and state/operation filters;
- job details with request, result, error, warnings and progress;
- bounded event history plus authorized fetch-SSE reconnect with `Last-Event-ID`;
- bearer token held only in React memory for the current tab.

The event timeline retains at most 1,000 events in the browser. Route or token changes abort the active stream. The API stream is consumed through `fetch`, not native `EventSource`, so reader-role bearer authentication remains attached to every reconnect.

No download, export, cancellation or repair mutation form is enabled in this read-only slice. The console does not change the FastAPI bind policy and does not enable remote access or deployment.

## Development

Prerequisites:

- Node.js 22.12 or newer;
- the Market Data API running on `127.0.0.1:8000`.

```bash
npm ci
npm run typecheck
npm test
npm run dev
```

Vite binds to `127.0.0.1:5173` and proxies `/api` only to a validated loopback target. The default target is `http://127.0.0.1:8000`; browser E2E may override only the TCP port through `MDI_E2E_API_PORT`.

## Production build

```bash
npm run build
npm run preview
```

The static build assumes same-origin `/api/v1` routes. Hosting or external bind is not authorized by this component.

## Local browser E2E

The committed Playwright gate uses:

- exact locked `@playwright/test` and Node types;
- an isolated Chromium download;
- a real `MarketDataApplication` with `FakeExchangeAdapter`;
- temporary local Dataset Registry, artifact and SQLite job/event stores;
- a real FastAPI process and Vite preview on dynamically allocated `127.0.0.1` ports;
- a generated masked reader token;
- no backend mock routes and no mutation controls.

The seed creates a fake immutable dataset, a verified export artifact and a durable read-only validation job with 1,006 events. Browser flows verify health, datasets, artifact download, job detail, bearer authorization, SSE reconnect with `Last-Event-ID`, the 1,000-event UI bound, route cleanup and absence of browser token persistence.

```bash
npm run e2e:typecheck
npm run e2e
```

CI keeps Playwright trace and video disabled, captures screenshots only on failure and retains evidence for seven days. The workflow has read-only repository permissions.
