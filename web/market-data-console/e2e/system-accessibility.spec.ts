import { expect, test } from "@playwright/test";

function controlReaderToken(): string {
  const value = process.env.MODULAR_CONTROL_E2E_READER_TOKEN;
  if (!value) throw new Error("MODULAR_CONTROL_E2E_READER_TOKEN is required");
  return value;
}

test("keeps periodic System snapshots out of broad live regions", async ({ page }) => {
  const readerToken = controlReaderToken();

  await page.route("**/api/control/v1/health", async (route) => {
    if (route.request().headers().authorization === `Bearer ${readerToken}`) {
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    await route.continue();
  });

  await page.goto("/#/system");
  await expect(page.getByRole("heading", { name: "System Health" })).toBeVisible();
  await page.getByLabel("Control reader token").fill(readerToken);

  const loading = page.getByText("Loading Control Plane state…", { exact: true }).locator("..");
  await expect(loading).toHaveAttribute("aria-live", "polite");

  await expect(page.getByRole("region", { name: "System summary" })).toBeVisible();
  await expect(page.locator('.dashboard-main [aria-live="polite"]')).toHaveCount(0);

  await page.getByRole("link", { name: "Resources", exact: true }).click();
  await expect(page.getByRole("heading", { name: "System Resources" })).toBeVisible();
  await expect(page.getByRole("region", { name: "System resource metrics" })).toBeVisible();
  await expect(page.locator('.dashboard-main [aria-live="polite"]')).toHaveCount(0);
});

test("keeps System read errors as focused alerts", async ({ page }) => {
  await page.route("**/api/control/v1/system/resources", async (route) => {
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: 1,
        error: {
          schema_version: 1,
          code: "CONTROL_SYSTEM_RESOURCES_UNAVAILABLE",
          message: "System resources are temporarily unavailable.",
          action: "Retry the local read.",
          error_id: "system-resources-e2e-error",
          component: null,
          operation: "system_resources",
          occurred_at: "2026-08-08T16:00:00Z",
          retryable: true,
          technical_details: {},
        },
      }),
    });
  });

  await page.goto("/#/system/resources");
  await page.getByLabel("Control reader token").fill(controlReaderToken());

  const alert = page.getByRole("alert");
  await expect(alert).toContainText("CONTROL_SYSTEM_RESOURCES_UNAVAILABLE");
  await expect(alert).toContainText("Retry the local read.");
});
