import {
  parsePaperBlockedScopeList,
  parsePaperLifecycleCapability,
  parsePaperLifecycleMutationResult,
  parsePaperOrderList,
  parsePaperPositionsSnapshot,
  parsePaperRuntimeSummary,
} from "./contracts";
import type {
  PaperBlockedScopeList,
  PaperLifecycleCapability,
  PaperLifecycleMutation,
  PaperLifecycleMutationResult,
  PaperOrderList,
  PaperPositionsSnapshot,
  PaperRuntimeSummary,
} from "./contracts";

const MAX_RESPONSE_BYTES = 256 * 1024;
const REQUEST_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const MUTATION_PATHS: Record<PaperLifecycleMutation, string> = {
  START: "/api/control/v1/paper/start",
  STOP: "/api/control/v1/paper/stop",
  ACTIVATE_KILL_SWITCH: "/api/control/v1/paper/kill-switch/activate",
  RESET_KILL_SWITCH: "/api/control/v1/paper/kill-switch/reset",
};

interface PaperRuntimeClientErrorOptions {
  action?: string | null;
  retryable?: boolean;
  errorId?: string | null;
}

export class PaperRuntimeClientError extends Error {
  readonly code: string;
  readonly status: number;
  readonly action: string | null;
  readonly retryable: boolean;
  readonly errorId: string | null;

  constructor(
    code: string,
    message: string,
    status: number,
    options: PaperRuntimeClientErrorOptions = {},
  ) {
    super(message);
    this.name = "PaperRuntimeClientError";
    this.code = code;
    this.status = status;
    this.action = options.action ?? null;
    this.retryable = options.retryable ?? false;
    this.errorId = options.errorId ?? null;
  }
}

export class PaperRuntimeControlApiClient {
  constructor(
    private readonly baseUrl = "",
    private readonly readerTokenProvider: () => string = () => "",
    private readonly operatorTokenProvider: () => string = () => "",
  ) {}

  async runtime(signal?: AbortSignal): Promise<PaperRuntimeSummary> {
    return this.parsed(
      "/api/control/v1/paper/runtime",
      "GET",
      null,
      false,
      parsePaperRuntimeSummary,
      signal,
    );
  }

  async orders(limit = 50, offset = 0, signal?: AbortSignal): Promise<PaperOrderList> {
    validatePage(limit, offset);
    return this.parsed(
      `/api/control/v1/paper/orders?limit=${limit}&offset=${offset}`,
      "GET",
      null,
      false,
      parsePaperOrderList,
      signal,
    );
  }

  async blockedScopes(signal?: AbortSignal): Promise<PaperBlockedScopeList> {
    return this.parsed(
      "/api/control/v1/paper/blocked-scopes",
      "GET",
      null,
      false,
      parsePaperBlockedScopeList,
      signal,
    );
  }

  async positions(signal?: AbortSignal): Promise<PaperPositionsSnapshot> {
    return this.parsed(
      "/api/control/v1/paper/positions",
      "GET",
      null,
      false,
      parsePaperPositionsSnapshot,
      signal,
    );
  }

  async lifecycleCapability(signal?: AbortSignal): Promise<PaperLifecycleCapability> {
    return this.parsed(
      "/api/control/v1/paper/lifecycle-capability",
      "GET",
      null,
      false,
      parsePaperLifecycleCapability,
      signal,
    );
  }

  async lifecycleMutate(
    mutation: PaperLifecycleMutation,
    requestId: string,
    signal?: AbortSignal,
  ): Promise<PaperLifecycleMutationResult> {
    validateRequestId(requestId);
    const result = await this.parsed(
      MUTATION_PATHS[mutation],
      "POST",
      { schema_version: 1, request_id: requestId },
      true,
      parsePaperLifecycleMutationResult,
      signal,
    );
    if (result.request_id !== requestId || result.mutation !== mutation) {
      throw new PaperRuntimeClientError(
        "CONTROL_PAPER_RUNTIME_CONTRACT_INVALID",
        "Paper Runtime lifecycle receipt identity does not match the request.",
        0,
        { action: "Update the local Control Plane and Dashboard together." },
      );
    }
    return result;
  }

  private async parsed<T>(
    path: string,
    method: "GET" | "POST",
    body: object | null,
    operator: boolean,
    parser: (value: unknown) => T,
    signal?: AbortSignal,
  ): Promise<T> {
    const value = await this.request(path, method, body, operator, signal);
    try {
      return parser(value);
    } catch {
      throw new PaperRuntimeClientError(
        "CONTROL_PAPER_RUNTIME_CONTRACT_INVALID",
        "Paper Runtime response failed the browser contract.",
        0,
        { action: "Update the local Control Plane and Dashboard together." },
      );
    }
  }

  private async request(
    path: string,
    method: "GET" | "POST",
    body: object | null,
    operator: boolean,
    signal?: AbortSignal,
  ): Promise<unknown> {
    const headers = new Headers({ Accept: "application/json" });
    const token = (operator ? this.operatorTokenProvider() : this.readerTokenProvider()).trim();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    let serialized: string | undefined;
    if (body !== null) {
      serialized = JSON.stringify(body);
      headers.set("Content-Type", "application/json");
    }
    let response: Response;
    try {
      response = await fetch(`${this.baseUrl}${path}`, {
        method,
        headers,
        cache: "no-store",
        credentials: "omit",
        ...(serialized === undefined ? {} : { body: serialized }),
        ...(signal ? { signal } : {}),
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      throw new PaperRuntimeClientError(
        "CONTROL_PAPER_RUNTIME_TRANSPORT_ERROR",
        "Paper Runtime endpoint is unavailable.",
        0,
        {
          action: "Check the local Control Plane and Trading Runner owner, then retry.",
          retryable: true,
        },
      );
    }
    const declared = response.headers.get("content-length");
    if (declared !== null) {
      const bytes = Number(declared);
      if (Number.isFinite(bytes) && bytes > MAX_RESPONSE_BYTES) throw tooLarge(response.status);
    }
    const text = await response.text();
    if (new TextEncoder().encode(text).byteLength > MAX_RESPONSE_BYTES) {
      throw tooLarge(response.status);
    }
    let value: unknown = null;
    if (text) {
      try {
        value = JSON.parse(text) as unknown;
      } catch {
        throw new PaperRuntimeClientError(
          "CONTROL_PAPER_RUNTIME_INVALID_RESPONSE",
          "Paper Runtime endpoint returned invalid JSON.",
          response.status,
        );
      }
    }
    if (!response.ok) throw serverError(response.status, value);
    return value;
  }
}

function validatePage(limit: number, offset: number): void {
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 200) {
    throw new PaperRuntimeClientError(
      "CONTROL_PAPER_RUNTIME_CLIENT_INVALID_LIMIT",
      "Paper order limit must be an integer between 1 and 200.",
      0,
    );
  }
  if (!Number.isSafeInteger(offset) || offset < 0) {
    throw new PaperRuntimeClientError(
      "CONTROL_PAPER_RUNTIME_CLIENT_INVALID_OFFSET",
      "Paper order offset must be a non-negative integer.",
      0,
    );
  }
}

function validateRequestId(value: string): void {
  if (!REQUEST_ID.test(value)) {
    throw new PaperRuntimeClientError(
      "CONTROL_PAPER_RUNTIME_CLIENT_INVALID_REQUEST_ID",
      "PAPER lifecycle request ID must be a bounded portable identifier.",
      0,
    );
  }
}

function tooLarge(status: number): PaperRuntimeClientError {
  return new PaperRuntimeClientError(
    "CONTROL_PAPER_RUNTIME_RESPONSE_TOO_LARGE",
    "Paper Runtime response exceeded the browser safety limit.",
    status,
  );
}

function serverError(status: number, value: unknown): PaperRuntimeClientError {
  const envelope = asRecord(value);
  const detail = asRecord(envelope?.error);
  return new PaperRuntimeClientError(
    safeString(detail?.code, 128) ?? "CONTROL_PAPER_RUNTIME_REQUEST_FAILED",
    safeString(detail?.message, 512) ?? "Paper Runtime request failed.",
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
