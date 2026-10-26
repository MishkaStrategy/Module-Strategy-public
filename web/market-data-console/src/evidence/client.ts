import {
  parseAuditDecisionList,
  parseAuditEvidenceList,
  parseEvidenceOverview,
  parseResultsEvidenceList,
} from "./contracts";
import type {
  AuditDecisionList,
  AuditEvidenceList,
  EvidenceOverview,
  ResultsEvidenceList,
} from "./contracts";

const MAX_RESPONSE_BYTES = 512 * 1024;

export class EvidenceControlClientError extends Error {
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
    this.name = "EvidenceControlClientError";
    this.code = code;
    this.status = status;
    this.action = action;
    this.retryable = retryable;
    this.errorId = errorId;
  }
}

export class EvidenceControlApiClient {
  constructor(
    private readonly baseUrl = "",
    private readonly readerTokenProvider: () => string = () => "",
  ) {}

  async overview(signal?: AbortSignal | undefined): Promise<EvidenceOverview> {
    return this.parsed(
      "/api/control/v1/evidence/overview",
      parseEvidenceOverview,
      signal,
    );
  }

  async auditDecisions(
    limit = 50,
    offset = 0,
    signal?: AbortSignal | undefined,
  ): Promise<AuditDecisionList> {
    validatePage(limit, offset);
    return this.parsed(
      `/api/control/v1/evidence/audit/decisions?limit=${limit}&offset=${offset}`,
      parseAuditDecisionList,
      signal,
    );
  }

  async auditEvidence(
    limit = 50,
    offset = 0,
    signal?: AbortSignal | undefined,
  ): Promise<AuditEvidenceList> {
    validatePage(limit, offset);
    return this.parsed(
      `/api/control/v1/evidence/audit/evidence?limit=${limit}&offset=${offset}`,
      parseAuditEvidenceList,
      signal,
    );
  }

  async resultsEvidence(
    limit = 50,
    offset = 0,
    signal?: AbortSignal | undefined,
  ): Promise<ResultsEvidenceList> {
    validatePage(limit, offset);
    return this.parsed(
      `/api/control/v1/evidence/results?limit=${limit}&offset=${offset}`,
      parseResultsEvidenceList,
      signal,
    );
  }

  private async parsed<T>(
    path: string,
    parser: (value: unknown) => T,
    signal?: AbortSignal | undefined,
  ): Promise<T> {
    const value = await this.request(path, signal);
    try {
      return parser(value);
    } catch (error) {
      throw new EvidenceControlClientError({
        code: "CONTROL_EVIDENCE_CONTRACT_INVALID",
        message: "Evidence response failed the browser contract.",
        status: 0,
        action: error instanceof Error ? "Update the local Control Plane and browser together." : null,
      });
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
      throw new EvidenceControlClientError({
        code: "CONTROL_EVIDENCE_TRANSPORT_ERROR",
        message: "Evidence endpoint is unavailable.",
        status: 0,
        action: "Check the local Control Plane and retry.",
        retryable: true,
      });
    }
    const declared = response.headers.get("content-length");
    if (declared !== null) {
      const bytes = Number(declared);
      if (Number.isFinite(bytes) && bytes > MAX_RESPONSE_BYTES) {
        throw new EvidenceControlClientError({
          code: "CONTROL_EVIDENCE_RESPONSE_TOO_LARGE",
          message: "Evidence response exceeded the browser safety limit.",
          status: response.status,
        });
      }
    }
    const text = await response.text();
    if (new TextEncoder().encode(text).byteLength > MAX_RESPONSE_BYTES) {
      throw new EvidenceControlClientError({
        code: "CONTROL_EVIDENCE_RESPONSE_TOO_LARGE",
        message: "Evidence response exceeded the browser safety limit.",
        status: response.status,
      });
    }
    let value: unknown = null;
    if (text) {
      try {
        value = JSON.parse(text) as unknown;
      } catch {
        throw new EvidenceControlClientError({
          code: "CONTROL_EVIDENCE_INVALID_RESPONSE",
          message: "Evidence endpoint returned invalid JSON.",
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
    throw new EvidenceControlClientError({
      code: "CONTROL_EVIDENCE_CLIENT_INVALID_LIMIT",
      message: "Evidence limit must be an integer between 1 and 200.",
      status: 0,
    });
  }
  if (!Number.isSafeInteger(offset) || offset < 0) {
    throw new EvidenceControlClientError({
      code: "CONTROL_EVIDENCE_CLIENT_INVALID_OFFSET",
      message: "Evidence offset must be a non-negative integer.",
      status: 0,
    });
  }
}

function serverError(status: number, value: unknown): EvidenceControlClientError {
  const envelope = asRecord(value);
  const detail = asRecord(envelope?.error);
  return new EvidenceControlClientError({
    code: safeString(detail?.code, 128) ?? "CONTROL_EVIDENCE_REQUEST_FAILED",
    message: safeString(detail?.message, 512) ?? "Evidence request failed.",
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

function safeString(value: unknown, maximum: number): string | null {
  if (typeof value !== "string" || value.length < 1 || value.length > maximum) return null;
  return value;
}
