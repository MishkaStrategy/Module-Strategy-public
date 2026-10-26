export type ResearchExperimentNativeStatus =
  | "CREATED"
  | "VALIDATING"
  | "PLANNED"
  | "RUNNING"
  | "PAUSED"
  | "CANCELED"
  | "COMPLETED"
  | "COMPLETED_WITH_FAILURES"
  | "FAILED"
  | "BLOCKED"
  | "CORRUPTED";

export type ResearchRunNativeStatus =
  | "PLANNED"
  | "QUEUED"
  | "RUNNING"
  | "RETRY_WAIT"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELED"
  | "BLOCKED"
  | "CORRUPTED";

export type ResearchControlStatus =
  | "QUEUED"
  | "STARTING"
  | "RUNNING"
  | "PAUSING"
  | "PAUSED"
  | "STOPPING"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELLED"
  | "RECOVERING"
  | "BLOCKED";

export type ResearchFailureCategory =
  | "TRANSIENT"
  | "RESOURCE"
  | "INPUT"
  | "ENGINE"
  | "CORRUPTION"
  | "CANCELED"
  | "UNKNOWN";

export interface ResearchExperimentSummary {
  schema_version: 1;
  experiment_id: string;
  specification_sha256: string;
  native_status: ResearchExperimentNativeStatus;
  status: ResearchControlStatus;
  has_failures: boolean;
  created_at: string;
  updated_at: string;
  run_counts: Partial<Record<ResearchRunNativeStatus, number>>;
}

export interface ResearchExperimentList {
  schema_version: 1;
  experiments: ResearchExperimentSummary[];
  offset: number;
  limit: number;
  has_more: boolean;
}

export interface ResearchRunSummary {
  schema_version: 1;
  run_id: string;
  experiment_id: string;
  native_status: ResearchRunNativeStatus;
  status: ResearchControlStatus;
  stage_id: string;
  split_id: string;
  dataset_id: string;
  scenario_id: string;
  stage_role: string;
  repetition_index: number;
  attempt_count: number;
  max_attempts: number;
  created_at: string;
  updated_at: string;
  available_at: string;
  revision: number;
  lease_expires_at: string | null;
  failure_category: ResearchFailureCategory | null;
  failure_retryable: boolean | null;
  engine_run_id: string | null;
  bars_processed: number | null;
  restart_verified: boolean | null;
}

export interface ResearchRunList {
  schema_version: 1;
  experiment_id: string;
  runs: ResearchRunSummary[];
  offset: number;
  limit: number;
  has_more: boolean;
}

const EXPERIMENT_STATUSES = new Set<ResearchExperimentNativeStatus>([
  "CREATED",
  "VALIDATING",
  "PLANNED",
  "RUNNING",
  "PAUSED",
  "CANCELED",
  "COMPLETED",
  "COMPLETED_WITH_FAILURES",
  "FAILED",
  "BLOCKED",
  "CORRUPTED",
]);
const RUN_STATUSES = new Set<ResearchRunNativeStatus>([
  "PLANNED",
  "QUEUED",
  "RUNNING",
  "RETRY_WAIT",
  "SUCCEEDED",
  "FAILED",
  "CANCELED",
  "BLOCKED",
  "CORRUPTED",
]);
const CONTROL_STATUSES = new Set<ResearchControlStatus>([
  "QUEUED",
  "STARTING",
  "RUNNING",
  "PAUSING",
  "PAUSED",
  "STOPPING",
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
  "RECOVERING",
  "BLOCKED",
]);
const FAILURE_CATEGORIES = new Set<ResearchFailureCategory>([
  "TRANSIENT",
  "RESOURCE",
  "INPUT",
  "ENGINE",
  "CORRUPTION",
  "CANCELED",
  "UNKNOWN",
]);
const HEX_64 = /^[0-9a-f]{64}$/;

const EXPERIMENT_STATUS_MAP: Record<ResearchExperimentNativeStatus, ResearchControlStatus> = {
  CREATED: "QUEUED",
  VALIDATING: "STARTING",
  PLANNED: "QUEUED",
  RUNNING: "RUNNING",
  PAUSED: "PAUSED",
  CANCELED: "CANCELLED",
  COMPLETED: "SUCCEEDED",
  COMPLETED_WITH_FAILURES: "SUCCEEDED",
  FAILED: "FAILED",
  BLOCKED: "BLOCKED",
  CORRUPTED: "FAILED",
};
const RUN_STATUS_MAP: Record<ResearchRunNativeStatus, ResearchControlStatus> = {
  PLANNED: "QUEUED",
  QUEUED: "QUEUED",
  RUNNING: "RUNNING",
  RETRY_WAIT: "RECOVERING",
  SUCCEEDED: "SUCCEEDED",
  FAILED: "FAILED",
  CANCELED: "CANCELLED",
  BLOCKED: "BLOCKED",
  CORRUPTED: "FAILED",
};

export function parseResearchExperimentList(value: unknown): ResearchExperimentList {
  const record = asRecord(value, "Research experiment list");
  const experiments = record.experiments;
  if (!Array.isArray(experiments)) throw new Error("Research experiments must be an array");
  return {
    schema_version: literalOne(record.schema_version, "Research experiment list schema_version"),
    experiments: experiments.map(parseResearchExperimentSummary),
    offset: nonNegativeInteger(record.offset, "Research experiment list offset"),
    limit: boundedLimit(record.limit, "Research experiment list limit"),
    has_more: booleanValue(record.has_more, "Research experiment list has_more"),
  };
}

export function parseResearchExperimentSummary(value: unknown): ResearchExperimentSummary {
  const record = asRecord(value, "Research experiment");
  const nativeStatus = enumValue(
    record.native_status,
    EXPERIMENT_STATUSES,
    "Research experiment native_status",
  );
  const status = enumValue(record.status, CONTROL_STATUSES, "Research experiment status");
  if (status !== EXPERIMENT_STATUS_MAP[nativeStatus]) {
    throw new Error("Research experiment common status does not match native status");
  }
  const hasFailures = booleanValue(record.has_failures, "Research experiment has_failures");
  if (hasFailures !== (nativeStatus === "COMPLETED_WITH_FAILURES")) {
    throw new Error("Research experiment has_failures is inconsistent");
  }
  const specification = boundedString(
    record.specification_sha256,
    "Research specification_sha256",
    64,
  );
  if (!HEX_64.test(specification)) throw new Error("Research specification_sha256 is invalid");
  const createdAt = timestamp(record.created_at, "Research experiment created_at");
  const updatedAt = timestamp(record.updated_at, "Research experiment updated_at");
  if (Date.parse(updatedAt) < Date.parse(createdAt)) {
    throw new Error("Research experiment updated_at precedes created_at");
  }
  const rawCounts = asRecord(record.run_counts, "Research run_counts");
  const runCounts: Partial<Record<ResearchRunNativeStatus, number>> = {};
  for (const [key, item] of Object.entries(rawCounts)) {
    if (!RUN_STATUSES.has(key as ResearchRunNativeStatus)) {
      throw new Error("Research run_counts contains unsupported status");
    }
    runCounts[key as ResearchRunNativeStatus] = nonNegativeInteger(item, `Research run_counts.${key}`);
  }
  return {
    schema_version: literalOne(record.schema_version, "Research experiment schema_version"),
    experiment_id: identifier(record.experiment_id, "Research experiment_id"),
    specification_sha256: specification,
    native_status: nativeStatus,
    status,
    has_failures: hasFailures,
    created_at: createdAt,
    updated_at: updatedAt,
    run_counts: runCounts,
  };
}

export function parseResearchRunList(value: unknown): ResearchRunList {
  const record = asRecord(value, "Research run list");
  const runs = record.runs;
  if (!Array.isArray(runs)) throw new Error("Research runs must be an array");
  return {
    schema_version: literalOne(record.schema_version, "Research run list schema_version"),
    experiment_id: identifier(record.experiment_id, "Research run list experiment_id"),
    runs: runs.map(parseResearchRunSummary),
    offset: nonNegativeInteger(record.offset, "Research run list offset"),
    limit: boundedLimit(record.limit, "Research run list limit"),
    has_more: booleanValue(record.has_more, "Research run list has_more"),
  };
}

export function parseResearchRunSummary(value: unknown): ResearchRunSummary {
  const record = asRecord(value, "Research run");
  const nativeStatus = enumValue(record.native_status, RUN_STATUSES, "Research run native_status");
  const status = enumValue(record.status, CONTROL_STATUSES, "Research run status");
  if (status !== RUN_STATUS_MAP[nativeStatus]) {
    throw new Error("Research run common status does not match native status");
  }
  const createdAt = timestamp(record.created_at, "Research run created_at");
  const updatedAt = timestamp(record.updated_at, "Research run updated_at");
  const availableAt = timestamp(record.available_at, "Research run available_at");
  if (Date.parse(updatedAt) < Date.parse(createdAt)) {
    throw new Error("Research run updated_at precedes created_at");
  }
  const leaseExpiresAt = nullableTimestamp(record.lease_expires_at, "Research run lease_expires_at");
  if (nativeStatus === "RUNNING" && leaseExpiresAt === null) {
    throw new Error("RUNNING Research run requires lease_expires_at");
  }
  if (nativeStatus !== "RUNNING" && leaseExpiresAt !== null) {
    throw new Error("Only RUNNING Research run may expose lease_expires_at");
  }
  const failureCategory = nullableEnumValue(
    record.failure_category,
    FAILURE_CATEGORIES,
    "Research run failure_category",
  );
  const failureRetryable = nullableBoolean(record.failure_retryable, "Research run failure_retryable");
  if ((failureCategory === null) !== (failureRetryable === null)) {
    throw new Error("Research run failure projection is incomplete");
  }
  const engineRunId = nullableIdentifier(record.engine_run_id, "Research engine_run_id");
  const barsProcessed = nullableNonNegativeInteger(record.bars_processed, "Research bars_processed");
  const restartVerified = nullableBoolean(record.restart_verified, "Research restart_verified");
  const completeResult = engineRunId !== null && barsProcessed !== null && restartVerified !== null;
  const hasAnyResult = engineRunId !== null || barsProcessed !== null || restartVerified !== null;
  if (nativeStatus === "SUCCEEDED" && !completeResult) {
    throw new Error("SUCCEEDED Research run requires complete result identity");
  }
  if (nativeStatus !== "SUCCEEDED" && hasAnyResult) {
    throw new Error("Only SUCCEEDED Research run may expose result identity");
  }
  const attemptCount = nonNegativeInteger(record.attempt_count, "Research attempt_count");
  const maxAttempts = positiveInteger(record.max_attempts, "Research max_attempts");
  if (attemptCount > maxAttempts) throw new Error("Research attempt counters are inconsistent");
  return {
    schema_version: literalOne(record.schema_version, "Research run schema_version"),
    run_id: identifier(record.run_id, "Research run_id"),
    experiment_id: identifier(record.experiment_id, "Research run experiment_id"),
    native_status: nativeStatus,
    status,
    stage_id: identifier(record.stage_id, "Research stage_id"),
    split_id: identifier(record.split_id, "Research split_id"),
    dataset_id: identifier(record.dataset_id, "Research dataset_id"),
    scenario_id: identifier(record.scenario_id, "Research scenario_id"),
    stage_role: identifier(record.stage_role, "Research stage_role"),
    repetition_index: nonNegativeInteger(record.repetition_index, "Research repetition_index"),
    attempt_count: attemptCount,
    max_attempts: maxAttempts,
    created_at: createdAt,
    updated_at: updatedAt,
    available_at: availableAt,
    revision: nonNegativeInteger(record.revision, "Research revision"),
    lease_expires_at: leaseExpiresAt,
    failure_category: failureCategory,
    failure_retryable: failureRetryable,
    engine_run_id: engineRunId,
    bars_processed: barsProcessed,
    restart_verified: restartVerified,
  };
}

function asRecord(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function literalOne(value: unknown, label: string): 1 {
  if (value !== 1) throw new Error(`${label} must equal 1`);
  return 1;
}

function boundedString(value: unknown, label: string, maxLength: number): string {
  if (typeof value !== "string" || value.length < 1 || value.length > maxLength) {
    throw new Error(`${label} must contain 1..${maxLength} characters`);
  }
  return value;
}

function identifier(value: unknown, label: string): string {
  const result = boundedString(value, label, 256);
  if (/[\u0000\r\n]/u.test(result)) throw new Error(`${label} contains forbidden control characters`);
  return result;
}

function nullableIdentifier(value: unknown, label: string): string | null {
  if (value === null) return null;
  return identifier(value, label);
}

function timestamp(value: unknown, label: string): string {
  const result = boundedString(value, label, 64);
  if (!Number.isFinite(Date.parse(result))) throw new Error(`${label} must be an ISO timestamp`);
  return result;
}

function nullableTimestamp(value: unknown, label: string): string | null {
  if (value === null) return null;
  return timestamp(value, label);
}

function nonNegativeInteger(value: unknown, label: string): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
    throw new Error(`${label} must be a non-negative integer`);
  }
  return value;
}

function positiveInteger(value: unknown, label: string): number {
  const result = nonNegativeInteger(value, label);
  if (result < 1) throw new Error(`${label} must be positive`);
  return result;
}

function nullableNonNegativeInteger(value: unknown, label: string): number | null {
  if (value === null) return null;
  return nonNegativeInteger(value, label);
}

function booleanValue(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") throw new Error(`${label} must be boolean`);
  return value;
}

function nullableBoolean(value: unknown, label: string): boolean | null {
  if (value === null) return null;
  return booleanValue(value, label);
}

function boundedLimit(value: unknown, label: string): number {
  const result = positiveInteger(value, label);
  if (result > 200) throw new Error(`${label} must not exceed 200`);
  return result;
}

function enumValue<T extends string>(value: unknown, allowed: Set<T>, label: string): T {
  if (typeof value !== "string" || !allowed.has(value as T)) {
    throw new Error(`${label} is unsupported`);
  }
  return value as T;
}

function nullableEnumValue<T extends string>(
  value: unknown,
  allowed: Set<T>,
  label: string,
): T | null {
  if (value === null) return null;
  return enumValue(value, allowed, label);
}
