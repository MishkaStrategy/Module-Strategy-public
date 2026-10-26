import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import type { FormEvent } from "react";

import { ControlApiClientError } from "../control/client";
import type { ControlPlaneApiClient } from "../control/client";
import { BacktestControlApiClient, BacktestControlClientError } from "./client";
import type {
  BacktestComparison,
  BacktestJobRecord,
  BacktestJobStatus,
  BacktestMetricScalar,
  BacktestResultSummary,
} from "./contracts";

const CONFIG_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const ACTIVE_STATUSES = new Set<BacktestJobStatus>([
  "QUEUED",
  "STARTING",
  "RUNNING",
  "PAUSING",
  "PAUSED",
  "STOPPING",
  "RECOVERING",
  "BLOCKED",
]);

interface BacktestSubmission {
  requestedConfigId: string;
  idempotencyKey: string;
}

export interface BacktestPageProps {
  controlApi: ControlPlaneApiClient;
  backtestApi: BacktestControlApiClient;
  readerAuthRevision: number;
  operatorReady: boolean;
}

export function BacktestPage({
  controlApi,
  backtestApi,
  readerAuthRevision,
  operatorReady,
}: BacktestPageProps) {
  const queryClient = useQueryClient();
  const [configId, setConfigId] = useState("");
  const [selectedJobId, setSelectedJobId] = useState("");
  const [leftJobId, setLeftJobId] = useState("");
  const [rightJobId, setRightJobId] = useState("");
  const [lastSubmission, setLastSubmission] = useState<BacktestSubmission | null>(null);

  const health = useQuery({
    queryKey: ["control-component-health", readerAuthRevision, "backtest"],
    queryFn: ({ signal }) => controlApi.getComponentHealth("backtest", signal),
    retry: false,
    refetchInterval: 10_000,
  });

  const jobs = useQuery({
    queryKey: ["backtest-jobs", readerAuthRevision],
    queryFn: ({ signal }) => backtestApi.list(signal),
    enabled: health.data?.status === "HEALTHY",
    retry: false,
    refetchInterval: 5_000,
  });

  const result = useQuery({
    queryKey: ["backtest-result", readerAuthRevision, selectedJobId],
    queryFn: ({ signal }) => backtestApi.result(selectedJobId, signal),
    enabled: selectedJobId.length > 0,
    retry: false,
  });

  const createRun = useMutation({
    mutationFn: ({ requestedConfigId, idempotencyKey }: BacktestSubmission) =>
      backtestApi.create(requestedConfigId, idempotencyKey),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["backtest-jobs"] });
    },
  });

  const compare = useMutation({
    mutationFn: ({ left, right }: { left: string; right: string }) =>
      backtestApi.compare(left, right),
  });

  const records = jobs.data ?? [];
  const succeededJobs = useMemo(
    () =>
      records.filter(
        (record) => record.job.status === "SUCCEEDED" && record.result !== null,
      ),
    [records],
  );
  const normalizedConfigId = configId.trim();
  const canSubmit =
    health.data?.status === "HEALTHY" &&
    operatorReady &&
    CONFIG_ID_PATTERN.test(normalizedConfigId) &&
    !createRun.isPending;
  const canCompare =
    leftJobId.length > 0 &&
    rightJobId.length > 0 &&
    leftJobId !== rightJobId &&
    !compare.isPending;
  const canRetrySameRequest =
    operatorReady &&
    !createRun.isPending &&
    lastSubmission !== null &&
    createRun.error instanceof BacktestControlClientError &&
    createRun.error.retryable;

  const queueSubmission = (submission: BacktestSubmission) => {
    setLastSubmission(submission);
    createRun.mutate(submission);
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canSubmit) return;
    queueSubmission({
      requestedConfigId: normalizedConfigId,
      idempotencyKey: `dashboard-${crypto.randomUUID()}`,
    });
  };

  return (
    <section className="dashboard-page backtest-page">
      <header className="dashboard-page-header">
        <div className="page-title-row">
          <div>
            <p className="eyebrow">Control UI · local-only</p>
            <h2>Backtests</h2>
          </div>
          <div className="mode-banner compact" aria-label="Текущий режим: PAPER">
            <span aria-hidden="true">●</span>
            PAPER
          </div>
        </div>
        <p>
          Durable orchestration поверх существующего deterministic Backtest Engine. Браузер передаёт
          только зарегистрированный config ID; filesystem paths, shell-команды и Live trading здесь
          отсутствуют.
        </p>
      </header>

      {health.isPending ? (
        <Notice title="Loading Backtest health…" text="Чтение серверной проекции." />
      ) : null}
      {health.error ? <ControlFailure error={health.error} /> : null}
      {health.data ? (
        <section
          className="control-panel backtest-health-panel"
          aria-label="Backtest integration health"
        >
          <div className="control-panel-heading">
            <div>
              <h3>Integration health</h3>
              <p>{health.data.summary}</p>
            </div>
            <HealthPill status={health.data.status} />
          </div>
          {health.data.status !== "HEALTHY" ? (
            <Notice
              title="Backtest Control unavailable"
              text="Run controls remain disabled until the server-owned Backtest adapter is healthy."
              error
            />
          ) : null}
        </section>
      ) : null}

      <section className="backtest-layout" aria-label="Backtest control workspace">
        <section className="control-panel backtest-run-panel">
          <div className="control-panel-heading">
            <div>
              <h3>Queue deterministic run</h3>
              <p>
                Только server-owned registered config identity. Idempotency key создаётся в памяти.
              </p>
            </div>
          </div>
          <form className="backtest-run-form" onSubmit={submit}>
            <label>
              <span>Config ID</span>
              <input
                value={configId}
                onChange={(event) => setConfigId(event.target.value)}
                placeholder="baseline_btc_15m"
                autoComplete="off"
                spellCheck={false}
                maxLength={128}
              />
            </label>
            <button type="submit" disabled={!canSubmit}>
              {createRun.isPending ? "Queueing…" : "Queue backtest"}
            </button>
          </form>
          {!operatorReady ? (
            <p className="backtest-inline-hint">
              Введите <strong>Control operator token</strong> в глобальной панели слева, чтобы
              разрешить mutation. Токен хранится только в памяти страницы.
            </p>
          ) : null}
          {normalizedConfigId && !CONFIG_ID_PATTERN.test(normalizedConfigId) ? (
            <p className="backtest-inline-error" role="alert">
              Config ID может содержать только буквы, цифры, точку, подчёркивание и дефис.
            </p>
          ) : null}
          {createRun.error ? <BacktestFailure error={createRun.error} /> : null}
          {canRetrySameRequest && lastSubmission ? (
            <button
              type="button"
              className="backtest-link-button backtest-retry-button"
              onClick={() => queueSubmission(lastSubmission)}
            >
              Retry same request
            </button>
          ) : null}
          {createRun.data ? (
            <Notice
              title={createRun.data.created ? "Backtest queued" : "Existing durable request reused"}
              text={`Job ${createRun.data.record.job.job_id} · ${createRun.data.record.job.status}`}
            />
          ) : null}
        </section>

        <section className="control-panel backtest-jobs-panel">
          <div className="control-panel-heading">
            <div>
              <h3>Durable jobs</h3>
              <p>Состояние читается из серверного SQLite store и обновляется каждые 5 секунд.</p>
            </div>
            <span className="integration-tag">{records.length} jobs</span>
          </div>
          {jobs.isPending && health.data?.status === "HEALTHY" ? (
            <Notice title="Loading durable jobs…" text="Запрашиваем Backtest Control Plane." />
          ) : null}
          {jobs.error ? <BacktestFailure error={jobs.error} /> : null}
          {jobs.data && records.length === 0 ? (
            <Notice
              title="No durable Backtest jobs"
              text="Queue the first registered configuration above."
            />
          ) : null}
          {records.length > 0 ? (
            <div className="backtest-table-scroll">
              <table className="backtest-table">
                <thead>
                  <tr>
                    <th>Job</th>
                    <th>Config</th>
                    <th>Status</th>
                    <th>Progress</th>
                    <th>Updated</th>
                    <th>Result</th>
                  </tr>
                </thead>
                <tbody>
                  {records.map((record) => (
                    <BacktestJobRow
                      record={record}
                      key={record.job.job_id}
                      selected={record.job.job_id === selectedJobId}
                      onSelectResult={setSelectedJobId}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </section>
      </section>

      <section className="backtest-result-grid" aria-label="Backtest results and comparison">
        <section className="control-panel">
          <div className="control-panel-heading">
            <div>
              <h3>Result metrics</h3>
              <p>Метрики читаются из engine result/manifest с проверкой durable run identity.</p>
            </div>
          </div>
          {!selectedJobId ? (
            <Notice
              title="No result selected"
              text="Выберите View result у завершённого durable job."
            />
          ) : null}
          {result.isPending && selectedJobId ? (
            <Notice title="Loading result…" text={`Job ${selectedJobId}`} />
          ) : null}
          {result.error ? <BacktestFailure error={result.error} /> : null}
          {result.data ? <ResultMetrics result={result.data} /> : null}
        </section>

        <section className="control-panel">
          <div className="control-panel-heading">
            <div>
              <h3>A/B comparison</h3>
              <p>Сравниваются только два успешно завершённых durable job identity.</p>
            </div>
          </div>
          <div className="backtest-compare-controls">
            <JobSelect
              label="Left job"
              value={leftJobId}
              jobs={succeededJobs}
              onChange={setLeftJobId}
            />
            <JobSelect
              label="Right job"
              value={rightJobId}
              jobs={succeededJobs}
              onChange={setRightJobId}
            />
            <button
              type="button"
              disabled={!canCompare}
              onClick={() => compare.mutate({ left: leftJobId, right: rightJobId })}
            >
              {compare.isPending ? "Comparing…" : "Compare"}
            </button>
          </div>
          {succeededJobs.length < 2 ? (
            <p className="backtest-inline-hint">
              Для A/B comparison нужны минимум два успешных запуска.
            </p>
          ) : null}
          {leftJobId && leftJobId === rightJobId ? (
            <p className="backtest-inline-error" role="alert">
              Выберите два разных job.
            </p>
          ) : null}
          {compare.error ? <BacktestFailure error={compare.error} /> : null}
          {compare.data ? <ComparisonTable comparison={compare.data} /> : null}
        </section>
      </section>
    </section>
  );
}

function BacktestJobRow({
  record,
  selected,
  onSelectResult,
}: {
  record: BacktestJobRecord;
  selected: boolean;
  onSelectResult: (jobId: string) => void;
}) {
  const progress = record.job.progress;
  const progressText = progress
    ? progress.determinate && progress.percent !== null
      ? `${formatNumber(progress.percent)}% · ${progress.phase}`
      : `${progress.phase} · indeterminate`
    : record.job.phase;
  return (
    <tr className={selected ? "backtest-row-selected" : undefined}>
      <td>
        <code>{record.job.job_id}</code>
      </td>
      <td>
        <code>{record.config_id}</code>
      </td>
      <td>
        <JobStatusPill status={record.job.status} />
      </td>
      <td>
        <span>{progressText}</span>
        {progress?.message ? <small>{progress.message}</small> : null}
        {record.job.error ? (
          <small className="backtest-job-error">{record.job.error.code}</small>
        ) : null}
      </td>
      <td>{formatUtc(record.job.updated_at)}</td>
      <td>
        {record.job.status === "SUCCEEDED" && record.result ? (
          <button
            type="button"
            className="backtest-link-button"
            onClick={() => onSelectResult(record.job.job_id)}
          >
            View result
          </button>
        ) : (
          <span aria-label="No result available">—</span>
        )}
      </td>
    </tr>
  );
}

function ResultMetrics({ result }: { result: BacktestResultSummary }) {
  const metrics = Object.entries(result.metrics);
  return (
    <div>
      <dl className="backtest-result-summary">
        <div>
          <dt>Run ID</dt>
          <dd>
            <code>{result.run_id}</code>
          </dd>
        </div>
        <div>
          <dt>Bars processed</dt>
          <dd>{result.bars_processed}</dd>
        </div>
        <div>
          <dt>Restart verified</dt>
          <dd>{result.restart_verified ? "yes" : "no"}</dd>
        </div>
      </dl>
      {metrics.length ? (
        <div className="backtest-table-scroll">
          <table className="backtest-table backtest-metrics-table">
            <thead>
              <tr>
                <th>Metric</th>
                <th>Value</th>
              </tr>
            </thead>
            <tbody>
              {metrics.map(([name, value]) => (
                <tr key={name}>
                  <td>{name}</td>
                  <td>{formatScalar(value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Notice
          title="No scalar metrics"
          text="Engine result identity is valid, but no scalar metrics were projected."
        />
      )}
    </div>
  );
}

function ComparisonTable({ comparison }: { comparison: BacktestComparison }) {
  const changes = Object.entries(comparison.changes);
  return (
    <div className="backtest-comparison-result">
      <div className="backtest-comparison-identities">
        <span>
          <strong>Left:</strong> <code>{comparison.left_run_id}</code>
        </span>
        <span>
          <strong>Right:</strong> <code>{comparison.right_run_id}</code>
        </span>
      </div>
      {changes.length ? (
        <div className="backtest-table-scroll">
          <table className="backtest-table">
            <thead>
              <tr>
                <th>Metric</th>
                <th>Left</th>
                <th>Right</th>
                <th>Delta</th>
              </tr>
            </thead>
            <tbody>
              {changes.map(([name, change]) => (
                <tr key={name}>
                  <td>{name}</td>
                  <td>{formatScalar(change.left)}</td>
                  <td>{formatScalar(change.right)}</td>
                  <td>{change.delta === null ? "—" : formatNumber(change.delta)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Notice
          title="No comparable scalar changes"
          text="The engine returned no scalar metric deltas."
        />
      )}
    </div>
  );
}

function JobSelect({
  label,
  value,
  jobs,
  onChange,
}: {
  label: string;
  value: string;
  jobs: BacktestJobRecord[];
  onChange: (jobId: string) => void;
}) {
  return (
    <label>
      <span>{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">Select durable job</option>
        {jobs.map((record) => (
          <option key={record.job.job_id} value={record.job.job_id}>
            {record.config_id} · {record.job.job_id}
          </option>
        ))}
      </select>
    </label>
  );
}

function BacktestFailure({ error }: { error: unknown }) {
  if (error instanceof BacktestControlClientError) {
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
      title="CONTROL_BACKTEST_UI_ERROR"
      text="Не удалось обработать Backtest Control response."
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
      text="Не удалось прочитать Backtest component health."
      error
    />
  );
}

function Notice({
  title,
  text,
  error = false,
}: {
  title: string;
  text: string;
  error?: boolean;
}) {
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

function JobStatusPill({ status }: { status: BacktestJobStatus }) {
  const active = ACTIVE_STATUSES.has(status);
  const kind =
    status === "SUCCEEDED"
      ? "success"
      : status === "FAILED" || status === "CANCELLED"
        ? "error"
        : active
          ? "active"
          : "neutral";
  return <span className={`backtest-job-status backtest-job-status-${kind}`}>{status}</span>;
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

function formatScalar(value: BacktestMetricScalar): string {
  if (value === null) return "—";
  if (typeof value === "number") return formatNumber(value);
  return String(value);
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat("en-US", { maximumSignificantDigits: 8 }).format(value);
}
