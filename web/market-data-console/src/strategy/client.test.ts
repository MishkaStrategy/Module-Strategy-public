import { afterEach, describe, expect, it, vi } from "vitest";

import { StrategyControlApiClient, StrategyControlClientError } from "./client";

const RUN = {
  schema_version: 1,
  run_id: "factory-run-1",
  factory_status: "COMPLETED",
  status: "SUCCEEDED",
  stage: "package",
  stage_sequence: 14,
  updated_at: "2026-08-07T04:15:00+00:00",
  request_fingerprint: "a".repeat(64),
  dataset_fingerprint: "b".repeat(64),
  commit_sha: "c".repeat(40),
  manifest_id: "manifest-1",
  completed_artifact_count: 3,
  submitted_batch_count: 2,
  processed_event_count: 9,
  decision_count: 4,
  decision_chain_head: "d".repeat(64),
  decision_chain_verified: true,
} as const;

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("StrategyControlApiClient", () => {
  it("attaches only the memory-only reader token to list requests", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          schema_version: 1,
          runs: [RUN],
          total_valid: 1,
          invalid_count: 0,
          ignored_count: 0,
          truncated: false,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new StrategyControlApiClient("", () => "reader-token");

    const response = await client.list();

    expect(response.runs[0]?.run_id).toBe("factory-run-1");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer reader-token");
    expect(init.cache).toBe("no-store");
    expect(init.credentials).toBe("omit");
  });

  it("rejects invalid run identities before fetch", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const client = new StrategyControlApiClient();

    await expect(client.get("../secret")).rejects.toMatchObject({
      code: "CONTROL_STRATEGY_CLIENT_INVALID_RUN_ID",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("projects safe typed server errors without exposing raw fields", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          error: {
            code: "CONTROL_STRATEGY_RUN_INTEGRITY_FAILED",
            message: "Strategy state failed integrity validation.",
            action: "Inspect local evidence.",
            error_id: "ctrl_123",
            retryable: false,
            raw_secret: "must-not-be-used",
          },
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new StrategyControlApiClient();

    const error = await client.get("factory-run-1").catch((value: unknown) => value);

    expect(error).toBeInstanceOf(StrategyControlClientError);
    expect(error).toMatchObject({
      code: "CONTROL_STRATEGY_RUN_INTEGRITY_FAILED",
      status: 409,
      action: "Inspect local evidence.",
      errorId: "ctrl_123",
      retryable: false,
    });
    expect(String(error)).not.toContain("must-not-be-used");
  });

  it("fails closed when the server returns an invalid Strategy contract", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            schema_version: 1,
            runs: [{ ...RUN, decision_chain_verified: "yes" }],
            total_valid: 1,
            invalid_count: 0,
            ignored_count: 0,
            truncated: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    const client = new StrategyControlApiClient();

    await expect(client.list()).rejects.toMatchObject({
      code: "CONTROL_STRATEGY_CONTRACT_INVALID",
    });
  });
});
