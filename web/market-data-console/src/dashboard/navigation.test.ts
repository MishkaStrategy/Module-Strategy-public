import { describe, expect, it } from "vitest";

import { NAVIGATION, findComponentForPath, isNavigationItemActive } from "./navigation";

describe("dashboard navigation", () => {
  it("keeps accepted workspaces available and future modules explicitly pending", () => {
    const items = NAVIGATION.flatMap((group) => group.items);
    expect(items.find((item) => item.path === "/system/resources")?.available).toBe(true);
    expect(items.find((item) => item.path === "/datasets")?.available).toBe(true);
    expect(items.find((item) => item.path === "/jobs")?.available).toBe(true);
    expect(items.find((item) => item.path === "/strategies")?.available).toBe(true);
    expect(items.find((item) => item.path === "/backtests")?.available).toBe(true);
    expect(items.find((item) => item.path === "/research")?.available).toBe(true);
    expect(items.find((item) => item.path === "/evidence")?.available).toBe(true);
    expect(items.find((item) => item.path === "/paper")?.available).toBe(true);
    expect(items.find((item) => item.path === "/notifications")?.available).toBe(true);
    expect(items.find((item) => item.path === "/events")?.available).toBe(true);
    expect(items.find((item) => item.path === "/acceptance")?.available).toBe(true);
  });

  it("keeps System Health exact when Resources is active", () => {
    const items = NAVIGATION.flatMap((group) => group.items);
    const health = items.find((item) => item.path === "/system");
    const resources = items.find((item) => item.path === "/system/resources");
    expect(health).toBeDefined();
    expect(resources).toBeDefined();
    if (!health || !resources) throw new Error("System navigation item missing");
    expect(isNavigationItemActive(health, "/system")).toBe(true);
    expect(isNavigationItemActive(health, "/system/resources")).toBe(false);
    expect(isNavigationItemActive(resources, "/system/resources")).toBe(true);
  });

  it("maps nested Market Data detail routes to the existing component", () => {
    expect(findComponentForPath("/datasets/BTCUSDT")).toBe("market_data");
    expect(findComponentForPath("/jobs/job-1")).toBe("market_data");
  });

  it("maps project sections to their typed Control Plane component", () => {
    expect(findComponentForPath("/strategies/factory-run-1")).toBe("strategy");
    expect(findComponentForPath("/backtests")).toBe("backtest");
    expect(findComponentForPath("/research/experiments")).toBe("research");
    expect(findComponentForPath("/evidence/audit")).toBe("evidence");
    expect(findComponentForPath("/paper/runtime")).toBe("paper_runtime");
    expect(findComponentForPath("/notifications/telegram")).toBe("signal_bot");
    expect(findComponentForPath("/acceptance/current")).toBe("acceptance");
    expect(findComponentForPath("/events")).toBeUndefined();
    expect(findComponentForPath("/system/resources")).toBeUndefined();
  });

  it("does not treat unrelated prefix collisions as active routes", () => {
    const datasets = NAVIGATION.flatMap((group) => group.items).find(
      (item) => item.path === "/datasets",
    );
    expect(datasets).toBeDefined();
    if (!datasets) throw new Error("datasets navigation item missing");
    expect(isNavigationItemActive(datasets, "/datasets/BTCUSDT")).toBe(true);
    expect(isNavigationItemActive(datasets, "/datasets-archive")).toBe(false);
  });
});
