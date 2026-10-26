import { afterEach, describe, expect, it, vi } from "vitest";

import { AcceptanceClientError, AcceptanceControlApiClient } from "./client";

const DIGEST_A = "a".repeat(64);
const DIGEST_B = "b".repeat(64);
const DIGEST_C = "c".repeat(64);

const CURRENT = {
  schema_version: 1,
  run: {
    schema_version: 1,
    acceptance_run_id: "acceptance-1",
    status: "RUNNING",
    phase: "SOAK_2H",
    created_at: "2026-08-09T06:00:00Z",
    started_at: "2026-08-09T06:00:01Z",
    updated_at: "2026-08-09T06:30:00Z",
    completed_at: null,
    active_test_time: 1200,
    wall_clock_elapsed: 1800,
    restart_count: 1,
    checkpoint: {
      schema_version: 1,
      phase: "SOAK_2H",
      updated_at: "2026-08-09T06:29:59Z",
      token: "soak-checkpoint-1",
      digest: DIGEST_A,
    },
    resources: [
      {
        schema_version: 1,
        timestamp: "2026-08-09T06:29:58Z",
        cpu_percent: 12.5,
        ram_bytes: 123456789,
        disk_free_bytes: 987654321,
      },
    ],
    warnings: [],
    error: null,
    next_action: "CONTINUE_PHASE",
    evidence: [
      {
        schema_version: 1,
        kind: "checkpoint",
        digest: DIGEST_B,
        reference: "acceptance:evidence:1",
      },
    ],
    identity: {
      schema_version: 1,
      full_rc_sha: "1".repeat(40),
      dataset_id: "dataset-1",
      dataset_digest: DIGEST_A,
      strategy_id: "strategy-1",
      strategy_digest: DIGEST_B,
      config_digest: DIGEST_C,
      mode: "PAPER",
      enable_soak_24h: true,
      enable_soak_72h: false,
    },
    phases: [
      "ENVIRONMENT",
      "DATASET",
      "BACKTEST_A",
      "BACKTEST_B",
      "DETERMINISM",
      "RESEARCH",
      "RUNTIME_SMOKE",
      "RECOVERY",
      "SOAK_2H",
      "SOAK_24H",
      "FINAL_VERIFICATION",
    ],
  },
};

const START_RESULT = {
  schema_version: 1,
  request_id: "dashboard-acceptance-start-1",
  operator: "control-plane",
  mutation: "START",
  acceptance_run_id: "acceptance-1",
  status: "QUEUED",
  phase: "ENVIRONMENT",
  completed_at: "2026-08-09T06:00:00Z",
  replayed: false,
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

describe("AcceptanceControlApiClient", () => {
  it("reads the durable snapshot with reader auth and no-store browser transport", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.method).toBe("GET");
      expect(init?.cache).toBe("no-store");
      expect(init?.credentials).toBe("omit");
      expect(new Headers(init?.headers).get("authorization")).toBe("Bearer reader-token");
      return jsonResponse(CURRENT);
    });
    vi.stubGlobal("fetch", fetchMock);

    const client = new AcceptanceControlApiClient(
      "",
      () => "reader-token",
      () => "operator-token",
    );
    const current = await client.current();

    expect(current.run?.acceptance_run_id).toBe("acceptance-1");
    expect(current.run?.active_test_time).toBe(1200);
    expect(current.run?.wall_clock_elapsed).toBe(1800);
    expect(current.run?.identity.mode).toBe("PAPER");
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("sends only bounded candidate fields on START and never browser full-RC or mode", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.method).toBe("POST");
      expect(init?.cache).toBe("no-store");
      expect(init?.credentials).toBe("omit");
      expect(new Headers(init?.headers).get("authorization")).toBe("Bearer operator-token");
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      expect(body).toEqual({
        schema_version: 1,
        request_id: "dashboard-acceptance-start-1",
        dataset_id: "dataset-1",
        dataset_digest: DIGEST_A,
        strategy_id: "strategy-1",
        strategy_digest: DIGEST_B,
        config_digest: DIGEST_C,
        enable_soak_24h: true,
        enable_soak_72h: false,
      });
      expect(body).not.toHaveProperty("full_rc_sha");
      expect(body).not.toHaveProperty("mode");
      return jsonResponse(START_RESULT);
    });
    vi.stubGlobal("fetch", fetchMock);

    const client = new AcceptanceControlApiClient(
      "",
      () => "reader-token",
      () => "operator-token",
    );
    const result = await client.start({
      schema_version: 1,
      request_id: "dashboard-acceptance-start-1",
      dataset_id: "dataset-1",
      dataset_digest: DIGEST_A,
      strategy_id: "strategy-1",
      strategy_digest: DIGEST_B,
      config_digest: DIGEST_C,
      enable_soak_24h: true,
      enable_soak_72h: false,
    });

    expect(result.mutation).toBe("START");
    expect(result.replayed).toBe(false);
  });

  it("fails closed when the owner projection is not PAPER-only", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({
          ...CURRENT,
          run: {
            ...CURRENT.run,
            identity: { ...CURRENT.run.identity, mode: "LIVE" },
          },
        }),
      ),
    );

    const client = new AcceptanceControlApiClient();
    await expect(client.current()).rejects.toMatchObject({
      code: "CONTROL_ACCEPTANCE_CONTRACT_INVALID",
    });
  });

  it("rejects inconsistent terminal state instead of manufacturing browser truth", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({
          ...CURRENT,
          run: { ...CURRENT.run, status: "SUCCEEDED", completed_at: null },
        }),
      ),
    );

    const client = new AcceptanceControlApiClient();
    await expect(client.current()).rejects.toMatchObject({
      code: "CONTROL_ACCEPTANCE_CONTRACT_INVALID",
    });
  });

  it("preserves safe server error semantics without surfacing technical payloads", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(
          {
            error: {
              code: "RECOVERY_REQUIRED",
              message: "Acceptance owner requires recovery.",
              action: "Retry after owner recovery completes.",
              error_id: "acceptance_safe_1",
              retryable: true,
              technical_details: {
                path: "/Users/private/acceptance.sqlite",
                exception: "secret-owner-stack",
              },
            },
          },
          503,
        ),
      ),
    );

    const client = new AcceptanceControlApiClient();
    let error: unknown;
    try {
      await client.current();
    } catch (caught) {
      error = caught;
    }

    expect(error).toBeInstanceOf(AcceptanceClientError);
    expect(error).toMatchObject({
      code: "RECOVERY_REQUIRED",
      status: 503,
      action: "Retry after owner recovery completes.",
      retryable: true,
      errorId: "acceptance_safe_1",
    });
    expect(String(error)).not.toContain("/Users/private");
    expect(String(error)).not.toContain("secret-owner-stack");
  });

  it("reuses the caller-provided request id exactly for retry-safe STOP transport", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      expect(body).toEqual({
        schema_version: 1,
        request_id: "dashboard-acceptance-stop-1",
        acceptance_run_id: "acceptance-1",
      });
      return jsonResponse({
        ...START_RESULT,
        request_id: "dashboard-acceptance-stop-1",
        mutation: "STOP",
        status: "STOPPING",
        phase: "SOAK_2H",
        replayed: true,
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const client = new AcceptanceControlApiClient("", () => "", () => "operator-token");
    const request = {
      schema_version: 1 as const,
      request_id: "dashboard-acceptance-stop-1",
      acceptance_run_id: "acceptance-1",
    };
    const first = await client.stop(request);
    const second = await client.stop(request);

    expect(first.replayed).toBe(true);
    expect(second.request_id).toBe(request.request_id);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
