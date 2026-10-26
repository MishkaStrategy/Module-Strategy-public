import { expect, test } from "@playwright/test";

const EVENTS_E2E = process.env.MODULAR_CONTROL_EVENTS_E2E === "1";

function readerToken(): string {
  const value = process.env.MODULAR_CONTROL_E2E_READER_TOKEN;
  if (!value) throw new Error("MODULAR_CONTROL_E2E_READER_TOKEN is required");
  return value;
}

test("queries structured Trading Runner events with cursor pagination and no path surface", async ({ page }) => {
  test.skip(!EVENTS_E2E, "focused structured-events fixture is not configured");

  await page.goto("/#/events");
  await expect(page.getByRole("heading", { name: "Logs & Events" })).toBeVisible();
  await page.getByLabel("Control reader token").fill(readerToken());

  const events = page.getByRole("list", { name: "Structured operational events" });
  await expect(events).toContainText("runtime.started");
  await expect(events).toContainText("operator.lifecycle");
  await expect(events).toContainText("browser-paper-runtime");
  await expect(page.getByText("Trading Runner source · V1", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Next" }).click();
  await expect(events).toContainText("kill_switch.activated");
  await expect(events).toContainText("kill_switch.reset");
  await expect(page.getByRole("button", { name: "Next" })).toBeDisabled();
  await page.getByRole("button", { name: "Previous" }).click();
  await expect(events).toContainText("runtime.started");

  await page.getByLabel("Resource · current page").fill("no-such-resource");
  await expect(events).toContainText("No events match this page.");
  await page.getByLabel("Resource · current page").fill("");
  await expect(events).toContainText("runtime.started");

  await page.getByLabel("Event type").selectOption("runtime.failed");
  await expect(events).toContainText("runtime.failed");
  await expect(events).toContainText("BootstrapError");
  await expect(events).not.toContainText("/Users/private");
  await expect(events).not.toContainText("BROWSER-PRIVATE-EVENT-TOKEN");

  const malformed = await page.request.get(
    "/api/control/v1/events/trading-runner?cursor=%2FUsers%2Fprivate%2Fruntime.log",
    { headers: { Authorization: `Bearer ${readerToken()}` } },
  );
  expect(malformed.status()).toBe(400);
  expect((await malformed.json()).error.code).toBe("CONTROL_EVENTS_INVALID_CURSOR");

  const rejected = await page.request.get(
    "/api/control/v1/events/trading-runner?path=%2FUsers%2Fprivate%2Fruntime.log&command=tail%20-f",
    { headers: { Authorization: `Bearer ${readerToken()}` } },
  );
  expect(rejected.status()).toBe(400);
  const rejectedBody = JSON.stringify(await rejected.json());
  expect(JSON.parse(rejectedBody).error.code).toBe("CONTROL_EVENTS_INVALID_REQUEST");
  expect(rejectedBody).not.toContain("/Users/private");
  expect(rejectedBody).not.toContain("tail -f");
  expect(rejectedBody).not.toContain("BROWSER-PRIVATE-EVENT-TOKEN");

  const body = page.locator("body");
  await expect(body).not.toContainText(readerToken());
  await expect(body).not.toContainText("BROWSER-PRIVATE-EVENT-TOKEN");
  await expect(page.getByRole("button", { name: /live trading/i })).toHaveCount(0);
  expect(
    await page.evaluate(() => ({
      local: window.localStorage.length,
      session: window.sessionStorage.length,
    })),
  ).toEqual({ local: 0, session: 0 });
});
