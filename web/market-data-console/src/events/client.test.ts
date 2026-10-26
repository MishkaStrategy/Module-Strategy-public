import { afterEach, describe, expect, it, vi } from "vitest";

import { OperationalEventsClientError, OperationalEventsControlApiClient } from "./client";

const PAGE = {
  schema_version: 1,
  source: "trading_runner",
  events: [
    {
      schema_version: 1,
      event_id: "trr-000000000007-0123456789abcdef",
      source: "trading_runner",
      resource: "paper-runtime-1",
      timestamp: "2026-08-09T16:00:00Z",
      event_type: "operator.lifecycle",
      severity: "INFO",
      state: "running",
      payload: { operation: "START", result: "SUCCEEDED", replayed: false },
      cursor: "trr-v1:0123456789abcdef:7",
    },
  ],
  next_cursor: "trr-v1:0123456789abcdef:7",
  has_more: false,
  scanned_count: 1,
  omitted_count: 0,
  retention_limit: 1000,
  truncated: false,
};

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("OperationalEventsControlApiClient", () => {
  it("uses reader auth and only accepted owner filters", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      expect(url).toContain("/api/control/v1/events/trading-runner?");
      expect(url).toContain("limit=25");
      expect(url).toContain("cursor=trr-v1%3A0123456789abcdef%3A3");
      expect(url).toContain("event_type=operator.lifecycle");
      expect(url).toContain("severity=INFO");
      expect(url).not.toContain("path=");
      expect(url).not.toContain("command=");
      expect(new Headers(init?.headers).get("authorization")).toBe("Bearer reader-token");
      expect(init?.method).toBe("GET");
      expect(init?.cache).toBe("no-store");
      expect(init?.credentials).toBe("omit");
      return jsonResponse(PAGE);
    });
    vi.stubGlobal("fetch", fetchMock);

    const client = new OperationalEventsControlApiClient("", () => "reader-token");
    const page = await client.events({
      cursor: "trr-v1:0123456789abcdef:3",
      limit: 25,
      eventTypes: ["operator.lifecycle"],
      severities: ["INFO"],
    });

    expect(page.source).toBe("trading_runner");
    expect(page.events[0]?.event_type).toBe("operator.lifecycle");
  });

  it("fails closed on nested, private or unknown owner payload", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({
          ...PAGE,
          events: [
            {
              ...PAGE.events[0],
              payload: { raw_stack: ["private", "stack"] },
            },
          ],
        }),
      ),
    );

    const client = new OperationalEventsControlApiClient();
    await expect(client.events()).rejects.toMatchObject({
      code: "CONTROL_EVENTS_CONTRACT_INVALID",
    });
  });

  it("rejects unsafe cursor and unsupported filters before transport", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const client = new OperationalEventsControlApiClient();

    await expect(client.events({ cursor: "/Users/private/runtime.log" })).rejects.toMatchObject({
      code: "CONTROL_EVENTS_CLIENT_INVALID_CURSOR",
    });
    await expect(client.events({ eventTypes: ["shell.command"] })).rejects.toMatchObject({
      code: "CONTROL_EVENTS_CLIENT_INVALID_FILTER",
    });
    await expect(client.events({ severities: ["CRITICAL" as never] })).rejects.toMatchObject({
      code: "CONTROL_EVENTS_CLIENT_INVALID_FILTER",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("preserves bounded safe server error semantics without technical details", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(
          {
            error: {
              code: "CONTROL_EVENTS_SOURCE_UNAVAILABLE",
              message: "Trading Runner structured operational events are temporarily unavailable.",
              action: "Retry the same bounded query after the owner source recovers.",
              error_id: "ctrl_safe",
              retryable: true,
              technical_details: {
                path: "/Users/private/runtime.log",
                token: "private-token",
              },
            },
          },
          503,
        ),
      ),
    );

    const client = new OperationalEventsControlApiClient();
    let error: unknown;
    try {
      await client.events();
    } catch (caught) {
      error = caught;
    }
    expect(error).toBeInstanceOf(OperationalEventsClientError);
    expect(error).toMatchObject({
      code: "CONTROL_EVENTS_SOURCE_UNAVAILABLE",
      retryable: true,
      errorId: "ctrl_safe",
    });
    expect(String(error)).not.toContain("/Users/private");
    expect(String(error)).not.toContain("private-token");
  });
});
