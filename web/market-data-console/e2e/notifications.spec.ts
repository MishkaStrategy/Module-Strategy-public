import { expect, test } from "@playwright/test";

const NOTIFICATIONS_E2E = process.env.MODULAR_SIGNAL_BOT_E2E === "1";

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

test("uses the real Signal Bot owner and safely replays the same test-notification request", async ({
  page,
}) => {
  test.skip(!NOTIFICATIONS_E2E, "real Signal Bot owner fixture is not configured");

  const readerToken = requiredEnvironment("MODULAR_CONTROL_E2E_READER_TOKEN");
  const operatorToken = requiredEnvironment("MODULAR_CONTROL_E2E_OPERATOR_TOKEN");

  await page.goto("/#/notifications");
  await expect(page.getByRole("heading", { name: "Notifications", exact: true })).toBeVisible();
  await page.getByLabel("Control reader token").fill(readerToken);
  await page.getByLabel("Control operator token").fill(operatorToken);

  const status = page.getByRole("region", { name: "Notification owner status" });
  await expect(status).toContainText("OK");
  await expect(status).toContainText("ENABLED");
  await expect(page.getByText("17", { exact: true })).toBeVisible();
  await expect(page.getByText("3", { exact: true })).toBeVisible();

  const firstResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith("/api/control/v1/notifications/test"),
  );
  await page.getByRole("button", { name: "Test notification", exact: true }).click();
  const firstResponse = await firstResponsePromise;
  expect(firstResponse.status()).toBe(200);
  const firstRequest = firstResponse.request().postDataJSON() as Record<string, unknown>;
  expect(Object.keys(firstRequest).sort()).toEqual(["request_id", "schema_version"]);
  expect(firstRequest).not.toHaveProperty("message");
  expect(firstRequest).not.toHaveProperty("operator");
  expect(firstRequest).not.toHaveProperty("chat_id");
  const firstResult = (await firstResponse.json()) as {
    request_id: string;
    operator: string;
    replayed: boolean;
  };
  expect(firstResult.replayed).toBe(false);
  expect(firstResult.request_id).toBe(firstRequest.request_id);
  expect(firstResult.operator).toBe("control-plane:local-operator");
  await expect(page.getByText("Test notification accepted", { exact: true })).toBeVisible();

  const replayResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith("/api/control/v1/notifications/test"),
  );
  await page.getByRole("button", { name: "Replay same request safely", exact: true }).click();
  const replayResponse = await replayResponsePromise;
  expect(replayResponse.status()).toBe(200);
  const replayRequest = replayResponse.request().postDataJSON() as Record<string, unknown>;
  const replayResult = (await replayResponse.json()) as {
    request_id: string;
    operator: string;
    replayed: boolean;
  };
  expect(replayRequest).toEqual(firstRequest);
  expect(replayResult.request_id).toBe(firstResult.request_id);
  expect(replayResult.replayed).toBe(true);
  await expect(page.getByText("Owner replayed the accepted request", { exact: true })).toBeVisible();

  const body = page.locator("body");
  for (const forbidden of [
    "Module Strategy Signal Bot test notification",
    "PRIVATE-BOT-TOKEN",
    "PRIVATE-CHAT-ID",
    "/Users/private/signal-bot.sqlite",
  ]) {
    await expect(body).not.toContainText(forbidden);
  }
  await expect(page.getByLabel(/message/i)).toHaveCount(0);
  await expect(page.getByLabel(/chat/i)).toHaveCount(0);
  expect(await browserStorage(page)).toEqual({ local: 0, session: 0 });

  await page.reload();
  await expect(page.getByLabel("Control reader token")).toHaveValue("");
  await expect(page.getByLabel("Control operator token")).toHaveValue("");
  expect(await browserStorage(page)).toEqual({ local: 0, session: 0 });
});
