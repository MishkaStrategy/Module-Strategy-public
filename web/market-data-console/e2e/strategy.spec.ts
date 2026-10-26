import { expect, test } from "@playwright/test";

const RUN_ID = "browser-strategy-accepted";

function controlReaderToken(): string {
  const value = process.env.MODULAR_CONTROL_E2E_READER_TOKEN;
  if (!value) throw new Error("MODULAR_CONTROL_E2E_READER_TOKEN is required");
  return value;
}

test("reads Strategy Factory governance without mutation and survives browser reload", async ({
  page,
}) => {
  await page.goto("/#/strategies");
  await expect(page.getByRole("heading", { name: "Strategies" })).toBeVisible();
  await page.getByLabel("Control reader token").fill(controlReaderToken());

  await expect(
    page.getByText("Strategy Factory governance state is readable", { exact: true }),
  ).toBeVisible();

  const runList = page.getByRole("list", { name: "Strategy Factory runs" });
  await expect(runList).toContainText(RUN_ID);
  await expect(runList).toContainText("SUCCEEDED");
  await expect(runList).toContainText("Package");

  await runList.getByRole("button", { name: new RegExp(RUN_ID) }).click();
  await expect(page.getByText("browser-strategy-manifest", { exact: true })).toBeVisible();
  await expect(page.getByText("Decision chain", { exact: true })).toBeVisible();
  await expect(page.getByText("verified", { exact: true })).toBeVisible();
  await expect(page.getByText("Completed artifacts", { exact: true })).toBeVisible();

  await expect(page.getByRole("button", { name: /edit/i })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /resume/i })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /cancel/i })).toHaveCount(0);

  const beforeReloadStorage = await page.evaluate(() => ({
    local: window.localStorage.length,
    session: window.sessionStorage.length,
  }));
  expect(beforeReloadStorage).toEqual({ local: 0, session: 0 });

  await page.reload();
  await expect(page.getByLabel("Control reader token")).toHaveValue("");
  await expect(page.getByRole("list", { name: "Strategy Factory runs" })).toHaveCount(0);

  await page.getByLabel("Control reader token").fill(controlReaderToken());
  await expect(
    page.getByText("Strategy Factory governance state is readable", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("list", { name: "Strategy Factory runs" })).toContainText(RUN_ID);

  const afterReloadStorage = await page.evaluate(() => ({
    local: window.localStorage.length,
    session: window.sessionStorage.length,
  }));
  expect(afterReloadStorage).toEqual({ local: 0, session: 0 });
});
