import { afterEach, describe, expect, it, vi } from "vitest";

import { ControlApiClientError, ControlPlaneApiClient, buildControlHeaders } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
});

function healthyOverview() {
  return {
    schema_version: 1,
    mode: "PAPER",
    status: "DEGRADED",
    checked_at: "2026-08-07T04:40:00Z",
    components: [
      {
        schema_version: 1,
        component: "market_data",
        status: "HEALTHY",
        checked_at: "2026-08-07T04:40:00Z",
        summary: "Market Data is healthy",
        details: { api_version: "v1" },
      },
    ],
  };
}

function systemResources() {
  return {
    schema_version: 1,
    collected_at: "2026-08-08T01:00:00Z",
    cpu_percent: 12.5,
    logical_cpu_count: 8,
    memory: {
      schema_version: 1,
      total_bytes: 16_000,
      used_bytes: 6_000,
      available_bytes: 10_000,
      percent: 37.5,
    },
    disk: {
      schema_version: 1,
      total_bytes: 100_000,
      used_bytes: 25_000,
      available_bytes: 75_000,
      percent: 25,
    },
    uptime_seconds: 3_600,
    process_count: 42,
    control_process_rss_bytes: 123_456,
    control_process_threads: 7,
  };
}

describe("buildControlHeaders", () => {
  it("keeps an empty token out of requests", () => {
    const headers = buildControlHeaders("   ");
    expect(headers.get("Authorization")).toBeNull();
    expect(headers.get("Accept")).toBe("application/json");
  });

  it("uses a reader token only in the outgoing header", () => {
    const headers = buildControlHeaders("reader-secret");
    expect(headers.get("Authorization")).toBe("Bearer reader-secret");
  });
});

describe("ControlPlaneApiClient", () => {
  it("reads the local project overview without ambient cookies or cache", async () => {
    const signal = new AbortController().signal;
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(healthyOverview()), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const client = new ControlPlaneApiClient("", () => "reader-secret");

    const overview = await client.getOverview(signal);

    expect(overview.mode).toBe("PAPER");
    expect(overview.status).toBe("DEGRADED");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/control/v1/health");
    expect(init.signal).toBe(signal);
    expect(init.cache).toBe("no-store");
    expect(init.credentials).toBe("omit");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer reader-secret");
  });

  it("reads System Resources from the fixed reader-authenticated path", async () => {
    const signal = new AbortController().signal;
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(systemResources()), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const client = new ControlPlaneApiClient("", () => "reader-secret");

    const resources = await client.getSystemResources(signal);

    expect(resources.cpu_percent).toBe(12.5);
    expect(resources.control_process_threads).toBe(7);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/control/v1/system/resources");
    expect(init.signal).toBe(signal);
    expect(init.cache).toBe("no-store");
    expect(init.credentials).toBe("omit");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer reader-secret");
  });

  it("rejects privacy-expanded System Resources contracts", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ ...systemResources(), hostname: "private-host" }), {
          status: 200,
        }),
      ),
    );
    const client = new ControlPlaneApiClient();

    await expect(client.getSystemResources()).rejects.toMatchObject({
      code: "CONTROL_RESPONSE_INVALID",
      operation: "system_resources",
    });
  });

  it("rejects inconsistent System Resources accounting", async () => {
    const payload = systemResources();
    payload.memory.used_bytes = 99_999;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), { status: 200 })),
    );
    const client = new ControlPlaneApiClient();

    await expect(client.getSystemResources()).rejects.toMatchObject({
      code: "CONTROL_RESPONSE_INVALID",
      operation: "system_resources",
    });
  });

  it("rejects a response that tries to expose LIVE mode", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ ...healthyOverview(), mode: "LIVE" }), { status: 200 }),
      ),
    );
    const client = new ControlPlaneApiClient();

    await expect(client.getOverview()).rejects.toMatchObject({
      name: "ControlApiClientError",
      code: "CONTROL_RESPONSE_INVALID",
    });
  });

  it("rejects mismatched component identity", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(healthyOverview().components[0]), { status: 200 }),
      ),
    );
    const client = new ControlPlaneApiClient();

    await expect(client.getComponentHealth("backtest")).rejects.toMatchObject({
      code: "CONTROL_RESPONSE_INVALID",
    });
  });

  it("maps actionable API errors without exposing technical details", async () => {
    const secret = "must-not-render";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            schema_version: 1,
            error: {
              schema_version: 1,
              code: "CONTROL_PLANE_UNAVAILABLE",
              message: "State is unavailable.",
              action: "Open logs.",
              error_id: "ctrl_123",
              component: null,
              operation: "system_health",
              occurred_at: "2026-08-07T04:40:00Z",
              retryable: true,
              technical_details: { raw_secret: secret },
            },
          }),
          { status: 503 },
        ),
      ),
    );
    const client = new ControlPlaneApiClient();

    let captured: unknown;
    try {
      await client.getOverview();
    } catch (error) {
      captured = error;
    }
    expect(captured).toBeInstanceOf(ControlApiClientError);
    expect(captured).toMatchObject({
      code: "CONTROL_PLANE_UNAVAILABLE",
      action: "Open logs.",
      errorId: "ctrl_123",
      retryable: true,
    });
    expect(String(captured)).not.toContain(secret);
  });
});
