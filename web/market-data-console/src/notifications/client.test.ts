import { afterEach, describe, expect, it, vi } from "vitest";

import { NotificationsControlApiClient } from "./client";

const NOW = "2026-08-09T10:15:00+00:00";

function response(value: object, status = 200): Response {
  const body = JSON.stringify(value);
  return new Response(body, {
    status,
    headers: {
      "Content-Type": "application/json",
      "Content-Length": String(new TextEncoder().encode(body).byteLength),
    },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("NotificationsControlApiClient", () => {
  it("uses reader auth for safe status and omits ambient credentials", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.method).toBe("GET");
      expect(init?.credentials).toBe("omit");
      expect(init?.cache).toBe("no-store");
      expect(new Headers(init?.headers).get("Authorization")).toBe("Bearer reader-token");
      return response({
        schema_version: 1,
        configured: true,
        reachable: true,
        notifications_enabled: true,
        status: "ok",
        cycles: 4,
        signals_sent: 2,
        last_success_at: NOW,
        last_error_type: null,
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const client = new NotificationsControlApiClient("", () => "reader-token", () => "operator-token");
    const status = await client.status();

    expect(status.configured).toBe(true);
    expect(status.reachable).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("sends only request identity with operator auth and preserves replay", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.method).toBe("POST");
      expect(new Headers(init?.headers).get("Authorization")).toBe("Bearer operator-token");
      expect(JSON.parse(String(init?.body))).toEqual({
        schema_version: 1,
        request_id: "browser-test-1",
      });
      return response({
        schema_version: 1,
        request_id: "browser-test-1",
        operator: "control-plane:local-operator",
        completed_at: NOW,
        replayed: true,
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const client = new NotificationsControlApiClient("", () => "reader-token", () => "operator-token");
    const result = await client.testNotification({ schema_version: 1, request_id: "browser-test-1" });

    expect(result.replayed).toBe(true);
    expect(result.operator).toBe("control-plane:local-operator");
  });

  it("preserves safe rate-limit errors without private details", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        response(
          {
            error: {
              schema_version: 1,
              code: "CONTROL_NOTIFICATIONS_RATE_LIMITED",
              message: "Signal Bot test notification was rate limited.",
              action: "Retry safely later.",
              error_id: "ctrl_test",
              component: "signal_bot",
              operation: "test_notification",
              occurred_at: NOW,
              retryable: true,
              technical_details: { owner_code: "RATE_LIMITED" },
            },
          },
          429,
        ),
      ),
    );

    const client = new NotificationsControlApiClient("", () => "reader-token", () => "operator-token");
    await expect(
      client.testNotification({ schema_version: 1, request_id: "rate-limit-1" }),
    ).rejects.toMatchObject({
      code: "CONTROL_NOTIFICATIONS_RATE_LIMITED",
      status: 429,
      retryable: true,
      errorId: "ctrl_test",
    });
  });

  it("rejects private or malformed owner status fields", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        response({
          schema_version: 1,
          configured: true,
          reachable: true,
          notifications_enabled: true,
          status: "ok",
          cycles: 1,
          signals_sent: 1,
          last_success_at: NOW,
          last_error_type: null,
          bot_token: "PRIVATE-BOT-TOKEN",
        }),
      ),
    );

    const client = new NotificationsControlApiClient();
    await expect(client.status()).rejects.toMatchObject({
      code: "CONTROL_NOTIFICATIONS_CONTRACT_INVALID",
    });
  });
});
