import type { BacktestJobRecord, BacktestJobStatus } from "../backtest/contracts";
import type {
  ComponentHealth,
  ControlComponentId,
  SafeScalar,
  SystemOverview,
} from "../control/contracts";

const ACTIVE_BACKTEST_STATUSES = new Set<BacktestJobStatus>([
  "QUEUED",
  "STARTING",
  "RUNNING",
  "PAUSING",
  "PAUSED",
  "STOPPING",
  "RECOVERING",
  "BLOCKED",
]);

export function componentHealth(
  overview: SystemOverview,
  component: ControlComponentId,
): ComponentHealth | undefined {
  return overview.components.find((item) => item.component === component);
}

export function activeBacktestJobs(jobs: BacktestJobRecord[]): BacktestJobRecord[] {
  return jobs.filter((item) => ACTIVE_BACKTEST_STATUSES.has(item.job.status));
}

export function latestBacktestJob(jobs: BacktestJobRecord[]): BacktestJobRecord | undefined {
  return jobs[0];
}

export function healthDetail(
  health: ComponentHealth | undefined,
  key: string,
): SafeScalar | undefined {
  return health?.details[key];
}

export function healthDetailText(
  health: ComponentHealth | undefined,
  key: string,
): string | null {
  const value = healthDetail(health, key);
  return typeof value === "string" && value.length > 0 ? value : null;
}

export function healthDetailNumber(
  health: ComponentHealth | undefined,
  key: string,
): number | null {
  const value = healthDetail(health, key);
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
