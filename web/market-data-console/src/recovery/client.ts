import { buildControlHeaders } from "../control/client";
import {
  parseRecoveryCapability,
  parseRecoveryResolutionSummary,
  validateRecoveryResolutionRequest,
} from "./contracts";
import type {
  RecoveryCapability,
  RecoveryResolutionRequest,
  RecoveryResolutionSummary,
} from "./contracts";

export class RecoveryClientError extends Error {
  constructor(
    readonly code: string,
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "RecoveryClientError";
  }
}

export class RecoveryControlApiClient {
  constructor(
    private readonly baseUrl = "",
    private readonly readerTokenProvider: () => string = () => "",
    private readonly operatorTokenProvider: () => string = () => "",
  ) {}

  async capability(signal?: AbortSignal): Promise<RecoveryCapability> {
    let response: Response;
    try {
      response = await fetch(`${this.baseUrl}/api/control/v1/recovery/capability`, {
        method: "GET",
        headers: buildControlHeaders(this.readerTokenProvider()),
        cache: "no-store",
        credentials: "omit",
        ...(signal ? { signal } : {}),
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      throw new RecoveryClientError(
        "CONTROL_RECOVERY_TRANSPORT_ERROR",
        0,
        "Recovery capability endpoint is unavailable.",
      );
    }
    const payload = await response.json().catch(() => null) as unknown;
    if (!response.ok) throw errorFromResponse(response.status, payload);
    try {
      return parseRecoveryCapability(payload);
    } catch {
      throw new RecoveryClientError(
        "CONTROL_RECOVERY_CONTRACT_INVALID",
        response.status,
        "Recovery capability response is incompatible with this Dashboard.",
      );
    }
  }

  async resolveRequest(
    request: RecoveryResolutionRequest,
    signal?: AbortSignal,
  ): Promise<RecoveryResolutionSummary> {
    const validated = validateRecoveryResolutionRequest(request);
    let response: Response;
    try {
      const headers = buildControlHeaders(this.operatorTokenProvider());
      headers.set("Content-Type", "application/json");
      response = await fetch(`${this.baseUrl}/api/control/v1/recovery/requests`, {
        method: "POST",
        headers,
        body: JSON.stringify(validated),
        cache: "no-store",
        credentials: "omit",
        ...(signal ? { signal } : {}),
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      throw new RecoveryClientError(
        "CONTROL_RECOVERY_TRANSPORT_ERROR",
        0,
        "Recovery resolution request is unavailable.",
      );
    }
    const payload = await response.json().catch(() => null) as unknown;
    if (!response.ok) throw errorFromResponse(response.status, payload, "resolution");
    try {
      return parseRecoveryResolutionSummary(payload, validated.operation);
    } catch {
      throw new RecoveryClientError(
        "CONTROL_RECOVERY_CONTRACT_INVALID",
        response.status,
        "Recovery resolution response is incompatible with this Dashboard.",
      );
    }
  }
}

function errorFromResponse(
  status: number,
  payload: unknown,
  operation: "capability" | "resolution" = "capability",
): RecoveryClientError {
  const envelope = asRecord(payload);
  const detail = asRecord(envelope?.error);
  const code = safeCode(detail?.code) ?? "CONTROL_RECOVERY_REQUEST_FAILED";
  return new RecoveryClientError(
    code,
    status,
    operation === "resolution" ? "Recovery resolution request failed." : "Recovery capability request failed.",
  );
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function safeCode(value: unknown): string | null {
  return typeof value === "string" && /^[A-Z0-9_]{1,128}$/.test(value) ? value : null;
}

