import type { SafeScalar } from "./contracts";

export type ObservabilityHealth = "healthy" | "degraded" | "blocked" | "unavailable";
export type ObservabilityAlertSeverity = "warning" | "critical";

export type ObservabilityComponentId =
  | "backtest_research"
  | "dataset_registry"
  | "market_data"
  | "paper_execution"
  | "portfolio_position"
  | "risk_sizing"
  | "risk_supervisor"
  | "signal_bot"
  | "strategy_audit_factory"
  | "system_acceptance_recovery"
  | "system_resources"
  | "trading_runner";

export interface ObservabilityComponentView {
  schema_version: 1;
  component: ObservabilityComponentId;
  health: ObservabilityHealth;
  observed_at: string | null;
  evaluated_at: string;
  age_seconds: number | null;
  stale: boolean;
  progress_at: string | null;
  progress_age_seconds: number | null;
  stuck: boolean;
  state_ref: string | null;
  reason_codes: string[];
  correlation_id: string | null;
  public_details: Record<string, SafeScalar>;
  observation_hash: string;
}

export interface UnifiedOperationalOverview {
  schema_version: 1;
  evaluated_at: string;
  overall_health: ObservabilityHealth;
  overview_hash: string;
  components: ObservabilityComponentView[];
}

export interface ActiveObservabilityAlert {
  schema_version: 1;
  alert_id: string;
  component: ObservabilityComponentId;
  severity: ObservabilityAlertSeverity;
  reason_codes: string[];
  next_action: string;
  evaluated_at: string;
  source_observation_hash: string;
  correlation_id: string | null;
}

export interface UnifiedObservabilitySnapshot {
  schema_version: 1;
  mode: "PAPER";
  overview: UnifiedOperationalOverview;
  active_alerts: ActiveObservabilityAlert[];
}

const COMPONENT_IDS = [
  "backtest_research",
  "dataset_registry",
  "market_data",
  "paper_execution",
  "portfolio_position",
  "risk_sizing",
  "risk_supervisor",
  "signal_bot",
  "strategy_audit_factory",
  "system_acceptance_recovery",
  "system_resources",
  "trading_runner",
] as const satisfies readonly ObservabilityComponentId[];

const COMPONENT_SET = new Set<ObservabilityComponentId>(COMPONENT_IDS);
const HEALTH_SET = new Set<ObservabilityHealth>([
  "healthy",
  "degraded",
  "blocked",
  "unavailable",
]);
const SEVERITY_SET = new Set<ObservabilityAlertSeverity>(["warning", "critical"]);
const DIGEST = /^[0-9a-f]{64}$/;
const REASON = /^[a-z0-9][a-z0-9_.:-]{0,127}$/;
const NEXT_ACTION = REASON;
const CORRELATION = /^[A-Za-z0-9._:-]{1,128}$/;
const DETAIL_KEY = /^[a-z0-9][a-z0-9_.:-]{0,63}$/;
const AWARE_TIMESTAMP = /(?:Z|[+-]\d{2}:\d{2})$/;

const SNAPSHOT_KEYS = new Set(["schema_version", "mode", "overview", "active_alerts"]);
const OVERVIEW_KEYS = new Set([
  "schema_version",
  "evaluated_at",
  "overall_health",
  "overview_hash",
  "components",
]);
const COMPONENT_KEYS = new Set([
  "schema_version",
  "component",
  "health",
  "observed_at",
  "evaluated_at",
  "age_seconds",
  "stale",
  "progress_at",
  "progress_age_seconds",
  "stuck",
  "state_ref",
  "reason_codes",
  "correlation_id",
  "public_details",
  "observation_hash",
]);
const ALERT_KEYS = new Set([
  "schema_version",
  "alert_id",
  "component",
  "severity",
  "reason_codes",
  "next_action",
  "evaluated_at",
  "source_observation_hash",
  "correlation_id",
]);

export function isUnifiedObservabilitySnapshot(value: unknown): value is UnifiedObservabilitySnapshot {
  if (!isRecord(value) || !hasOnlyKeys(value, SNAPSHOT_KEYS)) return false;
  if (value.schema_version !== 1 || value.mode !== "PAPER") return false;
  if (!isUnifiedOverview(value.overview) || !Array.isArray(value.active_alerts)) return false;
  if (value.active_alerts.length > 32 || !value.active_alerts.every(isActiveAlert)) return false;

  const components = new Map(value.overview.components.map((item) => [item.component, item]));
  const alertIds = new Set<string>();
  const alertComponents = new Set<ObservabilityComponentId>();
  for (const alert of value.active_alerts) {
    if (alertIds.has(alert.alert_id) || alertComponents.has(alert.component)) return false;
    alertIds.add(alert.alert_id);
    alertComponents.add(alert.component);
    const component = components.get(alert.component);
    if (!component || component.health === "healthy") return false;
    if (
      component.observation_hash !== alert.source_observation_hash ||
      component.evaluated_at !== alert.evaluated_at
    ) {
      return false;
    }
  }
  return true;
}

export function observabilityComponent(
  snapshot: UnifiedObservabilitySnapshot,
  component: ObservabilityComponentId,
): ObservabilityComponentView {
  const value = snapshot.overview.components.find((item) => item.component === component);
  if (!value) throw new Error(`Observability component missing: ${component}`);
  return value;
}

function isUnifiedOverview(value: unknown): value is UnifiedOperationalOverview {
  if (!isRecord(value) || !hasOnlyKeys(value, OVERVIEW_KEYS)) return false;
  if (
    value.schema_version !== 1 ||
    !isTimestamp(value.evaluated_at) ||
    !isHealth(value.overall_health) ||
    !isDigest(value.overview_hash) ||
    !Array.isArray(value.components) ||
    value.components.length !== COMPONENT_IDS.length ||
    !value.components.every(isComponentView)
  ) {
    return false;
  }
  const ids = value.components.map((item) => item.component);
  if (ids.some((item, index) => item !== COMPONENT_IDS[index])) return false;
  if (value.components.some((item) => item.evaluated_at !== value.evaluated_at)) return false;
  return value.overall_health === aggregateHealth(value.components);
}

function isComponentView(value: unknown): value is ObservabilityComponentView {
  if (!isRecord(value) || !hasOnlyKeys(value, COMPONENT_KEYS)) return false;
  if (
    value.schema_version !== 1 ||
    !isComponentId(value.component) ||
    !isHealth(value.health) ||
    !isNullableTimestamp(value.observed_at) ||
    !isTimestamp(value.evaluated_at) ||
    !isNullableNonNegativeNumber(value.age_seconds) ||
    typeof value.stale !== "boolean" ||
    !isNullableTimestamp(value.progress_at) ||
    !isNullableNonNegativeNumber(value.progress_age_seconds) ||
    typeof value.stuck !== "boolean" ||
    !(value.state_ref === null || isDigest(value.state_ref)) ||
    !isReasonCodes(value.reason_codes, false) ||
    !(value.correlation_id === null || isCorrelationId(value.correlation_id)) ||
    !isPublicDetails(value.public_details) ||
    !isDigest(value.observation_hash)
  ) {
    return false;
  }
  if ((value.observed_at === null) !== (value.age_seconds === null)) return false;
  if ((value.progress_at === null) !== (value.progress_age_seconds === null)) return false;
  if (value.stuck && value.progress_at === null) return false;
  if (value.health === "healthy" ? value.reason_codes.length !== 0 : value.reason_codes.length === 0) {
    return false;
  }
  return true;
}

function isActiveAlert(value: unknown): value is ActiveObservabilityAlert {
  if (!isRecord(value) || !hasOnlyKeys(value, ALERT_KEYS)) return false;
  return (
    value.schema_version === 1 &&
    isDigest(value.alert_id) &&
    isComponentId(value.component) &&
    isSeverity(value.severity) &&
    isReasonCodes(value.reason_codes, true) &&
    typeof value.next_action === "string" &&
    NEXT_ACTION.test(value.next_action) &&
    isTimestamp(value.evaluated_at) &&
    isDigest(value.source_observation_hash) &&
    (value.correlation_id === null || isCorrelationId(value.correlation_id))
  );
}

function aggregateHealth(components: ObservabilityComponentView[]): ObservabilityHealth {
  const healths = new Set(components.map((item) => item.health));
  if (healths.has("unavailable")) return "unavailable";
  if (healths.has("blocked")) return "blocked";
  if (healths.has("degraded")) return "degraded";
  return "healthy";
}

function isReasonCodes(value: unknown, requireOne: boolean): value is string[] {
  if (!Array.isArray(value) || value.length > 32 || (requireOne && value.length === 0)) return false;
  if (!value.every((item) => typeof item === "string" && REASON.test(item))) return false;
  if (new Set(value).size !== value.length) return false;
  return value.every((item, index) => index === 0 || value[index - 1] <= item);
}

function isPublicDetails(value: unknown): value is Record<string, SafeScalar> {
  if (!isRecord(value) || Object.keys(value).length > 32) return false;
  return Object.entries(value).every(([key, item]) => {
    if (!DETAIL_KEY.test(key)) return false;
    if (item === null || typeof item === "boolean") return true;
    if (typeof item === "string") {
      return item.length <= 256 && !/[\u0000-\u001f\u007f]/.test(item);
    }
    if (typeof item === "number") {
      return Number.isFinite(item) && (!Number.isInteger(item) || Math.abs(item) <= Number.MAX_SAFE_INTEGER);
    }
    return false;
  });
}

function isComponentId(value: unknown): value is ObservabilityComponentId {
  return typeof value === "string" && COMPONENT_SET.has(value as ObservabilityComponentId);
}

function isHealth(value: unknown): value is ObservabilityHealth {
  return typeof value === "string" && HEALTH_SET.has(value as ObservabilityHealth);
}

function isSeverity(value: unknown): value is ObservabilityAlertSeverity {
  return typeof value === "string" && SEVERITY_SET.has(value as ObservabilityAlertSeverity);
}

function isDigest(value: unknown): value is string {
  return typeof value === "string" && DIGEST.test(value);
}

function isCorrelationId(value: unknown): value is string {
  return typeof value === "string" && CORRELATION.test(value);
}

function isTimestamp(value: unknown): value is string {
  if (typeof value !== "string" || value.length === 0 || !AWARE_TIMESTAMP.test(value)) return false;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed);
}

function isNullableTimestamp(value: unknown): value is string | null {
  return value === null || isTimestamp(value);
}

function isNullableNonNegativeNumber(value: unknown): value is number | null {
  return value === null || (typeof value === "number" && Number.isFinite(value) && value >= 0);
}

function hasOnlyKeys(value: Record<string, unknown>, allowed: Set<string>): boolean {
  return Object.keys(value).every((key) => allowed.has(key));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
