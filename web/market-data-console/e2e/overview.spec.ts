import { expect, test } from "@playwright/test";

function controlReaderToken(): string {
  const value = process.env.MODULAR_CONTROL_E2E_READER_TOKEN;
  if (!value) throw new Error("MODULAR_CONTROL_E2E_READER_TOKEN is required");
  return value;
}

function metric(region: import("@playwright/test").Locator, label: string) {
  return region.getByText(label, { exact: true }).locator("..");
}

test("projects the owner-backed operational snapshot on Overview", async ({ page }) => {
  await page.goto("/#/overview");
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await page.getByLabel("Control reader token").fill(controlReaderToken());

  const system = page.getByRole("region", { name: "System summary" });
  await expect(system).toBeVisible();
  await expect(system.getByText("PAPER", { exact: true })).toBeVisible();
  await expect(system.getByLabel("UNAVAILABLE", { exact: true })).toBeVisible();

  const operational = page.getByRole("region", { name: "Operational summary" });
  await expect(operational).toBeVisible();
  for (const label of [
    "Data",
    "Dataset Registry",
    "Strategy / Audit",
    "Backtest / Research",
    "Backtest Jobs",
    "Trading Runner",
    "Paper Execution",
    "Orders",
    "Positions",
    "Risk Sizing",
    "Risk Supervisor",
    "Acceptance / Recovery",
    "Notifications",
    "CPU",
    "RAM",
    "Disk",
    "Control Plane",
  ]) {
    await expect(metric(operational, label)).toBeVisible();
  }

  for (const label of [
    "Data",
    "Strategy / Audit",
    "Backtest / Research",
    "Positions",
    "Risk Supervisor",
  ]) {
    await expect(metric(operational, label).getByLabel("UNAVAILABLE", { exact: true })).toBeVisible();
  }
  await expect(metric(operational, "Dataset Registry").getByLabel("HEALTHY", { exact: true })).toBeVisible();
  await expect(metric(operational, "CPU").locator("strong")).not.toHaveText("—");
  await expect(metric(operational, "RAM").locator("strong")).not.toHaveText("—");
  await expect(metric(operational, "Disk").locator("strong")).not.toHaveText("—");

  await expect(page.getByRole("region", { name: "Active backtests" })).toBeVisible();
  const alerts = page.getByRole("region", { name: "Current alerts" });
  await expect(alerts).toBeVisible();
  const riskSupervisorAlert = alerts
    .locator("article.component-health-card")
    .filter({ hasText: "Risk Supervisor" });
  await expect(riskSupervisorAlert).toHaveCount(1);
  await expect(riskSupervisorAlert.getByText("Risk Supervisor", { exact: true })).toBeVisible();
  await expect(
    riskSupervisorAlert.getByText("Next action: restore_owner_visibility", { exact: true }),
  ).toBeVisible();

  const storage = await page.evaluate(() => ({
    local: window.localStorage.length,
    session: window.sessionStorage.length,
  }));
  expect(storage).toEqual({ local: 0, session: 0 });
});

test("does not claim there are no durable jobs when the Backtest list read fails", async ({
  page,
}) => {
  await page.route("**/api/control/v1/backtests?limit=100&offset=0", async (route) => {
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({
        error: {
          code: "CONTROL_BACKTEST_UNAVAILABLE",
          message: "Backtest Control is temporarily unavailable.",
          action: "Retry the local read.",
          retryable: true,
        },
      }),
    });
  });

  await page.goto("/#/overview");
  await page.getByLabel("Control reader token").fill(controlReaderToken());

  const operational = page.getByRole("region", { name: "Operational summary" });
  await expect(operational).toBeVisible();
  await expect(metric(operational, "Backtest Jobs").locator("strong")).toHaveText("READ ERROR");
  await expect(
    metric(operational, "Backtest / Research").getByLabel("UNAVAILABLE", { exact: true }),
  ).toBeVisible();

  const activeBacktests = page.getByRole("region", { name: "Active backtests" });
  await expect(activeBacktests.getByText("Backtest list unavailable", { exact: true })).toBeVisible();
  await expect(activeBacktests.getByText("No active Backtest jobs", { exact: true })).toHaveCount(0);
});
