export type AcceptancePhase =
  | "ENVIRONMENT"
  | "DATASET"
  | "BACKTEST_A"
  | "BACKTEST_B"
  | "DETERMINISM"
  | "RESEARCH"
  | "RUNTIME_SMOKE"
  | "RECOVERY"
  | "SOAK_2H"
  | "SOAK_24H"
  | "SOAK_72H"
  | "FINAL_VERIFICATION";

export type AcceptanceStatus =
  | "QUEUED"
  | "STARTING"
  | "RUNNING"
  | "STOPPING"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELLED"
  | "RECOVERING"
  | "BLOCKED";

export type AcceptanceMutation = "START" | "STOP" | "CANCEL" | "RETRY";

export interface AcceptanceCandidate {
  schema_version: 1;
  full_rc_sha: string;
  dataset_id: string;
  dataset_digest: string;
  strategy_id: string;
  strategy_digest: string;
  config_digest: string;
  mode: "PAPER";
  enable_soak_24h: boolean;
  enable_soak_72h: boolean;
}

export interface AcceptanceCheckpoint {
  schema_version: 1;
  phase: AcceptancePhase;
  updated_at: string;
  token: string;
  digest: string;
}

export interface AcceptanceResourceSample {
  schema_version: 1;
  timestamp: string;
  cpu_percent: number;
  ram_bytes: number;
  disk_free_bytes: number;
}

export interface AcceptanceEvidenceReference {
  schema_version: 1;
  kind: string;
  digest: string;
  reference: string;
}

export interface AcceptanceRunSnapshot {
  schema_version: 1;
  acceptance_run_id: string;
  status: AcceptanceStatus;
  phase: AcceptancePhase;
  created_at: string;
  started_at: string | null;
  updated_at: string;
  completed_at: string | null;
  active_test_time: number;
  wall_clock_elapsed: number;
  restart_count: number;
  checkpoint: AcceptanceCheckpoint | null;
  resources: AcceptanceResourceSample[];
  warnings: string[];
  error: string | null;
  next_action: string | null;
  evidence: AcceptanceEvidenceReference[];
  identity: AcceptanceCandidate;
  phases: AcceptancePhase[];
}

export interface AcceptanceCurrent {
  schema_version: 1;
  run: AcceptanceRunSnapshot | null;
}

export interface AcceptanceStartRequest {
  schema_version: 1;
  request_id: string;
  dataset_id: string;
  dataset_digest: string;
  strategy_id: string;
  strategy_digest: string;
  config_digest: string;
  enable_soak_24h: boolean;
  enable_soak_72h: boolean;
}

export interface AcceptanceRunMutationRequest {
  schema_version: 1;
  request_id: string;
  acceptance_run_id: string;
}

export interface AcceptanceMutationResult {
  schema_version: 1;
  request_id: string;
  operator: string;
  mutation: AcceptanceMutation;
  acceptance_run_id: string;
  status: AcceptanceStatus;
  phase: AcceptancePhase;
  completed_at: string;
  replayed: boolean;
}

const PHASES = new Set<AcceptancePhase>([
  "ENVIRONMENT",
  "DATASET",
  "BACKTEST_A",
  "BACKTEST_B",
  "DETERMINISM",
  "RESEARCH",
  "RUNTIME_SMOKE",
  "RECOVERY",
  "SOAK_2H",
  "SOAK_24H",
  "SOAK_72H",
  "FINAL_VERIFICATION",
]);
const STATUSES = new Set<AcceptanceStatus>([
  "QUEUED",
  "STARTING",
  "RUNNING",
  "STOPPING",
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
  "RECOVERING",
  "BLOCKED",
]);
const MUTATIONS = new Set<AcceptanceMutation>(["START", "STOP", "CANCEL", "RETRY"]);
const TERMINAL = new Set<AcceptanceStatus>(["SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED"]);
const HEX_40 = /^[0-9a-f]{40}$/;
const HEX_64 = /^[0-9a-f]{64}$/;
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$/;
const REQUEST_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const CHECKPOINT_TOKEN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;
const EVIDENCE_KIND = /^[a-z][a-z0-9._-]{0,63}$/;

export function parseAcceptanceCurrent(value: unknown): AcceptanceCurrent {
  const record = asRecord(value, "Acceptance current");
  return {
    schema_version: one(record.schema_version, "schema_version"),
    run: record.run === null ? null : parseRun(record.run),
  };
}

export function parseAcceptanceMutationResult(value: unknown): AcceptanceMutationResult {
  const record = asRecord(value, "Acceptance mutation result");
  return {
    schema_version: one(record.schema_version, "schema_version"),
    request_id: requestId(record.request_id, "request_id"),
    operator: identifier(record.operator, "operator"),
    mutation: enumValue(record.mutation, MUTATIONS, "mutation"),
    acceptance_run_id: identifier(record.acceptance_run_id, "acceptance_run_id"),
    status: enumValue(record.status, STATUSES, "status"),
    phase: enumValue(record.phase, PHASES, "phase"),
    completed_at: timestamp(record.completed_at, "completed_at"),
    replayed: booleanValue(record.replayed, "replayed"),
  };
}

function parseRun(value: unknown): AcceptanceRunSnapshot {
  const record = asRecord(value, "Acceptance run");
  const status = enumValue(record.status, STATUSES, "status");
  const phase = enumValue(record.phase, PHASES, "phase");
  const completedAt = nullableTimestamp(record.completed_at, "completed_at");
  if (TERMINAL.has(status) !== (completedAt !== null)) {
    throw new Error("Acceptance terminal status and completed_at are inconsistent");
  }
  const phases = arrayValue(record.phases, "phases").map((item) =>
    enumValue(item, PHASES, "phase"),
  );
  if (phases.length < 1 || new Set(phases).size !== phases.length || !phases.includes(phase)) {
    throw new Error("Acceptance phase list is inconsistent");
  }
  const warnings = arrayValue(record.warnings, "warnings").map((item) =>
    boundedString(item, "warning", 128),
  );
  const resources = arrayValue(record.resources, "resources").map(parseResource);
  const evidence = arrayValue(record.evidence, "evidence").map(parseEvidence);
  if (warnings.length > 32 || resources.length > 256 || evidence.length > 64) {
    throw new Error("Acceptance bounded collections exceed the browser contract");
  }
  return {
    schema_version: one(record.schema_version, "schema_version"),
    acceptance_run_id: identifier(record.acceptance_run_id, "acceptance_run_id"),
    status,
    phase,
    created_at: timestamp(record.created_at, "created_at"),
    started_at: nullableTimestamp(record.started_at, "started_at"),
    updated_at: timestamp(record.updated_at, "updated_at"),
    completed_at: completedAt,
    active_test_time: nonNegativeNumber(record.active_test_time, "active_test_time"),
    wall_clock_elapsed: nonNegativeNumber(record.wall_clock_elapsed, "wall_clock_elapsed"),
    restart_count: nonNegativeInteger(record.restart_count, "restart_count"),
    checkpoint: record.checkpoint === null ? null : parseCheckpoint(record.checkpoint),
    resources,
    warnings,
    error: nullableIdentifier(record.error, "error"),
    next_action: nullableIdentifier(record.next_action, "next_action"),
    evidence,
    identity: parseCandidate(record.identity),
    phases,
  };
}

function parseCandidate(value: unknown): AcceptanceCandidate {
  const record = asRecord(value, "Acceptance candidate");
  const fullRc = boundedString(record.full_rc_sha, "full_rc_sha", 40);
  if (!HEX_40.test(fullRc)) throw new Error("full_rc_sha is invalid");
  const soak24 = booleanValue(record.enable_soak_24h, "enable_soak_24h");
  const soak72 = booleanValue(record.enable_soak_72h, "enable_soak_72h");
  if (soak72 && !soak24) throw new Error("72h soak requires 24h soak");
  return {
    schema_version: one(record.schema_version, "schema_version"),
    full_rc_sha: fullRc,
    dataset_id: identifier(record.dataset_id, "dataset_id"),
    dataset_digest: digest(record.dataset_digest, "dataset_digest"),
    strategy_id: identifier(record.strategy_id, "strategy_id"),
    strategy_digest: digest(record.strategy_digest, "strategy_digest"),
    config_digest: digest(record.config_digest, "config_digest"),
    mode: exactPaper(record.mode),
    enable_soak_24h: soak24,
    enable_soak_72h: soak72,
  };
}

function parseCheckpoint(value: unknown): AcceptanceCheckpoint {
  const record = asRecord(value, "Acceptance checkpoint");
  return {
    schema_version: one(record.schema_version, "schema_version"),
    phase: enumValue(record.phase, PHASES, "checkpoint.phase"),
    updated_at: timestamp(record.updated_at, "checkpoint.updated_at"),
    token: checkpointToken(record.token, "checkpoint.token"),
    digest: digest(record.digest, "checkpoint.digest"),
  };
}

function parseResource(value: unknown): AcceptanceResourceSample {
  const record = asRecord(value, "Acceptance resource");
  const cpu = nonNegativeNumber(record.cpu_percent, "cpu_percent");
  if (cpu > 100) throw new Error("cpu_percent is invalid");
  return {
    schema_version: one(record.schema_version, "schema_version"),
    timestamp: timestamp(record.timestamp, "resource.timestamp"),
    cpu_percent: cpu,
    ram_bytes: nonNegativeInteger(record.ram_bytes, "ram_bytes"),
    disk_free_bytes: nonNegativeInteger(record.disk_free_bytes, "disk_free_bytes"),
  };
}

function parseEvidence(value: unknown): AcceptanceEvidenceReference {
  const record = asRecord(value, "Acceptance evidence");
  const kind = boundedString(record.kind, "evidence.kind", 64);
  if (!EVIDENCE_KIND.test(kind)) throw new Error("evidence.kind is invalid");
  return {
    schema_version: one(record.schema_version, "schema_version"),
    kind,
    digest: digest(record.digest, "evidence.digest"),
    reference: identifier(record.reference, "evidence.reference"),
  };
}

function asRecord(value: unknown, name: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`${name} must be an object`);
  }
  return value as Record<string, unknown>;
}

function arrayValue(value: unknown, name: string): unknown[] {
  if (!Array.isArray(value)) throw new Error(`${name} must be an array`);
  return value;
}

function boundedString(value: unknown, name: string, maximum: number): string {
  if (typeof value !== "string" || value.trim() !== value || value.length < 1 || value.length > maximum) {
    throw new Error(`${name} is invalid`);
  }
  return value;
}

function identifier(value: unknown, name: string): string {
  const result = boundedString(value, name, 128);
  if (!IDENTIFIER.test(result)) throw new Error(`${name} is invalid`);
  return result;
}

function nullableIdentifier(value: unknown, name: string): string | null {
  return value === null ? null : identifier(value, name);
}

function requestId(value: unknown, name: string): string {
  const result = boundedString(value, name, 128);
  if (!REQUEST_ID.test(result)) throw new Error(`${name} is invalid`);
  return result;
}

function checkpointToken(value: unknown, name: string): string {
  const result = boundedString(value, name, 256);
  if (!CHECKPOINT_TOKEN.test(result)) throw new Error(`${name} is invalid`);
  return result;
}

function digest(value: unknown, name: string): string {
  const result = boundedString(value, name, 64);
  if (!HEX_64.test(result)) throw new Error(`${name} must be a SHA-256 digest`);
  return result;
}

function one(value: unknown, name: string): 1 {
  if (value !== 1) throw new Error(`${name} must be 1`);
  return 1;
}

function booleanValue(value: unknown, name: string): boolean {
  if (typeof value !== "boolean") throw new Error(`${name} must be boolean`);
  return value;
}

function nonNegativeNumber(value: unknown, name: string): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new Error(`${name} must be non-negative`);
  }
  return value;
}

function nonNegativeInteger(value: unknown, name: string): number {
  if (!Number.isSafeInteger(value) || Number(value) < 0) throw new Error(`${name} is invalid`);
  return Number(value);
}

function timestamp(value: unknown, name: string): string {
  if (typeof value !== "string" || value.length < 1 || value.length > 64) {
    throw new Error(`${name} must be a bounded timestamp`);
  }
  if (!/(?:Z|[+-]\d{2}:\d{2})$/.test(value) || Number.isNaN(Date.parse(value))) {
    throw new Error(`${name} must include a timezone`);
  }
  return value;
}

function nullableTimestamp(value: unknown, name: string): string | null {
  return value === null ? null : timestamp(value, name);
}

function exactPaper(value: unknown): "PAPER" {
  if (value !== "PAPER") throw new Error("System Acceptance must remain PAPER-only");
  return "PAPER";
}

function enumValue<T extends string>(value: unknown, allowed: Set<T>, name: string): T {
  if (typeof value !== "string" || !allowed.has(value as T)) throw new Error(`${name} is invalid`);
  return value as T;
}
