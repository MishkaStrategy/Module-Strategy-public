export type ControlHealthStatus = "HEALTHY" | "DEGRADED" | "UNAVAILABLE";

export type ControlComponentId =
  | "acceptance"
  | "backtest"
  | "evidence"
  | "market_data"
  | "paper_runtime"
  | "research"
  | "signal_bot"
  | "strategy";

export type SafeScalar = string | number | boolean | null;

export interface ComponentHealth {
  schema_version: 1;
  component: ControlComponentId;
  status: ControlHealthStatus;
  checked_at: string;
  summary: string;
  details: Record<string, SafeScalar>;
}

export interface SystemOverview {
  schema_version: 1;
  mode: "PAPER";
  status: ControlHealthStatus;
  checked_at: string;
  components: ComponentHealth[];
}

export interface ResourceUsage {
  schema_version: 1;
  total_bytes: number;
  used_bytes: number;
  available_bytes: number;
  percent: number;
}

export interface SystemResourcesSnapshot {
  schema_version: 1;
  collected_at: string;
  cpu_percent: number;
  logical_cpu_count: number | null;
  memory: ResourceUsage;
  disk: ResourceUsage;
  uptime_seconds: number;
  process_count: number;
  control_process_rss_bytes: number;
  control_process_threads: number;
}

export interface ControlApiVersion {
  schema_version: 1;
  api_version: "v1";
  mode: "PAPER";
  remote_access_allowed: false;
}

export interface ControlApiErrorDetail {
  schema_version: 1;
  code: string;
  message: string;
  action: string | null;
  error_id: string;
  component: ControlComponentId | null;
  operation: string;
  occurred_at: string;
  retryable: boolean;
  technical_details: Record<string, SafeScalar>;
}

export interface ControlApiErrorEnvelope {
  schema_version: 1;
  error: ControlApiErrorDetail;
}

const HEALTH_STATUSES = new Set<ControlHealthStatus>(["HEALTHY", "DEGRADED", "UNAVAILABLE"]);
const COMPONENT_IDS = new Set<ControlComponentId>([
  "acceptance",
  "backtest",
  "evidence",
  "market_data",
  "paper_runtime",
  "research",
  "signal_bot",
  "strategy",
]);
const SYSTEM_RESOURCE_KEYS = new Set([
  "schema_version",
  "collected_at",
  "cpu_percent",
  "logical_cpu_count",
  "memory",
  "disk",
  "uptime_seconds",
  "process_count",
  "control_process_rss_bytes",
  "control_process_threads",
]);
const RESOURCE_USAGE_KEYS = new Set([
  "schema_version",
  "total_bytes",
  "used_bytes",
  "available_bytes",
  "percent",
]);

export function isControlHealthStatus(value: unknown): value is ControlHealthStatus {
  return typeof value === "string" && HEALTH_STATUSES.has(value as ControlHealthStatus);
}

export function isControlComponentId(value: unknown): value is ControlComponentId {
  return typeof value === "string" && COMPONENT_IDS.has(value as ControlComponentId);
}

export function isSystemOverview(value: unknown): value is SystemOverview {
  if (!isRecord(value)) return false;
  if (value.schema_version !== 1 || value.mode !== "PAPER" || !isControlHealthStatus(value.status)) {
    return false;
  }
  if (typeof value.checked_at !== "string" || !Array.isArray(value.components)) return false;
  return value.components.every(isComponentHealth);
}

export function isSystemResourcesSnapshot(value: unknown): value is SystemResourcesSnapshot {
  if (!isRecord(value) || !hasOnlyKeys(value, SYSTEM_RESOURCE_KEYS) || value.schema_version !== 1) {
    return false;
  }
  return (
    typeof value.collected_at === "string" &&
    isBoundedPercent(value.cpu_percent) &&
    (value.logical_cpu_count === null || isPositiveInteger(value.logical_cpu_count)) &&
    isResourceUsage(value.memory) &&
    isResourceUsage(value.disk) &&
    isNonNegativeNumber(value.uptime_seconds) &&
    isPositiveInteger(value.process_count) &&
    isNonNegativeInteger(value.control_process_rss_bytes) &&
    isPositiveInteger(value.control_process_threads)
  );
}

export function isComponentHealth(value: unknown): value is ComponentHealth {
  if (!isRecord(value)) return false;
  return (
    value.schema_version === 1 &&
    isControlComponentId(value.component) &&
    isControlHealthStatus(value.status) &&
    typeof value.checked_at === "string" &&
    typeof value.summary === "string" &&
    isSafeScalarRecord(value.details)
  );
}

export function isControlApiVersion(value: unknown): value is ControlApiVersion {
  return (
    isRecord(value) &&
    value.schema_version === 1 &&
    value.api_version === "v1" &&
    value.mode === "PAPER" &&
    value.remote_access_allowed === false
  );
}

export function isControlApiErrorEnvelope(value: unknown): value is ControlApiErrorEnvelope {
  if (!isRecord(value) || value.schema_version !== 1 || !isRecord(value.error)) return false;
  const error = value.error;
  return (
    error.schema_version === 1 &&
    typeof error.code === "string" &&
    typeof error.message === "string" &&
    (error.action === null || typeof error.action === "string") &&
    typeof error.error_id === "string" &&
    (error.component === null || isControlComponentId(error.component)) &&
    typeof error.operation === "string" &&
    typeof error.occurred_at === "string" &&
    typeof error.retryable === "boolean" &&
    isSafeScalarRecord(error.technical_details)
  );
}

function isResourceUsage(value: unknown): value is ResourceUsage {
  if (!isRecord(value) || !hasOnlyKeys(value, RESOURCE_USAGE_KEYS) || value.schema_version !== 1) {
    return false;
  }
  if (
    !isNonNegativeInteger(value.total_bytes) ||
    !isNonNegativeInteger(value.used_bytes) ||
    !isNonNegativeInteger(value.available_bytes) ||
    !isBoundedPercent(value.percent)
  ) {
    return false;
  }
  return value.used_bytes <= value.total_bytes && value.available_bytes <= value.total_bytes;
}

function hasOnlyKeys(value: Record<string, unknown>, allowed: Set<string>): boolean {
  return Object.keys(value).every((key) => allowed.has(key));
}

function isBoundedPercent(value: unknown): value is number {
  return isNonNegativeNumber(value) && value <= 100;
}

function isNonNegativeNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function isNonNegativeInteger(value: unknown): value is number {
  return isNonNegativeNumber(value) && Number.isInteger(value);
}

function isPositiveInteger(value: unknown): value is number {
  return isNonNegativeInteger(value) && value >= 1;
}

function isSafeScalarRecord(value: unknown): value is Record<string, SafeScalar> {
  if (!isRecord(value)) return false;
  return Object.values(value).every(
    (item) =>
      item === null ||
      typeof item === "string" ||
      typeof item === "number" ||
      typeof item === "boolean",
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
