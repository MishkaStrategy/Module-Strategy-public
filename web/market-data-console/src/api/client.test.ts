import { afterEach, describe, expect, it, vi } from "vitest";

import {
  MarketDataApiClient,
  buildApiUrl,
  buildHeaders,
  formatBytes,
} from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("buildApiUrl", () => {
  it("keeps same-origin paths when no API base is configured", () => {
    expect(buildApiUrl("", "/api/v1/version")).toBe("/api/v1/version");
  });

  it("normalizes an explicit loopback base", () => {
    expect(buildApiUrl("http://127.0.0.1:8000/", "api/v1/health")).toBe(
      "http://127.0.0.1:8000/api/v1/health",
    );
  });
});

describe("buildHeaders", () => {
  it("does not emit authorization for an empty token", () => {
    const headers = buildHeaders("  ");
    expect(headers.get("Authorization")).toBeNull();
    expect(headers.get("Accept")).toBe("application/json");
  });

  it("emits a bearer token and explicit SSE accept header", () => {
    const headers = buildHeaders("secret-token", "text/event-stream");
    expect(headers.get("Authorization")).toBe("Bearer secret-token");
    expect(headers.get("Accept")).toBe("text/event-stream");
  });
});

describe("formatBytes", () => {
  it("formats binary units in the Russian locale", () => {
    expect(formatBytes(1536)).toBe("1,5 КиБ");
  });

  it("rejects invalid values", () => {
    expect(formatBytes(-1)).toBe("—");
  });
});

describe("MarketDataApiClient", () => {
  it("encodes identifiers, forwards abort signals and omits ambient browser state", async () => {
    const signal = new AbortController().signal;
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          schema_version: 1,
          artifact_id: "mdi_artifact_0123456789abcdef01234567",
          dataset_id: "BTC/USDT",
          version: 1,
          dataset_hash: "a".repeat(64),
          filename: "mdi_artifact_0123456789abcdef01234567.zip",
          content_type: "application/zip",
          checksum_sha256: "b".repeat(64),
          byte_size: 10,
          file_count: 2,
          created_at: "2026-07-29T00:00:00Z",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new MarketDataApiClient("", () => "reader-token");
    await client.getArtifact("mdi artifact", signal);

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/artifacts/mdi%20artifact");
    expect(init.signal).toBe(signal);
    expect(init.cache).toBe("no-store");
    expect(init.credentials).toBe("omit");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer reader-token");
  });

  it("serializes job pagination and filters with backend field names", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          schema_version: 1,
          items: [],
          pagination: { schema_version: 1, limit: 25, offset: 50, total: 0 },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new MarketDataApiClient();
    await client.listJobs(25, 50, "FAILED", "export");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/jobs?limit=25&offset=50&states=FAILED&operations=export");
    expect(init.cache).toBe("no-store");
    expect(init.credentials).toBe("omit");
  });

  it("encodes the job identity and event cursor", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new MarketDataApiClient();
    await client.getJobEvents("job/id", 17, 100);

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/jobs/job%2Fid/events?after_event_id=17&limit=100");
    expect(init.cache).toBe("no-store");
    expect(init.credentials).toBe("omit");
  });

  it("sends bearer and Last-Event-ID headers without ambient cookies and stops on abort", async () => {
    const controller = new AbortController();
    const event = JSON.stringify({
      schema_version: 1,
      event_id: 5,
      job_id: "job/id",
      event_type: "state",
      state: "COMPLETED",
      timestamp: "2026-07-29T00:00:00Z",
      payload: {},
    });
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(`id: 5\nevent: state\ndata: ${event}\n\n`, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const statuses: string[] = [];
    const client = new MarketDataApiClient("", () => "reader-token");

    const cursor = await client.watchJobEvents(
      "job/id",
      4,
      {
        onEvent: () => controller.abort(),
        onStatus: (status) => statuses.push(status),
        onError: (error) => {
          throw error;
        },
      },
      controller.signal,
    );

    expect(cursor).toBe(5);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(url).toBe("/api/v1/jobs/job%2Fid/events/stream?after_event_id=4");
    expect(init.cache).toBe("no-store");
    expect(init.credentials).toBe("omit");
    expect(headers.get("Authorization")).toBe("Bearer reader-token");
    expect(headers.get("Last-Event-ID")).toBe("4");
    expect(headers.get("Accept")).toBe("text/event-stream");
    expect(statuses).toEqual(["connecting", "connected", "stopped"]);
  });

  it("maps structured API errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            schema_version: 1,
            error: {
              schema_version: 1,
              code: "ARTIFACT_NOT_FOUND",
              message: "Artifact was not found",
              details: {},
              correlation_id: "corr_test",
            },
          }),
          { status: 404, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    const client = new MarketDataApiClient();
    await expect(client.getArtifact("missing")).rejects.toMatchObject({
      name: "ApiClientError",
      message: "Artifact was not found",
      code: "ARTIFACT_NOT_FOUND",
      status: 404,
      correlationId: "corr_test",
    });
  });
});
