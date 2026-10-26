import { afterEach, describe, expect, it, vi } from "vitest";

import { ResearchControlApiClient, ResearchControlClientError } from "./client";

const EXPERIMENT = {
  schema_version: 1,
  experiment_id: "experiment-1",
  specification_sha256: "a".repeat(64),
  native_status: "COMPLETED_WITH_FAILURES",
  status: "SUCCEEDED",
  has_failures: true,
  created_at: "2026-08-07T10:00:00Z",
  updated_at: "2026-08-07T10:05:00Z",
  run_counts: { SUCCEEDED: 1, FAILED: 1 },
};
const RUN = {
  schema_version: 1,
  run_id: "run-1",
  experiment_id: "experiment-1",
  native_status: "SUCCEEDED",
  status: "SUCCEEDED",
  stage_id: "stage-1",
  split_id: "split-1",
  dataset_id: "dataset-1",
  scenario_id: "scenario-1",
  stage_role: "train",
  repetition_index: 0,
  attempt_count: 1,
  max_attempts: 2,
  created_at: "2026-08-07T10:00:00Z",
  updated_at: "2026-08-07T10:05:00Z",
  available_at: "2026-08-07T10:00:00Z",
  revision: 3,
  lease_expires_at: null,
  failure_category: null,
  failure_retryable: null,
  engine_run_id: "engine-run-1",
  bars_processed: 120,
  restart_verified: true,
};

afterEach(() => vi.unstubAllGlobals());

describe("ResearchControlApiClient", () => {
  it("uses only the memory-only reader token and validates experiment lists", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          schema_version: 1,
          experiments: [EXPERIMENT],
          offset: 0,
          limit: 50,
          has_more: false,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new ResearchControlApiClient("", () => "reader-secret");

    const result = await client.listExperiments();

    expect(result.experiments[0]?.has_failures).toBe(true);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer reader-secret");
    expect(init.method).toBe("GET");
    expect(init.cache).toBe("no-store");
    expect(init.credentials).toBe("omit");
  });

  it("reads a run without exposing any mutation request", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(RUN), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new ResearchControlApiClient();

    const result = await client.getRun("run-1");

    expect(result.engine_run_id).toBe("engine-run-1");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/control/v1/research/runs/run-1",
      expect.objectContaining({ method: "GET", cache: "no-store", credentials: "omit" }),
    );
  });

  it("fails closed when native and common experiment statuses disagree", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          schema_version: 1,
          experiments: [{ ...EXPERIMENT, status: "FAILED" }],
          offset: 0,
          limit: 50,
          has_more: false,
        }),
        { status: 200 },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new ResearchControlApiClient();

    await expect(client.listExperiments()).rejects.toMatchObject({
      code: "CONTROL_RESEARCH_CONTRACT_INVALID",
    });
  });

  it("parses typed Control Plane errors without leaking arbitrary response fields", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            error: {
              code: "CONTROL_RESEARCH_STATE_INVALID",
              message: "Research durable state failed read-only validation.",
              action: "Inspect Research Runner diagnostics locally.",
              retryable: false,
              error_id: "ctrl_123",
              technical_details: { secret: "must-not-surface" },
            },
          }),
          { status: 409 },
        ),
      ),
    );
    const client = new ResearchControlApiClient();

    try {
      await client.getExperiment("experiment-1");
      throw new Error("expected ResearchControlClientError");
    } catch (error) {
      expect(error).toBeInstanceOf(ResearchControlClientError);
      expect(error).toMatchObject({
        code: "CONTROL_RESEARCH_STATE_INVALID",
        status: 409,
        retryable: false,
        errorId: "ctrl_123",
      });
      expect(String(error)).not.toContain("must-not-surface");
    }
  });

  it("rejects invalid paging before fetch", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const client = new ResearchControlApiClient();

    await expect(client.listExperiments(201)).rejects.toMatchObject({
      code: "CONTROL_RESEARCH_CLIENT_INVALID_LIMIT",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
