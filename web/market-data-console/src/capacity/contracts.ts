export type CapacityMeasurementUnit = "bytes" | "bytes_per_run";
export type RunnerCapacityRole = "fast" | "docker" | "macos" | "backtester";
export type CapacityTierId =
  | "system_acceptance"
  | "market_data_registry_storage"
  | "market_data_application_control_plane_storage"
  | "research_platform"
  | "core_paper_trading_durable_state";

export interface CapacityMeasurement {
  schema_version: 1;
  metric: string;
  value: number;
  unit: CapacityMeasurementUnit;
}

export interface RunnerCapacityWatermark {
  schema_version: 1;
  role: RunnerCapacityRole;
  high_water_free_bytes: number;
  critical_free_bytes: number;
}

export interface CapacityPhysicalBoundary {
  schema_version: 1;
  status: string;
  high_water_free_bytes: number | null;
  critical_free_bytes: number | null;
}

export interface SystemCapacityTier {
  schema_version: 1;
  tier_id: CapacityTierId;
  kind: string;
  status: string;
  physical_free_disk: CapacityPhysicalBoundary;
  deletion_authority: "NONE";
  next_publication_admission: boolean;
  recovery_reserve_bytes: number | null;
  owner_hard_limit_bytes: number | null;
  owner_payload_limit_bytes: number | null;
  measurements_aggregatable: false;
  measurements: CapacityMeasurement[];
}

export interface SystemCapacitySnapshot {
  schema_version: 1;
  observed_at: string;
  source_schema_version: 2;
  source_digest_sha256: string;
  policy_status: string;
  unknown_means: "NO_DESTRUCTIVE_OR_CAPACITY_CLAIM";
  global_local_disk_watermark_available: false;
  runner_watermarks: RunnerCapacityWatermark[];
  tiers: SystemCapacityTier[];
}

export interface ExternalArtifactCapacitySnapshot {
  schema_version: 1;
  observed_at: string;
  source_schema_version: 2;
  source_digest_sha256: string;
  kind: "external_quota_and_maintenance_bound";
  status: "PARTIAL_EXTERNAL_QUOTA";
  proactive_usage_status: "UNKNOWN_NO_ACCEPTED_USAGE_SOURCE";
  critical_signal: "CREATE_ARTIFACT_QUOTA_FAILURE";
  maintenance_max_count: number;
  maintenance_max_bytes: number;
  rate_limit_reserve_requests: number;
  local_disk_watermark_applicable: false;
  operator_cleanup_available: false;
}

const RUNNER_ROLES = new Set<RunnerCapacityRole>(["fast", "docker", "macos", "backtester"]);
const TIER_IDS = new Set<CapacityTierId>([
  "system_acceptance",
  "market_data_registry_storage",
  "market_data_application_control_plane_storage",
  "research_platform",
  "core_paper_trading_durable_state",
]);
const SNAPSHOT_KEYS = new Set([
  "schema_version",
  "observed_at",
  "source_schema_version",
  "source_digest_sha256",
  "policy_status",
  "unknown_means",
  "global_local_disk_watermark_available",
  "runner_watermarks",
  "tiers",
]);
const ARTIFACT_CAPACITY_KEYS = new Set([
  "schema_version",
  "observed_at",
  "source_schema_version",
  "source_digest_sha256",
  "kind",
  "status",
  "proactive_usage_status",
  "critical_signal",
  "maintenance_max_count",
  "maintenance_max_bytes",
  "rate_limit_reserve_requests",
  "local_disk_watermark_applicable",
  "operator_cleanup_available",
]);
const WATERMARK_KEYS = new Set([
  "schema_version",
  "role",
  "high_water_free_bytes",
  "critical_free_bytes",
]);
const PHYSICAL_KEYS = new Set([
  "schema_version",
  "status",
  "high_water_free_bytes",
  "critical_free_bytes",
]);
const TIER_KEYS = new Set([
  "schema_version",
  "tier_id",
  "kind",
  "status",
  "physical_free_disk",
  "deletion_authority",
  "next_publication_admission",
  "recovery_reserve_bytes",
  "owner_hard_limit_bytes",
  "owner_payload_limit_bytes",
  "measurements_aggregatable",
  "measurements",
]);
const MEASUREMENT_KEYS = new Set(["schema_version", "metric", "value", "unit"]);
const STATUS_PATTERN = /^[A-Z0-9_]{1,128}$/;
const KIND_PATTERN = /^[a-z0-9_]{1,128}$/;
const METRIC_PATTERN = /^[a-z0-9_]{1,96}$/;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;

export function isSystemCapacitySnapshot(value: unknown): value is SystemCapacitySnapshot {
  if (!isRecord(value) || !hasOnlyKeys(value, SNAPSHOT_KEYS)) return false;
  if (
    value.schema_version !== 1 ||
    value.source_schema_version !== 2 ||
    typeof value.observed_at !== "string" ||
    typeof value.source_digest_sha256 !== "string" ||
    !SHA256_PATTERN.test(value.source_digest_sha256) ||
    !isStatus(value.policy_status) ||
    value.unknown_means !== "NO_DESTRUCTIVE_OR_CAPACITY_CLAIM" ||
    value.global_local_disk_watermark_available !== false ||
    !Array.isArray(value.runner_watermarks) ||
    value.runner_watermarks.length !== 4 ||
    !Array.isArray(value.tiers) ||
    value.tiers.length !== 5
  ) {
    return false;
  }
  if (!value.runner_watermarks.every(isRunnerCapacityWatermark)) return false;
  if (!value.tiers.every(isSystemCapacityTier)) return false;
  const roles = new Set(value.runner_watermarks.map((item) => item.role));
  const tiers = new Set(value.tiers.map((item) => item.tier_id));
  return roles.size === 4 && tiers.size === 5;
}

export function isExternalArtifactCapacitySnapshot(
  value: unknown,
): value is ExternalArtifactCapacitySnapshot {
  return (
    isRecord(value) &&
    hasOnlyKeys(value, ARTIFACT_CAPACITY_KEYS) &&
    value.schema_version === 1 &&
    value.source_schema_version === 2 &&
    typeof value.observed_at === "string" &&
    typeof value.source_digest_sha256 === "string" &&
    SHA256_PATTERN.test(value.source_digest_sha256) &&
    value.kind === "external_quota_and_maintenance_bound" &&
    value.status === "PARTIAL_EXTERNAL_QUOTA" &&
    value.proactive_usage_status === "UNKNOWN_NO_ACCEPTED_USAGE_SOURCE" &&
    value.critical_signal === "CREATE_ARTIFACT_QUOTA_FAILURE" &&
    isPositiveInteger(value.maintenance_max_count) &&
    isPositiveInteger(value.maintenance_max_bytes) &&
    isPositiveInteger(value.rate_limit_reserve_requests) &&
    value.local_disk_watermark_applicable === false &&
    value.operator_cleanup_available === false
  );
}

function isRunnerCapacityWatermark(value: unknown): value is RunnerCapacityWatermark {
  if (!isRecord(value) || !hasOnlyKeys(value, WATERMARK_KEYS)) return false;
  if (
    value.schema_version !== 1 ||
    typeof value.role !== "string" ||
    !RUNNER_ROLES.has(value.role as RunnerCapacityRole) ||
    !isPositiveInteger(value.high_water_free_bytes) ||
    !isPositiveInteger(value.critical_free_bytes)
  ) {
    return false;
  }
  return value.high_water_free_bytes > value.critical_free_bytes;
}

function isCapacityPhysicalBoundary(value: unknown): value is CapacityPhysicalBoundary {
  if (!isRecord(value) || !hasOnlyKeys(value, PHYSICAL_KEYS)) return false;
  if (value.schema_version !== 1 || !isStatus(value.status)) return false;
  const high = value.high_water_free_bytes;
  const critical = value.critical_free_bytes;
  if (high === null || critical === null) return high === null && critical === null;
  return isNonNegativeInteger(high) && isNonNegativeInteger(critical) && high > critical;
}

function isSystemCapacityTier(value: unknown): value is SystemCapacityTier {
  if (!isRecord(value) || !hasOnlyKeys(value, TIER_KEYS)) return false;
  if (
    value.schema_version !== 1 ||
    typeof value.tier_id !== "string" ||
    !TIER_IDS.has(value.tier_id as CapacityTierId) ||
    typeof value.kind !== "string" ||
    !KIND_PATTERN.test(value.kind) ||
    !isStatus(value.status) ||
    !isCapacityPhysicalBoundary(value.physical_free_disk) ||
    value.deletion_authority !== "NONE" ||
    typeof value.next_publication_admission !== "boolean" ||
    value.measurements_aggregatable !== false ||
    !isOptionalNonNegativeInteger(value.recovery_reserve_bytes) ||
    !isOptionalNonNegativeInteger(value.owner_hard_limit_bytes) ||
    !isOptionalNonNegativeInteger(value.owner_payload_limit_bytes) ||
    !Array.isArray(value.measurements) ||
    value.measurements.length > 16 ||
    !value.measurements.every(isCapacityMeasurement)
  ) {
    return false;
  }
  return true;
}

function isCapacityMeasurement(value: unknown): value is CapacityMeasurement {
  return (
    isRecord(value) &&
    hasOnlyKeys(value, MEASUREMENT_KEYS) &&
    value.schema_version === 1 &&
    typeof value.metric === "string" &&
    METRIC_PATTERN.test(value.metric) &&
    isNonNegativeNumber(value.value) &&
    (value.unit === "bytes" || value.unit === "bytes_per_run")
  );
}

function isStatus(value: unknown): value is string {
  return typeof value === "string" && STATUS_PATTERN.test(value);
}

function isOptionalNonNegativeInteger(value: unknown): value is number | null {
  return value === null || isNonNegativeInteger(value);
}

function isPositiveInteger(value: unknown): value is number {
  return isNonNegativeInteger(value) && value > 0;
}

function isNonNegativeInteger(value: unknown): value is number {
  return isNonNegativeNumber(value) && Number.isInteger(value);
}

function isNonNegativeNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function hasOnlyKeys(value: Record<string, unknown>, allowed: Set<string>): boolean {
  return Object.keys(value).every((key) => allowed.has(key));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
