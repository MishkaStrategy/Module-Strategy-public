import { expect, test } from "@playwright/test";

const ACCEPTANCE_E2E = process.env.MODULAR_ACCEPTANCE_E2E === "1";

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

test("keeps durable System Acceptance owner truth across browser reload and request replay", async ({
  page,
  request,
}) => {
  test.skip(!ACCEPTANCE_E2E, "real System Acceptance owner fixture is not configured");

  const readerToken = requiredEnvironment("MODULAR_CONTROL_E2E_READER_TOKEN");
  const operatorToken = requiredEnvironment("MODULAR_CONTROL_E2E_OPERATOR_TOKEN");
  const controlPort = requiredEnvironment("MODULAR_CONTROL_E2E_PORT");
  const pinnedFullRc = requiredEnvironment("MODULAR_ACCEPTANCE_E2E_FULL_RC_SHA");

  await page.goto("/#/acceptance");
  await expect(page.getByRole("heading", { name: "System Acceptance" })).toBeVisible();
  await page.getByLabel("Control reader token").fill(readerToken);
  await page.getByLabel("Control operator token").fill(operatorToken);

  await page.getByLabel("Dataset ID").fill("browser-dataset-v1");
  await page.getByLabel("Dataset SHA-256").fill("b".repeat(64));
  await page.getByLabel("Strategy ID").fill("browser-strategy-v1");
  await page.getByLabel("Strategy SHA-256").fill("c".repeat(64));
  await page.getByLabel("Config SHA-256").fill("d".repeat(64));

  const startResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith("/api/control/v1/acceptance/start"),
  );
  await page.getByRole("button", { name: "Start durable acceptance" }).click();
  const startResponse = await startResponsePromise;
  expect(startResponse.ok()).toBe(true);

  const startPayload = startResponse.request().postDataJSON() as Record<string, unknown>;
  expect(startPayload).not.toHaveProperty("full_rc_sha");
  expect(startPayload).not.toHaveProperty("mode");
  const startResult = (await startResponse.json()) as {
    acceptance_run_id: string;
    request_id: string;
    replayed: boolean;
  };
  expect(startResult.replayed).toBe(false);
  expect(startResult.request_id).toBe(startPayload.request_id);

  await expect(page.getByText(startResult.acceptance_run_id, { exact: true }).first()).toBeVisible();
  await expect(page.getByText(pinnedFullRc, { exact: true })).toBeVisible();
  expect(await browserStorage(page)).toEqual({ local: 0, session: 0 });

  const replay = await request.post(
    `http://127.0.0.1:${controlPort}/api/control/v1/acceptance/start`,
    {
      headers: { Authorization: `Bearer ${operatorToken}` },
      data: startPayload,
    },
  );
  expect(replay.status()).toBe(200);
  const replayResult = (await replay.json()) as {
    acceptance_run_id: string;
    request_id: string;
    replayed: boolean;
  };
  expect(replayResult).toEqual(
    expect.objectContaining({
      acceptance_run_id: startResult.acceptance_run_id,
      request_id: startResult.request_id,
      replayed: true,
    }),
  );

  await page.reload();
  await expect(page.getByLabel("Control reader token")).toHaveValue("");
  await expect(page.getByLabel("Control operator token")).toHaveValue("");
  await page.getByLabel("Control reader token").fill(readerToken);

  await expect(page.getByRole("heading", { name: "Current run" })).toBeVisible();
  await expect(page.getByText(startResult.acceptance_run_id, { exact: true }).first()).toBeVisible();
  await expect(page.getByText(pinnedFullRc, { exact: true })).toBeVisible();
  expect(await browserStorage(page)).toEqual({ local: 0, session: 0 });
});
