import { parseOperationalEventPage } from "./contracts";
import type { OperationalEventPage, OperationalEventSeverity } from "./contracts";

const MAX_RESPONSE_BYTES = 512 * 1024;
const ACCEPTED_EVENT_TYPES = new Set([
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
const ACCEPTED_SEVERITIES = new Set<OperationalEventSeverity>(["INFO", "WARNING", "ERROR"]);
const CURSOR = /^trr-v1:[0-9a-f]{16}:[0-9]{1,7}$/;

interface OperationalEventsQuery {
  cursor?: string | null;
  limit?: number;
  eventTypes?: string[];
  severities?: OperationalEventSeverity[];
}

interface OperationalEventsClientErrorOptions {
  action?: string | null;
  retryable?: boolean;
  errorId?: string | null;
}

export class OperationalEventsClientError extends Error {
  readonly code: string;
  readonly status: number;
  readonly action: string | null;
  readonly retryable: boolean;
  readonly errorId: string | null;

  constructor(
    code: string,
    message: string,
    status: number,
    options: OperationalEventsClientErrorOptions = {},
  ) {
    super(message);
    this.name = "OperationalEventsClientError";
    this.code = code;
    this.status = status;
    this.action = options.action ?? null;
    this.retryable = options.retryable ?? false;
    this.errorId = options.errorId ?? null;
  }
}

export class OperationalEventsControlApiClient {
  constructor(
    private readonly baseUrl = "",
    private readonly readerTokenProvider: () => string = () => "",
  ) {}

  async events(query: OperationalEventsQuery = {}, signal?: AbortSignal): Promise<OperationalEventPage> {
    const parameters = validatedParameters(query);
    const suffix = parameters.toString();
    const value = await this.request(
      `/api/control/v1/events/trading-runner${suffix ? `?${suffix}` : ""}`,
      signal,
    );
    try {
      return parseOperationalEventPage(value);
    } catch {
      throw new OperationalEventsClientError(
        "CONTROL_EVENTS_CONTRACT_INVALID",
        "Structured operational-events response failed the browser contract.",
        0,
        { action: "Update the local Control Plane and Dashboard together." },
      );
    }
  }

  private async request(path: string, signal?: AbortSignal): Promise<unknown> {
    const headers = new Headers({ Accept: "application/json" });
    const token = this.readerTokenProvider().trim();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    let response: Response;
    try {
      response = await fetch(`${this.baseUrl}${path}`, {
        method: "GET",
        headers,
        cache: "no-store",
        credentials: "omit",
        ...(signal ? { signal } : {}),
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      throw new OperationalEventsClientError(
        "CONTROL_EVENTS_TRANSPORT_ERROR",
        "Structured operational events are unavailable.",
        0,
        { action: "Check the local Control Plane and Trading Runner owner.", retryable: true },
      );
    }
    const declared = response.headers.get("content-length");
    if (declared !== null) {
      const bytes = Number(declared);
      if (Number.isFinite(bytes) && bytes > MAX_RESPONSE_BYTES) throw tooLarge(response.status);
    }
    const text = await response.text();
    if (new TextEncoder().encode(text).byteLength > MAX_RESPONSE_BYTES) throw tooLarge(response.status);
    let value: unknown = null;
    if (text) {
      try {
        value = JSON.parse(text) as unknown;
      } catch {
        throw new OperationalEventsClientError(
          "CONTROL_EVENTS_INVALID_RESPONSE",
          "Structured operational-events endpoint returned invalid JSON.",
          response.status,
        );
      }
    }
    if (!response.ok) throw serverError(response.status, value);
    return value;
  }
}

function validatedParameters(query: OperationalEventsQuery): URLSearchParams {
  const limit = query.limit ?? 100;
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 200) {
    throw new OperationalEventsClientError(
      "CONTROL_EVENTS_CLIENT_INVALID_LIMIT",
      "Event page limit must be an integer between 1 and 200.",
      0,
    );
  }
  const parameters = new URLSearchParams({ limit: String(limit) });
  if (query.cursor !== undefined && query.cursor !== null) {
    if (!CURSOR.test(query.cursor)) {
      throw new OperationalEventsClientError(
        "CONTROL_EVENTS_CLIENT_INVALID_CURSOR",
        "Operational event cursor is invalid.",
        0,
      );
    }
    parameters.append("cursor", query.cursor);
  }
  const eventTypes = query.eventTypes ?? [];
  if (eventTypes.length > ACCEPTED_EVENT_TYPES.size || new Set(eventTypes).size !== eventTypes.length) {
    throw new OperationalEventsClientError(
      "CONTROL_EVENTS_CLIENT_INVALID_FILTER",
      "Event-type filters contain duplicates or too many values.",
      0,
    );
  }
  for (const value of eventTypes) {
    if (!ACCEPTED_EVENT_TYPES.has(value)) {
      throw new OperationalEventsClientError(
        "CONTROL_EVENTS_CLIENT_INVALID_FILTER",
        "Unsupported event-type filter.",
        0,
      );
    }
    parameters.append("event_type", value);
  }
  const severities = query.severities ?? [];
  if (severities.length > ACCEPTED_SEVERITIES.size || new Set(severities).size !== severities.length) {
    throw new OperationalEventsClientError(
      "CONTROL_EVENTS_CLIENT_INVALID_FILTER",
      "Severity filters contain duplicates or too many values.",
      0,
    );
  }
  for (const value of severities) {
    if (!ACCEPTED_SEVERITIES.has(value)) {
      throw new OperationalEventsClientError(
        "CONTROL_EVENTS_CLIENT_INVALID_FILTER",
        "Unsupported severity filter.",
        0,
      );
    }
    parameters.append("severity", value);
  }
  return parameters;
}

function tooLarge(status: number): OperationalEventsClientError {
  return new OperationalEventsClientError(
    "CONTROL_EVENTS_RESPONSE_TOO_LARGE",
    "Structured operational-events response exceeded the browser safety limit.",
    status,
  );
}

function serverError(status: number, value: unknown): OperationalEventsClientError {
  const envelope = asRecord(value);
  const detail = asRecord(envelope?.error);
  return new OperationalEventsClientError(
    safeString(detail?.code, 128) ?? "CONTROL_EVENTS_REQUEST_FAILED",
    safeString(detail?.message, 512) ?? "Structured operational-events request failed.",
    status,
    {
      action: safeString(detail?.action, 512),
      retryable: detail?.retryable === true,
      errorId: safeString(detail?.error_id, 128),
    },
  );
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function safeString(value: unknown, maximum: number): string | null {
  if (typeof value !== "string" || value.length < 1 || value.length > maximum) return null;
  return value;
}
