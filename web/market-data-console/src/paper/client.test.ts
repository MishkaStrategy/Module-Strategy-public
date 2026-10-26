import { afterEach, describe, expect, it, vi } from "vitest";

import { PaperRuntimeClientError, PaperRuntimeControlApiClient } from "./client";
import { parsePaperPositionsSnapshot } from "./contracts";

const RUNTIME = {
  schema_version: 1,
  runtime_id: "paper-runtime-1",
  portfolio_id: "portfolio-1",
  strategy_id: "strategy-1",
  execution_mode: "paper",
  native_state: "running",
  base_timeframe: "5m",
  market: "usdt_perpetual",
  instruments: ["BTCUSDT"],
  configuration_hash: "a".repeat(64),
  kill_switch: {
    schema_version: 1,
    active: false,
    scope: "global",
    activated_at: null,
    cancellation_pending: false,
  },
  health_ready: true,
  queue_depth: 0,
  queue_capacity: 1024,
  queue_high_water_mark: 3,
  paper_execution_lifecycle: "ready",
  order_count: 1,
  active_order_count: 1,
  blocked_scope_count: 0,
  position_projection_available: false,
  lifecycle_mutations_available: false,
};

const LIFECYCLE_RESULT = {
  schema_version: 1,
  request_id: "paper-start-1",
  operator: "control-plane:local-operator",
  mutation: "START",
  previous_state: "configured",
  new_state: "running",
  config_hash: "b".repeat(64),
  completed_at: "2026-08-09T14:45:00Z",
  replayed: false,
};

const POSITIONS = {
  schema_version: 1,
  status: "available",
  owner_id: "paper-execution",
  composition_id: "composition-1",
  portfolio_id: "portfolio-1",
  owner_generation: "generation-2",
  membership_version: "membership-8",
  snapshot_version: "snapshot-17",
  state_version: "state-23",
  as_of: "2026-10-23T05:00:00Z",
  coverage: "complete",
  positions: [
    {
      schema_version: 1,
      owner_position_id: "position-long",
      instrument_id: "venue:ABC",
      instrument_version: "instrument-v1",
      side: "long",
      quantity: "1.25",
      quantity_unit: "contracts",
      strategy_id: null,
    },
    {
      schema_version: 1,
      owner_position_id: "position-short",
      instrument_id: "venue:ABC",
      instrument_version: "instrument-v1",
      side: "short",
      quantity: "2",
      quantity_unit: "contracts",
      strategy_id: "strategy-2",
    },
  ],
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

describe("PaperRuntimeControlApiClient", () => {
  it("uses only memory reader auth and parses the runtime contract", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(new Headers(init?.headers).get("authorization")).toBe("Bearer reader-token");
      expect(init?.method).toBe("GET");
      expect(init?.cache).toBe("no-store");
      expect(init?.credentials).toBe("omit");
      return jsonResponse(RUNTIME);
    });
    vi.stubGlobal("fetch", fetchMock);

    const client = new PaperRuntimeControlApiClient("", () => "reader-token");
    const runtime = await client.runtime();

    expect(runtime.native_state).toBe("running");
    expect(runtime.position_projection_available).toBe(false);
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("reads lifecycle capability through reader scope without inventing runtime truth", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe("/api/control/v1/paper/lifecycle-capability");
      expect(new Headers(init?.headers).get("authorization")).toBe("Bearer reader-token");
      expect(init?.method).toBe("GET");
      return jsonResponse({ schema_version: 1, available: true, paper_only: true });
    });
    vi.stubGlobal("fetch", fetchMock);

    const client = new PaperRuntimeControlApiClient("", () => "reader-token", () => "operator-token");
    const capability = await client.lifecycleCapability();

    expect(capability).toEqual({ schema_version: 1, available: true, paper_only: true });
  });

  it("sends only request id with operator scope for lifecycle mutation", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe("/api/control/v1/paper/start");
      expect(new Headers(init?.headers).get("authorization")).toBe("Bearer operator-token");
      expect(new Headers(init?.headers).get("content-type")).toBe("application/json");
      expect(init?.method).toBe("POST");
      expect(init?.cache).toBe("no-store");
      expect(init?.credentials).toBe("omit");
      expect(JSON.parse(String(init?.body))).toEqual({
        schema_version: 1,
        request_id: "paper-start-1",
      });
      return jsonResponse(LIFECYCLE_RESULT);
    });
    vi.stubGlobal("fetch", fetchMock);

    const client = new PaperRuntimeControlApiClient("", () => "reader-token", () => "operator-token");
    const receipt = await client.lifecycleMutate("START", "paper-start-1");

    expect(receipt.mutation).toBe("START");
    expect(receipt.previous_state).toBe("configured");
    expect(receipt.new_state).toBe("running");
    expect(receipt.replayed).toBe(false);
  });

  it("rejects lifecycle request identity mismatch and malformed request ids", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ ...LIFECYCLE_RESULT, request_id: "different-request" }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new PaperRuntimeControlApiClient("", () => "reader", () => "operator");

    await expect(client.lifecycleMutate("START", "paper-start-1")).rejects.toMatchObject({
      code: "CONTROL_PAPER_RUNTIME_CONTRACT_INVALID",
    });
    await expect(client.lifecycleMutate("STOP", "bad request with spaces")).rejects.toMatchObject({
      code: "CONTROL_PAPER_RUNTIME_CLIENT_INVALID_REQUEST_ID",
    });
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("fails closed on an impossible kill-switch shape", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({
          ...RUNTIME,
          kill_switch: {
            ...RUNTIME.kill_switch,
            active: true,
            activated_at: null,
          },
        }),
      ),
    );

    const client = new PaperRuntimeControlApiClient();
    await expect(client.runtime()).rejects.toMatchObject({
      code: "CONTROL_PAPER_RUNTIME_CONTRACT_INVALID",
    });
  });

  it("preserves safe error semantics without surfacing private technical fields", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(
          {
            error: {
              code: "CONTROL_PAPER_RUNTIME_UNAVAILABLE",
              message: "Paper Runtime owner state could not be projected safely.",
              action: "Inspect the owning paper runtime locally and retry the read.",
              error_id: "ctrl_safe",
              retryable: true,
              technical_details: {
                path: "/Users/private/runtime.json",
                reason: "private-kill-switch-reason",
              },
            },
          },
          503,
        ),
      ),
    );

    const client = new PaperRuntimeControlApiClient();
    let error: unknown;
    try {
      await client.runtime();
    } catch (caught) {
      error = caught;
    }
    expect(error).toBeInstanceOf(PaperRuntimeClientError);
    expect(error).toMatchObject({
      code: "CONTROL_PAPER_RUNTIME_UNAVAILABLE",
      status: 503,
      action: "Inspect the owning paper runtime locally and retry the read.",
      retryable: true,
      errorId: "ctrl_safe",
    });
    expect(String(error)).not.toContain("/Users/private");
    expect(String(error)).not.toContain("private-kill-switch-reason");
  });

  it("rejects invalid order pagination before transport", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const client = new PaperRuntimeControlApiClient();

    await expect(client.orders(0, 0)).rejects.toMatchObject({
      code: "CONTROL_PAPER_RUNTIME_CLIENT_INVALID_LIMIT",
    });
    await expect(client.orders(50, -1)).rejects.toMatchObject({
      code: "CONTROL_PAPER_RUNTIME_CLIENT_INVALID_OFFSET",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("reads bounded owner positions with reader auth and preserves exact decimal facts", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe("/api/control/v1/paper/positions");
      expect(new Headers(init?.headers).get("authorization")).toBe("Bearer reader-token");
      expect(init?.method).toBe("GET");
      expect(init?.cache).toBe("no-store");
      expect(init?.credentials).toBe("omit");
      return jsonResponse(POSITIONS);
    });
    vi.stubGlobal("fetch", fetchMock);

    const client = new PaperRuntimeControlApiClient("", () => "reader-token");
    const result = await client.positions();

    expect(result.status).toBe("available");
    if (result.status !== "available") throw new Error("expected owner snapshot");
    expect(result.positions.map((row) => row.owner_position_id)).toEqual([
      "position-long",
      "position-short",
    ]);
    expect(result.positions.map((row) => row.quantity)).toEqual(["1.25", "2"]);
    expect(result.positions[0]?.strategy_id).toBeNull();
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it.each(["unavailable", "recovering", "stale"] as const)(
    "preserves the owner %s positions status",
    async (status) => {
      vi.stubGlobal(
        "fetch",
        vi.fn(async () => jsonResponse({ schema_version: 1, status, reason_code: "owner_not_ready" })),
      );
      const result = await new PaperRuntimeControlApiClient().positions();
      expect(result).toEqual({ schema_version: 1, status, reason_code: "owner_not_ready" });
    },
  );

  it.each([
    { ...POSITIONS, coverage: "partial" },
    { ...POSITIONS, positions: [POSITIONS.positions[0], POSITIONS.positions[0]] },
    { ...POSITIONS, positions: [{ ...POSITIONS.positions[0], quantity: "0" }] },
  ])("rejects incomplete or duplicated position data", (invalid) => {
    expect(() => parsePaperPositionsSnapshot(invalid)).toThrow();
  });
});
