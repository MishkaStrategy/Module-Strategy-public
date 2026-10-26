import { ControlApiClientError, buildControlApiUrl, buildControlHeaders } from "../control/client";
import { isControlApiErrorEnvelope } from "../control/contracts";
import type { ExternalArtifactCapacitySnapshot, SystemCapacitySnapshot } from "./contracts";
import { isExternalArtifactCapacitySnapshot, isSystemCapacitySnapshot } from "./contracts";

export class SystemCapacityApiClient {
  constructor(
    private readonly baseUrl = "",
    private readonly tokenProvider: () => string = () => "",
  ) {}

  async getCapacity(signal?: AbortSignal): Promise<SystemCapacitySnapshot> {
    const payload = await this.read(
      "/api/control/v1/system/capacity",
      "system_capacity",
      signal,
    );
    if (!isSystemCapacitySnapshot(payload)) throw incompatibleResponse("system_capacity");
    return payload;
  }

  async getExternalArtifactCapacity(
    signal?: AbortSignal,
  ): Promise<ExternalArtifactCapacitySnapshot> {
    const payload = await this.read(
      "/api/control/v1/system/capacity/artifacts",
      "external_artifact_capacity",
      signal,
    );
    if (!isExternalArtifactCapacitySnapshot(payload)) {
      throw incompatibleResponse("external_artifact_capacity");
    }
    return payload;
  }

  private async read(path: string, operation: string, signal?: AbortSignal): Promise<unknown> {
    const request: RequestInit = {
      method: "GET",
      headers: buildControlHeaders(this.tokenProvider()),
      cache: "no-store",
      credentials: "omit",
    };
    if (signal) request.signal = signal;
    const response = await fetch(buildControlApiUrl(this.baseUrl, path), request);
    if (!response.ok) throw await capacityApiError(response);
    try {
      return (await response.json()) as unknown;
    } catch {
      throw incompatibleResponse(operation);
    }
  }
}

async function capacityApiError(response: Response): Promise<ControlApiClientError> {
  let payload: unknown;
  try {
    payload = (await response.json()) as unknown;
  } catch {
    payload = undefined;
  }
  if (isControlApiErrorEnvelope(payload)) {
    return new ControlApiClientError(payload.error.message, response.status, payload.error.code, {
      action: payload.error.action,
      errorId: payload.error.error_id,
      operation: payload.error.operation,
      retryable: payload.error.retryable,
    });
  }
  return new ControlApiClientError(
    `Control Plane request failed: HTTP ${response.status}.`,
    response.status,
    "CONTROL_REQUEST_FAILED",
    { action: "Проверьте состояние локального Control Plane." },
  );
}

function incompatibleResponse(operation: string): ControlApiClientError {
  return new ControlApiClientError(
    "Control Plane вернул несовместимый System Capacity контракт.",
    502,
    "CONTROL_RESPONSE_INVALID",
    {
      action: "Проверьте, что Dashboard и Control Plane используют совместимые версии.",
      operation,
    },
  );
}
