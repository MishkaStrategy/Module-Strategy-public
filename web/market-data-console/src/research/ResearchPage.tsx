import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { ControlApiClientError } from "../control/client";
import type { ControlPlaneApiClient } from "../control/client";
import { ResearchControlApiClient, ResearchControlClientError } from "./client";
import type {
  ResearchControlStatus,
  ResearchExperimentSummary,
  ResearchRunSummary,
} from "./contracts";
import "./research.css";

export interface ResearchPageProps {
  controlApi: ControlPlaneApiClient;
  researchApi: ResearchControlApiClient;
  readerAuthRevision: number;
}

export function ResearchPage({ controlApi, researchApi, readerAuthRevision }: ResearchPageProps) {
  const [selectedExperimentId, setSelectedExperimentId] = useState("");
  const [selectedRunId, setSelectedRunId] = useState("");
  const health = useQuery({
    queryKey: ["control-component-health", readerAuthRevision, "research"],
    queryFn: ({ signal }) => controlApi.getComponentHealth("research", signal),
    retry: false,
    refetchInterval: 10_000,
  });
  const experiments = useQuery({
    queryKey: ["research-experiments", readerAuthRevision],
    queryFn: ({ signal }) => researchApi.listExperiments(100, 0, signal),
    enabled: health.data?.status === "HEALTHY" || health.data?.status === "DEGRADED",
    retry: false,
    refetchInterval: 10_000,
  });
  const runs = useQuery({
    queryKey: ["research-runs", readerAuthRevision, selectedExperimentId],
    queryFn: ({ signal }) => researchApi.listRuns(selectedExperimentId, 100, 0, signal),
    enabled: selectedExperimentId.length > 0,
    retry: false,
    refetchInterval: 10_000,
  });
  const selectedRun = useQuery({
    queryKey: ["research-run", readerAuthRevision, selectedRunId],
    queryFn: ({ signal }) => researchApi.getRun(selectedRunId, signal),
    enabled: selectedRunId.length > 0,
    retry: false,
  });

  const visibleExperiments = experiments.data?.experiments ?? [];
  const selectedExperiment = useMemo(
    () => visibleExperiments.find((item) => item.experiment_id === selectedExperimentId) ?? null,
    [selectedExperimentId, visibleExperiments],
  );
  const visibleRuns = runs.data?.runs ?? [];
  const selectedRunFromList = useMemo(
    () => visibleRuns.find((item) => item.run_id === selectedRunId) ?? null,
    [selectedRunId, visibleRuns],
  );
  const runDetail = selectedRun.data ?? selectedRunFromList;

  const selectExperiment = (experimentId: string) => {
    setSelectedExperimentId(experimentId);
    setSelectedRunId("");
  };

  return (
    <section className="dashboard-page research-page">
      <header className="dashboard-page-header">
        <div className="page-title-row">
          <div>
            <p className="eyebrow">Control UI · durable research projection</p>
            <h2>Research</h2>
          </div>
          <div className="mode-banner compact" aria-label="Текущий режим: PAPER">
            <span aria-hidden="true">●</span>
            PAPER
          </div>
        </div>
        <p>
          Read-only проекция существующей Research Runner SQLite state. Control Plane открывает
          базу только в SQLite mode=ro/query_only и не запускает scheduler, lease claim или schema
          migration.
        </p>
      </header>

      {health.isPending ? <Notice title="Loading Research health…" text="Чтение server truth." /> : null}
      {health.error ? <ControlFailure error={health.error} /> : null}
      {health.data ? (
        <section className="control-panel research-health-panel" aria-label="Research integration health">
          <div className="control-panel-heading">
            <div>
              <h3>Durable state health</h3>
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
        </section>
      ) : null}

      <section className="research-grid" aria-label="Research read-only workspace">
        <section className="control-panel research-experiments-panel">
          <PanelHeading title="Experiments" text="Native status и operator projection остаются раздельными." />
          {experiments.isPending && health.data?.status !== "UNAVAILABLE" ? (
            <Notice title="Loading experiments…" text="Чтение существующей durable state." />
          ) : null}
          {experiments.error ? <ResearchFailure error={experiments.error} /> : null}
          {experiments.data?.has_more ? (
            <Notice title="Bounded list" text="Показаны первые 100 experiments; server сообщает, что записи ещё есть." />
          ) : null}
          {experiments.data && visibleExperiments.length === 0 ? (
            <Notice title="No experiments" text="В существующей Research state нет experiments." />
          ) : null}
          {visibleExperiments.length ? (
            <div className="research-list" role="list" aria-label="Research experiments">
              {visibleExperiments.map((experiment) => (
                <div role="listitem" key={experiment.experiment_id}>
                  <button
                    type="button"
                    className={`research-card${experiment.experiment_id === selectedExperimentId ? " selected" : ""}`}
                    onClick={() => selectExperiment(experiment.experiment_id)}
                  >
                    <span className="research-card-heading">
                      <code>{experiment.experiment_id}</code>
                      <StatusPill status={experiment.status} />
                    </span>
                    <span>{experiment.native_status}</span>
                    <small>{formatUtc(experiment.updated_at)}</small>
                    {experiment.has_failures ? <strong className="research-partial">completed with failures</strong> : null}
                  </button>
                </div>
              ))}
            </div>
          ) : null}
        </section>

        <section className="control-panel research-runs-panel">
          <PanelHeading
            title="Runs"
            text={selectedExperiment ? `Experiment ${selectedExperiment.experiment_id}` : "Выберите experiment слева."}
          />
          {selectedExperiment ? <ExperimentSummary experiment={selectedExperiment} /> : null}
          {runs.isPending && selectedExperimentId ? <Notice title="Loading runs…" text="Чтение native run state." /> : null}
          {runs.error ? <ResearchFailure error={runs.error} /> : null}
          {runs.data?.has_more ? (
            <Notice title="Bounded run list" text="Показаны первые 100 runs; server сообщает, что записи ещё есть." />
          ) : null}
          {runs.data && visibleRuns.length === 0 ? <Notice title="No runs" text="У experiment нет зарегистрированных runs." /> : null}
          {visibleRuns.length ? (
            <div className="research-list" role="list" aria-label="Research runs">
              {visibleRuns.map((run) => (
                <div role="listitem" key={run.run_id}>
                  <button
                    type="button"
                    className={`research-card${run.run_id === selectedRunId ? " selected" : ""}`}
                    onClick={() => setSelectedRunId(run.run_id)}
                  >
                    <span className="research-card-heading">
                      <code>{run.run_id}</code>
                      <StatusPill status={run.status} />
                    </span>
                    <span>{run.native_status} · {run.stage_role}</span>
                    <small>{run.stage_id} · {run.split_id}</small>
                  </button>
                </div>
              ))}
            </div>
          ) : null}
        </section>

        <section className="control-panel research-detail-panel">
          <PanelHeading title="Run detail" text="Safe identities/counters only — без paths, parameters, metadata и raw failure message." />
          {!selectedRunId ? <Notice title="No run selected" text="Выберите run для безопасной детализации." /> : null}
          {selectedRun.isPending && selectedRunId ? <Notice title="Validating run…" text={`Run ${selectedRunId}`} /> : null}
          {selectedRun.error ? <ResearchFailure error={selectedRun.error} /> : null}
          {runDetail ? <RunDetail run={runDetail} /> : null}
        </section>
      </section>

      <section className="control-panel research-readonly-note" aria-label="Research mutation policy">
        <div className="control-panel-heading">
          <div>
            <h3>Read-only v1</h3>
            <p>
              Start, Pause, Cancel, Retry, lease ownership и scheduler control намеренно отсутствуют.
              Они могут появиться только через отдельный idempotent/audited lifecycle contract владельца Research Runner.
            </p>
          </div>
          <span className="integration-tag">no mutation</span>
        </div>
      </section>
    </section>
  );
}

function ExperimentSummary({ experiment }: { experiment: ResearchExperimentSummary }) {
  return (
    <dl className="research-summary-grid">
      <Detail label="Native status" value={experiment.native_status} />
      <Detail label="Projected status" value={experiment.status} />
      <Detail label="Updated UTC" value={formatUtc(experiment.updated_at)} />
      <Detail label="Specification" value={shortHash(experiment.specification_sha256)} mono title={experiment.specification_sha256} />
      {Object.entries(experiment.run_counts).map(([status, count]) => (
        <Detail key={status} label={`Runs · ${status}`} value={String(count)} />
      ))}
    </dl>
  );
}

function RunDetail({ run }: { run: ResearchRunSummary }) {
  return (
    <div className="research-detail-content">
      <div className="research-detail-heading">
        <code>{run.run_id}</code>
        <StatusPill status={run.status} />
      </div>
      <dl className="research-summary-grid">
        <Detail label="Native status" value={run.native_status} />
        <Detail label="Stage" value={`${run.stage_id} · ${run.stage_role}`} />
        <Detail label="Split" value={run.split_id} />
        <Detail label="Dataset" value={run.dataset_id} />
        <Detail label="Scenario" value={run.scenario_id} />
        <Detail label="Repetition" value={String(run.repetition_index)} />
        <Detail label="Attempts" value={`${run.attempt_count} / ${run.max_attempts}`} />
        <Detail label="Revision" value={String(run.revision)} />
        <Detail label="Updated UTC" value={formatUtc(run.updated_at)} />
        {run.lease_expires_at ? <Detail label="Lease expires UTC" value={formatUtc(run.lease_expires_at)} /> : null}
        {run.failure_category ? <Detail label="Failure category" value={run.failure_category} /> : null}
        {run.failure_retryable !== null ? <Detail label="Failure retryable" value={run.failure_retryable ? "yes" : "no"} /> : null}
        {run.engine_run_id ? <Detail label="Engine run" value={run.engine_run_id} mono /> : null}
        {run.bars_processed !== null ? <Detail label="Bars processed" value={String(run.bars_processed)} /> : null}
        {run.restart_verified !== null ? <Detail label="Restart verified" value={run.restart_verified ? "yes" : "no"} /> : null}
      </dl>
    </div>
  );
}

function PanelHeading({ title, text }: { title: string; text: string }) {
  return (
    <div className="control-panel-heading">
      <div>
        <h3>{title}</h3>
        <p>{text}</p>
      </div>
    </div>
  );
}

function Detail({ label, value, mono = false, title }: { label: string; value: string; mono?: boolean; title?: string | undefined }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd className={mono ? "mono-value" : undefined} title={title}>{value}</dd>
    </div>
  );
}

function ResearchFailure({ error }: { error: unknown }) {
  if (error instanceof ResearchControlClientError) {
    return (
      <div className="control-notice control-notice-error" role="alert">
        <strong>{error.code}</strong>
        <span>{error.message}</span>
        {error.action ? <span>Action: {error.action}</span> : null}
        {error.errorId ? <small>Error ID: {error.errorId}</small> : null}
      </div>
    );
  }
  return <Notice title="CONTROL_RESEARCH_UI_ERROR" text="Research response could not be validated." error />;
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
  return <Notice title="CONTROL_UI_ERROR" text="Research component health could not be read." error />;
}

function Notice({ title, text, error = false }: { title: string; text: string; error?: boolean }) {
  return (
    <div className={`control-notice${error ? " control-notice-error" : ""}`} role={error ? "alert" : "status"}>
      <strong>{title}</strong>
      <span>{text}</span>
    </div>
  );
}

function HealthPill({ status }: { status: "HEALTHY" | "DEGRADED" | "UNAVAILABLE" }) {
  const symbol = status === "HEALTHY" ? "✓" : status === "DEGRADED" ? "!" : "×";
  return <span className={`control-status control-status-${status.toLowerCase()}`}><span aria-hidden="true">{symbol}</span>{status}</span>;
}

function StatusPill({ status }: { status: ResearchControlStatus }) {
  const kind = status === "SUCCEEDED" ? "success" : status === "FAILED" || status === "CANCELLED" ? "error" : status === "BLOCKED" || status === "PAUSED" ? "warning" : "active";
  return <span className={`research-status research-status-${kind}`}>{status}</span>;
}

function shortHash(value: string): string {
  return value.length <= 18 ? value : `${value.slice(0, 8)}…${value.slice(-8)}`;
}

function formatUtc(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "invalid timestamp";
  return new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "medium", timeZone: "UTC" }).format(date);
}
