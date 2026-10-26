import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { OperationalEventsClientError, OperationalEventsControlApiClient } from "./client";
import type { OperationalEventSeverity } from "./contracts";
import "./events.css";

const EVENT_TYPES = [
  "runtime.configuring",
  "runtime.started",
  "runtime.failed",
  "runtime.stopping",
  "runtime.stopped",
  "kill_switch.activated",
  "kill_switch.reset",
  "kill_switch.cancellation_retry",
  "operator.lifecycle",
] as const;
const SEVERITIES: OperationalEventSeverity[] = ["INFO", "WARNING", "ERROR"];

export function OperationalEventsPage({
  api,
  readerAuthRevision,
}: {
  api: OperationalEventsControlApiClient;
  readerAuthRevision: number;
}) {
  const [cursor, setCursor] = useState<string | null>(null);
  const [history, setHistory] = useState<string[]>([]);
  const [eventType, setEventType] = useState("");
  const [severity, setSeverity] = useState<OperationalEventSeverity | "">("");
  const [resourceFilter, setResourceFilter] = useState("");
  const query = useQuery({
    queryKey: ["structured-operational-events", readerAuthRevision, cursor, eventType, severity],
    queryFn: ({ signal }) =>
      api.events(
        {
          cursor,
          limit: 100,
          eventTypes: eventType ? [eventType] : [],
          severities: severity ? [severity] : [],
        },
        signal,
      ),
    retry: false,
    refetchInterval: 10_000,
  });
  const visibleEvents = useMemo(() => {
    const normalized = resourceFilter.trim().toLowerCase();
    if (!normalized) return query.data?.events ?? [];
    return (query.data?.events ?? []).filter((event) =>
      event.resource.toLowerCase().includes(normalized),
    );
  }, [query.data?.events, resourceFilter]);

  const resetCursor = () => {
    setCursor(null);
    setHistory([]);
  };
  const updateEventType = (value: string) => {
    setEventType(value);
    resetCursor();
  };
  const updateSeverity = (value: OperationalEventSeverity | "") => {
    setSeverity(value);
    resetCursor();
  };
  const nextPage = () => {
    if (!query.data?.has_more) return;
    setHistory((current) => [...current, cursor ?? ""]);
    setCursor(query.data.next_cursor);
  };
  const previousPage = () => {
    if (!history.length) return;
    const previous = history[history.length - 1];
    setHistory((current) => current.slice(0, -1));
    setCursor(previous || null);
  };

  return (
    <section className="events-workspace" aria-label="Structured operational events workspace">
      <header className="events-header">
        <div>
          <p className="eyebrow">Owner-backed structured events</p>
          <h1>Logs &amp; Events</h1>
          <p>
            Sanitized Trading Runner events from the accepted owner cursor contract. This surface
            never reads arbitrary log files, paths, shell commands or owner databases.
          </p>
        </div>
        <button type="button" className="secondary-action" onClick={() => void query.refetch()}>
          Refresh
        </button>
      </header>

      <div className="events-boundary-note" role="note">
        <strong>Trading Runner source · V1</strong>
        <span>
          Event type and severity are owner-side filters. Resource search applies only to the current
          loaded page because owner contract #391 does not expose a global resource query yet.
        </span>
      </div>

      <section className="events-filters" aria-label="Event filters">
        <label>
          <span>Event type</span>
          <select value={eventType} onChange={(event) => updateEventType(event.target.value)}>
            <option value="">All accepted types</option>
            {EVENT_TYPES.map((value) => (
              <option key={value} value={value}>{value}</option>
            ))}
          </select>
        </label>
        <label>
          <span>Severity</span>
          <select
            value={severity}
            onChange={(event) => updateSeverity(event.target.value as OperationalEventSeverity | "")}
          >
            <option value="">All severities</option>
            {SEVERITIES.map((value) => (
              <option key={value} value={value}>{value}</option>
            ))}
          </select>
        </label>
        <label>
          <span>Resource · current page</span>
          <input
            value={resourceFilter}
            onChange={(event) => setResourceFilter(event.target.value)}
            placeholder="paper-runtime-1"
            maxLength={128}
          />
        </label>
      </section>

      {query.isPending ? <p className="muted-copy">Loading owner events…</p> : null}
      {query.error ? <EventsFailure error={query.error} /> : null}

      {query.data ? (
        <>
          <section className="events-summary" aria-label="Event page summary">
            <span>Source <strong>{query.data.source}</strong></span>
            <span>Visible <strong>{visibleEvents.length}</strong></span>
            <span>Scanned <strong>{query.data.scanned_count}</strong></span>
            <span>Omitted <strong>{query.data.omitted_count}</strong></span>
            <span>Retention <strong>{query.data.retention_limit}</strong></span>
            <span>Truncated <strong>{query.data.truncated ? "yes" : "no"}</strong></span>
          </section>
          <div className="events-list" role="list" aria-label="Structured operational events">
            {visibleEvents.map((event) => (
              <article className="event-card" role="listitem" key={event.event_id}>
                <div className="event-card-heading">
                  <div>
                    <strong>{event.event_type}</strong>
                    <span>{event.resource}</span>
                  </div>
                  <span className={`event-severity event-severity-${event.severity.toLowerCase()}`}>
                    {event.severity}
                  </span>
                </div>
                <dl>
                  <div><dt>Timestamp</dt><dd>{formatUtc(event.timestamp)}</dd></div>
                  <div><dt>State</dt><dd>{event.state ?? "—"}</dd></div>
                  <div><dt>Event ID</dt><dd className="mono-wrap">{event.event_id}</dd></div>
                </dl>
                {Object.keys(event.payload).length ? (
                  <dl className="event-payload">
                    {Object.entries(event.payload).map(([key, value]) => (
                      <div key={key}><dt>{key}</dt><dd>{value === null ? "—" : String(value)}</dd></div>
                    ))}
                  </dl>
                ) : null}
              </article>
            ))}
            {!visibleEvents.length ? <p className="muted-copy">No events match this page.</p> : null}
          </div>
          <nav className="events-pagination" aria-label="Event cursor pagination">
            <button type="button" className="secondary-action" disabled={!history.length} onClick={previousPage}>
              Previous
            </button>
            <button type="button" className="secondary-action" disabled={!query.data.has_more} onClick={nextPage}>
              Next
            </button>
          </nav>
        </>
      ) : null}
    </section>
  );
}

function EventsFailure({ error }: { error: Error }) {
  if (error instanceof OperationalEventsClientError) {
    return (
      <div className="events-error" role="alert">
        <strong>{error.code}</strong>
        <span>{error.message}</span>
        {error.action ? <small>{error.action}</small> : null}
      </div>
    );
  }
  return <div className="events-error" role="alert">Structured operational events are unavailable.</div>;
}

function formatUtc(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "invalid timestamp";
  return `${new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "medium",
    timeStyle: "medium",
    timeZone: "UTC",
  }).format(date)} UTC`;
}
