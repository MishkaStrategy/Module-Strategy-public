import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";

interface Fixture {
  dataset_id: string;
  dataset_version: number;
  instrument_id: string;
  artifact_id: string;
  artifact_filename: string;
  download_job_id: string;
  export_job_id: string;
  observable_job_id: string;
  observable_first_page_events: number;
  observable_total_events: number;
  backtest_baseline_config_id: string;
  backtest_candidate_config_id: string;
}

function fixture(): Fixture {
  const path = process.env.MDI_E2E_FIXTURE;
  if (!path) throw new Error("MDI_E2E_FIXTURE is required");
  return JSON.parse(readFileSync(path, "utf-8")) as Fixture;
}

function readerToken(): string {
  const value = process.env.MDI_E2E_READER_TOKEN;
  if (!value) throw new Error("MDI_E2E_READER_TOKEN is required");
  return value;
}

function controlReaderToken(): string {
  const value = process.env.MODULAR_CONTROL_E2E_READER_TOKEN;
  if (!value) throw new Error("MODULAR_CONTROL_E2E_READER_TOKEN is required");
  return value;
}

function controlOperatorToken(): string {
  const value = process.env.MODULAR_CONTROL_E2E_OPERATOR_TOKEN;
  if (!value) throw new Error("MODULAR_CONTROL_E2E_OPERATOR_TOKEN is required");
  return value;
}

function dashboardNavigation(page: import("@playwright/test").Page) {
  return page.getByRole("navigation", { name: "Навигация Modular Strategy" });
}

async function authenticateMarketData(page: import("@playwright/test").Page): Promise<void> {
  await page.goto("/#/datasets");
  await expect(page.getByRole("heading", { name: "Датасеты" })).toBeVisible();
  await page.getByLabel("Market Data reader token").fill(readerToken());

  const workspace = page.getByRole("region", { name: "Market Data workspace" });
  await expect(workspace.getByText("Market Data workspace", { exact: true })).toBeVisible();
  await expect(workspace.getByText("native shell", { exact: true })).toBeVisible();

  // Phase 3 acceptance: Dashboard owns the only navigation/sidebar shell.
  await expect(page.locator(".dashboard-sidebar")).toHaveCSS("position", "sticky");
  await expect(workspace.locator(".app-shell")).toHaveCount(0);
  await expect(workspace.locator(".sidebar")).toHaveCount(0);
  await expect(workspace.getByRole("navigation")).toHaveCount(0);
}

async function authenticateControlPlane(page: import("@playwright/test").Page): Promise<void> {
  await page.goto("/#/overview");
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await page.getByLabel("Control reader token").fill(controlReaderToken());
  await expect(page.getByRole("region", { name: "System summary" })).toBeVisible();
}

async function authenticateBacktest(page: import("@playwright/test").Page): Promise<void> {
  await page.goto("/#/backtests");
  await expect(page.getByRole("heading", { name: "Backtests" })).toBeVisible();
  await page.getByLabel("Control reader token").fill(controlReaderToken());
  await page.getByLabel("Control operator token").fill(controlOperatorToken());
  await expect(page.getByText("Backtest durable control is ready", { exact: true })).toBeVisible();
}

test("shows whole-system health through the real local Control Plane", async ({ page }) => {
  await authenticateControlPlane(page);

  const summary = page.getByRole("region", { name: "System summary" });
  await expect(summary.getByText("PAPER", { exact: true })).toBeVisible();
  await expect(summary.getByLabel("UNAVAILABLE", { exact: true })).toBeVisible();

  await dashboardNavigation(page).getByRole("link", { name: "Health", exact: true }).click();
  await expect(page.getByRole("heading", { name: "System Health" })).toBeVisible();

  const marketData = page.locator(".component-health-card").filter({ hasText: "Market Data" });
  await expect(marketData.getByLabel("HEALTHY", { exact: true })).toBeVisible();
  await expect(marketData.getByText("Market Data is healthy", { exact: true })).toBeVisible();

  const backtest = page.locator(".component-health-card").filter({ hasText: "Backtest" });
  await expect(backtest.getByLabel("HEALTHY", { exact: true })).toBeVisible();
  await expect(backtest.getByText("Backtest durable control is ready", { exact: true })).toBeVisible();

  await page.getByRole("link", { name: "Backtests" }).click();
  await expect(page.getByRole("heading", { name: "Backtests" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Queue backtest" })).toBeDisabled();
  await expect(page.getByRole("button", { name: /cancel/i })).toHaveCount(0);

  const storage = await page.evaluate(() => ({
    local: window.localStorage.length,
    session: window.sessionStorage.length,
  }));
  expect(storage).toEqual({ local: 0, session: 0 });

  await page.reload();
  await expect(page.getByLabel("Control reader token")).toHaveValue("");
  await expect(page.getByLabel("Control operator token")).toHaveValue("");
  await expect(page.getByLabel("Market Data reader token")).toHaveValue("");
});

test("runs, inspects and compares durable Backtests through the native workspace", async ({
  page,
}) => {
  const data = fixture();
  await authenticateBacktest(page);

  const configInput = page.getByLabel("Config ID");
  const queueButton = page.getByRole("button", { name: "Queue backtest" });
  const jobRows = page.locator(".backtest-jobs-panel .backtest-table tbody tr");

  await expect(queueButton).toBeDisabled();
  await configInput.fill(data.backtest_baseline_config_id);
  await expect(queueButton).toBeEnabled();
  await queueButton.click();
  await expect(page.getByText("Backtest queued", { exact: true })).toBeVisible();
  await expect(jobRows).toHaveCount(1, { timeout: 30_000 });
  await expect(
    jobRows.first().getByText(data.backtest_baseline_config_id, { exact: true }),
  ).toBeVisible();
  await expect(jobRows.first().getByText("SUCCEEDED", { exact: true })).toBeVisible({
    timeout: 30_000,
  });
  await jobRows.first().getByRole("button", { name: "View result" }).click();
  await expect(page.getByText("Run ID", { exact: true })).toBeVisible();
  await expect(page.getByText("Bars processed", { exact: true })).toBeVisible();

  await page.reload();
  await expect(page.getByLabel("Control reader token")).toHaveValue("");
  await expect(page.getByLabel("Control operator token")).toHaveValue("");
  await page.getByLabel("Control reader token").fill(controlReaderToken());
  await page.getByLabel("Control operator token").fill(controlOperatorToken());
  await expect(page.getByText("Backtest durable control is ready", { exact: true })).toBeVisible();
  await expect(jobRows).toHaveCount(1, { timeout: 30_000 });
  await expect(
    jobRows.first().getByText(data.backtest_baseline_config_id, { exact: true }),
  ).toBeVisible();
  await expect(jobRows.first().getByText("SUCCEEDED", { exact: true })).toBeVisible();

  await configInput.fill(data.backtest_candidate_config_id);
  await expect(queueButton).toBeEnabled();
  await queueButton.click();
  await expect(jobRows).toHaveCount(2, { timeout: 30_000 });
  await expect(
    jobRows.nth(0).getByText(data.backtest_candidate_config_id, { exact: true }),
  ).toBeVisible();
  await expect(jobRows.nth(0).getByText("SUCCEEDED", { exact: true })).toBeVisible({
    timeout: 30_000,
  });
  await expect(
    jobRows.nth(1).getByText(data.backtest_baseline_config_id, { exact: true }),
  ).toBeVisible();
  await expect(jobRows.nth(1).getByText("SUCCEEDED", { exact: true })).toBeVisible({
    timeout: 30_000,
  });

  const left = page.getByLabel("Left job");
  const right = page.getByLabel("Right job");
  await expect(left.locator("option")).toHaveCount(3);
  const leftJobId = await left.locator("option").nth(1).getAttribute("value");
  const rightJobId = await right.locator("option").nth(2).getAttribute("value");
  if (!leftJobId || !rightJobId) throw new Error("durable Backtest comparison options missing");
  await left.selectOption(leftJobId);
  await right.selectOption(rightJobId);
  await page.getByRole("button", { name: "Compare" }).click();
  await expect(page.locator(".backtest-comparison-identities")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("button", { name: /cancel/i })).toHaveCount(0);

  const storage = await page.evaluate(() => ({
    local: window.localStorage.length,
    session: window.sessionStorage.length,
  }));
  expect(storage).toEqual({ local: 0, session: 0 });
});

test("reads datasets and immutable artifact through the preserved real Market Data API", async ({
  page,
}) => {
  const data = fixture();
  await authenticateMarketData(page);

  await expect(page.getByText(data.instrument_id, { exact: true })).toBeVisible();
  await expect(page.getByText(data.dataset_id, { exact: true })).toBeVisible();
  await page.getByRole("link", { name: data.instrument_id }).click();
  await expect(
    page.getByText(`${data.dataset_id} · v${data.dataset_version}`, { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Dataset hash", { exact: true })).toBeVisible();

  await dashboardNavigation(page).getByRole("link", { name: "Artifacts" }).click();
  await page.getByLabel("Artifact ID").fill(data.artifact_id);
  await page.getByRole("button", { name: "Открыть" }).click();
  await expect(page.getByText(data.artifact_filename, { exact: true })).toBeVisible();
  await expect(page.getByText("SHA-256", { exact: true })).toBeVisible();

  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "Скачать ZIP" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe(data.artifact_filename);
  await download.delete();

  const storage = await page.evaluate(() => ({
    local: window.localStorage.length,
    session: window.sessionStorage.length,
  }));
  expect(storage).toEqual({ local: 0, session: 0 });

  await page.reload();
  await expect(page.getByLabel("Market Data reader token")).toHaveValue("");
});

test("resumes authorized SSE after a deliberate disconnect and cleans up on navigation", async ({
  page,
}) => {
  const data = fixture();
  expect(data.observable_first_page_events).toBe(1_000);
  expect(data.observable_total_events).toBe(1_006);

  let attempts = 0;
  const resumedHeaders: Record<string, string> = {};
  let resumedUrl = "";
  await page.route("**/api/v1/jobs/*/events/stream?**", async (route) => {
    attempts += 1;
    if (attempts === 1) {
      await route.abort("failed");
      return;
    }
    if (attempts === 2) {
      Object.assign(resumedHeaders, route.request().headers());
      resumedUrl = route.request().url();
    }
    await route.continue();
  });

  await authenticateMarketData(page);
  await dashboardNavigation(page).getByRole("link", { name: "Jobs" }).click();
  await page.getByLabel("Операция").selectOption("validate");
  await expect(page.getByRole("link", { name: data.observable_job_id })).toBeVisible();
  await page.getByRole("link", { name: data.observable_job_id }).click();

  await expect.poll(() => attempts).toBeGreaterThanOrEqual(2);
  expect(resumedHeaders.authorization).toBe(`Bearer ${readerToken()}`);
  expect(resumedHeaders["last-event-id"]).toBe("500");
  expect(resumedHeaders.accept).toContain("text/event-stream");
  expect(resumedUrl).toContain("after_event_id=500");

  await expect(page.getByText("Показаны последние 1000 из максимальных 1000.")).toBeVisible();
  await expect(page.getByText("#1006", { exact: true })).toBeVisible();

  await dashboardNavigation(page).getByRole("link", { name: "Overview" }).click();
  await page.waitForTimeout(500);
  const attemptsAfterNavigation = attempts;
  await page.waitForTimeout(2_000);
  expect(attempts).toBe(attemptsAfterNavigation);

  const storage = await page.evaluate(() => ({
    local: window.localStorage.length,
    session: window.sessionStorage.length,
  }));
  expect(storage).toEqual({ local: 0, session: 0 });
});
