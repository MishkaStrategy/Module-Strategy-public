import { describe, expect, it } from "vitest";

import { documentTitleForPath, pageLabelForPath, routePathFromHash } from "./accessibility";

describe("dashboard accessibility routing", () => {
  it("normalizes hash routes without leaking query state into page context", () => {
    expect(routePathFromHash("#/research?run=browser-run-a-success")).toBe("/research");
    expect(routePathFromHash("#/system/resources")).toBe("/system/resources");
    expect(routePathFromHash("#unexpected")).toBe("/overview");
    expect(routePathFromHash("")).toBe("/overview");
  });

  it("derives page labels from the canonical dashboard navigation", () => {
    expect(pageLabelForPath("/")).toBe("Overview");
    expect(pageLabelForPath("/overview")).toBe("Overview");
    expect(pageLabelForPath("/system/resources")).toBe("Resources");
    expect(pageLabelForPath("/datasets/BTCUSDT")).toBe("Datasets");
    expect(pageLabelForPath("/research")).toBe("Research");
    expect(pageLabelForPath("/not-registered")).toBe("Control Dashboard");
  });

  it("produces stable route-aware document titles", () => {
    expect(documentTitleForPath("/research")).toBe("Research · Modular Strategy");
    expect(documentTitleForPath("/overview")).toBe("Overview · Modular Strategy");
    expect(documentTitleForPath("/not-registered")).toBe("Control Dashboard · Modular Strategy");
  });
});
