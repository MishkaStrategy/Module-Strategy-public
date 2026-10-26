import { describe, expect, it } from "vitest";

import type { BacktestJobRecord } from "../backtest/contracts";
import type { SystemOverview } from "../control/contracts";
import {
  activeBacktestJobs,
  componentHealth,
  healthDetailNumber,
  healthDetailText,
  latestBacktestJob,
} from "./overview";

function job(status: BacktestJobRecord["job"]["status"], id: string): BacktestJobRecord {
  return {
    schema_version: 1,
    config_id: `config-${id}`,
    result: null,
    job: {
      schema_version: 1,
      job_id: id,
      component: "backtest",
      operation: "run",
      status,
      phase: status,
      progress: null,
      created_at: "2026-08-08T12:00:00Z",
      updated_at: "2026-08-08T12:00:00Z",
      started_at: null,
      completed_at: null,
      error: null,
    },
  };
}

const overview: SystemOverview = {
  schema_version: 1,
  mode: "PAPER",
  status: "DEGRADED",
  checked_at: "2026-08-08T12:00:00Z",
  components: [
    {
      schema_version: 1,
      component: "paper_runtime",
      status: "HEALTHY",
      checked_at: "2026-08-08T12:00:00Z",
      summary: "Paper Runtime is running and reconciled",
      details: {
        runtime_state: "running",
        active_order_count: 2,
        position_projection_available: null,
      },
    },
    {
      schema_version: 1,
      component: "acceptance",
      status: "UNAVAILABLE",
      checked_at: "2026-08-08T12:00:00Z",
      summary: "Acceptance owner is unavailable",
      details: {},
    },
  ],
};

describe("operational overview projection helpers", () => {
  it("selects component health and keeps detail typing fail-closed", () => {
    const paper = componentHealth(overview, "paper_runtime");
    expect(paper?.status).toBe("HEALTHY");
    expect(healthDetailText(paper, "runtime_state")).toBe("running");
    expect(healthDetailNumber(paper, "active_order_count")).toBe(2);
    expect(healthDetailNumber(paper, "runtime_state")).toBeNull();
    expect(healthDetailText(paper, "active_order_count")).toBeNull();
    expect(componentHealth(overview, "signal_bot")).toBeUndefined();
  });

  it("keeps only non-terminal durable Backtest jobs active", () => {
    const jobs = [
      job("SUCCEEDED", "bt_00000000000000000001"),
      job("RUNNING", "bt_00000000000000000002"),
      job("BLOCKED", "bt_00000000000000000003"),
      job("FAILED", "bt_00000000000000000004"),
    ];

    expect(activeBacktestJobs(jobs).map((item) => item.job.status)).toEqual([
      "RUNNING",
      "BLOCKED",
    ]);
    expect(latestBacktestJob(jobs)?.job.status).toBe("SUCCEEDED");
    expect(latestBacktestJob([])).toBeUndefined();
  });
});
