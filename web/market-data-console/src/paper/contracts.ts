export type PaperRuntimeNativeState =
  | "created"
  | "configuring"
  | "configured"
  | "initializing"
  | "recovering"
  | "reconciling"
  | "starting"
  | "running"
  | "degraded"
  | "pausing"
  | "paused"
  | "stopping"
  | "stopped"
  | "failed"
  | "kill_switch_active"
  | "disposed";

export type PaperExecutionNativeLifecycle =
  | "created"
  | "recovering"
  | "reconciling"
  | "ready"
  | "degraded"
  | "blocked"
  | "failed"
  | "stopped";

export type PaperKillSwitchScope = "global" | "portfolio";
export type PaperLifecycleMutation =
  | "START"
  | "STOP"
  | "ACTIVATE_KILL_SWITCH"
  | "RESET_KILL_SWITCH";

export interface PaperKillSwitch {
  schema_version: 1;
  active: boolean;
  scope: PaperKillSwitchScope;
  activated_at: string | null;
  cancellation_pending: boolean;
}

export interface PaperRuntimeSummary {
  schema_version: 1;
  runtime_id: string;
  portfolio_id: string;
  strategy_id: string;
  execution_mode: "paper";
  native_state: PaperRuntimeNativeState;
  base_timeframe: string;
  market: string;
  instruments: string[];
  configuration_hash: string;
  kill_switch: PaperKillSwitch;
  health_ready: boolean;
  queue_depth: number | null;
  queue_capacity: number | null;
  queue_high_water_mark: number | null;
  paper_execution_lifecycle: PaperExecutionNativeLifecycle;
  order_count: number;
  active_order_count: number;
  blocked_scope_count: number;
  position_projection_available: false;
  lifecycle_mutations_available: false;
}

export interface PaperLifecycleCapability {
  schema_version: 1;
  available: boolean;
  paper_only: true;
}

export interface PaperLifecycleMutationResult {
  schema_version: 1;
  request_id: string;
  operator: string;
  mutation: PaperLifecycleMutation;
  previous_state: PaperRuntimeNativeState;
  new_state: PaperRuntimeNativeState;
  config_hash: string;
  completed_at: string;
  replayed: boolean;
}

export interface PaperOrderSummary {
  schema_version: 1;
  client_order_id: string;
  core_order_id: string;
  portfolio_id: string;
  instrument: string;
  intent_role: string;
  native_state: string;
  requested_quantity: string;
  filled_quantity: string;
  remaining_quantity: string;
  average_fill_price: string | null;
  fees_paid: string;
  last_sequence: number;
  last_event_timestamp: string | null;
}

export interface PaperOrderList {
  schema_version: 1;
  orders: PaperOrderSummary[];
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface PaperBlockedScope {
  schema_version: 1;
  portfolio_id: string;
  instrument: string;
}

export interface PaperBlockedScopeList {
  schema_version: 1;
  scopes: PaperBlockedScope[];
}

export interface PaperPositionFact {
  schema_version: 1;
  owner_position_id: string;
  instrument_id: string;
  instrument_version: string;
  side: "long" | "short";
  quantity: string;
  quantity_unit: string;
  strategy_id: string | null;
}

export interface PaperPositionsAvailableSnapshot {
  schema_version: 1;
  status: "available";
  owner_id: string;
  composition_id: string;
  portfolio_id: string;
  owner_generation: string;
  membership_version: string;
  snapshot_version: string;
  state_version: string;
  as_of: string;
  coverage: "complete";
  positions: PaperPositionFact[];
}

export interface PaperPositionsUnavailableSnapshot {
  schema_version: 1;
  status: "unavailable" | "recovering" | "stale";
  reason_code: string;
}

export type PaperPositionsSnapshot =
  | PaperPositionsAvailableSnapshot
  | PaperPositionsUnavailableSnapshot;

const RUNTIME_STATES = new Set<PaperRuntimeNativeState>([
  "created",
  "configuring",
  "configured",
  "initializing",
  "recovering",
  "reconciling",
  "starting",
  "running",
  "degraded",
  "pausing",
  "paused",
  "stopping",
  "stopped",
  "failed",
  "kill_switch_active",
  "disposed",
]);
const EXECUTION_LIFECYCLES = new Set<PaperExecutionNativeLifecycle>([
  "created",
  "recovering",
  "reconciling",
  "ready",
  "degraded",
  "blocked",
  "failed",
  "stopped",
]);
const KILL_SWITCH_SCOPES = new Set<PaperKillSwitchScope>(["global", "portfolio"]);
const LIFECYCLE_MUTATIONS = new Set<PaperLifecycleMutation>([
  "START",
  "STOP",
  "ACTIVATE_KILL_SWITCH",
  "RESET_KILL_SWITCH",
]);
const HEX_64 = /^[0-9a-f]{64}$/;
const DECIMAL = /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[Ee][+-]?\d+)?$/;
const REQUEST_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const OPERATOR_ID = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$/;

export function parsePaperRuntimeSummary(value: unknown): PaperRuntimeSummary {
  const record = asRecord(value, "Paper runtime");
  const killSwitch = parseKillSwitch(record.kill_switch);
  const queueDepth = nullableNonNegativeInteger(record.queue_depth, "queue_depth");
  const queueCapacity = nullablePositiveInteger(record.queue_capacity, "queue_capacity");
  const queueHigh = nullableNonNegativeInteger(
    record.queue_high_water_mark,
    "queue_high_water_mark",
  );
  const queueValues = [queueDepth, queueCapacity, queueHigh];
  if (queueValues.some((item) => item === null) && queueValues.some((item) => item !== null)) {
    throw new Error("Paper queue metrics must be all present or all absent");
  }
  if (queueDepth !== null && queueCapacity !== null && queueDepth > queueCapacity) {
    throw new Error("Paper queue depth exceeds capacity");
  }
  if (queueHigh !== null && queueCapacity !== null && queueHigh > queueCapacity) {
    throw new Error("Paper queue high-water mark exceeds capacity");
  }
  const orderCount = nonNegativeInteger(record.order_count, "order_count");
  const activeOrderCount = nonNegativeInteger(record.active_order_count, "active_order_count");
  if (activeOrderCount > orderCount) throw new Error("Active order count exceeds order count");
  falseLiteral(record.position_projection_available, "position_projection_available");
  falseLiteral(record.lifecycle_mutations_available, "lifecycle_mutations_available");
  const instruments = arrayValue(record.instruments, "instruments").map((item) =>
    boundedString(item, "instrument", 128),
  );
  if (instruments.length < 1 || instruments.length > 512) {
    throw new Error("Paper instruments are empty or too large");
  }
  if (new Set(instruments).size !== instruments.length) {
    throw new Error("Paper instruments must be unique");
  }
  return {
    schema_version: literalOne(record.schema_version, "schema_version"),
    runtime_id: boundedString(record.runtime_id, "runtime_id", 256),
    portfolio_id: boundedString(record.portfolio_id, "portfolio_id", 256),
    strategy_id: boundedString(record.strategy_id, "strategy_id", 256),
    execution_mode: exactString(record.execution_mode, "paper"),
    native_state: enumValue(record.native_state, RUNTIME_STATES, "native_state"),
    base_timeframe: boundedString(record.base_timeframe, "base_timeframe", 64),
    market: boundedString(record.market, "market", 128),
    instruments,
    configuration_hash: digest(record.configuration_hash, "configuration_hash"),
    kill_switch: killSwitch,
    health_ready: booleanValue(record.health_ready, "health_ready"),
    queue_depth: queueDepth,
    queue_capacity: queueCapacity,
    queue_high_water_mark: queueHigh,
    paper_execution_lifecycle: enumValue(
      record.paper_execution_lifecycle,
      EXECUTION_LIFECYCLES,
      "paper_execution_lifecycle",
    ),
    order_count: orderCount,
    active_order_count: activeOrderCount,
    blocked_scope_count: nonNegativeInteger(record.blocked_scope_count, "blocked_scope_count"),
    position_projection_available: false,
    lifecycle_mutations_available: false,
  };
}

export function parsePaperLifecycleCapability(value: unknown): PaperLifecycleCapability {
  const record = asRecord(value, "Paper lifecycle capability");
  if (record.paper_only !== true) throw new Error("Paper lifecycle capability must remain PAPER-only");
  return {
    schema_version: literalOne(record.schema_version, "schema_version"),
    available: booleanValue(record.available, "available"),
    paper_only: true,
  };
}

export function parsePaperLifecycleMutationResult(value: unknown): PaperLifecycleMutationResult {
  const record = asRecord(value, "Paper lifecycle mutation result");
  const requestId = boundedString(record.request_id, "request_id", 128);
  if (!REQUEST_ID.test(requestId)) throw new Error("request_id is invalid");
  const operator = boundedString(record.operator, "operator", 128);
  if (!OPERATOR_ID.test(operator)) throw new Error("operator is invalid");
  return {
    schema_version: literalOne(record.schema_version, "schema_version"),
    request_id: requestId,
    operator,
    mutation: enumValue(record.mutation, LIFECYCLE_MUTATIONS, "mutation"),
    previous_state: enumValue(record.previous_state, RUNTIME_STATES, "previous_state"),
    new_state: enumValue(record.new_state, RUNTIME_STATES, "new_state"),
    config_hash: boundedPortableHash(record.config_hash, "config_hash"),
    completed_at: timestamp(record.completed_at, "completed_at"),
    replayed: booleanValue(record.replayed, "replayed"),
  };
}

export function parsePaperOrderList(value: unknown): PaperOrderList {
  const record = asRecord(value, "Paper order list");
  return {
    schema_version: literalOne(record.schema_version, "schema_version"),
    orders: arrayValue(record.orders, "orders").map(parsePaperOrder),
    limit: pageLimit(record.limit),
    offset: nonNegativeInteger(record.offset, "offset"),
    has_more: booleanValue(record.has_more, "has_more"),
  };
}

export function parsePaperBlockedScopeList(value: unknown): PaperBlockedScopeList {
  const record = asRecord(value, "Paper blocked scope list");
  const scopes = arrayValue(record.scopes, "scopes").map((item) => {
    const row = asRecord(item, "Paper blocked scope");
    return {
      schema_version: literalOne(row.schema_version, "schema_version"),
      portfolio_id: boundedString(row.portfolio_id, "portfolio_id", 256),
      instrument: boundedString(row.instrument, "instrument", 128),
    } satisfies PaperBlockedScope;
  });
  const identities = scopes.map((item) => `${item.portfolio_id}\0${item.instrument}`);
  if (new Set(identities).size !== identities.length) {
    throw new Error("Paper blocked scopes must be unique");
  }
  return {
    schema_version: literalOne(record.schema_version, "schema_version"),
    scopes,
  };
}

export function parsePaperPositionsSnapshot(value: unknown): PaperPositionsSnapshot {
  const record = asRecord(value, "Paper positions snapshot");
  const schemaVersion = literalOne(record.schema_version, "schema_version");
  if (record.status === "unavailable" || record.status === "recovering" || record.status === "stale") {
    return {
      schema_version: schemaVersion,
      status: record.status,
      reason_code: stableIdentifier(record.reason_code, "reason_code", 128),
    };
  }
  if (record.status !== "available") throw new Error("Paper positions status is invalid");
  if (record.coverage !== "complete") throw new Error("Paper positions coverage is incomplete");
  const positions = arrayValue(record.positions, "positions");
  if (positions.length > 500) throw new Error("Paper positions exceed the row limit");
  const parsedPositions = positions.map((item) => {
    const row = asRecord(item, "Paper position");
    if (row.side !== "long" && row.side !== "short") throw new Error("side is invalid");
    const side: PaperPositionFact["side"] = row.side;
    const quantity = decimalString(row.quantity, "quantity");
    if (quantity.startsWith("-") || !/[1-9]/.test(quantity.split(/[Ee]/, 1)[0] ?? "")) {
      throw new Error("quantity must be positive");
    }
    const strategyId =
      row.strategy_id === null ? null : boundedString(row.strategy_id, "strategy_id", 256);
    return {
      schema_version: literalOne(row.schema_version, "schema_version"),
      owner_position_id: stableIdentifier(row.owner_position_id, "owner_position_id", 256),
      instrument_id: stableIdentifier(row.instrument_id, "instrument_id", 256),
      instrument_version: stableIdentifier(row.instrument_version, "instrument_version", 256),
      side,
      quantity,
      quantity_unit: boundedString(row.quantity_unit, "quantity_unit", 64),
      strategy_id: strategyId,
    } satisfies PaperPositionFact;
  });
  const ids = parsedPositions.map((item) => item.owner_position_id);
  if (new Set(ids).size !== ids.length) throw new Error("Paper position IDs must be unique");
  return {
    schema_version: schemaVersion,
    status: "available",
    owner_id: stableIdentifier(record.owner_id, "owner_id", 256),
    composition_id: stableIdentifier(record.composition_id, "composition_id", 256),
    portfolio_id: stableIdentifier(record.portfolio_id, "portfolio_id", 256),
    owner_generation: stableIdentifier(record.owner_generation, "owner_generation", 256),
    membership_version: stableIdentifier(record.membership_version, "membership_version", 256),
    snapshot_version: stableIdentifier(record.snapshot_version, "snapshot_version", 256),
    state_version: stableIdentifier(record.state_version, "state_version", 256),
    as_of: timestamp(record.as_of, "as_of"),
    coverage: "complete",
    positions: parsedPositions,
  };
}

function parseKillSwitch(value: unknown): PaperKillSwitch {
  const record = asRecord(value, "Paper kill switch");
  const active = booleanValue(record.active, "kill_switch.active");
  const activatedAt = nullableTimestamp(record.activated_at, "kill_switch.activated_at");
  const cancellationPending = booleanValue(
    record.cancellation_pending,
    "kill_switch.cancellation_pending",
  );
  if (active !== (activatedAt !== null)) {
    throw new Error("Paper kill switch activation timestamp is inconsistent");
  }
  if (!active && cancellationPending) {
    throw new Error("Inactive kill switch cannot have pending cancellation");
  }
  return {
    schema_version: literalOne(record.schema_version, "schema_version"),
    active,
    scope: enumValue(record.scope, KILL_SWITCH_SCOPES, "kill_switch.scope"),
    activated_at: activatedAt,
    cancellation_pending: cancellationPending,
  };
}

function parsePaperOrder(value: unknown): PaperOrderSummary {
  const record = asRecord(value, "Paper order");
  return {
    schema_version: literalOne(record.schema_version, "schema_version"),
    client_order_id: boundedString(record.client_order_id, "client_order_id", 256),
    core_order_id: boundedString(record.core_order_id, "core_order_id", 256),
    portfolio_id: boundedString(record.portfolio_id, "portfolio_id", 256),
    instrument: boundedString(record.instrument, "instrument", 128),
    intent_role: boundedString(record.intent_role, "intent_role", 128),
    native_state: boundedString(record.native_state, "native_state", 64),
    requested_quantity: decimalString(record.requested_quantity, "requested_quantity"),
    filled_quantity: decimalString(record.filled_quantity, "filled_quantity"),
    remaining_quantity: decimalString(record.remaining_quantity, "remaining_quantity"),
    average_fill_price:
      record.average_fill_price === null
        ? null
        : decimalString(record.average_fill_price, "average_fill_price"),
    fees_paid: decimalString(record.fees_paid, "fees_paid"),
    last_sequence: nonNegativeInteger(record.last_sequence, "last_sequence"),
    last_event_timestamp: nullableTimestamp(record.last_event_timestamp, "last_event_timestamp"),
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
  if (
    typeof value !== "string" ||
    value.trim() !== value ||
    value.length < 1 ||
    value.length > maximum
  ) {
    throw new Error(`${name} is invalid`);
  }
  return value;
}

function stableIdentifier(value: unknown, name: string, maximum: number): string {
  const result = boundedString(value, name, maximum);
  if (result.trim() !== result) throw new Error(`${name} must not have surrounding whitespace`);
  return result;
}

function exactString<T extends string>(value: unknown, expected: T): T {
  if (value !== expected) throw new Error(`Expected ${expected}`);
  return expected;
}

function enumValue<T extends string>(value: unknown, allowed: Set<T>, name: string): T {
  if (typeof value !== "string" || !allowed.has(value as T)) throw new Error(`${name} is invalid`);
  return value as T;
}

function literalOne(value: unknown, name: string): 1 {
  if (value !== 1) throw new Error(`${name} must be 1`);
  return 1;
}

function booleanValue(value: unknown, name: string): boolean {
  if (typeof value !== "boolean") throw new Error(`${name} must be boolean`);
  return value;
}

function falseLiteral(value: unknown, name: string): false {
  if (value !== false) throw new Error(`${name} must be false`);
  return false;
}

function nonNegativeInteger(value: unknown, name: string): number {
  if (!Number.isSafeInteger(value) || Number(value) < 0) throw new Error(`${name} is invalid`);
  return Number(value);
}

function nullableNonNegativeInteger(value: unknown, name: string): number | null {
  return value === null ? null : nonNegativeInteger(value, name);
}

function nullablePositiveInteger(value: unknown, name: string): number | null {
  if (value === null) return null;
  if (!Number.isSafeInteger(value) || Number(value) < 1) throw new Error(`${name} is invalid`);
  return Number(value);
}

function pageLimit(value: unknown): number {
  const result = nonNegativeInteger(value, "limit");
  if (result < 1 || result > 200) throw new Error("limit must be between 1 and 200");
  return result;
}

function digest(value: unknown, name: string): string {
  const result = boundedString(value, name, 64);
  if (!HEX_64.test(result)) throw new Error(`${name} must be a SHA-256 digest`);
  return result;
}

function boundedPortableHash(value: unknown, name: string): string {
  const result = boundedString(value, name, 128);
  if (!/^[A-Za-z0-9._:-]+$/.test(result)) throw new Error(`${name} is invalid`);
  return result;
}

function decimalString(value: unknown, name: string): string {
  const result = boundedString(value, name, 128);
  if (!DECIMAL.test(result)) throw new Error(`${name} must be a decimal string`);
  return result;
}

function timestamp(value: unknown, name: string): string {
  if (typeof value !== "string" || value.length < 1 || value.length > 64) {
    throw new Error(`${name} must be a timestamp`);
  }
  if (!/(?:Z|[+-]\d{2}:\d{2})$/.test(value) || Number.isNaN(Date.parse(value))) {
    throw new Error(`${name} must include a timezone`);
  }
  return value;
}

function nullableTimestamp(value: unknown, name: string): string | null {
  if (value === null) return null;
  return timestamp(value, name);
}
