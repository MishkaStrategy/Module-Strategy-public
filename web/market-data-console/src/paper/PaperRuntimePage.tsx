import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useReducer, useRef, useState } from "react";

import type { ControlPlaneApiClient } from "../control/client";
import { PaperRuntimeClientError, PaperRuntimeControlApiClient } from "./client";
import type {
  PaperLifecycleMutation,
  PaperLifecycleMutationResult,
  PaperPositionsAvailableSnapshot,
  PaperPositionsSnapshot,
  PaperRuntimeNativeState,
} from "./contracts";
import "./paper.css";

interface PendingLifecycleMutation {
  mutation: PaperLifecycleMutation;
  requestId: string;
}

const STOPPABLE_STATES = new Set<PaperRuntimeNativeState>([
  "configured",
  "running",
  "degraded",
  "pausing",
  "paused",
  "kill_switch_active",
  "stopping",
  "failed",
]);
const ORDERS_PAGE_LIMIT = 100;

export type PaperPositionSideFilter = "all" | "long" | "short";
export type PaperPositionStrategyFilter = "all" | "unknown" | `known:${string}`;

export interface PaperPositionFilters {
  symbol: string;
  side: PaperPositionSideFilter;
  strategy: PaperPositionStrategyFilter;
}

type PaperPositionRow = Extract<PaperPositionsSnapshot, { status: "available" }>['positions'][number];

export function serializePaperPositionsSnapshot(snapshot: PaperPositionsSnapshot): string {
  if (snapshot.status !== "available" || snapshot.coverage !== "complete") {
    throw new Error("Only a complete available positions snapshot can be exported.");
  }
  return `${JSON.stringify(snapshot, null, 2)}\n`;
}

function downloadPaperPositionsSnapshot(snapshot: PaperPositionsAvailableSnapshot): void {
  const blob = new Blob([serializePaperPositionsSnapshot(snapshot)], {
    type: "application/json;charset=utf-8",
  });
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  const snapshotName = snapshot.snapshot_version.replace(/[^a-zA-Z0-9._-]+/g, "-");
  anchor.href = objectUrl;
  anchor.download = `paper-positions-${snapshotName || "snapshot"}.json`;
  anchor.hidden = true;
  document.body.append(anchor);
  try {
    anchor.click();
  } finally {
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
  }
}

export const EMPTY_PAPER_POSITION_FILTERS: PaperPositionFilters = {
  symbol: "",
  side: "all",
  strategy: "all",
};

export type PaperPositionFilterAction =
  | { type: "symbol"; value: string }
  | { type: "side"; value: PaperPositionSideFilter }
  | { type: "strategy"; value: PaperPositionStrategyFilter }
  | { type: "reset" };

export function paperPositionFiltersReducer(
  _state: PaperPositionFilters,
  action: PaperPositionFilterAction,
): PaperPositionFilters {
  switch (action.type) {
    case "symbol":
      return { ..._state, symbol: action.value };
    case "side":
      return { ..._state, side: action.value };
    case "strategy":
      return { ..._state, strategy: action.value };
    case "reset":
      return EMPTY_PAPER_POSITION_FILTERS;
  }
}

export function PaperRuntimePage({
  controlApi,
  paperApi,
  readerAuthRevision,
  operatorReady,
}: {
  controlApi: ControlPlaneApiClient;
  paperApi: PaperRuntimeControlApiClient;
  readerAuthRevision: number;
  operatorReady: boolean;
}) {
  const queryClient = useQueryClient();
  const [lastMutation, setLastMutation] = useState<PendingLifecycleMutation | null>(null);
  const previousPositionsAuthRevision = useRef(readerAuthRevision);
  const [ordersOffset, setOrdersOffset] = useState(0);
  const [ordersProjectionUnavailable, setOrdersProjectionUnavailable] = useState(false);
  const ordersPaginationFocusRef = useRef<"previous" | "next" | null>(null);
  const ordersErrorReturnFocusRef = useRef<"previous" | "next" | null>(null);
  const previousOrdersButtonRef = useRef<HTMLButtonElement>(null);
  const nextOrdersButtonRef = useRef<HTMLButtonElement>(null);
  const ordersPaginationRef = useRef<HTMLElement>(null);
  const ordersErrorRef = useRef<HTMLParagraphElement>(null);
  const health = useQuery({
    queryKey: ["control-health", "paper_runtime", readerAuthRevision],
    queryFn: ({ signal }) => controlApi.getComponentHealth("paper_runtime", signal),
    retry: false,
    refetchInterval: 5_000,
  });
  const runtime = useQuery({
    queryKey: ["paper-runtime", readerAuthRevision],
    queryFn: ({ signal }) => paperApi.runtime(signal),
    retry: false,
    refetchInterval: 5_000,
  });
  const capability = useQuery({
    queryKey: ["paper-lifecycle-capability", readerAuthRevision],
    queryFn: ({ signal }) => paperApi.lifecycleCapability(signal),
    retry: false,
    refetchInterval: 10_000,
  });
  const orders = useQuery({
    queryKey: ["paper-orders", readerAuthRevision, ordersOffset],
    queryFn: ({ signal }) => paperApi.orders(ORDERS_PAGE_LIMIT, ordersOffset, signal),
    retry: false,
    refetchInterval: 5_000,
  });
  const blockedScopes = useQuery({
    queryKey: ["paper-blocked-scopes", readerAuthRevision],
    queryFn: ({ signal }) => paperApi.blockedScopes(signal),
    retry: false,
    refetchInterval: 5_000,
  });
  const positions = useQuery({
    queryKey: ["paper-positions", readerAuthRevision],
    queryFn: ({ signal }) => paperApi.positions(signal),
    retry: false,
    refetchInterval: 5_000,
  });
  useEffect(() => {
    if (previousPositionsAuthRevision.current === readerAuthRevision) return;
    previousPositionsAuthRevision.current = readerAuthRevision;
    const belongsToPreviousAuth = (query: { queryKey: readonly unknown[] }) =>
      query.queryKey[0] === "paper-positions" && query.queryKey[1] !== readerAuthRevision;
    void queryClient.cancelQueries({ predicate: belongsToPreviousAuth });
    queryClient.removeQueries({ predicate: belongsToPreviousAuth });
  }, [queryClient, readerAuthRevision]);
  const lifecycle = useMutation({
    mutationFn: (entry: PendingLifecycleMutation) =>
      paperApi.lifecycleMutate(entry.mutation, entry.requestId),
    onSuccess: () => refreshPaper(queryClient),
  });

  const refresh = () => refreshPaper(queryClient);
  const lifecycleAvailable =
    capability.data?.available === true && capability.data.paper_only && !capability.isError;
  const nativeState = runtime.data?.native_state ?? null;
  const killActive = runtime.data?.kill_switch.active === true;
  const baseActionReady =
    lifecycleAvailable &&
    operatorReady &&
    runtime.data !== undefined &&
    !runtime.isError &&
    !lifecycle.isPending;
  const canStart = baseActionReady && nativeState === "created";
  const canStop =
    baseActionReady && nativeState !== null && STOPPABLE_STATES.has(nativeState);
  const canKill = baseActionReady && nativeState !== "disposed" && !killActive;
  const canReset = baseActionReady && nativeState !== "disposed" && killActive;
  const canRetrySameRequest =
    baseActionReady &&
    lastMutation !== null &&
    lifecycle.error instanceof PaperRuntimeClientError &&
    lifecycle.error.retryable &&
    !lifecycle.isPending;
  const retryableRequestExists =
    lastMutation !== null &&
    lifecycle.error instanceof PaperRuntimeClientError &&
    lifecycle.error.retryable &&
    !lifecycle.isPending;
  const orderCount = orders.data?.orders.length ?? 0;
  const firstOrderNumber = orderCount > 0 ? ordersOffset + 1 : 0;
  const lastOrderNumber = ordersOffset + orderCount;

  useEffect(() => {
    const requestedButton = ordersPaginationFocusRef.current;
    if (orders.isPending || orders.isFetching) return;

    if (orders.isError) {
      setOrdersProjectionUnavailable(true);
      if (requestedButton === null) return;
      ordersPaginationFocusRef.current = null;
      ordersErrorReturnFocusRef.current = requestedButton;
      (ordersErrorRef.current ?? ordersPaginationRef.current)?.focus();
      return;
    }
    if (!orders.isSuccess) return;

    setOrdersProjectionUnavailable(false);
    if (requestedButton !== null) ordersPaginationFocusRef.current = null;
    const shouldRestoreAfterError =
      ordersErrorReturnFocusRef.current !== null &&
      ordersErrorRef.current === document.activeElement;
    const buttonToRestore =
      requestedButton ?? (shouldRestoreAfterError ? ordersErrorReturnFocusRef.current : null);
    ordersErrorReturnFocusRef.current = null;
    if (buttonToRestore === null) return;

    const requested =
      buttonToRestore === "previous"
        ? previousOrdersButtonRef.current
        : nextOrdersButtonRef.current;
    if (requested && !requested.disabled) {
      requested.focus();
      return;
    }

    const fallback =
      buttonToRestore === "previous"
        ? nextOrdersButtonRef.current
        : previousOrdersButtonRef.current;
    if (fallback && !fallback.disabled) {
      fallback.focus();
      return;
    }

    ordersPaginationRef.current?.focus();
  }, [
    orders.dataUpdatedAt,
    orders.isError,
    orders.isFetching,
    orders.isPending,
    orders.isSuccess,
    ordersOffset,
  ]);

  const runLifecycle = (mutation: PaperLifecycleMutation) => {
    if (!baseActionReady) return;
    const label = mutationLabel(mutation);
    if (
      !window.confirm(
        `${label} for the authoritative PAPER runtime? The Trading Runner owner remains authoritative.`,
      )
    ) {
      return;
    }
    const entry = { mutation, requestId: newRequestId(mutation) };
    setLastMutation(entry);
    lifecycle.mutate(entry);
  };

  return (
    <section className="paper-workspace" aria-label="Paper Runtime workspace">
      <header className="paper-header">
        <div>
          <p className="eyebrow">Embedded owner projection</p>
          <h1>Paper Trading</h1>
          <p>
            Live view of the already-owned Trading Runner and Paper Execution state. Control Plane
            does not create a second runtime, broker, order ledger or position store.
          </p>
        </div>
        <button type="button" className="secondary-action" onClick={refresh}>
          Refresh
        </button>
      </header>

      <div className="paper-safety-note" role="note">
        <strong>PAPER only</strong>
        <span>
          Live execution, exchange credentials and signed private requests do not exist on this
          surface. Lifecycle commands are forwarded only to the authenticated local Trading Runner
          owner.
        </span>
      </div>

      <section className="paper-health-grid" aria-label="Paper Runtime health">
        <article>
          <span>Component</span>
          <strong>{health.data?.status ?? (health.isError ? "UNAVAILABLE" : "LOADING")}</strong>
        </article>
        <article>
          <span>Runner</span>
          <strong>{runtime.data?.native_state ?? "—"}</strong>
        </article>
        <article>
          <span>Paper Execution</span>
          <strong>{runtime.data?.paper_execution_lifecycle ?? "—"}</strong>
        </article>
        <article>
          <span>Kill switch</span>
          <strong>{runtime.data?.kill_switch.active ? "ACTIVE" : "inactive"}</strong>
        </article>
      </section>

      {!operatorReady ? (
        <PaperNotice
          title="Lifecycle mutations locked"
          text="Enter the Control operator token in the global panel. Trading Runner owner credentials remain server-side."
        />
      ) : null}
      {capability.data && !capability.data.available ? (
        <PaperNotice
          title="Lifecycle owner not attached"
          text="Read-only Paper Runtime remains available, but lifecycle mutations are disabled until the authenticated owner transport is configured."
        />
      ) : null}
      {capability.isError ? (
        <>
          <PaperFailure
            error={capability.error}
            fallback="Lifecycle capability could not be read safely."
          />
          {capability.data ? (
            <p className="status-error" role="alert">
              The last-known lifecycle capability is stale; controls are read-only until a fresh capability is available.
            </p>
          ) : null}
        </>
      ) : null}
      {lifecycle.error ? (
        <PaperFailure error={lifecycle.error} fallback="PAPER lifecycle mutation failed safely." />
      ) : null}
      {lifecycle.data ? <LifecycleReceipt result={lifecycle.data} /> : null}
      {retryableRequestExists && lastMutation ? (
        <button
          type="button"
          className="secondary-action"
          disabled={!canRetrySameRequest}
          onClick={() => lifecycle.mutate(lastMutation)}
        >
          Retry same idempotency request
        </button>
      ) : null}

      {runtime.isError ? (
        <p className="status-error" role="alert">
          Paper Runtime is not attached or cannot be projected safely. This Dashboard will not
          bootstrap another runtime.
          {runtime.data ? (
            <> Showing the last successful snapshot; lifecycle controls are locked until a fresh projection is available.</>
          ) : null}
        </p>
      ) : null}

      {runtime.data ? (
        <>
          <section className="paper-panel" aria-labelledby="paper-runtime-heading">
            <div className="paper-panel-heading">
              <div>
                <p className="eyebrow">Trading Runner</p>
                <h2 id="paper-runtime-heading">Runtime state</h2>
              </div>
              <span className="paper-mode-badge">{runtime.data.execution_mode.toUpperCase()}</span>
            </div>
            <dl className="paper-summary-grid">
              <div><dt>Runtime</dt><dd>{runtime.data.runtime_id}</dd></div>
              <div><dt>Portfolio</dt><dd>{runtime.data.portfolio_id}</dd></div>
              <div><dt>Strategy</dt><dd>{runtime.data.strategy_id}</dd></div>
              <div><dt>Market</dt><dd>{runtime.data.market}</dd></div>
              <div><dt>Timeframe</dt><dd>{runtime.data.base_timeframe}</dd></div>
              <div><dt>Instruments</dt><dd>{runtime.data.instruments.join(", ")}</dd></div>
              <div><dt>Health ready</dt><dd>{runtime.data.health_ready ? "yes" : "no"}</dd></div>
              <div>
                <dt>Queue</dt>
                <dd>{formatQueue(runtime.data.queue_depth, runtime.data.queue_capacity, runtime.data.queue_high_water_mark)}</dd>
              </div>
            </dl>
          </section>

          <section className="paper-panel" aria-labelledby="paper-safety-heading">
            <div className="paper-panel-heading">
              <div>
                <p className="eyebrow">Safety & recovery</p>
                <h2 id="paper-safety-heading">Execution boundary</h2>
              </div>
              <span className="paper-mode-badge">
                {baseActionReady ? "OWNER CONTROL" : "READ ONLY"}
              </span>
            </div>
            <div className="paper-safety-grid">
              <article className={runtime.data.kill_switch.active ? "attention" : ""}>
                <span>Kill switch</span>
                <strong>{runtime.data.kill_switch.active ? "ACTIVE" : "inactive"}</strong>
                <small>scope: {runtime.data.kill_switch.scope}</small>
                {runtime.data.kill_switch.cancellation_pending ? (
                  <small>pending entry cancellation requires owner recovery</small>
                ) : null}
              </article>
              <article className={runtime.data.blocked_scope_count ? "attention" : ""}>
                <span>Blocked scopes</span>
                <strong>{runtime.data.blocked_scope_count}</strong>
                <small>Paper Execution reconciliation truth</small>
              </article>
              <PaperPositionsSafetyCard
                positions={positions.data}
                loading={positions.isPending}
                failed={positions.isError}
              />
              <article>
                <span>Lifecycle controls</span>
                <strong>{baseActionReady ? "owner-backed" : "read-only"}</strong>
                <small>Request replay/idempotency remains owned by Trading Runner.</small>
              </article>
            </div>
            <div className="paper-lifecycle-actions" aria-label="PAPER lifecycle owner actions">
              <button
                type="button"
                disabled={!canStart}
                onClick={() => runLifecycle("START")}
              >
                Start
              </button>
              <button
                type="button"
                disabled={!canStop}
                onClick={() => runLifecycle("STOP")}
              >
                Stop
              </button>
              <button
                type="button"
                className="danger-action"
                disabled={!canKill}
                onClick={() => runLifecycle("ACTIVATE_KILL_SWITCH")}
              >
                Activate kill switch
              </button>
              <button
                type="button"
                disabled={!canReset}
                onClick={() => runLifecycle("RESET_KILL_SWITCH")}
              >
                Reset / recover
              </button>
            </div>
          </section>
        </>
      ) : null}

      <section className="paper-panel" aria-labelledby="paper-orders-heading">
        <div className="paper-panel-heading">
          <div>
            <p className="eyebrow">Paper Execution</p>
            <h2 id="paper-orders-heading">Orders</h2>
          </div>
          <span>{orderCount} visible</span>
        </div>
        {orders.isError || ordersProjectionUnavailable ? (
          <p className="status-error" role="alert" tabIndex={-1} ref={ordersErrorRef}>
            Order projection unavailable.
          </p>
        ) : null}
        {(orders.isError || ordersProjectionUnavailable) && orders.data ? (
          <p className="status-error" role="status">
            Showing the last successful order projection; these rows may be stale.
          </p>
        ) : null}
        {orders.isPending ? <p className="muted-copy" role="status">Loading Paper Execution orders…</p> : null}
        {orders.data?.orders.length ? (
          <div className="paper-order-list" role="list" aria-label="Paper orders">
            {orders.data.orders.map((order) => (
              <article className="paper-order-card" role="listitem" key={order.client_order_id}>
                <div className="paper-order-title">
                  <strong>{order.instrument}</strong>
                  <span>{order.native_state}</span>
                </div>
                <dl>
                  <div><dt>Role</dt><dd>{order.intent_role}</dd></div>
                  <div><dt>Client ID</dt><dd className="mono-wrap">{order.client_order_id}</dd></div>
                  <div><dt>Core ID</dt><dd className="mono-wrap">{order.core_order_id}</dd></div>
                  <div><dt>Quantity</dt><dd>{order.filled_quantity} / {order.requested_quantity}</dd></div>
                  <div><dt>Remaining</dt><dd>{order.remaining_quantity}</dd></div>
                  <div><dt>Avg fill</dt><dd>{order.average_fill_price ?? "—"}</dd></div>
                  <div><dt>Fees</dt><dd>{order.fees_paid}</dd></div>
                  <div><dt>Last event</dt><dd>{formatUtc(order.last_event_timestamp)}</dd></div>
                </dl>
              </article>
            ))}
          </div>
        ) : !orders.isError && !orders.isPending ? (
          <p className="muted-copy">No Paper Execution orders in the current page.</p>
        ) : null}
        <nav
          className="paper-order-pagination"
          aria-label="Paper order pagination"
          tabIndex={-1}
          ref={ordersPaginationRef}
        >
          <span aria-live="polite" aria-atomic="true">
            {orders.isPending
              ? ""
              : orders.isError && !orders.data
                ? "Order range unavailable"
                : orderCount > 0
                  ? `Orders ${firstOrderNumber}–${lastOrderNumber}`
                  : "No orders on this page"}
          </span>
          <button
            type="button"
            ref={previousOrdersButtonRef}
            disabled={ordersOffset === 0 || orders.isPending}
            onClick={() => {
              ordersPaginationFocusRef.current = "previous";
              setOrdersOffset((current) => Math.max(0, current - ORDERS_PAGE_LIMIT));
            }}
          >
            Previous
          </button>
          <button
            type="button"
            ref={nextOrdersButtonRef}
            disabled={orders.isPending || orders.isError || orders.data?.has_more !== true}
            onClick={() => {
              ordersPaginationFocusRef.current = "next";
              setOrdersOffset((current) => current + ORDERS_PAGE_LIMIT);
            }}
          >
            Next
          </button>
        </nav>
      </section>

      <PaperPositionsPanel positions={positions.data} loading={positions.isPending} failed={positions.isError} />

      <section className="paper-panel" aria-labelledby="paper-blocked-heading">
        <div className="paper-panel-heading">
          <div>
            <p className="eyebrow">Reconciliation</p>
            <h2 id="paper-blocked-heading">Blocked scopes</h2>
          </div>
        </div>
        {blockedScopes.isError ? (
          <p className="status-error" role="alert">
            Blocked scopes could not be read safely.
            {blockedScopes.data ? " Showing the last successful projection." : ""}
          </p>
        ) : null}
        {blockedScopes.isPending ? (
          <p className="muted-copy" role="status">Loading blocked reconciliation scopes…</p>
        ) : null}
        {blockedScopes.data?.scopes.length ? (
          <ul className="paper-scope-list">
            {blockedScopes.data.scopes.map((scope) => (
              <li key={`${scope.portfolio_id}:${scope.instrument}`}>
                <strong>{scope.portfolio_id}</strong>
                <span>{scope.instrument}</span>
              </li>
            ))}
          </ul>
        ) : !blockedScopes.isError && !blockedScopes.isPending ? (
          <p className="muted-copy">No blocked Paper Execution scopes are reported.</p>
        ) : null}
      </section>
    </section>
  );
}

export function PaperPositionsPanel({
  positions,
  loading,
  failed,
}: {
  positions: PaperPositionsSnapshot | undefined;
  loading: boolean;
  failed: boolean;
}) {
  const [filters, dispatchFilters] = useReducer(
    paperPositionFiltersReducer,
    EMPTY_PAPER_POSITION_FILTERS,
  );
  const available = positions?.status === "available" ? positions : null;
  const canExportSnapshot =
    available?.coverage === "complete" && !loading && !failed;
  const allRows = available?.positions ?? [];
  const strategyIds = Array.from(new Set(
    allRows.flatMap((position) => position.strategy_id === null ? [] : [position.strategy_id]),
  )).sort();
  const hasUnknownAttribution = allRows.some((position) => position.strategy_id === null);
  const matchingRows = filterPaperPositions(allRows, filters);
  const filtersActive =
    filters.symbol.trim().length > 0 || filters.side !== "all" || filters.strategy !== "all";
  return (
    <section className="paper-panel" aria-labelledby="paper-positions-heading">
      <div className="paper-panel-heading">
        <div>
          <p className="eyebrow">Paper Execution owner snapshot</p>
          <h2 id="paper-positions-heading">Positions</h2>
        </div>
        <button
          type="button"
          className="secondary-action"
          disabled={!canExportSnapshot}
          aria-describedby="paper-position-export-hint"
          onClick={() => {
            if (available && canExportSnapshot) downloadPaperPositionsSnapshot(available);
          }}
        >
          Download complete snapshot (JSON)
        </button>
        {available ? (
          <span aria-label="Full snapshot status and count">
            Snapshot available · {available.positions.length} owner positions
          </span>
        ) : null}
      </div>
      {loading ? <p className="muted-copy" role="status">Loading owner positions…</p> : null}
      {failed ? (
        <p className="status-error" role="alert">
          {available
            ? "The latest position refresh failed. The rows below are the last-known owner snapshot."
            : "Owner positions could not be read safely."}
        </p>
      ) : null}
      {positions?.status === "unavailable" ? (
        <p className="muted-copy" role="status">Positions unavailable ({positions.reason_code}).</p>
      ) : null}
      {positions?.status === "recovering" ? (
        <p className="status-warning" role="status">Positions are recovering ({positions.reason_code}); no flat-state inference is available.</p>
      ) : null}
      {positions?.status === "stale" ? (
        <p className="status-warning" role="status">The owner marks this positions snapshot stale ({positions.reason_code}).</p>
      ) : null}
      <p id="paper-position-export-hint" className="muted-copy">
        Export requires a complete available snapshot and a successful, idle refresh. It includes all owner rows, regardless of filters.
      </p>
      {available ? (
        <>
          <dl className="paper-summary-grid" aria-label="Position snapshot scope">
            <div><dt>Owner</dt><dd className="mono-wrap">{available.owner_id}</dd></div>
            <div><dt>Composition</dt><dd className="mono-wrap">{available.composition_id}</dd></div>
            <div><dt>Portfolio</dt><dd className="mono-wrap">{available.portfolio_id}</dd></div>
            <div><dt>Generation</dt><dd className="mono-wrap">{available.owner_generation}</dd></div>
            <div><dt>Membership</dt><dd className="mono-wrap">{available.membership_version}</dd></div>
            <div><dt>State version</dt><dd className="mono-wrap">{available.state_version}</dd></div>
            <div><dt>Snapshot version</dt><dd className="mono-wrap">{available.snapshot_version}</dd></div>
            <div><dt>Observed</dt><dd><time dateTime={available.as_of}>{formatUtc(available.as_of)}</time></dd></div>
          </dl>
          <section className="paper-position-filters" aria-label="Position filters">
            <label>
              <span>Symbol / instrument ID</span>
              <input
                type="search"
                value={filters.symbol}
                onChange={(event) => dispatchFilters({ type: "symbol", value: event.target.value })}
                placeholder="Filter symbol"
              />
            </label>
            <label>
              <span>Side</span>
              <select
                value={filters.side}
                onChange={(event) => dispatchFilters({ type: "side", value: event.target.value as PaperPositionSideFilter })}
              >
                <option value="all">All sides</option>
                <option value="long">Long</option>
                <option value="short">Short</option>
              </select>
            </label>
            <label>
              <span>Known strategy attribution</span>
              <select
                value={filters.strategy}
                onChange={(event) => dispatchFilters({ type: "strategy", value: event.target.value as PaperPositionStrategyFilter })}
              >
                <option value="all">All attribution</option>
                {strategyIds.map((strategyId) => (
                  <option key={strategyId} value={`known:${strategyId}`}>{strategyId}</option>
                ))}
                {hasUnknownAttribution ? <option value="unknown">Unknown attribution</option> : null}
              </select>
            </label>
            <button
              type="button"
              className="secondary-action"
              disabled={!filtersActive}
              onClick={() => dispatchFilters({ type: "reset" })}
            >
              Clear filters
            </button>
          </section>
          <PaperPositionsResults positions={allRows} matchingRows={matchingRows} />
        </>
      ) : null}
    </section>
  );
}

export function filterPaperPositions(
  positions: PaperPositionRow[],
  filters: PaperPositionFilters,
) {
  const symbolNeedle = filters.symbol.trim().toLowerCase();
  return positions.filter((position) => {
    if (symbolNeedle && !position.instrument_id.toLowerCase().includes(symbolNeedle)) {
      return false;
    }
    if (filters.side !== "all" && position.side !== filters.side) return false;
    if (filters.strategy === "unknown" && position.strategy_id !== null) return false;
    if (
      filters.strategy.startsWith("known:") &&
      position.strategy_id !== filters.strategy.slice("known:".length)
    ) {
      return false;
    }
    return true;
  });
}

export function PaperPositionsResults({
  positions,
  matchingRows,
}: {
  positions: PaperPositionRow[];
  matchingRows: PaperPositionRow[];
}) {
  return (
    <>
      <p className="muted-copy" role="status">
        Showing {matchingRows.length} matching rows from {positions.length} owner positions.
      </p>
      {positions.length === 0 ? (
        <p className="muted-copy">No open positions.</p>
      ) : matchingRows.length === 0 ? (
        <p className="muted-copy" role="status">No positions match the current filters.</p>
      ) : (
        <div className="paper-position-list" role="list" aria-label="PAPER positions">
          {matchingRows.map((position) => (
            <article className="paper-order-card" role="listitem" key={position.owner_position_id}>
              <div className="paper-order-title">
                <strong>{position.instrument_id}</strong>
                <span>{position.side}</span>
              </div>
              <dl>
                <div><dt>Owner position</dt><dd className="mono-wrap">{position.owner_position_id}</dd></div>
                <div><dt>Instrument version</dt><dd className="mono-wrap">{position.instrument_version}</dd></div>
                <div><dt>Quantity</dt><dd>{position.quantity} {position.quantity_unit}</dd></div>
                <div>
                  <dt>Strategy attribution</dt>
                  <dd className="mono-wrap">
                    {position.strategy_id ?? "Unknown attribution (owner supplied no strategy ID)"}
                  </dd>
                </div>
              </dl>
            </article>
          ))}
        </div>
      )}
    </>
  );
}

function PaperPositionsSafetyCard({
  positions,
  loading,
  failed,
}: {
  positions: PaperPositionsSnapshot | undefined;
  loading: boolean;
  failed: boolean;
}) {
  let state = "unavailable";
  let detail = "No complete owner snapshot is available.";

  if (loading && positions === undefined) {
    state = "recovering";
    detail = "Loading the owner snapshot.";
  } else if (failed) {
    state = positions === undefined ? "unavailable" : "stale";
    detail = positions === undefined
      ? "Owner snapshot could not be refreshed."
      : "Showing last-known owner snapshot; refresh failed.";
  } else if (positions?.status === "available") {
    state = positions.positions.length === 0 ? "complete-empty" : "available";
    detail = `${positions.positions.length} owner position(s); as of ${positions.as_of}.`;
  } else if (positions?.status === "recovering") {
    state = "recovering";
    detail = `Owner recovery: ${positions.reason_code}.`;
  } else if (positions?.status === "stale") {
    state = "stale";
    detail = `Owner marks snapshot stale: ${positions.reason_code}.`;
  } else if (positions?.status === "unavailable") {
    detail = `Owner snapshot unavailable: ${positions.reason_code}.`;
  }

  return (
    <article aria-label="Position snapshot status">
      <span>Positions</span>
      <strong>{state}</strong>
      <small>{detail}</small>
    </article>
  );
}

function refreshPaper(queryClient: ReturnType<typeof useQueryClient>): void {
  void queryClient.invalidateQueries({ queryKey: ["control-health", "paper_runtime"] });
  void queryClient.invalidateQueries({ queryKey: ["paper-runtime"] });
  void queryClient.invalidateQueries({ queryKey: ["paper-lifecycle-capability"] });
  void queryClient.invalidateQueries({ queryKey: ["paper-orders"] });
  void queryClient.invalidateQueries({ queryKey: ["paper-positions"] });
  void queryClient.invalidateQueries({ queryKey: ["paper-blocked-scopes"] });
}

function newRequestId(mutation: PaperLifecycleMutation): string {
  return `dashboard-paper-${mutation.toLowerCase().replaceAll("_", "-")}-${crypto.randomUUID()}`;
}

function mutationLabel(mutation: PaperLifecycleMutation): string {
  return {
    START: "Start",
    STOP: "Stop",
    ACTIVATE_KILL_SWITCH: "Activate kill switch",
    RESET_KILL_SWITCH: "Reset/recover kill switch",
  }[mutation];
}

function LifecycleReceipt({ result }: { result: PaperLifecycleMutationResult }) {
  return (
    <div className="paper-lifecycle-receipt" role="status">
      <strong>{result.mutation} accepted</strong>
      <span>{result.previous_state} → {result.new_state}</span>
      <small>
        {result.replayed ? "durable owner replay of the same request" : `request ${result.request_id}`}
      </small>
    </div>
  );
}

function PaperNotice({ title, text }: { title: string; text: string }) {
  return <div className="paper-lifecycle-notice" role="note"><strong>{title}</strong><span>{text}</span></div>;
}

function PaperFailure({ error, fallback }: { error: Error; fallback: string }) {
  if (error instanceof PaperRuntimeClientError) {
    return (
      <div className="paper-lifecycle-error" role="alert">
        <strong>{error.code}</strong>
        <span>{error.message}</span>
        {error.action ? <small>{error.action}</small> : null}
      </div>
    );
  }
  return <div className="paper-lifecycle-error" role="alert"><strong>Paper Runtime unavailable</strong><span>{fallback}</span></div>;
}

function formatQueue(depth: number | null, capacity: number | null, high: number | null): string {
  if (depth === null || capacity === null || high === null) return "not attached";
  return `${depth}/${capacity} · high ${high}`;
}

function formatUtc(value: string | null): string {
  if (value === null) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "invalid timestamp";
  return `${new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "medium",
    timeStyle: "medium",
    timeZone: "UTC",
  }).format(date)} UTC`;
}
