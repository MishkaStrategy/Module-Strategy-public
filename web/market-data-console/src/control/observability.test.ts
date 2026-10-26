import { afterEach, describe, expect, it, vi } from "vitest";

import { ControlPlaneApiClient } from "./client";
import type {
  ActiveObservabilityAlert,
  ObservabilityComponentId,
  ObservabilityComponentView,
  UnifiedObservabilitySnapshot,
} from "./observability";
import { isUnifiedObservabilitySnapshot } from "./observability";

const NOW = "2026-08-28T06:45:00Z";
const COMPONENTS: ObservabilityComponentId[] = [
  "backtest_research",
  "dataset_registry",
  "market_data",
  "paper_execution",
  "portfolio_position",
  "risk_sizing",
  "risk_supervisor",
  "signal_bot",
  "strategy_audit_factory",
  "system_acceptance_recovery",
  "system_resources",
  "trading_runner",
];

afterEach(() => {
  vi.unstubAllGlobals();
});

function digest(index: number): string {
  return index.toString(16).padStart(64, "0");
}

function component(componentId: ObservabilityComponentId, index: number): ObservabilityComponentView {
  return {
    schema_version: 1,
    component: componentId,
    health: "healthy",
    observed_at: NOW,
    evaluated_at: NOW,
    age_seconds: 0,
    stale: false,
    progress_at: null,
    progress_age_seconds: null,
    stuck: false,
    state_ref: digest(index + 100),
    reason_codes: [],
    correlation_id: null,
    public_details: {},
    observation_hash: digest(index + 1),
  };
}

function healthySnapshot(): UnifiedObservabilitySnapshot {
  return {
    schema_version: 1,
    mode: "PAPER",
    overview: {
      schema_version: 1,
      evaluated_at: NOW,
      overall_health: "healthy",
      overview_hash: "e".repeat(64),
      components: COMPONENTS.map(component),
    },
    active_alerts: [],
  };
}

function blockedSnapshot(): UnifiedObservabilitySnapshot {
  const snapshot = healthySnapshot();
  const risk = snapshot.overview.components.find((item) => item.component === "risk_supervisor");
  if (!risk) throw new Error("risk supervisor fixture missing");
  risk.health = "blocked";
  risk.reason_codes = ["risk_supervisor.operator_emergency_halt"];
  const alert: ActiveObservabilityAlert = {
    schema_version: 1,
    alert_id: "f".repeat(64),
    component: "risk_supervisor",
    severity: "critical",
    reason_codes: ["risk_supervisor.operator_emergency_halt"],
    next_action: "follow_risk_supervisor_recovery",
    evaluated_at: NOW,
    source_observation_hash: risk.observation_hash,
    correlation_id: null,
  };
  snapshot.overview.overall_health = "blocked";
  snapshot.active_alerts = [alert];
  return snapshot;
}

function clone(value: UnifiedObservabilitySnapshot): Record<string, unknown> {
  return JSON.parse(JSON.stringify(value)) as Record<string, unknown>;
}

describe("isUnifiedObservabilitySnapshot", () => {
  it("accepts the exact sorted PAPER contract", () => {
    expect(isUnifiedObservabilitySnapshot(healthySnapshot())).toBe(true);
    expect(isUnifiedObservabilitySnapshot(blockedSnapshot())).toBe(true);
  });

  it("rejects privacy-expanded top-level fields", () => {
    expect(isUnifiedObservabilitySnapshot({ ...healthySnapshot(), private_path: "/secret" })).toBe(
      false,
    );
  });

  it("rejects an incomplete or reordered component set", () => {
    const payload = healthySnapshot();
    payload.overview.components = [...payload.overview.components].reverse();
    expect(isUnifiedObservabilitySnapshot(payload)).toBe(false);

    payload.overview.components = payload.overview.components.slice(1);
    expect(isUnifiedObservabilitySnapshot(payload)).toBe(false);
  });

  it("rejects non-SHA digests and alert/source mismatches", () => {
    const invalidDigest = clone(healthySnapshot());
    const invalidOverview = invalidDigest.overview as Record<string, unknown>;
    invalidOverview.overview_hash = "g".repeat(64);
    expect(isUnifiedObservabilitySnapshot(invalidDigest)).toBe(false);

    const mismatched = blockedSnapshot();
    const alert = mismatched.active_alerts[0];
    if (!alert) throw new Error("active alert fixture missing");
    alert.source_observation_hash = "a".repeat(64);
    expect(isUnifiedObservabilitySnapshot(mismatched)).toBe(false);
  });

  it("rejects a green summary when a component is blocked", () => {
    const payload = blockedSnapshot();
    payload.overview.overall_health = "healthy";
    expect(isUnifiedObservabilitySnapshot(payload)).toBe(false);
  });

  it("rejects timezone-naive timestamps", () => {
    const payload = healthySnapshot();
    payload.overview.evaluated_at = "2026-08-28T06:45:00";
    expect(isUnifiedObservabilitySnapshot(payload)).toBe(false);
  });

  it("rejects integer details outside the shared JSON safe-integer boundary", () => {
    const payload = healthySnapshot();
    const resources = payload.overview.components.find(
      (item) => item.component === "system_resources",
    );
    if (!resources) throw new Error("system resources fixture missing");
    resources.public_details = { unsafe_integer: Number.MAX_SAFE_INTEGER + 1 };
    expect(isUnifiedObservabilitySnapshot(payload)).toBe(false);
  });
});

describe("ControlPlaneApiClient unified observability", () => {
  it("reads the fixed authenticated endpoint without cookies or cache", async () => {
    const signal = new AbortController().signal;
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(blockedSnapshot()), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const client = new ControlPlaneApiClient("", () => "reader-secret");

    const snapshot = await client.getObservability(signal);

    expect(snapshot.overview.overall_health).toBe("blocked");
    expect(snapshot.active_alerts[0]?.next_action).toBe("follow_risk_supervisor_recovery");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/control/v1/observability/overview");
    expect(init.signal).toBe(signal);
    expect(init.cache).toBe("no-store");
    expect(init.credentials).toBe("omit");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer reader-secret");
  });

  it("fails closed on an invalid unified response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ ...healthySnapshot(), mode: "LIVE" }), { status: 200 }),
      ),
    );
    const client = new ControlPlaneApiClient();

    await expect(client.getObservability()).rejects.toMatchObject({
      code: "CONTROL_RESPONSE_INVALID",
      operation: "observability_overview",
    });
  });
});
