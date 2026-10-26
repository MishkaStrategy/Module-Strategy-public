import {
  parseResearchExperimentList,
  parseResearchExperimentSummary,
  parseResearchRunList,
  parseResearchRunSummary,
} from "./contracts";
import type {
  ResearchExperimentList,
  ResearchExperimentSummary,
  ResearchRunList,
  ResearchRunSummary,
} from "./contracts";

const MAX_RESPONSE_BYTES = 256 * 1024;

export class ResearchControlClientError extends Error {
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
    this.name = "ResearchControlClientError";
    this.code = code;
    this.status = status;
    this.action = action;
    this.retryable = retryable;
    this.errorId = errorId;
  }
}

export class ResearchControlApiClient {
  constructor(
    private readonly baseUrl = "",
    private readonly readerTokenProvider: () => string = () => "",
  ) {}

  async listExperiments(
    limit = 50,
    offset = 0,
    signal?: AbortSignal | undefined,
  ): Promise<ResearchExperimentList> {
    validatePage(limit, offset);
    const value = await this.request(
      `/api/control/v1/research/experiments?limit=${limit}&offset=${offset}`,
      signal,
    );
    try {
      return parseResearchExperimentList(value);
    } catch (error) {
      throw contractError(error);
    }
  }

  async getExperiment(
    experimentId: string,
    signal?: AbortSignal | undefined,
  ): Promise<ResearchExperimentSummary> {
    const id = identifier(experimentId, "experiment");
    const value = await this.request(
      `/api/control/v1/research/experiments/${encodeURIComponent(id)}`,
      signal,
    );
    try {
      return parseResearchExperimentSummary(value);
    } catch (error) {
      throw contractError(error);
    }
  }

  async listRuns(
    experimentId: string,
    limit = 100,
    offset = 0,
    signal?: AbortSignal | undefined,
  ): Promise<ResearchRunList> {
    const id = identifier(experimentId, "experiment");
    validatePage(limit, offset);
    const value = await this.request(
      `/api/control/v1/research/experiments/${encodeURIComponent(id)}/runs?limit=${limit}&offset=${offset}`,
      signal,
    );
    try {
      const parsed = parseResearchRunList(value);
      if (parsed.experiment_id !== id) {
        throw new Error("Research run list experiment identity mismatch");
      }
      return parsed;
    } catch (error) {
      throw contractError(error);
    }
  }

  async getRun(runId: string, signal?: AbortSignal | undefined): Promise<ResearchRunSummary> {
    const id = identifier(runId, "run");
    const value = await this.request(
      `/api/control/v1/research/runs/${encodeURIComponent(id)}`,
      signal,
    );
    try {
      const parsed = parseResearchRunSummary(value);
      if (parsed.run_id !== id) throw new Error("Research run identity mismatch");
      return parsed;
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
      throw new ResearchControlClientError({
        code: "CONTROL_RESEARCH_TRANSPORT_ERROR",
        message: "Research read-only endpoint is unavailable.",
        status: 0,
        action: "Check the local Control Plane and retry.",
        retryable: true,
      });
    }
    const declared = response.headers.get("content-length");
    if (declared !== null) {
      const bytes = Number(declared);
      if (Number.isFinite(bytes) && bytes > MAX_RESPONSE_BYTES) {
        throw responseTooLarge(response.status);
      }
    }
    const text = await response.text();
    if (new TextEncoder().encode(text).byteLength > MAX_RESPONSE_BYTES) {
      throw responseTooLarge(response.status);
    }
    let value: unknown = null;
    if (text) {
      try {
        value = JSON.parse(text) as unknown;
      } catch {
        throw new ResearchControlClientError({
          code: "CONTROL_RESEARCH_INVALID_RESPONSE",
          message: "Research endpoint returned invalid JSON.",
          status: response.status,
        });
      }
    }
    if (!response.ok) throw serverError(response.status, value);
    return value;
  }
}

function validatePage(limit: number, offset: number): void {
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 200) {
    throw new ResearchControlClientError({
      code: "CONTROL_RESEARCH_CLIENT_INVALID_LIMIT",
      message: "Research list limit must be an integer between 1 and 200.",
      status: 0,
    });
  }
  if (!Number.isSafeInteger(offset) || offset < 0) {
    throw new ResearchControlClientError({
      code: "CONTROL_RESEARCH_CLIENT_INVALID_OFFSET",
      message: "Research list offset must be a non-negative integer.",
      status: 0,
    });
  }
}

function identifier(value: string, kind: string): string {
  const normalized = value.trim();
  if (normalized.length < 1 || normalized.length > 256 || /[\u0000\r\n]/u.test(normalized)) {
    throw new ResearchControlClientError({
      code: "CONTROL_RESEARCH_CLIENT_INVALID_ID",
      message: `Research ${kind} identity is invalid.`,
      status: 0,
    });
  }
  return normalized;
}

function contractError(error: unknown): ResearchControlClientError {
  return new ResearchControlClientError({
    code: "CONTROL_RESEARCH_CONTRACT_INVALID",
    message: "Research response failed the browser contract.",
    status: 0,
    action: error instanceof Error ? "Update the local Control Plane and browser together." : null,
  });
}

function responseTooLarge(status: number): ResearchControlClientError {
  return new ResearchControlClientError({
    code: "CONTROL_RESEARCH_RESPONSE_TOO_LARGE",
    message: "Research response exceeded the browser safety limit.",
    status,
  });
}

function serverError(status: number, value: unknown): ResearchControlClientError {
  const envelope = asRecord(value);
  const detail = asRecord(envelope?.error);
  return new ResearchControlClientError({
    code: safeString(detail?.code, 128) ?? "CONTROL_RESEARCH_REQUEST_FAILED",
    message: safeString(detail?.message, 512) ?? "Research request failed.",
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
