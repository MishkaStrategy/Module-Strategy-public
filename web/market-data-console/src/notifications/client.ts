import {
  parseNotificationStatus,
  parseTestNotificationResult,
} from "./contracts";
import type {
  NotificationStatus,
  TestNotificationRequest,
  TestNotificationResult,
} from "./contracts";

const MAX_RESPONSE_BYTES = 128 * 1024;

interface NotificationClientErrorOptions {
  action?: string | null;
  retryable?: boolean;
  errorId?: string | null;
}

export class NotificationClientError extends Error {
  readonly code: string;
  readonly status: number;
  readonly action: string | null;
  readonly retryable: boolean;
  readonly errorId: string | null;

  constructor(
    code: string,
    message: string,
    status: number,
    options: NotificationClientErrorOptions = {},
  ) {
    super(message);
    this.name = "NotificationClientError";
    this.code = code;
    this.status = status;
    this.action = options.action ?? null;
    this.retryable = options.retryable ?? false;
    this.errorId = options.errorId ?? null;
  }
}

export class NotificationsControlApiClient {
  constructor(
    private readonly baseUrl = "",
    private readonly readerTokenProvider: () => string = () => "",
    private readonly operatorTokenProvider: () => string = () => "",
  ) {}

  async status(signal?: AbortSignal): Promise<NotificationStatus> {
    return this.parsed(
      "/api/control/v1/notifications/status",
      "GET",
      null,
      false,
      parseNotificationStatus,
      signal,
    );
  }

  async testNotification(
    request: TestNotificationRequest,
    signal?: AbortSignal,
  ): Promise<TestNotificationResult> {
    return this.parsed(
      "/api/control/v1/notifications/test",
      "POST",
      request,
      true,
      parseTestNotificationResult,
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
      throw new NotificationClientError(
        "CONTROL_NOTIFICATIONS_CONTRACT_INVALID",
        "Notifications response failed the browser contract.",
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
      throw new NotificationClientError(
        "CONTROL_NOTIFICATIONS_TRANSPORT_ERROR",
        "Notifications endpoint is unavailable.",
        0,
        {
          action: "Check the local Control Plane and Signal Bot owner, then retry safely.",
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
        throw new NotificationClientError(
          "CONTROL_NOTIFICATIONS_INVALID_RESPONSE",
          "Notifications endpoint returned invalid JSON.",
          response.status,
        );
      }
    }
    if (!response.ok) throw serverError(response.status, value);
    return value;
  }
}

function tooLarge(status: number): NotificationClientError {
  return new NotificationClientError(
    "CONTROL_NOTIFICATIONS_RESPONSE_TOO_LARGE",
    "Notifications response exceeded the browser safety limit.",
    status,
  );
}

function serverError(status: number, value: unknown): NotificationClientError {
  const envelope = asRecord(value);
  const detail = asRecord(envelope?.error);
  return new NotificationClientError(
    safeString(detail?.code, 128) ?? "CONTROL_NOTIFICATIONS_REQUEST_FAILED",
    safeString(detail?.message, 512) ?? "Notifications request failed.",
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
