import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { BacktestControlApiClient } from "../backtest/client";
import type { BacktestJobRecord } from "../backtest/contracts";
import { ControlPlaneApiClient } from "../control/client";
import type { ComponentHealth, SystemOverview, SystemResourcesSnapshot } from "../control/contracts";
import type {
  ActiveObservabilityAlert,
  ObservabilityComponentView,
  ObservabilityHealth,
  UnifiedObservabilitySnapshot,
} from "../control/observability";
import { observabilityComponent } from "../control/observability";
import {
  activeBacktestJobs,
  componentHealth,
  healthDetailNumber,
  latestBacktestJob,
} from "./overview";

const REFRESH_INTERVAL_MS = 10_000;
const MAX_ACTIVE_BACKTESTS = 5;

export function OperationalOverviewPage({
  controlApi,
  backtestApi,
  authRevision,
}: {
  controlApi: ControlPlaneApiClient;
  backtestApi: BacktestControlApiClient;
  authRevision: number;
}) {
  const overview = useQuery({
    queryKey: ["control-operational-overview", authRevision],
    queryFn: ({ signal }) => controlApi.getOverview(signal),
    retry: false,
    refetchInterval: REFRESH_INTERVAL_MS,
  });
  const observability = useQuery({
    queryKey: ["control-unified-observability", authRevision],
    queryFn: ({ signal }) => controlApi.getObservability(signal),
    retry: false,
    refetchInterval: REFRESH_INTERVAL_MS,
  });
  const resources = useQuery({
    queryKey: ["control-operational-resources", authRevision],
    queryFn: ({ signal }) => controlApi.getSystemResources(signal),
    retry: false,
    refetchInterval: REFRESH_INTERVAL_MS,
    enabled: overview.data !== undefined && observability.data !== undefined,
  });
  const backtestHealth =
    overview.data === undefined ? undefined : componentHealth(overview.data, "backtest");
  const backtestAvailable =
    backtestHealth !== undefined && backtestHealth.status !== "UNAVAILABLE";
  const backtests = useQuery({
    queryKey: ["control-operational-backtests", authRevision],
    queryFn: ({ signal }) => backtestApi.list(signal),
    retry: false,
    refetchInterval: REFRESH_INTERVAL_MS,
    enabled: backtestAvailable && observability.data !== undefined,
  });

  return (
    <section className="dashboard-page">
      <header className="dashboard-page-header">
        <div className="page-title-row">
          <div>
            <p className="eyebrow">Control UI · local-only</p>
            <h2>Overview</h2>
          </div>
          <div className="mode-banner compact" aria-label="Текущий режим: PAPER">
            <span aria-hidden="true">●</span>
            PAPER
          </div>
        </div>
        <p>
          Операционный snapshot Modular Strategy из существующих owner-backed read-only
          контрактов. Недоступные capabilities не имитируются.
        </p>
      </header>

      {overview.isPending || observability.isPending ? <OverviewLoading /> : null}
      {overview.error ? (
        <InlineReadError
          title="Legacy component detail unavailable"
          action="Проверьте локальный Control Plane и reader token."
        />
      ) : null}
      {observability.error ? (
        <InlineReadError
          title="Unified observability unavailable"
          action="Глобальный health не выводится из legacy данных. Проверьте observability backend и owner attachments."
        />
      ) : null}
      {overview.data && observability.data ? (
        <OperationalSnapshot
          overview={overview.data}
          observability={observability.data}
          resources={resources.data}
          resourcesFailed={resources.isError}
          backtestAvailable={backtestAvailable}
          backtests={backtests.data}
          backtestsPending={backtests.isPending && backtestAvailable}
          backtestsFailed={backtests.isError}
        />
      ) : null}
    </section>
  );
}

function OperationalSnapshot({
  overview,
  observability,
  resources,
  resourcesFailed,
  backtestAvailable,
  backtests,
  backtestsPending,
  backtestsFailed,
}: {
  overview: SystemOverview;
  observability: UnifiedObservabilitySnapshot;
  resources: SystemResourcesSnapshot | undefined;
  resourcesFailed: boolean;
  backtestAvailable: boolean;
  backtests: BacktestJobRecord[] | undefined;
  backtestsPending: boolean;
  backtestsFailed: boolean;
}) {
  const backtest = componentHealth(overview, "backtest");
  const paper = componentHealth(overview, "paper_runtime");
  const activeJobs = activeBacktestJobs(backtests ?? []);
  const latestJob = latestBacktestJob(backtests ?? []);
  const alerts = observability.active_alerts;
  const activeOrderCount = healthDetailNumber(paper, "active_order_count");
  const marketData = observabilityComponent(observability, "market_data");
  const datasetRegistry = observabilityComponent(observability, "dataset_registry");
  const backtestResearch = observabilityComponent(observability, "backtest_research");
  const strategy = observabilityComponent(observability, "strategy_audit_factory");
  const tradingRunner = observabilityComponent(observability, "trading_runner");
  const paperExecution = observabilityComponent(observability, "paper_execution");
  const positions = observabilityComponent(observability, "portfolio_position");
  const riskSizing = observabilityComponent(observability, "risk_sizing");
  const riskSupervisor = observabilityComponent(observability, "risk_supervisor");
  const acceptance = observabilityComponent(observability, "system_acceptance_recovery");
  const notifications = observabilityComponent(observability, "signal_bot");

  return (
    <div>
      <section className="control-summary-grid" aria-label="System summary">
        <MetricCard
          label="System"
          detail={`Evaluated ${formatDateTime(observability.overview.evaluated_at)}`}
        >
          <ObservabilityHealthBadge status={observability.overview.overall_health} />
        </MetricCard>
        <MetricCard label="Mode" value="PAPER" detail="Live trading is not available." />
        <MetricCard
          label="Components"
          value={String(observability.overview.components.length)}
          detail="Unified authoritative projections"
        />
        <MetricCard
          label="Alerts"
          value={String(alerts.length)}
          detail="Backend-deduplicated active conditions"
        />
      </section>

      <section
        className="control-summary-grid control-operational-grid"
        aria-label="Operational summary"
      >
        <ObservabilityHealthMetric label="Data" health={marketData} />
        <ObservabilityHealthMetric label="Dataset Registry" health={datasetRegistry} />
        <ObservabilityHealthMetric label="Strategy / Audit" health={strategy} />
        <ObservabilityHealthMetric label="Backtest / Research" health={backtestResearch} />
        <MetricCard
          label="Backtest Jobs"
          value={backtestValue(
            backtest,
            activeJobs,
            latestJob,
            backtestsPending,
            backtestsFailed,
          )}
          detail={backtestDetail(
            backtestAvailable,
            activeJobs,
            latestJob,
            backtestsPending,
            backtestsFailed,
          )}
        />
        <ObservabilityHealthMetric label="Trading Runner" health={tradingRunner} />
        <ObservabilityHealthMetric label="Paper Execution" health={paperExecution} />
        <MetricCard
          label="Orders"
          value={activeOrderCount === null ? "—" : String(activeOrderCount)}
          detail="Active PAPER orders from runtime owner detail"
        />
        <ObservabilityHealthMetric label="Positions" health={positions} />
        <ObservabilityHealthMetric label="Risk Sizing" health={riskSizing} />
        <ObservabilityHealthMetric label="Risk Supervisor" health={riskSupervisor} />
        <ObservabilityHealthMetric label="Acceptance / Recovery" health={acceptance} />
        <ObservabilityHealthMetric label="Notifications" health={notifications} />
        <MetricCard
          label="CPU"
          value={resources ? `${resources.cpu_percent.toFixed(1)}%` : "—"}
          detail={resourcesFailed ? "Resource read unavailable" : "Aggregate host utilization"}
        />
        <MetricCard
          label="RAM"
          value={resources ? `${resources.memory.percent.toFixed(1)}%` : "—"}
          detail={
            resourcesFailed ? "Resource read unavailable" : formatBytes(resources?.memory.used_bytes)
          }
        />
        <MetricCard
          label="Disk"
          value={resources ? `${resources.disk.percent.toFixed(1)}%` : "—"}
          detail={
            resourcesFailed ? "Resource read unavailable" : formatBytes(resources?.disk.used_bytes)
          }
        />
        <MetricCard
          label="Control Plane"
          value={resources ? formatBytes(resources.control_process_rss_bytes) : "—"}
          detail={
            resources
              ? `${resources.control_process_threads} threads`
              : "Read-only resource snapshot"
          }
        />
      </section>

      {resourcesFailed ? (
        <InlineReadError
          title="Resource snapshot unavailable"
          action="Unified component health remains authoritative; retry resources independently."
        />
      ) : null}

      <section className="control-panel" aria-label="Active backtests">
        <div className="control-panel-heading">
          <div>
            <h3>Active Backtests</h3>
            <p>Durable jobs continue independently of this browser view.</p>
          </div>
          <small>{backtestAvailable && !backtestsFailed ? `${activeJobs.length} active` : "unavailable"}</small>
        </div>
        {!backtestAvailable ? (
          <div className="control-notice" role="status">
            <strong>Backtest job projection unavailable</strong>
            <span>The Backtest adapter is unavailable; no durable job-state claim is made.</span>
          </div>
        ) : null}
        {backtestAvailable && backtestsPending ? (
          <p className="backtest-inline-hint">Reading durable jobs…</p>
        ) : null}
        {backtestAvailable && backtestsFailed ? (
          <InlineReadError
            title="Backtest list unavailable"
            action="Backtest job detail remains separate from unified health."
          />
        ) : null}
        {backtestAvailable && !backtestsPending && !backtestsFailed && activeJobs.length === 0 ? (
          <div className="control-notice" role="status">
            <strong>No active Backtest jobs</strong>
            <span>
              {latestJob
                ? `Latest durable job: ${latestJob.job.status}.`
                : "No durable jobs recorded."}
            </span>
          </div>
        ) : null}
        {backtestAvailable && !backtestsFailed && activeJobs.length > 0 ? (
          <div className="component-health-grid">
            {activeJobs.slice(0, MAX_ACTIVE_BACKTESTS).map((record) => (
              <article className="component-health-card" key={record.job.job_id}>
                <div className="component-health-heading">
                  <strong>{record.config_id}</strong>
                  <span className="backtest-job-status backtest-job-status-active">
                    {record.job.status}
                  </span>
                </div>
                <p>{record.job.phase}</p>
                <small>
                  {record.job.progress?.percent === null || record.job.progress === null
                    ? "Progress is not determinate"
                    : `${record.job.progress.percent.toFixed(1)}%`}
                </small>
              </article>
            ))}
          </div>
        ) : null}
      </section>

      <section className="control-panel" aria-label="Current alerts">
        <div className="control-panel-heading">
          <div>
            <h3>Current alerts</h3>
            <p>Backend-owned dedupe/cooldown state; browser does not recreate alert policy.</p>
          </div>
          <small>{alerts.length} current</small>
        </div>
        {alerts.length === 0 ? (
          <div className="control-notice" role="status">
            <strong>No active observability alerts</strong>
            <span>The backend alert engine reports no current alert conditions.</span>
          </div>
        ) : (
          <div className="component-health-grid">
            {alerts.map((alert) => (
              <article className="component-health-card" key={alert.alert_id}>
                <div className="component-health-heading">
                  <strong>{observabilityComponentLabel(alert.component)}</strong>
                  <AlertSeverityBadge severity={alert.severity} />
                </div>
                <p>{alert.reason_codes.join(", ")}</p>
                <small>Next action: {alert.next_action}</small>
                <small>Evaluated {formatDateTime(alert.evaluated_at)}</small>
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function MetricCard({
  label,
  value,
  detail,
  children,
}: {
  label: string;
  value?: string;
  detail: string;
  children?: ReactNode;
}) {
  return (
    <article className="control-metric-card">
      <span>{label}</span>
      {children ?? <strong>{value ?? "—"}</strong>}
      <small>{detail}</small>
    </article>
  );
}

function ObservabilityHealthMetric({
  label,
  health,
}: {
  label: string;
  health: ObservabilityComponentView;
}) {
  return (
    <MetricCard label={label} detail={observabilityHealthDetail(health)}>
      <ObservabilityHealthBadge status={health.health} />
    </MetricCard>
  );
}

function ObservabilityHealthBadge({ status }: { status: ObservabilityHealth }) {
  const symbol = status === "healthy" ? "✓" : status === "degraded" ? "!" : "×";
  const visualStatus = status === "blocked" ? "unavailable" : status;
  return (
    <span className={`control-status control-status-${visualStatus}`} aria-label={status.toUpperCase()}>
      <span aria-hidden="true">{symbol}</span>
      {status.toUpperCase()}
    </span>
  );
}

function AlertSeverityBadge({ severity }: { severity: ActiveObservabilityAlert["severity"] }) {
  const visualStatus = severity === "critical" ? "unavailable" : "degraded";
  return (
    <span className={`control-status control-status-${visualStatus}`} aria-label={severity.toUpperCase()}>
      <span aria-hidden="true">{severity === "critical" ? "×" : "!"}</span>
      {severity.toUpperCase()}
    </span>
  );
}

function InlineReadError({ title, action }: { title: string; action: string }) {
  return (
    <div className="control-notice control-notice-error operational-read-error" role="alert">
      <strong>{title}</strong>
      <span>{action}</span>
    </div>
  );
}

function OverviewLoading() {
  return (
    <div className="control-notice" aria-live="polite">
      <strong>Loading operational state…</strong>
      <span>Reading legacy detail and unified observability projections.</span>
    </div>
  );
}

function backtestValue(
  health: ComponentHealth | undefined,
  activeJobs: BacktestJobRecord[],
  latestJob: BacktestJobRecord | undefined,
  pending: boolean,
  failed: boolean,
): string {
  if (!health || health.status === "UNAVAILABLE") return "UNAVAILABLE";
  if (failed) return "READ ERROR";
  if (pending) return "LOADING";
  if (activeJobs.length > 0) return activeJobs[0]?.job.status ?? "RUNNING";
  return latestJob?.job.status ?? "IDLE";
}

function backtestDetail(
  available: boolean,
  activeJobs: BacktestJobRecord[],
  latestJob: BacktestJobRecord | undefined,
  pending: boolean,
  failed: boolean,
): string {
  if (!available) return "Backtest adapter unavailable";
  if (failed) return "Durable job list unavailable";
  if (pending) return "Reading durable jobs";
  if (activeJobs.length > 0) return `${activeJobs.length} active durable job(s)`;
  if (latestJob) return `Latest: ${latestJob.config_id}`;
  return "No active durable jobs";
}

function observabilityHealthDetail(health: ObservabilityComponentView): string {
  const flags = [];
  if (health.stale) flags.push("stale");
  if (health.stuck) flags.push("stuck");
  const reasons = health.reason_codes.length > 0 ? health.reason_codes.join(", ") : "owner healthy";
  return flags.length > 0 ? `${reasons} · ${flags.join(", ")}` : reasons;
}

function observabilityComponentLabel(component: ActiveObservabilityAlert["component"]): string {
  const labels: Record<ActiveObservabilityAlert["component"], string> = {
    backtest_research: "Backtest / Research",
    dataset_registry: "Dataset Registry",
    market_data: "Market Data",
    paper_execution: "Paper Execution",
    portfolio_position: "Portfolio / Position",
    risk_sizing: "Risk Sizing",
    risk_supervisor: "Risk Supervisor",
    signal_bot: "Signal Bot",
    strategy_audit_factory: "Strategy / Audit",
    system_acceptance_recovery: "Acceptance / Recovery",
    system_resources: "System Resources",
    trading_runner: "Trading Runner",
  };
  return labels[component];
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

function formatBytes(value: number | undefined): string {
  if (value === undefined || !Number.isFinite(value) || value < 0) return "—";
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
