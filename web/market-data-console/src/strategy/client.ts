import { parseStrategyRunList, parseStrategyRunSummary } from "./contracts";
import type { StrategyRunList, StrategyRunSummary } from "./contracts";

const MAX_RESPONSE_BYTES = 256 * 1024;
const RUN_ID = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;

export class StrategyControlClientError extends Error {
  readonly code: string;
  readonly status: number;
  readonly action: string | null;
  readonly retryable: boolean;
  readonly errorId: string | null;

  constructor({
    code,
    message,
    status,
    action = null,
    retryable = false,
    errorId = null,
  }: {
    code: string;
    message: string;
    status: number;
    action?: string | null;
    retryable?: boolean;
    errorId?: string | null;
  }) {
    super(message);
    this.name = "StrategyControlClientError";
    this.code = code;
    this.status = status;
    this.action = action;
    this.retryable = retryable;
    this.errorId = errorId;
  }
}

export class StrategyControlApiClient {
  constructor(
    private readonly baseUrl = "",
    private readonly readerTokenProvider: () => string = () => "",
  ) {}

  async list(
    limit = 50,
    offset = 0,
    signal?: AbortSignal | undefined,
  ): Promise<StrategyRunList> {
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 200) {
      throw new StrategyControlClientError({
        code: "CONTROL_STRATEGY_CLIENT_INVALID_LIMIT",
        message: "Strategy run limit must be an integer between 1 and 200.",
        status: 0,
      });
    }
    if (!Number.isSafeInteger(offset) || offset < 0) {
      throw new StrategyControlClientError({
        code: "CONTROL_STRATEGY_CLIENT_INVALID_OFFSET",
        message: "Strategy run offset must be a non-negative integer.",
        status: 0,
      });
    }
    const value = await this.request(
      `/api/control/v1/strategy/runs?limit=${limit}&offset=${offset}`,
      signal,
    );
    try {
      return parseStrategyRunList(value);
    } catch (error) {
      throw contractError(error);
    }
  }

  async get(runId: string, signal?: AbortSignal | undefined): Promise<StrategyRunSummary> {
    const normalized = runId.trim();
    if (!RUN_ID.test(normalized)) {
      throw new StrategyControlClientError({
        code: "CONTROL_STRATEGY_CLIENT_INVALID_RUN_ID",
        message: "Strategy run identity is invalid.",
        status: 0,
      });
    }
    const value = await this.request(
      `/api/control/v1/strategy/runs/${encodeURIComponent(normalized)}`,
      signal,
    );
    try {
      return parseStrategyRunSummary(value);
    } catch (error) {
      throw contractError(error);
    }
  }

  private async request(path: string, signal?: AbortSignal | undefined): Promise<unknown> {
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
      throw new StrategyControlClientError({
        code: "CONTROL_STRATEGY_TRANSPORT_ERROR",
        message: "Strategy governance endpoint is unavailable.",
        status: 0,
        action: "Check the local Control Plane and retry.",
        retryable: true,
      });
    }
    const declared = response.headers.get("content-length");
    if (declared !== null) {
      const bytes = Number(declared);
      if (Number.isFinite(bytes) && bytes > MAX_RESPONSE_BYTES) {
        throw new StrategyControlClientError({
          code: "CONTROL_STRATEGY_RESPONSE_TOO_LARGE",
          message: "Strategy governance response exceeded the browser safety limit.",
          status: response.status,
        });
      }
    }
    const text = await response.text();
    if (new TextEncoder().encode(text).byteLength > MAX_RESPONSE_BYTES) {
      throw new StrategyControlClientError({
        code: "CONTROL_STRATEGY_RESPONSE_TOO_LARGE",
        message: "Strategy governance response exceeded the browser safety limit.",
        status: response.status,
      });
    }
    let value: unknown = null;
    if (text) {
      try {
        value = JSON.parse(text) as unknown;
      } catch {
        throw new StrategyControlClientError({
          code: "CONTROL_STRATEGY_INVALID_RESPONSE",
          message: "Strategy governance endpoint returned invalid JSON.",
          status: response.status,
        });
      }
    }
    if (!response.ok) throw serverError(response.status, value);
    return value;
  }
}

function contractError(error: unknown): StrategyControlClientError {
  return new StrategyControlClientError({
    code: "CONTROL_STRATEGY_CONTRACT_INVALID",
    message: "Strategy governance response failed the browser contract.",
    status: 0,
    action: error instanceof Error ? "Update the local Control Plane and browser together." : null,
  });
}

function serverError(status: number, value: unknown): StrategyControlClientError {
  const envelope = asRecord(value);
  const detail = asRecord(envelope?.error);
  return new StrategyControlClientError({
    code: safeString(detail?.code, 128) ?? "CONTROL_STRATEGY_REQUEST_FAILED",
    message: safeString(detail?.message, 512) ?? "Strategy governance request failed.",
    status,
    action: safeString(detail?.action, 512),
    retryable: detail?.retryable === true,
    errorId: safeString(detail?.error_id, 128),
  });
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function safeString(value: unknown, maxLength: number): string | null {
  if (typeof value !== "string" || value.length < 1 || value.length > maxLength) return null;
  return value;
}
