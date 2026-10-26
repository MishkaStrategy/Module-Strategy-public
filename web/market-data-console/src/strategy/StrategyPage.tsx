import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { ControlApiClientError } from "../control/client";
import type { ControlPlaneApiClient } from "../control/client";
import { StrategyControlApiClient, StrategyControlClientError } from "./client";
import type { StrategyRunSummary } from "./contracts";
import "./strategy.css";

export interface StrategyPageProps {
  controlApi: ControlPlaneApiClient;
  strategyApi: StrategyControlApiClient;
  readerAuthRevision: number;
}

export function StrategyPage({
  controlApi,
  strategyApi,
  readerAuthRevision,
}: StrategyPageProps) {
  const [selectedRunId, setSelectedRunId] = useState("");
  const health = useQuery({
    queryKey: ["control-component-health", readerAuthRevision, "strategy"],
    queryFn: ({ signal }) => controlApi.getComponentHealth("strategy", signal),
    retry: false,
    refetchInterval: 10_000,
  });
  const runs = useQuery({
    queryKey: ["strategy-runs", readerAuthRevision],
    queryFn: ({ signal }) => strategyApi.list(100, 0, signal),
    enabled: health.data?.status === "HEALTHY" || health.data?.status === "DEGRADED",
    retry: false,
    refetchInterval: 10_000,
  });
  const selected = useQuery({
    queryKey: ["strategy-run", readerAuthRevision, selectedRunId],
    queryFn: ({ signal }) => strategyApi.get(selectedRunId, signal),
    enabled: selectedRunId.length > 0,
    retry: false,
  });

  const visibleRuns = runs.data?.runs ?? [];
  const selectedFromList = useMemo(
    () => visibleRuns.find((run) => run.run_id === selectedRunId) ?? null,
    [selectedRunId, visibleRuns],
  );
  const detail = selected.data ?? selectedFromList;

  return (
    <section className="dashboard-page strategy-page">
      <header className="dashboard-page-header">
        <div className="page-title-row">
          <div>
            <p className="eyebrow">Control UI · governance projection</p>
            <h2>Strategies</h2>
          </div>
          <div className="mode-banner compact" aria-label="Текущий режим: PAPER">
            <span aria-hidden="true">●</span>
            PAPER
          </div>
        </div>
        <p>
          Read-only проекция существующих Strategy Factory checkpoints и tamper-evident decision
          chains. Принятая стратегия остаётся неизменяемой: эта поверхность не редактирует baseline,
          не запускает resume/cancel и не создаёт кандидатов.
        </p>
      </header>

      {health.isPending ? (
        <Notice title="Loading Strategy health…" text="Проверяем существующее governance state." />
      ) : null}
      {health.error ? <ControlFailure error={health.error} /> : null}
      {health.data ? (
        <section
          className="control-panel strategy-health-panel"
          aria-label="Strategy integration health"
        >
          <div className="control-panel-heading">
            <div>
              <h3>Governance integrity</h3>
              <p>{health.data.summary}</p>
            </div>
            <HealthPill status={health.data.status} />
          </div>
          {Object.keys(health.data.details).length ? (
            <dl className="control-details">
              {Object.entries(health.data.details).map(([name, value]) => (
                <div key={name}>
                  <dt>{name}</dt>
                  <dd>{value === null ? "—" : String(value)}</dd>
                </div>
              ))}
            </dl>
          ) : null}
          {health.data.status === "UNAVAILABLE" ? (
            <Notice
              title="Strategy Factory state unavailable"
              text="Read-only controls stay disabled until the configured server-owned state root is readable."
              error
            />
          ) : null}
        </section>
      ) : null}

      <section className="strategy-layout" aria-label="Strategy governance workspace">
        <section className="control-panel strategy-runs-panel">
          <div className="control-panel-heading">
            <div>
              <h3>Factory runs</h3>
              <p>Сортировка по server checkpoint updated_at; браузер не реконструирует состояние.</p>
            </div>
            <span className="integration-tag">{visibleRuns.length} visible</span>
          </div>
          {runs.isPending &&
          (health.data?.status === "HEALTHY" || health.data?.status === "DEGRADED") ? (
            <Notice title="Loading Strategy runs…" text="Чтение существующих checkpoints." />
          ) : null}
          {runs.error ? <StrategyFailure error={runs.error} /> : null}
          {runs.data?.truncated ? (
            <Notice
              title="Bounded scan"
              text="Control Plane reached its configured bounded run scan; the list is intentionally incomplete."
              error
            />
          ) : null}
          {runs.data && runs.data.invalid_count > 0 ? (
            <Notice
              title="Integrity warnings"
              text={`${runs.data.invalid_count} run entries failed validation and are not projected as trustworthy state.`}
              error
            />
          ) : null}
          {runs.data && visibleRuns.length === 0 ? (
            <Notice
              title="No readable Strategy runs"
              text="No valid existing Strategy Factory checkpoints are visible."
            />
          ) : null}
          {visibleRuns.length ? (
            <div className="strategy-run-list" role="list" aria-label="Strategy Factory runs">
              {visibleRuns.map((run) => (
                <div key={run.run_id} role="listitem">
                  <button
                    type="button"
                    className={`strategy-run-card${run.run_id === selectedRunId ? " selected" : ""}`}
                    onClick={() => setSelectedRunId(run.run_id)}
                  >
                    <span className="strategy-run-card-heading">
                      <code>{run.run_id}</code>
                      <StatusPill status={run.status} />
                    </span>
                    <span>{formatStage(run.stage)}</span>
                    <small>
                      {run.factory_status} · {formatUtc(run.updated_at)}
                    </small>
                  </button>
                </div>
              ))}
            </div>
          ) : null}
        </section>

        <section className="control-panel strategy-detail-panel">
          <div className="control-panel-heading">
            <div>
              <h3>Governance detail</h3>
              <p>Идентичности и integrity counters без filesystem paths и raw evidence payloads.</p>
            </div>
          </div>
          {!selectedRunId ? (
            <Notice title="No run selected" text="Выберите существующий Factory run слева." />
          ) : null}
          {selected.isPending && selectedRunId ? (
            <Notice title="Validating run…" text={`Run ${selectedRunId}`} />
          ) : null}
          {selected.error ? <StrategyFailure error={selected.error} /> : null}
          {detail ? <StrategyRunDetail run={detail} /> : null}
        </section>
      </section>

      <section
        className="control-panel strategy-immutability-note"
        aria-label="Strategy mutation policy"
      >
        <div className="control-panel-heading">
          <div>
            <h3>Accepted-strategy immutability</h3>
            <p>
              Этот v1-срез намеренно read-only. Изменение принятой стратегии должно создавать новый
              candidate/config identity через проверенный governance workflow; in-place edit здесь
              отсутствует.
            </p>
          </div>
          <span className="integration-tag">read-only v1</span>
        </div>
      </section>
    </section>
  );
}

function StrategyRunDetail({ run }: { run: StrategyRunSummary }) {
  return (
    <div className="strategy-detail-content">
      <div className="strategy-detail-heading">
        <div>
          <span className="eyebrow">Run identity</span>
          <code>{run.run_id}</code>
        </div>
        <StatusPill status={run.status} />
      </div>
      <dl className="strategy-detail-grid">
        <Detail label="Factory status" value={run.factory_status} />
        <Detail label="Stage" value={`${formatStage(run.stage)} · #${run.stage_sequence}`} />
        <Detail label="Updated UTC" value={formatUtc(run.updated_at)} />
        <Detail label="Manifest" value={run.manifest_id} mono />
        <Detail label="Commit" value={shortHash(run.commit_sha)} mono title={run.commit_sha} />
        <Detail
          label="Request fingerprint"
          value={shortHash(run.request_fingerprint)}
          mono
          title={run.request_fingerprint}
        />
        <Detail
          label="Dataset fingerprint"
          value={shortHash(run.dataset_fingerprint)}
          mono
          title={run.dataset_fingerprint}
        />
        <Detail label="Completed artifacts" value={String(run.completed_artifact_count)} />
        <Detail label="Submitted batches" value={String(run.submitted_batch_count)} />
        <Detail label="Processed events" value={String(run.processed_event_count)} />
        <Detail label="Decisions" value={String(run.decision_count)} />
        <Detail
          label="Decision chain"
          value={run.decision_chain_verified ? "verified" : "not verified"}
        />
        <Detail
          label="Decision head"
          value={run.decision_chain_head ? shortHash(run.decision_chain_head) : "—"}
          mono={run.decision_chain_head !== null}
          title={run.decision_chain_head ?? undefined}
        />
      </dl>
    </div>
  );
}

function Detail({
  label,
  value,
  mono = false,
  title,
}: {
  label: string;
  value: string;
  mono?: boolean;
  title?: string | undefined;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd title={title} className={mono ? "mono-value" : undefined}>
        {value}
      </dd>
    </div>
  );
}

function StrategyFailure({ error }: { error: unknown }) {
  if (error instanceof StrategyControlClientError) {
    return (
      <div className="control-notice control-notice-error" role="alert">
        <strong>{error.code}</strong>
        <span>{error.message}</span>
        {error.action ? <span>Action: {error.action}</span> : null}
        {error.errorId ? <small>Error ID: {error.errorId}</small> : null}
      </div>
    );
  }
  return (
    <Notice
      title="CONTROL_STRATEGY_UI_ERROR"
      text="Strategy response could not be validated."
      error
    />
  );
}

function ControlFailure({ error }: { error: unknown }) {
  if (error instanceof ControlApiClientError) {
    return (
      <div className="control-notice control-notice-error" role="alert">
        <strong>{error.code}</strong>
        <span>{error.message}</span>
        {error.action ? <span>Action: {error.action}</span> : null}
      </div>
    );
  }
  return (
    <Notice
      title="CONTROL_UI_ERROR"
      text="Strategy component health could not be read."
      error
    />
  );
}

function Notice({ title, text, error = false }: { title: string; text: string; error?: boolean }) {
  return (
    <div
      className={`control-notice${error ? " control-notice-error" : ""}`}
      role={error ? "alert" : "status"}
    >
      <strong>{title}</strong>
      <span>{text}</span>
    </div>
  );
}

function HealthPill({ status }: { status: "HEALTHY" | "DEGRADED" | "UNAVAILABLE" }) {
  const symbol = status === "HEALTHY" ? "✓" : status === "DEGRADED" ? "!" : "×";
  return (
    <span className={`control-status control-status-${status.toLowerCase()}`}>
      <span aria-hidden="true">{symbol}</span>
      {status}
    </span>
  );
}

function StatusPill({ status }: { status: StrategyRunSummary["status"] }) {
  const kind =
    status === "SUCCEEDED"
      ? "success"
      : status === "FAILED" || status === "CANCELLED"
        ? "error"
        : status === "BLOCKED" || status === "PAUSED"
          ? "warning"
          : "active";
  return <span className={`strategy-status strategy-status-${kind}`}>{status}</span>;
}

function shortHash(value: string): string {
  return value.length <= 16 ? value : `${value.slice(0, 8)}…${value.slice(-8)}`;
}

function formatStage(value: string): string {
  return value
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function formatUtc(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "invalid timestamp";
  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "short",
    timeStyle: "medium",
    timeZone: "UTC",
  }).format(date);
}
