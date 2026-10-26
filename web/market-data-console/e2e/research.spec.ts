import { expect, test } from "@playwright/test";

const EXPERIMENT_ID = "browser-research-experiment";
const SUCCESS_RUN_ID = "browser-run-a-success";
const FAILED_RUN_ID = "browser-run-b-failed";
const RESEARCH_STATE_CONFIGURED = Boolean(process.env.MODULAR_CONTROL_RESEARCH_STATE_PATH);

function controlReaderToken(): string {
  const value = process.env.MODULAR_CONTROL_E2E_READER_TOKEN;
  if (!value) throw new Error("MODULAR_CONTROL_E2E_READER_TOKEN is required");
  return value;
}

test("reads durable Research state without mutation and survives browser reload", async ({ page }) => {
  test.skip(!RESEARCH_STATE_CONFIGURED, "Research durable-state fixture is not configured");

  await page.goto("/#/research");
  await expect(page).toHaveTitle("Research · Modular Strategy");
  await expect(page.getByRole("heading", { name: "Research", exact: true })).toBeVisible();

  const skipLink = page.getByRole("link", { name: "Перейти к основному содержимому" });
  const main = page.getByRole("main");
  await page.keyboard.press("Tab");
  await expect(skipLink).toBeFocused();
  await expect(skipLink).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(main).toBeFocused();
  await expect(page).toHaveURL(/#\/research$/);

  await page.getByRole("link", { name: "Overview", exact: true }).click();
  await expect(page).toHaveURL(/#\/overview$/);
  await expect(page).toHaveTitle("Overview · Modular Strategy");
  await expect(main).toBeFocused();

  await page.getByRole("link", { name: "Research", exact: true }).click();
  await expect(page).toHaveURL(/#\/research$/);
  await expect(page).toHaveTitle("Research · Modular Strategy");
  await expect(main).toBeFocused();
  await expect(page.getByRole("heading", { name: "Research", exact: true })).toBeVisible();

  await page.getByLabel("Control reader token").fill(controlReaderToken());

  await expect(
    page.getByText("Research Runner durable state is readable", { exact: true }),
  ).toBeVisible();

  const experiments = page.getByRole("list", { name: "Research experiments" });
  await expect(experiments).toContainText(EXPERIMENT_ID);
  await expect(experiments).toContainText("COMPLETED_WITH_FAILURES");
  await expect(experiments).toContainText("completed with failures");
  await experiments.getByRole("button", { name: new RegExp(EXPERIMENT_ID) }).click();

  await expect(page.getByText("Runs · SUCCEEDED", { exact: true })).toBeVisible();
  await expect(page.getByText("Runs · FAILED", { exact: true })).toBeVisible();

  const runs = page.getByRole("list", { name: "Research runs" });
  await expect(runs).toContainText(SUCCESS_RUN_ID);
  await expect(runs).toContainText(FAILED_RUN_ID);
  await expect(runs).toContainText("SUCCEEDED");
  await expect(runs).toContainText("FAILED");

  await runs.getByRole("button", { name: new RegExp(SUCCESS_RUN_ID) }).click();
  await expect(page.getByText("engine-browser-success", { exact: true })).toBeVisible();
  await expect(page.getByText("144", { exact: true })).toBeVisible();
  await expect(page.getByText("Restart verified", { exact: true })).toBeVisible();

  await runs.getByRole("button", { name: new RegExp(FAILED_RUN_ID) }).click();
  await expect(page.getByText("ENGINE", { exact: true })).toBeVisible();
  await expect(page.getByText("Failure retryable", { exact: true })).toBeVisible();
  await expect(page.getByText("PRIVATE failure message must never be projected")).toHaveCount(0);
  await expect(page.getByText("PrivateEngineError")).toHaveCount(0);
  await expect(page.getByText("private-worker")).toHaveCount(0);
  await expect(page.getByText("private-lease-failed")).toHaveCount(0);
  await expect(page.getByText("private-result-output")).toHaveCount(0);

  for (const mutation of [/start/i, /pause/i, /cancel/i, /retry/i]) {
    await expect(page.getByRole("button", { name: mutation })).toHaveCount(0);
  }

  const beforeReloadStorage = await page.evaluate(() => ({
    local: window.localStorage.length,
    session: window.sessionStorage.length,
  }));
  expect(beforeReloadStorage).toEqual({ local: 0, session: 0 });

  await page.reload();
  await expect(page).toHaveTitle("Research · Modular Strategy");
  await expect(page.getByLabel("Control reader token")).toHaveValue("");
  await expect(page.getByRole("list", { name: "Research experiments" })).toHaveCount(0);

  await page.getByLabel("Control reader token").fill(controlReaderToken());
  await expect(
    page.getByText("Research Runner durable state is readable", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("list", { name: "Research experiments" })).toContainText(
    EXPERIMENT_ID,
  );

  const afterReloadStorage = await page.evaluate(() => ({
    local: window.localStorage.length,
    session: window.sessionStorage.length,
  }));
  expect(afterReloadStorage).toEqual({ local: 0, session: 0 });
});
