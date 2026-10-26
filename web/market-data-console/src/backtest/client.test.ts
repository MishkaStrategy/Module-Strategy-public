import { afterEach, describe, expect, it, vi } from "vitest";

import { BacktestControlApiClient, BacktestControlClientError } from "./client";

function record(jobId = "bt_0123456789abcdefabcd", configId = "baseline") {
  return {
    schema_version: 1,
    job: {
      schema_version: 1,
      job_id: jobId,
      component: "backtest",
      operation: "run",
      status: "SUCCEEDED",
      phase: "COMPLETED",
      progress: null,
      created_at: "2026-08-07T06:00:00+00:00",
      updated_at: "2026-08-07T06:01:00+00:00",
      started_at: "2026-08-07T06:00:01+00:00",
      completed_at: "2026-08-07T06:01:00+00:00",
      error: null,
    },
    config_id: configId,
    result: {
      schema_version: 1,
      run_id: `run-${configId}`,
      bars_processed: 42,
      restart_verified: false,
    },
  };
}

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("BacktestControlApiClient", () => {
  it("uses only reader bearer authentication for durable job reads", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse([record()]));
    vi.stubGlobal("fetch", fetchMock);
    const client = new BacktestControlApiClient("", () => "reader-secret", () => "operator-secret");

    const jobs = await client.list();

    expect(jobs).toHaveLength(1);
    expect(jobs[0]?.job.status).toBe("SUCCEEDED");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.method).toBe("GET");
    expect(init.cache).toBe("no-store");
    expect(init.credentials).toBe("omit");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer reader-secret");
  });

  it("uses operator bearer authentication and idempotency for create only", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ schema_version: 1, record: record(), created: true }, 202),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new BacktestControlApiClient("", () => "reader-secret", () => "operator-secret");

    const response = await client.create("baseline", "stable-key");

    expect(response.created).toBe(true);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/control/v1/backtests");
    expect(init.method).toBe("POST");
    expect(init.cache).toBe("no-store");
    expect(init.credentials).toBe("omit");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer operator-secret");
    expect((init.headers as Record<string, string>)["Idempotency-Key"]).toBe("stable-key");
    expect(String(init.body)).toContain('"config_id":"baseline"');
  });

  it("rejects path-like config identities before transport", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const client = new BacktestControlApiClient();

    await expect(client.create("../unsafe.yaml", "key")).rejects.toMatchObject({
      code: "BACKTEST_CONFIG_ID_INVALID",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("surfaces only typed safe server errors", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(
        {
          error: {
            code: "CONTROL_BACKTEST_NOT_CONFIGURED",
            message: "Backtest Control is not configured on this local Control Plane.",
            action: "Configure managed Backtest Control.",
            error_id: "ctrl_0123456789abcdefabcd",
            retryable: false,
            technical_details: { secret: "must not be used by browser error" },
          },
        },
        503,
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new BacktestControlApiClient();

    let caught: unknown;
    try {
      await client.list();
    } catch (error) {
      caught = error;
    }
    expect(caught).toBeInstanceOf(BacktestControlClientError);
    expect(caught).toMatchObject({
      code: "CONTROL_BACKTEST_NOT_CONFIGURED",
      action: "Configure managed Backtest Control.",
      errorId: "ctrl_0123456789abcdefabcd",
      retryable: false,
      status: 503,
    });
    expect(String(caught)).not.toContain("must not be used by browser error");
  });

  it("rejects malformed successful contracts", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse([{ unsafe: true }])));
    const client = new BacktestControlApiClient();

    await expect(client.list()).rejects.toMatchObject({
      code: "CONTROL_BACKTEST_CONTRACT_MISMATCH",
    });
  });

  it("compares only distinct durable job identities with reader auth", async () => {
    const left = "bt_0123456789abcdefabcd";
    const right = "bt_abcdef0123456789abcd";
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        schema_version: 1,
        left_job_id: left,
        right_job_id: right,
        left_run_id: "run-left",
        right_run_id: "run-right",
        changes: {
          net_return: { schema_version: 1, left: 0.1, right: 0.2, delta: 0.1 },
        },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new BacktestControlApiClient("", () => "reader-secret", () => "operator-secret");

    const comparison = await client.compare(left, right);

    expect(comparison.changes.net_return?.delta).toBe(0.1);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.cache).toBe("no-store");
    expect(init.credentials).toBe("omit");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer reader-secret");
  });
});
