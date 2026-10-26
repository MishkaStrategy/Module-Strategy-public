import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import type { FormEvent, ReactNode } from "react";

import {
  ApiClientError,
  MarketDataApiClient,
  formatBytes,
} from "./api/client";
import type { JobStreamStatus } from "./api/client";
import type {
  DatasetDetails,
  JobDetails,
  JobEvent,
  JobOperation,
  JobSummary,
  RunState,
} from "./api/contracts";

const JOB_OPERATIONS: JobOperation[] = [
  "download",
  "update",
  "validate",
  "repair",
  "backfill",
  "export",
];

const RUN_STATES: RunState[] = [
  "CREATED",
  "VALIDATING_REQUEST",
  "DISCOVERING_CAPABILITIES",
  "RESOLVING_INSTRUMENT",
  "PLANNING",
  "DOWNLOADING",
  "VERIFYING_RAW",
  "NORMALIZING",
  "VALIDATING_DATA",
  "REPAIRING",
  "BUILDING_DERIVED",
  "HASHING",
  "REGISTERING",
  "EXPORTING",
  "COMPLETED",
  "COMPLETED_WITH_WARNINGS",
  "PAUSED",
  "CANCELLED",
  "FAILED",
  "DATASET_REJECTED",
];

const TERMINAL_STATES = new Set<RunState>([
  "COMPLETED",
  "COMPLETED_WITH_WARNINGS",
  "CANCELLED",
  "FAILED",
  "DATASET_REJECTED",
]);

function readRoute(): string {
  const value = window.location.hash.replace(/^#/, "");
  return value.startsWith("/") ? value : "/overview";
}

function useHashRoute(): string {
  const [route, setRoute] = useState(readRoute);
  useEffect(() => {
    if (!window.location.hash) window.location.replace("#/overview");
    const update = () => setRoute(readRoute());
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  return route;
}

function HashLink({
  to,
  children,
  className,
  current,
}: {
  to: string;
  children: ReactNode;
  className?: string | undefined;
  current?: boolean | undefined;
}) {
  return (
    <a href={`#${to}`} className={className} aria-current={current ? "page" : undefined}>
      {children}
    </a>
  );
}

function AppShell({
  token,
  onTokenChange,
  route,
  children,
}: {
  token: string;
  onTokenChange: (value: string) => void;
  route: string;
  children: ReactNode;
}) {
  const path = route.split("?", 1)[0] ?? "/overview";
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div>
          <p className="eyebrow">Модульная Стратегия</p>
          <h1>Market Data</h1>
          <p className="sidebar-copy">Локальная консоль исторических данных.</p>
        </div>
        <nav aria-label="Основная навигация">
          <HashLink
            to="/overview"
            current={path === "/overview"}
            className={path === "/overview" ? "active" : undefined}
          >
            Обзор
          </HashLink>
          <HashLink
            to="/datasets"
            current={path.startsWith("/datasets")}
            className={path.startsWith("/datasets") ? "active" : undefined}
          >
            Датасеты
          </HashLink>
          <HashLink
            to="/artifacts"
            current={path.startsWith("/artifacts")}
            className={path.startsWith("/artifacts") ? "active" : undefined}
          >
            Артефакты
          </HashLink>
          <HashLink
            to="/jobs"
            current={path.startsWith("/jobs")}
            className={path.startsWith("/jobs") ? "active" : undefined}
          >
            Задачи
          </HashLink>
        </nav>
        <label className="token-control">
          <span>Bearer-токен</span>
          <input
            type="password"
            value={token}
            onChange={(event) => onTokenChange(event.target.value)}
            autoComplete="off"
            placeholder="Только в памяти вкладки"
          />
          <small>Не сохраняется браузером и используется только для API-запросов вкладки.</small>
        </label>
      </aside>
      <main className="main-content">{children}</main>
    </div>
  );
}

function PageHeader({ title, description }: { title: string; description: string }) {
  return (
    <header className="page-header">
      <p className="eyebrow">Historical Market Data Ingestion</p>
      <h2>{title}</h2>
      <p>{description}</p>
    </header>
  );
}

function StatusPill({ status }: { status: string }) {
  const normalized = status.toUpperCase();
  const success = ["OK", "VALID", "COMPLETED", "CONNECTED"].includes(normalized);
  const danger = ["ERROR", "INVALID", "FAILED", "CANCELLED", "DATASET_REJECTED"].includes(
    normalized,
  );
  const tone = success ? "success" : danger ? "danger" : "neutral";
  return <span className={`status-pill status-${tone}`}>{status}</span>;
}

function QueryError({ error }: { error: unknown }) {
  if (error instanceof ApiClientError) {
    return (
      <div className="notice notice-error" role="alert">
        <strong>{error.code}</strong>
        <span>{error.message}</span>
        {error.correlationId ? <small>Correlation ID: {error.correlationId}</small> : null}
      </div>
    );
  }
  if (error instanceof Error) {
    return (
      <div className="notice notice-error" role="alert">
        {error.message}
      </div>
    );
  }
  return (
    <div className="notice notice-error" role="alert">
      Неизвестная ошибка API.
    </div>
  );
}

function LoadingCard() {
  return (
    <div className="notice" aria-live="polite">
      Загрузка…
    </div>
  );
}

function OverviewPage({
  api,
  authRevision,
}: {
  api: MarketDataApiClient;
  authRevision: number;
}) {
  const version = useQuery({
    queryKey: ["version"],
    queryFn: ({ signal }) => api.getVersion(signal),
  });
  const health = useQuery({
    queryKey: ["health", authRevision],
    queryFn: ({ signal }) => api.getHealth(signal),
    retry: false,
  });
  return (
    <>
      <PageHeader title="Обзор" description="Состояние локального API и версия контракта." />
      <section className="metric-grid" aria-label="Сводка API">
        <article className="metric-card">
          <span>Application build</span>
          <strong>{version.data?.build_version ?? "—"}</strong>
          <small>{version.data?.api_version ?? "v1"}</small>
        </article>
        <article className="metric-card">
          <span>API health</span>
          <strong>{health.data ? <StatusPill status={health.data.status} /> : "—"}</strong>
          <small>{health.data?.component ?? "market_data_api"}</small>
        </article>
      </section>
      {version.isPending || health.isPending ? <LoadingCard /> : null}
      {version.error ? <QueryError error={version.error} /> : null}
      {health.error ? <QueryError error={health.error} /> : null}
      {health.data ? (
        <section className="panel">
          <div className="panel-heading">
            <h3>Проверки</h3>
            <StatusPill status={health.data.status} />
          </div>
          {Object.keys(health.data.checks).length === 0 ? (
            <p className="muted">Дополнительные проверки не опубликованы.</p>
          ) : (
            <dl className="definition-grid">
              {Object.entries(health.data.checks).map(([name, value]) => (
                <div key={name}>
                  <dt>{name}</dt>
                  <dd>{value}</dd>
                </div>
              ))}
            </dl>
          )}
        </section>
      ) : null}
    </>
  );
}

function DatasetTable({ items }: { items: DatasetDetails[] }) {
  if (items.length === 0) {
    return <div className="empty-state">Зарегистрированных датасетов пока нет.</div>;
  }
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Инструмент</th>
            <th>Биржа / рынок</th>
            <th>Версия</th>
            <th>Период</th>
            <th>Статус</th>
          </tr>
        </thead>
        <tbody>
          {items.map((dataset) => (
            <tr key={`${dataset.dataset_id}:${dataset.version}`}>
              <td>
                <HashLink
                  to={`/datasets/${encodeURIComponent(dataset.dataset_id)}?version=${dataset.version}`}
                >
                  {dataset.instrument_id}
                </HashLink>
                <small className="cell-subtitle">{dataset.dataset_id}</small>
              </td>
              <td>
                {dataset.exchange_id} · {dataset.market_type}
              </td>
              <td>v{dataset.version}</td>
              <td>
                {formatDate(dataset.start)} — {formatDate(dataset.end)}
              </td>
              <td>
                <StatusPill status={dataset.validation_status} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function DatasetsPage({
  api,
  authRevision,
}: {
  api: MarketDataApiClient;
  authRevision: number;
}) {
  const [offset, setOffset] = useState(0);
  const limit = 25;
  const query = useQuery({
    queryKey: ["datasets", authRevision, limit, offset],
    queryFn: ({ signal }) => api.listDatasets(limit, offset, signal),
    retry: false,
  });
  const total = query.data?.pagination.total ?? 0;
  return (
    <>
      <PageHeader
        title="Датасеты"
        description="Immutable Dataset Registry — источник истины для опубликованных версий."
      />
      {query.isPending ? <LoadingCard /> : null}
      {query.error ? <QueryError error={query.error} /> : null}
      {query.data ? (
        <section className="panel">
          <div className="panel-heading">
            <h3>Зарегистрированные версии</h3>
            <span className="muted">Всего: {total}</span>
          </div>
          <DatasetTable items={query.data.items} />
          <Pagination
            offset={offset}
            limit={limit}
            total={total}
            onOffsetChange={setOffset}
          />
        </section>
      ) : null}
    </>
  );
}

function DatasetPage({
  api,
  authRevision,
  datasetId,
  queryString,
}: {
  api: MarketDataApiClient;
  authRevision: number;
  datasetId: string;
  queryString: string;
}) {
  const versionValue = new URLSearchParams(queryString).get("version");
  const version = versionValue ? Number(versionValue) : undefined;
  const query = useQuery({
    queryKey: ["dataset", authRevision, datasetId, version],
    queryFn: ({ signal }) => api.getDataset(datasetId, version, signal),
    enabled: datasetId.length > 0 && (version === undefined || Number.isInteger(version)),
    retry: false,
  });
  return (
    <>
      <PageHeader
        title="Детали датасета"
        description="Read-only представление точной зарегистрированной версии и lineage."
      />
      <p>
        <HashLink to="/datasets">← К списку датасетов</HashLink>
      </p>
      {query.isPending ? <LoadingCard /> : null}
      {query.error ? <QueryError error={query.error} /> : null}
      {query.data ? <DatasetDetailsPanel dataset={query.data} /> : null}
    </>
  );
}

function DatasetDetailsPanel({ dataset }: { dataset: DatasetDetails }) {
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <h3>{dataset.instrument_id}</h3>
          <p className="muted">
            {dataset.dataset_id} · v{dataset.version}
          </p>
        </div>
        <StatusPill status={dataset.validation_status} />
      </div>
      <dl className="definition-grid">
        <Definition label="Биржа" value={dataset.exchange_id} />
        <Definition label="Рынок" value={dataset.market_type} />
        <Definition
          label="Период"
          value={`${formatDateTime(dataset.start)} — ${formatDateTime(dataset.end)}`}
        />
        <Definition label="Типы данных" value={dataset.data_types.join(", ") || "—"} />
        <Definition label="Таймфреймы" value={dataset.timeframes.join(", ") || "—"} />
        <Definition label="Зарегистрирован" value={formatDateTime(dataset.registered_at)} />
        <Definition label="Manifest" value={dataset.manifest_uri} code />
        <Definition label="Storage" value={dataset.storage_uri} code />
        <Definition label="Dataset hash" value={dataset.dataset_hash} code wide />
      </dl>
      {dataset.unresolved_issues.length ? (
        <div className="notice notice-error" role="alert">
          <strong>Нерешённые проблемы</strong>
          <ul>
            {dataset.unresolved_issues.map((issue) => (
              <li key={issue}>{issue}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}

function Definition({
  label,
  value,
  code = false,
  wide = false,
}: {
  label: string;
  value: string;
  code?: boolean;
  wide?: boolean;
}) {
  return (
    <div className={wide ? "definition-wide" : undefined}>
      <dt>{label}</dt>
      <dd className={code ? "code-value" : undefined}>{value}</dd>
    </div>
  );
}

function ArtifactSearchPage() {
  const [artifactId, setArtifactId] = useState("");
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const normalized = artifactId.trim();
    if (normalized) window.location.hash = `/artifacts/${encodeURIComponent(normalized)}`;
  };
  return (
    <>
      <PageHeader
        title="Артефакты"
        description="Поиск immutable ZIP-артефакта по публичному идентификатору."
      />
      <section className="panel narrow-panel">
        <form onSubmit={submit} className="lookup-form">
          <label htmlFor="artifact-id">Artifact ID</label>
          <input
            id="artifact-id"
            value={artifactId}
            onChange={(event) => setArtifactId(event.target.value)}
            placeholder="mdi_artifact_…"
            required
          />
          <button type="submit">Открыть</button>
        </form>
      </section>
    </>
  );
}

function ArtifactPage({
  api,
  authRevision,
  artifactId,
}: {
  api: MarketDataApiClient;
  authRevision: number;
  artifactId: string;
}) {
  const [downloadError, setDownloadError] = useState<unknown>();
  const [downloading, setDownloading] = useState(false);
  const query = useQuery({
    queryKey: ["artifact", authRevision, artifactId],
    queryFn: ({ signal }) => api.getArtifact(artifactId, signal),
    enabled: artifactId.length > 0,
    retry: false,
  });
  const download = async () => {
    if (!query.data) return;
    setDownloadError(undefined);
    setDownloading(true);
    try {
      await api.downloadArtifact(query.data);
    } catch (error) {
      setDownloadError(error);
    } finally {
      setDownloading(false);
    }
  };
  return (
    <>
      <PageHeader title="Артефакт" description="Metadata и авторизованная загрузка содержимого." />
      <p>
        <HashLink to="/artifacts">← Новый поиск</HashLink>
      </p>
      {query.isPending ? <LoadingCard /> : null}
      {query.error ? <QueryError error={query.error} /> : null}
      {downloadError ? <QueryError error={downloadError} /> : null}
      {query.data ? (
        <section className="panel">
          <div className="panel-heading">
            <div>
              <h3>{query.data.filename}</h3>
              <p className="muted">{query.data.artifact_id}</p>
            </div>
            <button type="button" onClick={download} disabled={downloading}>
              {downloading ? "Загрузка…" : "Скачать ZIP"}
            </button>
          </div>
          <dl className="definition-grid">
            <Definition label="Датасет" value={`${query.data.dataset_id} · v${query.data.version}`} />
            <Definition label="Размер" value={formatBytes(query.data.byte_size)} />
            <Definition label="Файлов" value={String(query.data.file_count)} />
            <Definition label="Создан" value={formatDateTime(query.data.created_at)} />
            <Definition label="Content-Type" value={query.data.content_type} code />
            <Definition label="SHA-256" value={query.data.checksum_sha256} code wide />
          </dl>
        </section>
      ) : null}
    </>
  );
}

function JobsPage({
  api,
  authRevision,
}: {
  api: MarketDataApiClient;
  authRevision: number;
}) {
  const [offset, setOffset] = useState(0);
  const [state, setState] = useState<RunState | "">("");
  const [operation, setOperation] = useState<JobOperation | "">("");
  const limit = 25;
  const query = useQuery({
    queryKey: ["jobs", authRevision, limit, offset, state, operation],
    queryFn: ({ signal }) =>
      api.listJobs(
        limit,
        offset,
        state || undefined,
        operation || undefined,
        signal,
      ),
    retry: false,
    refetchInterval: 5_000,
  });
  const total = query.data?.pagination.total ?? 0;
  return (
    <>
      <PageHeader
        title="Задачи"
        description="Read-only обзор durable jobs. Обновляется каждые пять секунд."
      />
      <section className="panel">
        <div className="filter-grid" aria-label="Фильтры задач">
          <label>
            <span>Состояние</span>
            <select
              value={state}
              onChange={(event) => {
                setOffset(0);
                setState(event.target.value as RunState | "");
              }}
            >
              <option value="">Все состояния</option>
              {RUN_STATES.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Операция</span>
            <select
              value={operation}
              onChange={(event) => {
                setOffset(0);
                setOperation(event.target.value as JobOperation | "");
              }}
            >
              <option value="">Все операции</option>
              {JOB_OPERATIONS.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </label>
          <button type="button" onClick={() => void query.refetch()} disabled={query.isFetching}>
            {query.isFetching ? "Обновление…" : "Обновить"}
          </button>
        </div>
        {query.isPending ? <LoadingCard /> : null}
        {query.error ? <QueryError error={query.error} /> : null}
        {query.data ? (
          <>
            <div className="panel-heading compact-heading">
              <h3>Durable jobs</h3>
              <span className="muted">Всего: {total}</span>
            </div>
            <JobTable items={query.data.items} />
            <Pagination
              offset={offset}
              limit={limit}
              total={total}
              onOffsetChange={setOffset}
            />
          </>
        ) : null}
      </section>
    </>
  );
}

function JobTable({ items }: { items: JobSummary[] }) {
  if (items.length === 0) return <div className="empty-state">Задачи не найдены.</div>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Job ID</th>
            <th>Операция</th>
            <th>Состояние</th>
            <th>Прогресс</th>
            <th>Обновлена</th>
          </tr>
        </thead>
        <tbody>
          {items.map((job) => (
            <tr key={job.job_id}>
              <td>
                <HashLink to={`/jobs/${encodeURIComponent(job.job_id)}`}>{job.job_id}</HashLink>
                <small className="cell-subtitle">{job.request_hash}</small>
              </td>
              <td>{job.operation}</td>
              <td>
                <StatusPill status={job.state} />
              </td>
              <td>
                <JobProgressView job={job} />
              </td>
              <td>{formatDateTime(job.updated_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function JobProgressView({ job }: { job: JobSummary }) {
  const progress = job.progress;
  if (!progress) return <span className="muted">—</span>;
  if (progress.determinate && progress.percent !== null) {
    return (
      <div className="progress-stack">
        <progress max={100} value={progress.percent} />
        <small>{new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(progress.percent)}%</small>
      </div>
    );
  }
  return (
    <span>
      {progress.phase}
      {progress.message ? <small className="cell-subtitle">{progress.message}</small> : null}
    </span>
  );
}

function JobPage({
  api,
  authRevision,
  jobId,
}: {
  api: MarketDataApiClient;
  authRevision: number;
  jobId: string;
}) {
  const detail = useQuery({
    queryKey: ["job", authRevision, jobId],
    queryFn: ({ signal }) => api.getJob(jobId, signal),
    enabled: jobId.length > 0,
    retry: false,
  });
  const history = useQuery({
    queryKey: ["job-events", authRevision, jobId],
    queryFn: ({ signal }) => api.getJobEvents(jobId, 0, 500, signal),
    enabled: jobId.length > 0,
    retry: false,
  });
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [streamStatus, setStreamStatus] = useState<JobStreamStatus | "idle">("idle");
  const [streamError, setStreamError] = useState<unknown>();

  useEffect(() => {
    setEvents([]);
    setStreamStatus("idle");
    setStreamError(undefined);
  }, [jobId, authRevision]);

  useEffect(() => {
    if (history.data) setEvents((current) => mergeJobEvents(current, history.data));
  }, [history.data]);

  useEffect(() => {
    if (!history.isSuccess || !detail.data || !jobId) return;
    if (TERMINAL_STATES.has(detail.data.state)) {
      setStreamStatus("stopped");
      return;
    }

    const controller = new AbortController();
    const cursor = Math.max(0, ...history.data.map((event) => event.event_id));
    setStreamError(undefined);
    void api
      .watchJobEvents(
        jobId,
        cursor,
        {
          onEvent: (event) => {
            setEvents((current) => mergeJobEvents(current, [event]));
            if (TERMINAL_STATES.has(event.state)) {
              controller.abort();
              void detail.refetch();
            }
          },
          onStatus: setStreamStatus,
          onError: setStreamError,
        },
        controller.signal,
      )
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setStreamError(error);
      });
    return () => controller.abort();
  }, [api, authRevision, detail.data?.state, history.data, history.isSuccess, jobId]);

  const latestState = events.at(-1)?.state ?? detail.data?.state;
  return (
    <>
      <PageHeader
        title="Задача"
        description="Read-only детали, bounded event history и SSE reconnect."
      />
      <p>
        <HashLink to="/jobs">← К списку задач</HashLink>
      </p>
      {detail.isPending || history.isPending ? <LoadingCard /> : null}
      {detail.error ? <QueryError error={detail.error} /> : null}
      {history.error ? <QueryError error={history.error} /> : null}
      {streamError ? <QueryError error={streamError} /> : null}
      {detail.data ? (
        <JobDetailsPanel
          job={detail.data}
          latestState={latestState ?? detail.data.state}
          streamStatus={streamStatus}
        />
      ) : null}
      <JobEventTimeline events={events} streamStatus={streamStatus} />
    </>
  );
}

function JobDetailsPanel({
  job,
  latestState,
  streamStatus,
}: {
  job: JobDetails;
  latestState: RunState;
  streamStatus: JobStreamStatus | "idle";
}) {
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <h3>{job.job_id}</h3>
          <p className="muted">{job.operation}</p>
        </div>
        <div className="status-cluster">
          <StatusPill status={latestState} />
          <StatusPill status={streamStatusLabel(streamStatus)} />
        </div>
      </div>
      <dl className="definition-grid">
        <Definition label="Создана" value={formatDateTime(job.created_at)} />
        <Definition label="Обновлена" value={formatDateTime(job.updated_at)} />
        <Definition label="Запущена" value={formatOptionalDate(job.started_at)} />
        <Definition label="Завершена" value={formatOptionalDate(job.completed_at)} />
        <Definition label="Backend run" value={job.backend_run_id ?? "—"} code />
        <Definition label="Actor" value={job.actor_id ?? "—"} code />
        <Definition label="Correlation ID" value={job.correlation_id} code />
        <Definition label="Request hash" value={job.request_hash} code wide />
      </dl>
      {job.progress ? <ProgressPanel job={job} /> : null}
      {job.warnings.length ? (
        <div className="notice">
          <strong>Предупреждения</strong>
          <ul>
            {job.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {job.error ? (
        <div className="notice notice-error" role="alert">
          <strong>{job.error.code}</strong>
          <span>{job.error.message}</span>
          <small>
            Stage: {job.error.stage ?? "—"} · retryable: {job.error.retryable ? "да" : "нет"}
          </small>
          <JsonBlock value={job.error.details} />
        </div>
      ) : null}
      <div className="json-grid">
        <JsonPanel title="Request" value={job.request} />
        <JsonPanel title="Result" value={job.result} />
      </div>
    </section>
  );
}

function ProgressPanel({ job }: { job: JobDetails }) {
  const progress = job.progress;
  if (!progress) return null;
  return (
    <div className="progress-panel">
      <div>
        <strong>{progress.phase}</strong>
        <span>{progress.message ?? "Прогресс без сообщения"}</span>
      </div>
      {progress.determinate && progress.percent !== null ? (
        <div className="progress-stack progress-wide">
          <progress max={100} value={progress.percent} />
          <small>
            {progress.completed_units} / {progress.total_units ?? "—"} · {progress.percent}%
          </small>
        </div>
      ) : (
        <small className="muted">Индикатор без измеримого общего объёма.</small>
      )}
    </div>
  );
}

function JobEventTimeline({
  events,
  streamStatus,
}: {
  events: JobEvent[];
  streamStatus: JobStreamStatus | "idle";
}) {
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <h3>События</h3>
          <p className="muted">Показаны последние {events.length} из максимальных 1000.</p>
        </div>
        <StatusPill status={streamStatusLabel(streamStatus)} />
      </div>
      {events.length === 0 ? (
        <div className="empty-state">Событий пока нет.</div>
      ) : (
        <ol className="event-list" reversed>
          {[...events].reverse().map((event) => (
            <li key={event.event_id}>
              <div className="event-heading">
                <span className="event-id">#{event.event_id}</span>
                <strong>{event.event_type}</strong>
                <StatusPill status={event.state} />
                <time dateTime={event.timestamp}>{formatDateTime(event.timestamp)}</time>
              </div>
              {Object.keys(event.payload).length ? <JsonBlock value={event.payload} /> : null}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function JsonPanel({ title, value }: { title: string; value: unknown }) {
  return (
    <details className="json-panel" open={title === "Request"}>
      <summary>{title}</summary>
      <JsonBlock value={value} />
    </details>
  );
}

function JsonBlock({ value }: { value: unknown }) {
  return <pre className="json-block">{JSON.stringify(value, null, 2) ?? "null"}</pre>;
}

function Pagination({
  offset,
  limit,
  total,
  onOffsetChange,
}: {
  offset: number;
  limit: number;
  total: number;
  onOffsetChange: (value: number) => void;
}) {
  return (
    <div className="pagination" aria-label="Пагинация">
      <button
        type="button"
        onClick={() => onOffsetChange(Math.max(0, offset - limit))}
        disabled={offset === 0}
      >
        Назад
      </button>
      <span>
        {total === 0 ? "0" : `${offset + 1}–${Math.min(offset + limit, total)}`} из {total}
      </span>
      <button
        type="button"
        onClick={() => onOffsetChange(offset + limit)}
        disabled={offset + limit >= total}
      >
        Далее
      </button>
    </div>
  );
}

function mergeJobEvents(current: JobEvent[], incoming: JobEvent[]): JobEvent[] {
  const byId = new Map<number, JobEvent>();
  for (const event of current) byId.set(event.event_id, event);
  for (const event of incoming) byId.set(event.event_id, event);
  return [...byId.values()]
    .sort((left, right) => left.event_id - right.event_id)
    .slice(-1_000);
}

function streamStatusLabel(status: JobStreamStatus | "idle"): string {
  if (status === "connected") return "connected";
  if (status === "connecting") return "connecting";
  if (status === "reconnecting") return "reconnecting";
  if (status === "stopped") return "stopped";
  return "idle";
}

function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return "";
  }
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeZone: "UTC" }).format(
    new Date(value),
  );
}

function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "UTC",
  }).format(new Date(value));
}

function formatOptionalDate(value: string | null): string {
  return value ? formatDateTime(value) : "—";
}

function MarketDataRouteContent({
  token,
  authRevision,
  route,
}: {
  token: string;
  authRevision: number;
  route: string;
}) {
  const api = useMemo(() => new MarketDataApiClient("", () => token), [token]);
  const [path = "/overview", queryString = ""] = route.split("?", 2);

  let page: ReactNode;
  if (path === "/" || path === "/overview") {
    page = <OverviewPage api={api} authRevision={authRevision} />;
  } else if (path === "/datasets") {
    page = <DatasetsPage api={api} authRevision={authRevision} />;
  } else if (path.startsWith("/datasets/")) {
    page = (
      <DatasetPage
        api={api}
        authRevision={authRevision}
        datasetId={safeDecode(path.slice("/datasets/".length))}
        queryString={queryString}
      />
    );
  } else if (path === "/artifacts") {
    page = <ArtifactSearchPage />;
  } else if (path.startsWith("/artifacts/")) {
    page = (
      <ArtifactPage
        api={api}
        authRevision={authRevision}
        artifactId={safeDecode(path.slice("/artifacts/".length))}
      />
    );
  } else if (path === "/jobs") {
    page = <JobsPage api={api} authRevision={authRevision} />;
  } else if (path.startsWith("/jobs/")) {
    page = (
      <JobPage
        api={api}
        authRevision={authRevision}
        jobId={safeDecode(path.slice("/jobs/".length))}
      />
    );
  } else {
    page = (
      <div className="empty-state">
        Страница не найдена. <HashLink to="/overview">Вернуться к обзору</HashLink>
      </div>
    );
  }

  return <>{page}</>;
}

export function MarketDataWorkspace({
  token,
  authRevision,
}: {
  token: string;
  authRevision: number;
}) {
  const route = useHashRoute();
  return (
    <section className="market-data-workspace-content" aria-label="Market Data workspace content">
      <MarketDataRouteContent token={token} authRevision={authRevision} route={route} />
    </section>
  );
}

export default function App() {
  const [token, setToken] = useState("");
  const [authRevision, setAuthRevision] = useState(0);
  const route = useHashRoute();
  const changeToken = (value: string) => {
    setToken(value);
    setAuthRevision((current) => current + 1);
  };

  return (
    <AppShell token={token} onTokenChange={changeToken} route={route}>
      <MarketDataRouteContent token={token} authRevision={authRevision} route={route} />
    </AppShell>
  );
}
