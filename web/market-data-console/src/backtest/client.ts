import {
  parseBacktestComparison,
  parseBacktestCreateResponse,
  parseBacktestJobList,
  parseBacktestJobRecord,
  parseBacktestResultSummary,
} from "./contracts";
import type {
  BacktestComparison,
  BacktestCreateResponse,
  BacktestJobRecord,
  BacktestResultSummary,
} from "./contracts";

const MAX_RESPONSE_BYTES = 256 * 1024;
const CONFIG_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

interface ApiErrorBody {
  error?: {
    code?: unknown;
    message?: unknown;
    action?: unknown;
    error_id?: unknown;
    retryable?: unknown;
  };
}

export class BacktestControlClientError extends Error {
  readonly code: string;
  readonly action: string | null;
  readonly errorId: string | null;
  readonly retryable: boolean;
  readonly status: number | null;

  constructor({
    code,
    message,
    action = null,
    errorId = null,
    retryable = false,
    status = null,
  }: {
    code: string;
    message: string;
    action?: string | null;
    errorId?: string | null;
    retryable?: boolean;
    status?: number | null;
  }) {
    super(message);
    this.name = "BacktestControlClientError";
    this.code = code;
    this.action = action;
    this.errorId = errorId;
    this.retryable = retryable;
    this.status = status;
  }
}

export class BacktestControlApiClient {
  constructor(
    private readonly baseUrl = "",
    private readonly readerToken: () => string = () => "",
    private readonly operatorToken: () => string = () => "",
  ) {}

  async list(signal?: AbortSignal): Promise<BacktestJobRecord[]> {
    const payload = await this.request("/api/control/v1/backtests?limit=100&offset=0", {
      method: "GET",
      auth: "reader",
      signal,
    });
    return parsePayload(payload, parseBacktestJobList, "backtest list");
  }

  async get(jobId: string, signal?: AbortSignal): Promise<BacktestJobRecord> {
    const safeJobId = durableJobId(jobId);
    const payload = await this.request(`/api/control/v1/backtests/${safeJobId}`, {
      method: "GET",
      auth: "reader",
      signal,
    });
    return parsePayload(payload, parseBacktestJobRecord, "backtest job");
  }

  async result(jobId: string, signal?: AbortSignal): Promise<BacktestResultSummary> {
    const safeJobId = durableJobId(jobId);
    const payload = await this.request(`/api/control/v1/backtests/${safeJobId}/result`, {
      method: "GET",
      auth: "reader",
      signal,
    });
    return parsePayload(payload, parseBacktestResultSummary, "backtest result");
  }

  async create(configId: string, idempotencyKey: string, signal?: AbortSignal): Promise<BacktestCreateResponse> {
    const normalizedConfigId = configId.trim();
    if (!CONFIG_ID_PATTERN.test(normalizedConfigId)) {
      throw new BacktestControlClientError({
        code: "BACKTEST_CONFIG_ID_INVALID",
        message: "Config identity must use only letters, digits, dot, underscore or dash.",
      });
    }
    const key = boundedHeaderValue(idempotencyKey, "Idempotency-Key");
    const payload = await this.request("/api/control/v1/backtests", {
      method: "POST",
      auth: "operator",
      signal,
      headers: { "Idempotency-Key": key, "Content-Type": "application/json" },
      body: JSON.stringify({ schema_version: 1, config_id: normalizedConfigId }),
    });
    return parsePayload(payload, parseBacktestCreateResponse, "backtest create response");
  }

  async compare(
    leftJobId: string,
    rightJobId: string,
    signal?: AbortSignal,
  ): Promise<BacktestComparison> {
    const left = durableJobId(leftJobId);
    const right = durableJobId(rightJobId);
    if (left === right) {
      throw new BacktestControlClientError({
        code: "BACKTEST_COMPARISON_IDENTICAL_JOBS",
        message: "Choose two different durable Backtest jobs for comparison.",
      });
    }
    const payload = await this.request("/api/control/v1/backtests/compare", {
      method: "POST",
      auth: "reader",
      signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ schema_version: 1, left_job_id: left, right_job_id: right }),
    });
    return parsePayload(payload, parseBacktestComparison, "backtest comparison");
  }

  private async request(
    path: string,
    options: {
      method: "GET" | "POST";
      auth: "reader" | "operator";
      signal?: AbortSignal | undefined;
      headers?: Record<string, string>;
      body?: string;
    },
  ): Promise<unknown> {
    const token = (options.auth === "reader" ? this.readerToken() : this.operatorToken()).trim();
    const headers: Record<string, string> = { Accept: "application/json", ...options.headers };
    if (token) headers.Authorization = `Bearer ${token}`;

    let response: Response;
    try {
      response = await fetch(`${this.baseUrl}${path}`, {
        method: options.method,
        headers,
        cache: "no-store",
        credentials: "omit",
        ...(options.body === undefined ? {} : { body: options.body }),
        ...(options.signal === undefined ? {} : { signal: options.signal }),
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      throw new BacktestControlClientError({
        code: "CONTROL_BACKTEST_TRANSPORT_ERROR",
        message: "Could not reach the local Backtest Control Plane.",
        action: "Verify modular-control-api is running on the local loopback endpoint.",
        retryable: true,
      });
    }

    const declaredLength = Number(response.headers.get("content-length"));
    if (Number.isFinite(declaredLength) && declaredLength > MAX_RESPONSE_BYTES) {
      throw new BacktestControlClientError({
        code: "CONTROL_BACKTEST_RESPONSE_TOO_LARGE",
        message: "Backtest Control response exceeded the browser safety limit.",
        status: response.status,
      });
    }
    const text = await response.text();
    if (new TextEncoder().encode(text).byteLength > MAX_RESPONSE_BYTES) {
      throw new BacktestControlClientError({
        code: "CONTROL_BACKTEST_RESPONSE_TOO_LARGE",
        message: "Backtest Control response exceeded the browser safety limit.",
        status: response.status,
      });
    }

    let payload: unknown = null;
    if (text) {
      try {
        payload = JSON.parse(text) as unknown;
      } catch {
        throw new BacktestControlClientError({
          code: "CONTROL_BACKTEST_INVALID_JSON",
          message: "Backtest Control returned an invalid JSON response.",
          status: response.status,
        });
      }
    }
    if (!response.ok) throw apiError(response.status, payload);
    return payload;
  }
}

function apiError(status: number, payload: unknown): BacktestControlClientError {
  const candidate = typeof payload === "object" && payload !== null ? (payload as ApiErrorBody).error : undefined;
  const code = typeof candidate?.code === "string" ? candidate.code : "CONTROL_BACKTEST_REQUEST_FAILED";
  const message =
    typeof candidate?.message === "string"
      ? candidate.message
      : "Backtest Control request failed without a safe server message.";
  const action = typeof candidate?.action === "string" ? candidate.action : null;
  const errorId = typeof candidate?.error_id === "string" ? candidate.error_id : null;
  const retryable = typeof candidate?.retryable === "boolean" ? candidate.retryable : false;
  return new BacktestControlClientError({ code, message, action, errorId, retryable, status });
}

function parsePayload<T>(value: unknown, parser: (input: unknown) => T, label: string): T {
  try {
    return parser(value);
  } catch {
    throw new BacktestControlClientError({
      code: "CONTROL_BACKTEST_CONTRACT_MISMATCH",
      message: `Backtest Control returned an invalid ${label} contract.`,
    });
  }
}

function durableJobId(value: string): string {
  const normalized = value.trim();
  if (!/^bt_[0-9a-f]{20}$/.test(normalized)) {
    throw new BacktestControlClientError({
      code: "BACKTEST_JOB_ID_INVALID",
      message: "Backtest job identity is not valid.",
    });
  }
  return normalized;
}

function boundedHeaderValue(value: string, name: string): string {
  const normalized = value.trim();
  if (!normalized || normalized.length > 128 || /[\r\n]/.test(normalized)) {
    throw new BacktestControlClientError({
      code: "BACKTEST_IDEMPOTENCY_KEY_INVALID",
      message: `${name} must contain 1..128 single-line characters.`,
    });
  }
  return normalized;
}