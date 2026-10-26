import type {
  ApiErrorEnvelope,
  ArtifactDescriptor,
  DatasetDetails,
  DatasetListResponse,
  HealthResponse,
  JobDetails,
  JobEvent,
  JobListResponse,
  JobOperation,
  RunState,
  VersionResponse,
} from "./contracts";
import { abortableDelay, consumeJobEventStream, isAbortError } from "./jobEvents";

export class ApiClientError extends Error {
  readonly status: number;
  readonly code: string;
  readonly correlationId: string | undefined;

  constructor(message: string, status: number, code: string, correlationId?: string) {
    super(message);
    this.name = "ApiClientError";
    this.status = status;
    this.code = code;
    this.correlationId = correlationId;
  }
}

export type JobStreamStatus = "connecting" | "connected" | "reconnecting" | "stopped";

export interface JobStreamCallbacks {
  onEvent: (event: JobEvent) => void;
  onStatus: (status: JobStreamStatus) => void;
  onError: (error: unknown) => void;
}

export function buildApiUrl(baseUrl: string, path: string): string {
  const normalizedBase = baseUrl.trim().replace(/\/$/, "");
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${normalizedBase}${normalizedPath}`;
}

export function buildHeaders(token: string, accept = "application/json"): Headers {
  const headers = new Headers({ Accept: accept });
  const normalized = token.trim();
  if (normalized) {
    headers.set("Authorization", `Bearer ${normalized}`);
  }
  return headers;
}

export function buildRequestInit(
  token: string,
  signal?: AbortSignal,
  accept = "application/json",
): RequestInit {
  const init: RequestInit = {
    method: "GET",
    headers: buildHeaders(token, accept),
    cache: "no-store",
    credentials: "omit",
  };
  if (signal) init.signal = signal;
  return init;
}

async function apiError(response: Response, fallback?: string): Promise<ApiClientError> {
  let payload: ApiErrorEnvelope | undefined;
  try {
    payload = (await response.json()) as ApiErrorEnvelope;
  } catch {
    payload = undefined;
  }
  return new ApiClientError(
    payload?.error.message ?? fallback ?? `HTTP ${response.status}`,
    response.status,
    payload?.error.code ?? "HTTP_ERROR",
    payload?.error.correlation_id,
  );
}

export function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "—";
  if (value < 1024) return `${value} Б`;
  const units = ["КиБ", "МиБ", "ГиБ", "ТиБ"];
  let current = value / 1024;
  let index = 0;
  while (current >= 1024 && index < units.length - 1) {
    current /= 1024;
    index += 1;
  }
  return `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(current)} ${units[index]}`;
}

export class MarketDataApiClient {
  constructor(
    private readonly baseUrl = "",
    private readonly tokenProvider: () => string = () => "",
  ) {}

  private async request<T>(path: string, signal?: AbortSignal): Promise<T> {
    const response = await fetch(
      buildApiUrl(this.baseUrl, path),
      buildRequestInit(this.tokenProvider(), signal),
    );
    if (!response.ok) throw await apiError(response);
    return (await response.json()) as T;
  }

  getVersion(signal?: AbortSignal): Promise<VersionResponse> {
    return this.request<VersionResponse>("/api/v1/version", signal);
  }

  getHealth(signal?: AbortSignal): Promise<HealthResponse> {
    return this.request<HealthResponse>("/api/v1/health", signal);
  }

  listDatasets(limit = 50, offset = 0, signal?: AbortSignal): Promise<DatasetListResponse> {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    return this.request<DatasetListResponse>(`/api/v1/datasets?${params}`, signal);
  }

  getDataset(datasetId: string, version?: number, signal?: AbortSignal): Promise<DatasetDetails> {
    const suffix = version === undefined ? "" : `?version=${encodeURIComponent(String(version))}`;
    return this.request<DatasetDetails>(
      `/api/v1/datasets/${encodeURIComponent(datasetId)}${suffix}`,
      signal,
    );
  }

  getArtifact(artifactId: string, signal?: AbortSignal): Promise<ArtifactDescriptor> {
    return this.request<ArtifactDescriptor>(
      `/api/v1/artifacts/${encodeURIComponent(artifactId)}`,
      signal,
    );
  }

  listJobs(
    limit = 50,
    offset = 0,
    state?: RunState,
    operation?: JobOperation,
    signal?: AbortSignal,
  ): Promise<JobListResponse> {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (state) params.append("states", state);
    if (operation) params.append("operations", operation);
    return this.request<JobListResponse>(`/api/v1/jobs?${params}`, signal);
  }

  getJob(jobId: string, signal?: AbortSignal): Promise<JobDetails> {
    return this.request<JobDetails>(`/api/v1/jobs/${encodeURIComponent(jobId)}`, signal);
  }

  getJobEvents(
    jobId: string,
    afterEventId = 0,
    limit = 500,
    signal?: AbortSignal,
  ): Promise<JobEvent[]> {
    const params = new URLSearchParams({
      after_event_id: String(afterEventId),
      limit: String(limit),
    });
    return this.request<JobEvent[]>(
      `/api/v1/jobs/${encodeURIComponent(jobId)}/events?${params}`,
      signal,
    );
  }

  async watchJobEvents(
    jobId: string,
    afterEventId: number,
    callbacks: JobStreamCallbacks,
    signal: AbortSignal,
  ): Promise<number> {
    let cursor = Math.max(0, afterEventId);
    let failures = 0;
    callbacks.onStatus("connecting");

    while (!signal.aborted) {
      const headers = buildHeaders(this.tokenProvider(), "text/event-stream");
      if (cursor > 0) headers.set("Last-Event-ID", String(cursor));
      try {
        const params = new URLSearchParams({ after_event_id: String(cursor) });
        const response = await fetch(
          buildApiUrl(
            this.baseUrl,
            `/api/v1/jobs/${encodeURIComponent(jobId)}/events/stream?${params}`,
          ),
          { method: "GET", headers, cache: "no-store", credentials: "omit", signal },
        );
        if (!response.ok) throw await apiError(response, `SSE HTTP ${response.status}`);
        callbacks.onStatus("connected");
        await consumeJobEventStream(
          response,
          (event) => {
            if (event.event_id <= cursor) return;
            cursor = event.event_id;
            callbacks.onEvent(event);
          },
          signal,
        );
        failures = 0;
      } catch (error) {
        if (signal.aborted || isAbortError(error)) break;
        failures += 1;
        callbacks.onError(error);
      }

      if (signal.aborted) break;
      callbacks.onStatus("reconnecting");
      const delay = failures === 0 ? 1_500 : Math.min(1_000 * 2 ** (failures - 1), 10_000);
      try {
        await abortableDelay(delay, signal);
      } catch (error) {
        if (!isAbortError(error)) callbacks.onError(error);
        break;
      }
    }

    callbacks.onStatus("stopped");
    return cursor;
  }

  async downloadArtifact(artifact: ArtifactDescriptor, signal?: AbortSignal): Promise<void> {
    const response = await fetch(
      buildApiUrl(
        this.baseUrl,
        `/api/v1/artifacts/${encodeURIComponent(artifact.artifact_id)}/content`,
      ),
      buildRequestInit(this.tokenProvider(), signal, artifact.content_type),
    );
    if (!response.ok) {
      throw await apiError(
        response,
        `Не удалось скачать артефакт: HTTP ${response.status}`,
      );
    }
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    try {
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = artifact.filename;
      anchor.rel = "noopener";
      anchor.click();
    } finally {
      URL.revokeObjectURL(objectUrl);
    }
  }
}