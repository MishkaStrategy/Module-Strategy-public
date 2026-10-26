export type OperationalEventSeverity = "INFO" | "WARNING" | "ERROR";

export interface OperationalEvent {
  schema_version: 1;
  event_id: string;
  source: "trading_runner";
  resource: string;
  timestamp: string;
  event_type: string;
  severity: OperationalEventSeverity;
  state: string | null;
  payload: Record<string, string | number | boolean | null>;
  cursor: string;
}

export interface OperationalEventPage {
  schema_version: 1;
  source: "trading_runner";
  events: OperationalEvent[];
  next_cursor: string;
  has_more: boolean;
  scanned_count: number;
  omitted_count: number;
  retention_limit: number;
  truncated: boolean;
}

const EVENT_TYPES = new Set([
  "runtime.configuring",
  "runtime.started",
  "runtime.failed",
  "runtime.stopping",
  "runtime.stopped",
  "kill_switch.activated",
  "kill_switch.reset",
  "kill_switch.cancellation_retry",
  "operator.lifecycle",
]);
const SEVERITIES = new Set<OperationalEventSeverity>(["INFO", "WARNING", "ERROR"]);
const CURSOR = /^trr-v1:[0-9a-f]{16}:[0-9]{1,7}$/;
const EVENT_ID = /^trr-[0-9]{12}-[0-9a-f]{16}$/;

export function parseOperationalEventPage(value: unknown): OperationalEventPage {
  const record = asRecord(value, "Operational events page");
  const events = arrayValue(record.events, "events").map(parseOperationalEvent);
  const scanned = boundedInteger(record.scanned_count, "scanned_count", 0, 1000);
  const omitted = boundedInteger(record.omitted_count, "omitted_count", 0, 1000);
  if (events.length > scanned || omitted > scanned) {
    throw new Error("Operational event page counts are inconsistent");
  }
  return {
    schema_version: literalOne(record.schema_version),
    source: exactString(record.source, "trading_runner"),
    events,
    next_cursor: cursor(record.next_cursor),
    has_more: booleanValue(record.has_more, "has_more"),
    scanned_count: scanned,
    omitted_count: omitted,
    retention_limit: boundedInteger(record.retention_limit, "retention_limit", 1, 1_000_000),
    truncated: booleanValue(record.truncated, "truncated"),
  };
}

function parseOperationalEvent(value: unknown): OperationalEvent {
  const record = asRecord(value, "Operational event");
  const eventId = boundedString(record.event_id, "event_id", 64);
  if (!EVENT_ID.test(eventId)) throw new Error("event_id is invalid");
  const eventType = boundedString(record.event_type, "event_type", 128);
  if (!EVENT_TYPES.has(eventType)) throw new Error("event_type is not accepted");
  const severity = boundedString(record.severity, "severity", 16) as OperationalEventSeverity;
  if (!SEVERITIES.has(severity)) throw new Error("severity is invalid");
  return {
    schema_version: literalOne(record.schema_version),
    event_id: eventId,
    source: exactString(record.source, "trading_runner"),
    resource: boundedString(record.resource, "resource", 128),
    timestamp: timestamp(record.timestamp),
    event_type: eventType,
    severity,
    state: record.state === null ? null : boundedString(record.state, "state", 64),
    payload: scalarPayload(record.payload),
    cursor: cursor(record.cursor),
  };
}

function scalarPayload(value: unknown): Record<string, string | number | boolean | null> {
  const record = asRecord(value, "payload");
  const entries = Object.entries(record);
  if (entries.length > 16) throw new Error("payload is too large");
  const result: Record<string, string | number | boolean | null> = {};
  for (const [key, item] of entries) {
    if (!/^[A-Za-z_][A-Za-z0-9_.-]{0,63}$/.test(key)) throw new Error("payload key is invalid");
    if (item !== null && !["string", "number", "boolean"].includes(typeof item)) {
      throw new Error("payload value must be scalar");
    }
    if (typeof item === "string" && item.length > 256) throw new Error("payload string is too large");
    if (typeof item === "number" && !Number.isFinite(item)) throw new Error("payload number is invalid");
    result[key] = item as string | number | boolean | null;
  }
  return result;
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
  if (typeof value !== "string" || value.length < 1 || value.length > maximum || value.trim() !== value) {
    throw new Error(`${name} is invalid`);
  }
  return value;
}

function cursor(value: unknown): string {
  const result = boundedString(value, "cursor", 128);
  if (!CURSOR.test(result)) throw new Error("cursor is invalid");
  return result;
}

function timestamp(value: unknown): string {
  const result = boundedString(value, "timestamp", 64);
  if (!/(?:Z|[+-]\d{2}:\d{2})$/.test(result) || Number.isNaN(Date.parse(result))) {
    throw new Error("timestamp must include a timezone");
  }
  return result;
}

function exactString<T extends string>(value: unknown, expected: T): T {
  if (value !== expected) throw new Error(`Expected ${expected}`);
  return expected;
}

function literalOne(value: unknown): 1 {
  if (value !== 1) throw new Error("schema_version must be 1");
  return 1;
}

function booleanValue(value: unknown, name: string): boolean {
  if (typeof value !== "boolean") throw new Error(`${name} must be boolean`);
  return value;
}

function boundedInteger(value: unknown, name: string, minimum: number, maximum: number): number {
  if (!Number.isSafeInteger(value) || Number(value) < minimum || Number(value) > maximum) {
    throw new Error(`${name} is invalid`);
  }
  return Number(value);
}
