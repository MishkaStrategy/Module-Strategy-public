export type StrategyFactoryStatus =
  | "CREATED"
  | "RUNNING"
  | "PAUSED"
  | "CANCELED"
  | "COMPLETED"
  | "FAILED"
  | "DATASET_REJECTED"
  | "BUDGET_EXHAUSTED"
  | "BLOCKED"
  | "CORRUPTED";

export type StrategyControlStatus =
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

export type StrategyFactoryStage =
  | "data_gate"
  | "instrument_profile"
  | "regime_discovery"
  | "hypothesis_generation"
  | "architecture_generation"
  | "experiment_planning"
  | "structural_screening"
  | "coarse_search"
  | "local_refinement"
  | "walk_forward"
  | "robustness"
  | "holdout"
  | "simplicity_selection"
  | "final_audit"
  | "package"
  | "paper_admission";

export interface StrategyRunSummary {
  schema_version: 1;
  run_id: string;
  factory_status: StrategyFactoryStatus;
  status: StrategyControlStatus;
  stage: StrategyFactoryStage;
  stage_sequence: number;
  updated_at: string;
  request_fingerprint: string;
  dataset_fingerprint: string;
  commit_sha: string;
  manifest_id: string;
  completed_artifact_count: number;
  submitted_batch_count: number;
  processed_event_count: number;
  decision_count: number;
  decision_chain_head: string | null;
  decision_chain_verified: boolean;
}

export interface StrategyRunList {
  schema_version: 1;
  runs: StrategyRunSummary[];
  total_valid: number;
  invalid_count: number;
  ignored_count: number;
  truncated: boolean;
}

const FACTORY_STATUSES = new Set<StrategyFactoryStatus>([
  "CREATED",
  "RUNNING",
  "PAUSED",
  "CANCELED",
  "COMPLETED",
  "FAILED",
  "DATASET_REJECTED",
  "BUDGET_EXHAUSTED",
  "BLOCKED",
  "CORRUPTED",
]);
const CONTROL_STATUSES = new Set<StrategyControlStatus>([
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
const FACTORY_STAGES = new Set<StrategyFactoryStage>([
  "data_gate",
  "instrument_profile",
  "regime_discovery",
  "hypothesis_generation",
  "architecture_generation",
  "experiment_planning",
  "structural_screening",
  "coarse_search",
  "local_refinement",
  "walk_forward",
  "robustness",
  "holdout",
  "simplicity_selection",
  "final_audit",
  "package",
  "paper_admission",
]);
const RUN_ID = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const HEX_64 = /^[0-9a-f]{64}$/;
const GIT_SHA = /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/;

export function parseStrategyRunList(value: unknown): StrategyRunList {
  const record = asRecord(value, "Strategy run list");
  const runs = record.runs;
  if (!Array.isArray(runs)) throw new Error("Strategy run list runs must be an array");
  return {
    schema_version: literalOne(record.schema_version, "Strategy run list schema_version"),
    runs: runs.map(parseStrategyRunSummary),
    total_valid: nonNegativeInteger(record.total_valid, "Strategy run list total_valid"),
    invalid_count: nonNegativeInteger(record.invalid_count, "Strategy run list invalid_count"),
    ignored_count: nonNegativeInteger(record.ignored_count, "Strategy run list ignored_count"),
    truncated: booleanValue(record.truncated, "Strategy run list truncated"),
  };
}

export function parseStrategyRunSummary(value: unknown): StrategyRunSummary {
  const record = asRecord(value, "Strategy run");
  const runId = boundedString(record.run_id, "Strategy run_id", 128);
  if (!RUN_ID.test(runId)) throw new Error("Strategy run_id is invalid");
  const factoryStatus = enumValue(record.factory_status, FACTORY_STATUSES, "Strategy factory_status");
  const status = enumValue(record.status, CONTROL_STATUSES, "Strategy status");
  const stage = enumValue(record.stage, FACTORY_STAGES, "Strategy stage");
  const requestFingerprint = boundedString(record.request_fingerprint, "Strategy request_fingerprint", 64);
  const datasetFingerprint = boundedString(record.dataset_fingerprint, "Strategy dataset_fingerprint", 64);
  const commitSha = boundedString(record.commit_sha, "Strategy commit_sha", 64);
  const manifestId = boundedString(record.manifest_id, "Strategy manifest_id", 128);
  const chainHead = nullableBoundedString(record.decision_chain_head, "Strategy decision_chain_head", 64);
  if (!HEX_64.test(requestFingerprint) || !HEX_64.test(datasetFingerprint)) {
    throw new Error("Strategy fingerprints are invalid");
  }
  if (!GIT_SHA.test(commitSha)) throw new Error("Strategy commit_sha is invalid");
  if (chainHead !== null && !HEX_64.test(chainHead)) {
    throw new Error("Strategy decision_chain_head is invalid");
  }
  const updatedAt = boundedString(record.updated_at, "Strategy updated_at", 64);
  if (!Number.isFinite(Date.parse(updatedAt))) throw new Error("Strategy updated_at is invalid");
  return {
    schema_version: literalOne(record.schema_version, "Strategy schema_version"),
    run_id: runId,
    factory_status: factoryStatus,
    status,
    stage,
    stage_sequence: nonNegativeInteger(record.stage_sequence, "Strategy stage_sequence"),
    updated_at: updatedAt,
    request_fingerprint: requestFingerprint,
    dataset_fingerprint: datasetFingerprint,
    commit_sha: commitSha,
    manifest_id: manifestId,
    completed_artifact_count: nonNegativeInteger(
      record.completed_artifact_count,
      "Strategy completed_artifact_count",
    ),
    submitted_batch_count: nonNegativeInteger(
      record.submitted_batch_count,
      "Strategy submitted_batch_count",
    ),
    processed_event_count: nonNegativeInteger(
      record.processed_event_count,
      "Strategy processed_event_count",
    ),
    decision_count: nonNegativeInteger(record.decision_count, "Strategy decision_count"),
    decision_chain_head: chainHead,
    decision_chain_verified: booleanValue(
      record.decision_chain_verified,
      "Strategy decision_chain_verified",
    ),
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

function nullableBoundedString(
  value: unknown,
  label: string,
  maxLength: number,
): string | null {
  if (value === null) return null;
  return boundedString(value, label, maxLength);
}

function nonNegativeInteger(value: unknown, label: string): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
    throw new Error(`${label} must be a non-negative integer`);
  }
  return value;
}

function booleanValue(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") throw new Error(`${label} must be boolean`);
  return value;
}

function enumValue<T extends string>(value: unknown, allowed: Set<T>, label: string): T {
  if (typeof value !== "string" || !allowed.has(value as T)) {
    throw new Error(`${label} is unsupported`);
  }
  return value as T;
}
