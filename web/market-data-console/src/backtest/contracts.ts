import type { ControlHealthStatus } from "../control/contracts";

export type BacktestJobStatus =
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

export interface BacktestJobError {
  schema_version: 1;
  code: string;
  message: string;
  action: string | null;
  retryable: boolean;
  error_id: string;
  details: Record<string, string | number | boolean | null>;
}

export interface BacktestJobProgress {
  schema_version: 1;
  phase: string;
  determinate: boolean;
  completed_units: number | null;
  total_units: number | null;
  percent: number | null;
  message: string | null;
  counters: Record<string, number>;
}

export interface BacktestJobSummary {
  schema_version: 1;
  job_id: string;
  component: "backtest";
  operation: string;
  status: BacktestJobStatus;
  phase: string;
  progress: BacktestJobProgress | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
  error: BacktestJobError | null;
}

export interface BacktestResultReference {
  schema_version: 1;
  run_id: string;
  bars_processed: number;
  restart_verified: boolean;
}

export interface BacktestJobRecord {
  schema_version: 1;
  job: BacktestJobSummary;
  config_id: string;
  result: BacktestResultReference | null;
}

export interface BacktestCreateResponse {
  schema_version: 1;
  record: BacktestJobRecord;
  created: boolean;
}

export type BacktestMetricScalar = string | number | boolean | null;

export interface BacktestResultSummary {
  schema_version: 1;
  job_id: string;
  run_id: string;
  bars_processed: number;
  restart_verified: boolean;
  metrics: Record<string, BacktestMetricScalar>;
}

export interface BacktestMetricChange {
  schema_version: 1;
  left: BacktestMetricScalar;
  right: BacktestMetricScalar;
  delta: number | null;
}

export interface BacktestComparison {
  schema_version: 1;
  left_job_id: string;
  right_job_id: string;
  left_run_id: string;
  right_run_id: string;
  changes: Record<string, BacktestMetricChange>;
}

export interface BacktestHealthProjection {
  status: ControlHealthStatus;
  summary: string;
}

const JOB_STATUSES = new Set<BacktestJobStatus>([
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

export function parseBacktestJobRecord(value: unknown): BacktestJobRecord {
  const root = objectValue(value, "backtest job");
  requireSchemaVersion(root);
  const job = parseBacktestJobSummary(root.job);
  const configId = stringValue(root.config_id, "config_id");
  const result = root.result === null ? null : parseBacktestResultReference(root.result);
  return { schema_version: 1, job, config_id: configId, result };
}

export function parseBacktestJobList(value: unknown): BacktestJobRecord[] {
  if (!Array.isArray(value)) throw new Error("backtest list must be an array");
  return value.map(parseBacktestJobRecord);
}

export function parseBacktestCreateResponse(value: unknown): BacktestCreateResponse {
  const root = objectValue(value, "backtest create response");
  requireSchemaVersion(root);
  return {
    schema_version: 1,
    record: parseBacktestJobRecord(root.record),
    created: booleanValue(root.created, "created"),
  };
}

export function parseBacktestResultSummary(value: unknown): BacktestResultSummary {
  const root = objectValue(value, "backtest result");
  requireSchemaVersion(root);
  return {
    schema_version: 1,
    job_id: stringValue(root.job_id, "job_id"),
    run_id: stringValue(root.run_id, "run_id"),
    bars_processed: nonNegativeInteger(root.bars_processed, "bars_processed"),
    restart_verified: booleanValue(root.restart_verified, "restart_verified"),
    metrics: scalarRecord(root.metrics, "metrics"),
  };
}

export function parseBacktestComparison(value: unknown): BacktestComparison {
  const root = objectValue(value, "backtest comparison");
  requireSchemaVersion(root);
  const rawChanges = objectValue(root.changes, "changes");
  const changes: Record<string, BacktestMetricChange> = {};
  for (const [name, rawChange] of Object.entries(rawChanges)) {
    const change = objectValue(rawChange, `changes.${name}`);
    requireSchemaVersion(change);
    const delta = change.delta;
    if (delta !== null && (typeof delta !== "number" || !Number.isFinite(delta))) {
      throw new Error(`changes.${name}.delta must be finite or null`);
    }
    changes[name] = {
      schema_version: 1,
      left: scalarValue(change.left, `changes.${name}.left`),
      right: scalarValue(change.right, `changes.${name}.right`),
      delta,
    };
  }
  return {
    schema_version: 1,
    left_job_id: stringValue(root.left_job_id, "left_job_id"),
    right_job_id: stringValue(root.right_job_id, "right_job_id"),
    left_run_id: stringValue(root.left_run_id, "left_run_id"),
    right_run_id: stringValue(root.right_run_id, "right_run_id"),
    changes,
  };
}

function parseBacktestJobSummary(value: unknown): BacktestJobSummary {
  const root = objectValue(value, "job");
  requireSchemaVersion(root);
  const status = stringValue(root.status, "job.status");
  if (!JOB_STATUSES.has(status as BacktestJobStatus)) {
    throw new Error("job.status is not a supported durable Backtest status");
  }
  if (root.component !== "backtest") throw new Error("job.component must be backtest");
  const progress = root.progress === null ? null : parseProgress(root.progress);
  const error = root.error === null ? null : parseError(root.error);
  return {
    schema_version: 1,
    job_id: stringValue(root.job_id, "job.job_id"),
    component: "backtest",
    operation: stringValue(root.operation, "job.operation"),
    status: status as BacktestJobStatus,
    phase: stringValue(root.phase, "job.phase"),
    progress,
    created_at: timestampValue(root.created_at, "job.created_at"),
    updated_at: timestampValue(root.updated_at, "job.updated_at"),
    started_at: nullableTimestamp(root.started_at, "job.started_at"),
    completed_at: nullableTimestamp(root.completed_at, "job.completed_at"),
    error,
  };
}

function parseProgress(value: unknown): BacktestJobProgress {
  const root = objectValue(value, "progress");
  requireSchemaVersion(root);
  const countersRaw = objectValue(root.counters, "progress.counters");
  const counters: Record<string, number> = {};
  for (const [key, item] of Object.entries(countersRaw)) {
    if (typeof item !== "number" || !Number.isFinite(item)) {
      throw new Error(`progress.counters.${key} must be finite`);
    }
    counters[key] = item;
  }
  return {
    schema_version: 1,
    phase: stringValue(root.phase, "progress.phase"),
    determinate: booleanValue(root.determinate, "progress.determinate"),
    completed_units: nullableNumber(root.completed_units, "progress.completed_units"),
    total_units: nullableNumber(root.total_units, "progress.total_units"),
    percent: nullableNumber(root.percent, "progress.percent"),
    message: nullableString(root.message, "progress.message"),
    counters,
  };
}

function parseError(value: unknown): BacktestJobError {
  const root = objectValue(value, "job.error");
  requireSchemaVersion(root);
  return {
    schema_version: 1,
    code: stringValue(root.code, "job.error.code"),
    message: stringValue(root.message, "job.error.message"),
    action: nullableString(root.action, "job.error.action"),
    retryable: booleanValue(root.retryable, "job.error.retryable"),
    error_id: stringValue(root.error_id, "job.error.error_id"),
    details: scalarRecord(root.details, "job.error.details"),
  };
}

function parseBacktestResultReference(value: unknown): BacktestResultReference {
  const root = objectValue(value, "result reference");
  requireSchemaVersion(root);
  return {
    schema_version: 1,
    run_id: stringValue(root.run_id, "result.run_id"),
    bars_processed: nonNegativeInteger(root.bars_processed, "result.bars_processed"),
    restart_verified: booleanValue(root.restart_verified, "result.restart_verified"),
  };
}

function requireSchemaVersion(value: Record<string, unknown>): void {
  if (value.schema_version !== 1) throw new Error("unsupported schema_version");
}

function objectValue(value: unknown, name: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`${name} must be an object`);
  }
  return value as Record<string, unknown>;
}

function stringValue(value: unknown, name: string): string {
  if (typeof value !== "string" || value.length === 0) throw new Error(`${name} must be a string`);
  return value;
}

function nullableString(value: unknown, name: string): string | null {
  if (value === null) return null;
  return stringValue(value, name);
}

function booleanValue(value: unknown, name: string): boolean {
  if (typeof value !== "boolean") throw new Error(`${name} must be boolean`);
  return value;
}

function nullableNumber(value: unknown, name: string): number | null {
  if (value === null) return null;
  if (typeof value !== "number" || !Number.isFinite(value)) throw new Error(`${name} must be finite`);
  return value;
}

function nonNegativeInteger(value: unknown, name: string): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0) {
    throw new Error(`${name} must be a non-negative integer`);
  }
  return value;
}

function timestampValue(value: unknown, name: string): string {
  const text = stringValue(value, name);
  if (Number.isNaN(Date.parse(text))) throw new Error(`${name} must be an ISO timestamp`);
  return text;
}

function nullableTimestamp(value: unknown, name: string): string | null {
  if (value === null) return null;
  return timestampValue(value, name);
}

function scalarValue(value: unknown, name: string): BacktestMetricScalar {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  throw new Error(`${name} must be a finite scalar`);
}

function scalarRecord(value: unknown, name: string): Record<string, BacktestMetricScalar> {
  const root = objectValue(value, name);
  const output: Record<string, BacktestMetricScalar> = {};
  for (const [key, item] of Object.entries(root)) output[key] = scalarValue(item, `${name}.${key}`);
  return output;
}
