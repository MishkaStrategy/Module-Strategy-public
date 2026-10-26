export type NotificationOwnerStatus = "ok" | "degraded" | "unavailable";

export interface NotificationStatus {
  schema_version: 1;
  configured: boolean;
  reachable: boolean | null;
  notifications_enabled: boolean | null;
  status: NotificationOwnerStatus;
  cycles: number;
  signals_sent: number;
  last_success_at: string | null;
  last_error_type: string | null;
}

export interface TestNotificationRequest {
  schema_version: 1;
  request_id: string;
}

export interface TestNotificationResult {
  schema_version: 1;
  request_id: string;
  operator: string;
  completed_at: string;
  replayed: boolean;
}

const STATUSES = new Set<NotificationOwnerStatus>(["ok", "degraded", "unavailable"]);
const REQUEST_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const OPERATOR = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$/;
const STATUS_KEYS = new Set([
  "schema_version",
  "configured",
  "reachable",
  "notifications_enabled",
  "status",
  "cycles",
  "signals_sent",
  "last_success_at",
  "last_error_type",
]);
const RESULT_KEYS = new Set([
  "schema_version",
  "request_id",
  "operator",
  "completed_at",
  "replayed",
]);

export function parseNotificationStatus(value: unknown): NotificationStatus {
  const record = asRecord(value, "Notification status");
  exactKeys(record, STATUS_KEYS, "Notification status");
  return {
    schema_version: one(record.schema_version),
    configured: booleanValue(record.configured, "configured"),
    reachable: nullableBoolean(record.reachable, "reachable"),
    notifications_enabled: nullableBoolean(
      record.notifications_enabled,
      "notifications_enabled",
    ),
    status: enumValue(record.status, STATUSES, "status"),
    cycles: nonnegativeInteger(record.cycles, "cycles"),
    signals_sent: nonnegativeInteger(record.signals_sent, "signals_sent"),
    last_success_at: nullableTimestamp(record.last_success_at, "last_success_at"),
    last_error_type: nullableBoundedString(record.last_error_type, "last_error_type", 128),
  };
}

export function parseTestNotificationResult(value: unknown): TestNotificationResult {
  const record = asRecord(value, "Test notification result");
  exactKeys(record, RESULT_KEYS, "Test notification result");
  return {
    schema_version: one(record.schema_version),
    request_id: patternString(record.request_id, REQUEST_ID, "request_id"),
    operator: patternString(record.operator, OPERATOR, "operator"),
    completed_at: timestamp(record.completed_at, "completed_at"),
    replayed: booleanValue(record.replayed, "replayed"),
  };
}

function asRecord(value: unknown, name: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`${name} must be an object`);
  }
  return value as Record<string, unknown>;
}

function exactKeys(record: Record<string, unknown>, expected: Set<string>, name: string): void {
  const keys = Object.keys(record);
  if (keys.length !== expected.size || keys.some((key) => !expected.has(key))) {
    throw new Error(`${name} contains unexpected fields`);
  }
}

function one(value: unknown): 1 {
  if (value !== 1) throw new Error("schema_version must be 1");
  return 1;
}

function booleanValue(value: unknown, name: string): boolean {
  if (typeof value !== "boolean") throw new Error(`${name} must be boolean`);
  return value;
}

function nullableBoolean(value: unknown, name: string): boolean | null {
  if (value === null) return null;
  return booleanValue(value, name);
}

function nonnegativeInteger(value: unknown, name: string): number {
  if (!Number.isSafeInteger(value) || typeof value !== "number" || value < 0) {
    throw new Error(`${name} must be a non-negative safe integer`);
  }
  return value;
}

function nullableBoundedString(
  value: unknown,
  name: string,
  maximum: number,
): string | null {
  if (value === null) return null;
  if (typeof value !== "string" || value.length < 1 || value.length > maximum) {
    throw new Error(`${name} must be a bounded string or null`);
  }
  return value;
}

function patternString(value: unknown, pattern: RegExp, name: string): string {
  if (typeof value !== "string" || !pattern.test(value)) {
    throw new Error(`${name} has invalid format`);
  }
  return value;
}

function timestamp(value: unknown, name: string): string {
  if (typeof value !== "string" || value.length < 1 || value.length > 64) {
    throw new Error(`${name} must be a bounded timestamp`);
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) throw new Error(`${name} must be a timestamp`);
  return value;
}

function nullableTimestamp(value: unknown, name: string): string | null {
  if (value === null) return null;
  return timestamp(value, name);
}

function enumValue<T extends string>(value: unknown, allowed: Set<T>, name: string): T {
  if (typeof value !== "string" || !allowed.has(value as T)) {
    throw new Error(`${name} contains an unsupported value`);
  }
  return value as T;
}
