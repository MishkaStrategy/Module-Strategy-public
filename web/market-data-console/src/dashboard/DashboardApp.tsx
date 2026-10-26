import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { AcceptancePage } from "../acceptance/AcceptancePage";
import { AcceptanceControlApiClient } from "../acceptance/client";
import { MarketDataWorkspace } from "../App";
import { BacktestPage } from "../backtest/BacktestPage";
import { BacktestControlApiClient } from "../backtest/client";
import { SystemCapacityPanel } from "../capacity/SystemCapacityPanel";
import { ControlApiClientError, ControlPlaneApiClient } from "../control/client";
import type {
  ComponentHealth,
  ControlComponentId,
  ControlHealthStatus,
} from "../control/contracts";
import { EvidenceControlApiClient } from "../evidence/client";
import { EvidencePage } from "../evidence/EvidencePage";
import { OperationalEventsControlApiClient } from "../events/client";
import { OperationalEventsPage } from "../events/OperationalEventsPage";
import { NotificationsControlApiClient } from "../notifications/client";
import { NotificationsPage } from "../notifications/NotificationsPage";
import { PaperRuntimeControlApiClient } from "../paper/client";
import { PaperRuntimePage } from "../paper/PaperRuntimePage";
import { RecoveryPage } from "../recovery/RecoveryPage";
import { RecoveryControlApiClient } from "../recovery/client";
import { ResearchPage } from "../research/ResearchPage";
import { ResearchControlApiClient } from "../research/client";
import { StrategyPage } from "../strategy/StrategyPage";
import { StrategyControlApiClient } from "../strategy/client";
import { NAVIGATION, findComponentForPath, isNavigationItemActive } from "./navigation";
import { OperationalOverviewPage } from "./OperationalOverviewPage";

const COMPONENT_LABELS: Record<ControlComponentId, string> = {
  acceptance: "Acceptance",
  backtest: "Backtest",
  evidence: "Evidence",
  market_data: "Market Data",
  paper_runtime: "Paper Runtime",
  research: "Research",
  signal_bot: "Notifications",
  strategy: "Strategy",
};

const INTEGRATED_MARKET_DATA_PREFIXES = ["/datasets", "/artifacts", "/jobs"];

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
  current,
  className,
}: {
  to: string;
  children: ReactNode;
  current?: boolean;
  className?: string;
}) {
  return (
    <a
      href={`#${to}`}
      className={className}
      aria-current={current ? "page" : undefined}
    >
      {children}
    </a>
  );
}

export default function DashboardApp() {
  const route = useHashRoute();
  const [path = "/overview"] = route.split("?", 1);
  const [controlToken, setControlToken] = useState("");
  const [controlAuthRevision, setControlAuthRevision] = useState(0);
  const [operatorToken, setOperatorToken] = useState("");
  const [marketDataToken, setMarketDataToken] = useState("");
  const [marketDataAuthRevision, setMarketDataAuthRevision] = useState(0);
  const controlApi = useMemo(
    () => new ControlPlaneApiClient("", () => controlToken),
    [controlToken],
  );
  const acceptanceApi = useMemo(
    () => new AcceptanceControlApiClient("", () => controlToken, () => operatorToken),
    [controlToken, operatorToken],
  );
  const notificationsApi = useMemo(
    () => new NotificationsControlApiClient("", () => controlToken, () => operatorToken),
    [controlToken, operatorToken],
  );
  const backtestApi = useMemo(
    () => new BacktestControlApiClient("", () => controlToken, () => operatorToken),
    [controlToken, operatorToken],
  );
  const strategyApi = useMemo(
    () => new StrategyControlApiClient("", () => controlToken),
    [controlToken],
  );
  const researchApi = useMemo(
    () => new ResearchControlApiClient("", () => controlToken),
    [controlToken],
  );
  const evidenceApi = useMemo(
    () => new EvidenceControlApiClient("", () => controlToken),
    [controlToken],
  );
  const eventsApi = useMemo(
    () => new OperationalEventsControlApiClient("", () => controlToken),
    [controlToken],
  );
  const paperApi = useMemo(
    () => new PaperRuntimeControlApiClient("", () => controlToken, () => operatorToken),
    [controlToken, operatorToken],
  );
  const recoveryApi = useMemo(
    () => new RecoveryControlApiClient("", () => controlToken, () => operatorToken),
    [controlToken, operatorToken],
  );
  const changeControlToken = (value: string) => {
    setControlToken(value);
    setControlAuthRevision((current) => current + 1);
  };
  const changeMarketDataToken = (value: string) => {
    setMarketDataToken(value);
    setMarketDataAuthRevision((current) => current + 1);
  };

  return (
    <div className="dashboard-shell">
      <aside className="dashboard-sidebar">
        <div className="dashboard-brand">
          <p className="eyebrow">Модульная Стратегия</p>
          <h1>Control Dashboard</h1>
          <div className="mode-banner" aria-label="Текущий режим: PAPER">
            <span aria-hidden="true">●</span>
            PAPER
          </div>
          <p className="sidebar-copy">
            Единая локальная панель управления и мониторинга paper/research системы.
          </p>
        </div>
        <nav className="dashboard-navigation" aria-label="Навигация Modular Strategy">
          {NAVIGATION.map((group) => (
            <section className="navigation-group" key={group.label} aria-label={group.label}>
              <h2>{group.label}</h2>
              <div className="navigation-items">
                {group.items.map((item) => {
                  const active = isNavigationItemActive(item, path);
                  return (
                    <HashLink
                      key={item.path}
                      to={item.path}
                      current={active}
                      className={active ? "active" : ""}
                    >
                      <span>{item.label}</span>
                      {!item.available ? <small>contract pending</small> : null}
                    </HashLink>
                  );
                })}
              </div>
            </section>
          ))}
        </nav>
        <div className="reader-token-stack" aria-label="Локальная авторизация">
          <label className="control-token-control">
            <span>Control reader token</span>
            <input
              type="password"
              value={controlToken}
              onChange={(event) => changeControlToken(event.target.value)}
              autoComplete="off"
              placeholder="Опционально · только в памяти"
            />
            <small>Для read-only `/api/control/v1`; не сохраняется браузером.</small>
          </label>
          <label className="control-token-control">
            <span>Control operator token</span>
            <input
              type="password"
              value={operatorToken}
              onChange={(event) => setOperatorToken(event.target.value)}
              autoComplete="off"
              placeholder="Для mutation · только в памяти"
            />
            <small>Отдельный scope для разрешённых mutations; не сохраняется браузером.</small>
          </label>
          <label className="control-token-control">
            <span>Market Data reader token</span>
            <input
              type="password"
              value={marketDataToken}
              onChange={(event) => changeMarketDataToken(event.target.value)}
              autoComplete="off"
              placeholder="Опционально · только в памяти"
            />
            <small>Для существующего Market Data API; не сохраняется браузером.</small>
          </label>
        </div>
      </aside>
      <main className="dashboard-main">
        {renderRoute(
          path,
          controlApi,
          acceptanceApi,
          notificationsApi,
          backtestApi,
          strategyApi,
          researchApi,
          evidenceApi,
          eventsApi,
          paperApi,
          recoveryApi,
          controlAuthRevision,
          controlToken,
          operatorToken.trim().length > 0,
          marketDataToken,
          marketDataAuthRevision,
        )}
      </main>
    </div>
  );
}

function renderRoute(
  path: string,
  controlApi: ControlPlaneApiClient,
  acceptanceApi: AcceptanceControlApiClient,
  notificationsApi: NotificationsControlApiClient,
  backtestApi: BacktestControlApiClient,
  strategyApi: StrategyControlApiClient,
  researchApi: ResearchControlApiClient,
  evidenceApi: EvidenceControlApiClient,
  eventsApi: OperationalEventsControlApiClient,
  paperApi: PaperRuntimeControlApiClient,
  recoveryApi: RecoveryControlApiClient,
  controlAuthRevision: number,
  controlReaderToken: string,
  operatorReady: boolean,
  marketDataToken: string,
  marketDataAuthRevision: number,
): ReactNode {
  if (path === "/" || path === "/overview") {
    return (
      <OperationalOverviewPage
        controlApi={controlApi}
        backtestApi={backtestApi}
        authRevision={controlAuthRevision}
      />
    );
  }
  if (path === "/system") {
    return (
      <SystemOverviewPage
        api={controlApi}
        authRevision={controlAuthRevision}
        title="System Health"
        description="Компонентная health-проекция. Браузер не является источником runtime truth."
      />
    );
  }
  if (path === "/system/resources") {
    return (
      <SystemResourcesPage
        api={controlApi}
        authRevision={controlAuthRevision}
        readerToken={controlReaderToken}
      />
    );
  }
  if (path === "/backtests") {
    return (
      <BacktestPage
        controlApi={controlApi}
        backtestApi={backtestApi}
        readerAuthRevision={controlAuthRevision}
        operatorReady={operatorReady}
      />
    );
  }
  if (path === "/strategies") {
    return (
      <StrategyPage
        controlApi={controlApi}
        strategyApi={strategyApi}
        readerAuthRevision={controlAuthRevision}
      />
    );
  }
  if (path === "/research") {
    return (
      <ResearchPage
        controlApi={controlApi}
        researchApi={researchApi}
        readerAuthRevision={controlAuthRevision}
      />
    );
  }
  if (path === "/evidence") {
    return (
      <EvidencePage
        controlApi={controlApi}
        evidenceApi={evidenceApi}
        readerAuthRevision={controlAuthRevision}
      />
    );
  }
  if (path === "/paper") {
    return (
      <PaperRuntimePage
        controlApi={controlApi}
        paperApi={paperApi}
        readerAuthRevision={controlAuthRevision}
        operatorReady={operatorReady}
      />
    );
  }
  if (path === "/acceptance") {
    return (
      <AcceptancePage
        controlApi={controlApi}
        acceptanceApi={acceptanceApi}
        readerAuthRevision={controlAuthRevision}
        operatorReady={operatorReady}
      />
    );
  }
  if (path === "/recovery") {
    return (
      <RecoveryPage
        api={recoveryApi}
        readerAuthRevision={controlAuthRevision}
        operatorReady={operatorReady}
      />
    );
  }
  if (path === "/notifications") {
    return (
      <NotificationsPage
        api={notificationsApi}
        readerAuthRevision={controlAuthRevision}
        operatorReady={operatorReady}
      />
    );
  }
  if (path === "/events") {
    return <OperationalEventsPage api={eventsApi} readerAuthRevision={controlAuthRevision} />;
  }
  if (INTEGRATED_MARKET_DATA_PREFIXES.some((prefix) => path.startsWith(prefix))) {
    return (
      <section className="market-data-workspace" aria-label="Market Data workspace">
        <div className="workspace-boundary-note">
          <div>
            <strong>Market Data workspace</strong>
            <span>
              Dataset, Artifact, Job и SSE поверхности теперь работают внутри единого Dashboard
              shell без вложенной навигации.
            </span>
          </div>
          <span className="integration-tag">native shell</span>
        </div>
        <MarketDataWorkspace token={marketDataToken} authRevision={marketDataAuthRevision} />
      </section>
    );
  }
  const component = findComponentForPath(path);
  if (component) {
    return (
      <IntegrationStatusPage
        api={controlApi}
        authRevision={controlAuthRevision}
        component={component}
      />
    );
  }
  return (
    <section className="dashboard-page">
      <PageHeader
        title="Страница не найдена"
        description="Маршрут не относится к зарегистрированной Control UI поверхности."
      />
      <div className="empty-state">
        <HashLink to="/overview">Вернуться к Overview</HashLink>
      </div>
    </section>
  );
}

function SystemOverviewPage({
  api,
  authRevision,
  title,
  description,
}: {
  api: ControlPlaneApiClient;
  authRevision: number;
  title: string;
  description: string;
}) {
  const overview = useQuery({
    queryKey: ["control-overview", authRevision],
    queryFn: ({ signal }) => api.getOverview(signal),
    retry: false,
    refetchInterval: 10_000,
  });

  return (
    <section className="dashboard-page">
      <PageHeader title={title} description={description} />
      {overview.isPending ? <LoadingState /> : null}
      {overview.error ? <ControlError error={overview.error} /> : null}
      {overview.data ? (
        <div>
          <section className="control-summary-grid" aria-label="System summary">
            <article className="control-metric-card">
              <span>System</span>
              <StatusBadge status={overview.data.status} />
              <small>Checked {formatDateTime(overview.data.checked_at)}</small>
            </article>
            <article className="control-metric-card">
              <span>Mode</span>
              <strong>PAPER</strong>
              <small>Live trading is not available.</small>
            </article>
            <article className="control-metric-card">
              <span>Components</span>
              <strong>{overview.data.components.length}</strong>
              <small>Authoritative adapter projections</small>
            </article>
            <article className="control-metric-card">
              <span>Unavailable</span>
              <strong>
                {
                  overview.data.components.filter((item) => item.status === "UNAVAILABLE")
                    .length
                }
              </strong>
              <small>Missing or unhealthy adapters</small>
            </article>
          </section>
          <section className="control-panel">
            <div className="control-panel-heading">
              <div>
                <h3>Component health</h3>
                <p>Статус и фаза остаются серверной проекцией, а не локальным состоянием UI.</p>
              </div>
              <StatusBadge status={overview.data.status} />
            </div>
            <div className="component-health-grid">
              {overview.data.components.map((component) => (
                <ComponentHealthCard health={component} key={component.component} />
              ))}
            </div>
          </section>
        </div>
      ) : null}
    </section>
  );
}

function SystemResourcesPage({
  api,
  authRevision,
  readerToken,
}: {
  api: ControlPlaneApiClient;
  authRevision: number;
  readerToken: string;
}) {
  const resources = useQuery({
    queryKey: ["control-system-resources", authRevision],
    queryFn: ({ signal }) => api.getSystemResources(signal),
    retry: false,
    refetchInterval: 10_000,
  });

  return (
    <section className="dashboard-page">
      <PageHeader
        title="System Resources"
        description="Read-only operational metrics plus fail-closed owner-backed capacity policy. Host identity and process details are intentionally not exposed."
      />
      {resources.isPending ? <LoadingState /> : null}
      {resources.error ? <ControlError error={resources.error} /> : null}
      {resources.data ? (
        <div>
          <section className="control-summary-grid" aria-label="System resource metrics">
            <article className="control-metric-card">
              <span>CPU</span>
              <strong>{resources.data.cpu_percent.toFixed(1)}%</strong>
              <small>{resources.data.logical_cpu_count ?? "—"} logical CPUs</small>
            </article>
            <article className="control-metric-card">
              <span>Memory</span>
              <strong>{resources.data.memory.percent.toFixed(1)}%</strong>
              <small>
                {formatBytes(resources.data.memory.used_bytes)} used · {formatBytes(resources.data.memory.available_bytes)} available
              </small>
            </article>
            <article className="control-metric-card">
              <span>Disk</span>
              <strong>{resources.data.disk.percent.toFixed(1)}%</strong>
              <small>
                {formatBytes(resources.data.disk.used_bytes)} used · {formatBytes(resources.data.disk.available_bytes)} available
              </small>
            </article>
            <article className="control-metric-card">
              <span>Uptime</span>
              <strong>{formatDuration(resources.data.uptime_seconds)}</strong>
              <small>Operating-system uptime</small>
            </article>
            <article className="control-metric-card">
              <span>Processes</span>
              <strong>{resources.data.process_count}</strong>
              <small>Aggregate count only · no process identities</small>
            </article>
            <article className="control-metric-card">
              <span>Control Plane</span>
              <strong>{formatBytes(resources.data.control_process_rss_bytes)}</strong>
              <small>{resources.data.control_process_threads} threads · current process only</small>
            </article>
          </section>
          <section className="control-panel">
            <div className="control-panel-heading">
              <div>
                <h3>Operational boundary</h3>
                <p>
                  Metrics refresh every 10 seconds from the same reader-authenticated Control Plane.
                  Hostname, username, environment, command line, paths, network inventory and
                  per-process identities remain unavailable.
                </p>
              </div>
              <small>Collected {formatDateTime(resources.data.collected_at)}</small>
            </div>
          </section>
        </div>
      ) : null}
      <SystemCapacityPanel
        readerToken={readerToken}
        authRevision={authRevision}
        currentDiskAvailableBytes={resources.data?.disk.available_bytes ?? null}
      />
    </section>
  );
}

function IntegrationStatusPage({
  api,
  authRevision,
  component,
}: {
  api: ControlPlaneApiClient;
  authRevision: number;
  component: ControlComponentId;
}) {
  const health = useQuery({
    queryKey: ["control-component-health", authRevision, component],
    queryFn: ({ signal }) => api.getComponentHealth(component, signal),
    retry: false,
    refetchInterval: 10_000,
  });
  return (
    <section className="dashboard-page">
      <PageHeader
        title={COMPONENT_LABELS[component]}
        description="Этот раздел подключается через typed Control Plane adapter. Недоступные операции не имитируются в UI."
      />
      {health.isPending ? <LoadingState /> : null}
      {health.error ? <ControlError error={health.error} /> : null}
      {health.data ? (
        <section className="control-panel">
          <div className="control-panel-heading">
            <div>
              <h3>Integration status</h3>
              <p>{health.data.summary}</p>
            </div>
            <StatusBadge status={health.data.status} />
          </div>
          {health.data.details.reason === "adapter_not_registered" ? (
            <div className="control-notice" role="status">
              <strong>Control contract pending</strong>
              <span>
                Компонент существует в проекте, но безопасный Control Plane adapter ещё не подключён.
                Кнопки start/stop не показываются до idempotency/audit/recovery acceptance.
              </span>
            </div>
          ) : (
            <ComponentHealthCard health={health.data} />
          )}
        </section>
      ) : null}
    </section>
  );
}

function ComponentHealthCard({ health }: { health: ComponentHealth }) {
  return (
    <article className="component-health-card">
      <div className="component-health-heading">
        <strong>{COMPONENT_LABELS[health.component]}</strong>
        <StatusBadge status={health.status} />
      </div>
      <p>{health.summary}</p>
      <small>Checked {formatDateTime(health.checked_at)}</small>
      {Object.keys(health.details).length ? (
        <dl className="control-details">
          {Object.entries(health.details).map(([name, value]) => (
            <div key={name}>
              <dt>{name}</dt>
              <dd>{value === null ? "—" : String(value)}</dd>
            </div>
          ))}
        </dl>
      ) : null}
    </article>
  );
}

function PageHeader({ title, description }: { title: string; description: string }) {
  return (
    <header className="dashboard-page-header">
      <div className="page-title-row">
        <div>
          <p className="eyebrow">Control UI · local-only</p>
          <h2>{title}</h2>
        </div>
        <div className="mode-banner compact" aria-label="Текущий режим: PAPER">
          <span aria-hidden="true">●</span>
          PAPER
        </div>
      </div>
      <p>{description}</p>
    </header>
  );
}

function StatusBadge({
  status,
  label,
}: {
  status: ControlHealthStatus;
  label?: string;
}) {
  const symbol = status === "HEALTHY" ? "✓" : status === "DEGRADED" ? "!" : "×";
  return (
    <span
      className={`control-status control-status-${status.toLowerCase()}`}
      aria-label={label ? `${label}: ${status}` : status}
    >
      <span aria-hidden="true">{symbol}</span>
      {label ?? status}
    </span>
  );
}

function LoadingState() {
  return (
    <div className="control-notice" aria-live="polite">
      <strong>Loading Control Plane state…</strong>
      <span>Чтение локальной серверной проекции.</span>
    </div>
  );
}

function ControlError({ error }: { error: unknown }) {
  if (error instanceof ControlApiClientError) {
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
    <div className="control-notice control-notice-error" role="alert">
      <strong>CONTROL_UI_ERROR</strong>
      <span>Не удалось прочитать состояние Control Plane.</span>
    </div>
  );
}

function formatDateTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "invalid timestamp";
  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "medium",
    timeStyle: "medium",
    timeZone: "UTC",
  }).format(date);
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "invalid size";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let amount = value;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024;
    unit += 1;
  }
  const digits = unit === 0 || amount >= 100 ? 0 : amount >= 10 ? 1 : 2;
  return `${amount.toFixed(digits)} ${units[unit]}`;
}

function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "invalid duration";
  const wholeSeconds = Math.floor(seconds);
  const days = Math.floor(wholeSeconds / 86_400);
  const hours = Math.floor((wholeSeconds % 86_400) / 3_600);
  const minutes = Math.floor((wholeSeconds % 3_600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}
