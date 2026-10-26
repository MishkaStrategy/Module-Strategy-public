import {
  parseAcceptanceCurrent,
  parseAcceptanceMutationResult,
} from "./contracts";
import type {
  AcceptanceCurrent,
  AcceptanceMutationResult,
  AcceptanceRunMutationRequest,
  AcceptanceStartRequest,
} from "./contracts";

const MAX_RESPONSE_BYTES = 512 * 1024;

interface AcceptanceClientErrorOptions {
  action?: string | null;
  retryable?: boolean;
  errorId?: string | null;
}

export class AcceptanceClientError extends Error {
  readonly code: string;
  readonly status: number;
  readonly action: string | null;
  readonly retryable: boolean;
  readonly errorId: string | null;

  constructor(
    code: string,
    message: string,
    status: number,
    options: AcceptanceClientErrorOptions = {},
  ) {
    super(message);
    this.name = "AcceptanceClientError";
    this.code = code;
    this.status = status;
    this.action = options.action ?? null;
    this.retryable = options.retryable ?? false;
    this.errorId = options.errorId ?? null;
  }
}

export class AcceptanceControlApiClient {
  constructor(
    private readonly baseUrl = "",
    private readonly readerTokenProvider: () => string = () => "",
    private readonly operatorTokenProvider: () => string = () => "",
  ) {}

  async current(signal?: AbortSignal): Promise<AcceptanceCurrent> {
    return this.parsed(
      "/api/control/v1/acceptance/current",
      "GET",
      null,
      false,
      parseAcceptanceCurrent,
      signal,
    );
  }

  async start(
    request: AcceptanceStartRequest,
    signal?: AbortSignal,
  ): Promise<AcceptanceMutationResult> {
    return this.mutation("/api/control/v1/acceptance/start", request, signal);
  }

  async stop(
    request: AcceptanceRunMutationRequest,
    signal?: AbortSignal,
  ): Promise<AcceptanceMutationResult> {
    return this.mutation("/api/control/v1/acceptance/stop", request, signal);
  }

  async cancel(
    request: AcceptanceRunMutationRequest,
    signal?: AbortSignal,
  ): Promise<AcceptanceMutationResult> {
    return this.mutation("/api/control/v1/acceptance/cancel", request, signal);
  }

  async retry(
    request: AcceptanceRunMutationRequest,
    signal?: AbortSignal,
  ): Promise<AcceptanceMutationResult> {
    return this.mutation("/api/control/v1/acceptance/retry", request, signal);
  }

  private async mutation(
    path: string,
    request: AcceptanceStartRequest | AcceptanceRunMutationRequest,
    signal?: AbortSignal,
  ): Promise<AcceptanceMutationResult> {
    return this.parsed(
      path,
      "POST",
      request,
      true,
      parseAcceptanceMutationResult,
      signal,
    );
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
      throw new AcceptanceClientError(
        "CONTROL_ACCEPTANCE_CONTRACT_INVALID",
        "System Acceptance response failed the browser contract.",
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
      throw new AcceptanceClientError(
        "CONTROL_ACCEPTANCE_TRANSPORT_ERROR",
        "System Acceptance endpoint is unavailable.",
        0,
        {
          action: "Check the local Control Plane and pinned Acceptance owner, then retry.",
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
    if (new TextEncoder().encode(text).byteLength > MAX_RESPONSE_BYTES) throw tooLarge(response.status);
    let value: unknown = null;
    if (text) {
      try {
        value = JSON.parse(text) as unknown;
      } catch {
        throw new AcceptanceClientError(
          "CONTROL_ACCEPTANCE_INVALID_RESPONSE",
          "System Acceptance endpoint returned invalid JSON.",
          response.status,
        );
      }
    }
    if (!response.ok) throw serverError(response.status, value);
    return value;
  }
}

function tooLarge(status: number): AcceptanceClientError {
  return new AcceptanceClientError(
    "CONTROL_ACCEPTANCE_RESPONSE_TOO_LARGE",
    "System Acceptance response exceeded the browser safety limit.",
    status,
  );
}

function serverError(status: number, value: unknown): AcceptanceClientError {
  const envelope = asRecord(value);
  const detail = asRecord(envelope?.error);
  return new AcceptanceClientError(
    safeString(detail?.code, 128) ?? "CONTROL_ACCEPTANCE_REQUEST_FAILED",
    safeString(detail?.message, 512) ?? "System Acceptance request failed.",
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
