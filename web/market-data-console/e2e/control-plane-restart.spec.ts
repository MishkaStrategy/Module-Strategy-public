import { expect, test } from "@playwright/test";

const phase = process.env.MODULAR_CONTROL_RESTART_PHASE;

function requiredEnvironment(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
}

async function browserStorage(page: import("@playwright/test").Page) {
  return page.evaluate(() => ({
    local: window.localStorage.length,
    session: window.sessionStorage.length,
  }));
}

test("fails closed while the real local Control Plane transport is down", async ({ page }) => {
  test.skip(phase !== "down", "executed only during the injected Control Plane outage");

  await page.goto("/#/overview");
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await page
    .getByLabel("Control reader token")
    .fill(requiredEnvironment("MODULAR_CONTROL_E2E_READER_TOKEN"));

  await expect(page.getByText("Legacy component detail unavailable", { exact: true })).toBeVisible();
  await expect(page.getByText("Unified observability unavailable", { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "System summary" })).toHaveCount(0);
  expect(await browserStorage(page)).toEqual({ local: 0, session: 0 });
});

test("re-reads durable owner truth after Control Plane loopback rebind", async ({ page }) => {
  test.skip(phase !== "recovered", "executed only after Control Plane loopback rebind");

  const readerToken = requiredEnvironment("MODULAR_CONTROL_E2E_READER_TOKEN");
  const acceptanceRunId = requiredEnvironment("MODULAR_CONTROL_RESTART_RUN_ID");

  await page.goto("/#/acceptance");
  await expect(page.getByRole("heading", { name: "System Acceptance" })).toBeVisible();
  await page.getByLabel("Control reader token").fill(readerToken);
  await expect(page.getByRole("heading", { name: "Current run" })).toBeVisible();
  await expect(page.getByText(acceptanceRunId, { exact: true }).first()).toBeVisible();

  await page.goto("/#/overview");
  await page.getByLabel("Control reader token").fill(readerToken);
  await expect(page.getByRole("region", { name: "System summary" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Operational summary" })).toBeVisible();
  await expect(page.getByText("Unified observability unavailable", { exact: true })).toHaveCount(0);
  expect(await browserStorage(page)).toEqual({ local: 0, session: 0 });
});
