import type {
  ComponentHealth,
  ControlApiVersion,
  ControlComponentId,
  SystemOverview,
  SystemResourcesSnapshot,
} from "./contracts";
import {
  isComponentHealth,
  isControlApiErrorEnvelope,
  isControlApiVersion,
  isSystemOverview,
  isSystemResourcesSnapshot,
} from "./contracts";
import type { UnifiedObservabilitySnapshot } from "./observability";
import { isUnifiedObservabilitySnapshot } from "./observability";

export class ControlApiClientError extends Error {
  readonly status: number;
  readonly code: string;
  readonly action: string | null;
  readonly errorId: string | undefined;
  readonly operation: string | undefined;
  readonly retryable: boolean;

  constructor(
    message: string,
    status: number,
    code: string,
    options: {
      action?: string | null;
      errorId?: string;
      operation?: string;
      retryable?: boolean;
    } = {},
  ) {
    super(message);
    this.name = "ControlApiClientError";
    this.status = status;
    this.code = code;
    this.action = options.action ?? null;
    this.errorId = options.errorId;
    this.operation = options.operation;
    this.retryable = options.retryable ?? false;
  }
}

export class ControlPlaneApiClient {
  constructor(
    private readonly baseUrl = "",
    private readonly tokenProvider: () => string = () => "",
  ) {}

  async getVersion(signal?: AbortSignal): Promise<ControlApiVersion> {
    const payload = await this.request("/api/control/v1/version", signal);
    if (!isControlApiVersion(payload)) throw incompatibleResponse("version");
    return payload;
  }

  async getOverview(signal?: AbortSignal): Promise<SystemOverview> {
    const payload = await this.request("/api/control/v1/health", signal);
    if (!isSystemOverview(payload)) throw incompatibleResponse("system_health");
    return payload;
  }

  async getObservability(signal?: AbortSignal): Promise<UnifiedObservabilitySnapshot> {
    const payload = await this.request("/api/control/v1/observability/overview", signal);
    if (!isUnifiedObservabilitySnapshot(payload)) throw incompatibleResponse("observability_overview");
    return payload;
  }

  async getSystemResources(signal?: AbortSignal): Promise<SystemResourcesSnapshot> {
    const payload = await this.request("/api/control/v1/system/resources", signal);
    if (!isSystemResourcesSnapshot(payload)) throw incompatibleResponse("system_resources");
    return payload;
  }

  async getComponentHealth(
    component: ControlComponentId,
    signal?: AbortSignal,
  ): Promise<ComponentHealth> {
    const payload = await this.request(
      `/api/control/v1/components/${encodeURIComponent(component)}/health`,
      signal,
    );
    if (!isComponentHealth(payload) || payload.component !== component) {
      throw incompatibleResponse("component_health");
    }
    return payload;
  }

  private async request(path: string, signal?: AbortSignal): Promise<unknown> {
    const request: RequestInit = {
      method: "GET",
      headers: buildControlHeaders(this.tokenProvider()),
      cache: "no-store",
      credentials: "omit",
    };
    if (signal) request.signal = signal;
    const response = await fetch(buildControlApiUrl(this.baseUrl, path), request);
    if (!response.ok) throw await controlApiError(response);
    try {
      return (await response.json()) as unknown;
    } catch {
      throw new ControlApiClientError(
        "Control Plane вернул некорректный JSON.",
        response.status,
        "CONTROL_RESPONSE_INVALID",
        { action: "Проверьте версию Control Plane и его логи." },
      );
    }
  }
}

export function buildControlApiUrl(baseUrl: string, path: string): string {
  const normalizedBase = baseUrl.trim().replace(/\/$/, "");
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${normalizedBase}${normalizedPath}`;
}

export function buildControlHeaders(token: string): Headers {
  const headers = new Headers({ Accept: "application/json" });
  const normalized = token.trim();
  if (normalized) headers.set("Authorization", `Bearer ${normalized}`);
  return headers;
}

async function controlApiError(response: Response): Promise<ControlApiClientError> {
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
    "Control Plane вернул несовместимый контракт.",
    502,
    "CONTROL_RESPONSE_INVALID",
    {
      action: "Проверьте, что Dashboard и Control Plane используют совместимые версии.",
      operation,
    },
  );
}
