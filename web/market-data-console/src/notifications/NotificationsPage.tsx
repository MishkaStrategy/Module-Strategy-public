import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { NotificationClientError, NotificationsControlApiClient } from "./client";
import type { TestNotificationRequest, TestNotificationResult } from "./contracts";
import "./notifications.css";

export interface NotificationsPageProps {
  api: NotificationsControlApiClient;
  readerAuthRevision: number;
  operatorReady: boolean;
}

export function NotificationsPage({
  api,
  readerAuthRevision,
  operatorReady,
}: NotificationsPageProps) {
  const queryClient = useQueryClient();
  const [lastRequest, setLastRequest] = useState<TestNotificationRequest | null>(null);
  const status = useQuery({
    queryKey: ["notifications-status", readerAuthRevision],
    queryFn: ({ signal }) => api.status(signal),
    retry: false,
    refetchInterval: 10_000,
  });
  const mutation = useMutation({
    mutationFn: (request: TestNotificationRequest) => api.testNotification(request),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["notifications-status"] });
      void queryClient.invalidateQueries({ queryKey: ["control-component-health"] });
    },
  });

  const sendNew = () => {
    const request: TestNotificationRequest = {
      schema_version: 1,
      request_id: `dashboard-notification-${crypto.randomUUID()}`,
    };
    setLastRequest(request);
    mutation.mutate(request);
  };
  const replaySame = () => {
    if (lastRequest !== null) mutation.mutate(lastRequest);
  };

  return (
    <section className="dashboard-page notifications-page" aria-label="Notifications workspace">
      <header className="dashboard-page-header">
        <div className="page-title-row">
          <div>
            <p className="eyebrow">Signal Bot owner · fixed safe test</p>
            <h2>Notifications</h2>
          </div>
          <div className="mode-banner compact" aria-label="Текущий режим: PAPER">
            <span aria-hidden="true">●</span>
            PAPER
          </div>
        </div>
        <p>
          Статус и test-notification приходят от Signal Bot owner. Dashboard не видит bot token,
          chat ID, message body или owner storage и не ведёт собственный idempotency ledger.
        </p>
      </header>

      {status.isPending ? <Notice title="Loading owner status…" text="Чтение локальной owner-проекции." /> : null}
      {status.error ? <Failure error={status.error} /> : null}
      {status.data ? (
        <>
          <section className="notifications-summary" aria-label="Notification owner status">
            <Summary label="Status" value={status.data.status.toUpperCase()} />
            <Summary label="Configured" value={yesNo(status.data.configured)} />
            <Summary label="Reachable" value={triState(status.data.reachable)} />
            <Summary
              label="Notifications"
              value={triState(status.data.notifications_enabled, "ENABLED", "DISABLED")}
            />
          </section>
          <section className="control-panel notifications-detail" aria-labelledby="notifications-owner-heading">
            <div className="control-panel-heading">
              <div>
                <p className="eyebrow">Authoritative allowlist projection</p>
                <h3 id="notifications-owner-heading">Signal Bot owner</h3>
              </div>
            </div>
            <dl className="notifications-details">
              <Detail label="Cycles" value={String(status.data.cycles)} />
              <Detail label="Signals sent" value={String(status.data.signals_sent)} />
              <Detail label="Last success" value={formatUtc(status.data.last_success_at)} />
              <Detail label="Last error type" value={status.data.last_error_type ?? "—"} />
            </dl>
          </section>
        </>
      ) : null}

      <section className="control-panel notifications-test" aria-labelledby="notifications-test-heading">
        <div className="control-panel-heading">
          <div>
            <p className="eyebrow">Owner-defined fixed message</p>
            <h3 id="notifications-test-heading">Test notification</h3>
            <p>
              Browser передаёт только bounded request ID. Текст, оператор и Telegram target
              назначаются owner-side и не могут быть изменены этой формой.
            </p>
          </div>
        </div>
        {!operatorReady ? (
          <Notice
            title="Mutation access is locked"
            text="Введите Control operator token в глобальной панели. Downstream Signal Bot credentials остаются на сервере."
          />
        ) : null}
        <div className="notifications-actions">
          <button type="button" disabled={!operatorReady || mutation.isPending} onClick={sendNew}>
            {mutation.isPending ? "Sending…" : "Test notification"}
          </button>
          {lastRequest !== null ? (
            <button
              type="button"
              className="secondary-action"
              disabled={!operatorReady || mutation.isPending}
              onClick={replaySame}
            >
              Replay same request safely
            </button>
          ) : null}
        </div>
        {mutation.data ? <Receipt result={mutation.data} /> : null}
        {mutation.error ? <Failure error={mutation.error} /> : null}
      </section>
    </section>
  );
}

function Receipt({ result }: { result: TestNotificationResult }) {
  return (
    <div className="notifications-receipt" role="status">
      <strong>{result.replayed ? "Owner replayed the accepted request" : "Test notification accepted"}</strong>
      <span>{result.operator}</span>
      <small>
        {result.request_id} · {formatUtc(result.completed_at)}
      </small>
    </div>
  );
}

function Failure({ error }: { error: Error }) {
  if (error instanceof NotificationClientError) {
    const state = error.code.endsWith("RATE_LIMITED")
      ? "Rate limited"
      : error.code.endsWith("UNREACHABLE")
        ? "Owner transport unavailable"
        : error.code.endsWith("RECOVERY_REQUIRED")
          ? "Owner recovery required"
          : "Notification control error";
    return (
      <div className="notifications-error" role="alert">
        <strong>{state}</strong>
        <span>{error.message}</span>
        {error.action ? <small>{error.action}</small> : null}
        {error.errorId ? <small>Error ID: {error.errorId}</small> : null}
      </div>
    );
  }
  return (
    <div className="notifications-error" role="alert">
      <strong>Notifications unavailable</strong>
      <span>The owner projection could not be read safely.</span>
    </div>
  );
}

function Summary({ label, value }: { label: string; value: string }) {
  return (
    <article>
      <span>{label}</span>
      <strong>{value}</strong>
    </article>
  );
}

function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function Notice({ title, text }: { title: string; text: string }) {
  return (
    <div className="notifications-notice" role="note">
      <strong>{title}</strong>
      <span>{text}</span>
    </div>
  );
}

function yesNo(value: boolean): string {
  return value ? "YES" : "NO";
}

function triState(
  value: boolean | null,
  trueLabel = "YES",
  falseLabel = "NO",
): string {
  if (value === null) return "UNKNOWN";
  return value ? trueLabel : falseLabel;
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
