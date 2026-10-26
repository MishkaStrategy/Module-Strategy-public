import { expect, test } from "@playwright/test";

const PAPER_RUNTIME_E2E = process.env.MODULAR_CONTROL_PAPER_E2E === "1";

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

async function browserStorage(page: import("@playwright/test").Page) {
  return page.evaluate(() => ({
    local: window.localStorage.length,
    session: window.sessionStorage.length,
  }));
}

async function confirmClick(
  page: import("@playwright/test").Page,
  button: RegExp,
): Promise<void> {
  page.once("dialog", async (dialog) => {
    expect(dialog.type()).toBe("confirm");
    expect(dialog.message()).toContain("authoritative PAPER runtime");
    await dialog.accept();
  });
  await page.getByRole("button", { name: button }).click();
}

async function receiptRequestId(page: import("@playwright/test").Page): Promise<string> {
  const text = await page.locator(".paper-lifecycle-receipt small").textContent();
  if (!text?.startsWith("request ")) throw new Error(`unexpected lifecycle receipt: ${text}`);
  return text.slice("request ".length);
}

test("controls the authoritative PAPER runtime through the real lifecycle owner", async ({ page }) => {
  test.skip(!PAPER_RUNTIME_E2E, "focused embedded Paper Runtime fixture is not configured");

  await page.goto("/#/paper");
  await expect(page.getByRole("heading", { name: "Paper Trading", exact: true })).toBeVisible();
  await page.getByLabel("Control reader token").fill(controlReaderToken());
  await page.getByLabel("Control operator token").fill(controlOperatorToken());

  const health = page.getByRole("region", { name: "Paper Runtime health" });
  await expect(health).toContainText("created");
  await expect(page.getByText("browser-paper-runtime", { exact: true })).toBeVisible();
  await expect(page.getByText("portfolio-1", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("browser-accepted-strategy", { exact: true })).toBeVisible();
  await expect(page.getByText("2/4096 · high 7", { exact: true })).toBeVisible();
  await expect(page.getByText("OWNER CONTROL", { exact: true })).toBeVisible();

  const start = page.getByRole("button", { name: /^Start$/i });
  await expect(start).toBeEnabled();
  await confirmClick(page, /^Start$/i);
  await expect(page.locator(".paper-lifecycle-receipt")).toContainText("START accepted");
  await expect(health).toContainText("running");
  const startRequestId = await receiptRequestId(page);

  const duplicateStart = await page.request.post("/api/control/v1/paper/start", {
    headers: { Authorization: `Bearer ${controlOperatorToken()}` },
    data: { schema_version: 1, request_id: startRequestId },
  });
  expect(duplicateStart.status()).toBe(200);
  const duplicateStartReceipt = (await duplicateStart.json()) as Record<string, unknown>;
  expect(duplicateStartReceipt.replayed).toBe(true);
  expect(duplicateStartReceipt.previous_state).toBe("created");
  expect(duplicateStartReceipt.new_state).toBe("running");
  await expect(start).toBeDisabled();

  await confirmClick(page, /activate kill switch/i);
  await expect(page.locator(".paper-lifecycle-receipt")).toContainText(
    "ACTIVATE_KILL_SWITCH accepted",
  );
  await expect(health).toContainText("kill_switch_active");
  await expect(health).toContainText("ACTIVE");
  const killRequestId = await receiptRequestId(page);

  const duplicateKill = await page.request.post("/api/control/v1/paper/kill-switch/activate", {
    headers: { Authorization: `Bearer ${controlOperatorToken()}` },
    data: { schema_version: 1, request_id: killRequestId },
  });
  expect(duplicateKill.status()).toBe(200);
  const firstKillReplay = (await duplicateKill.json()) as Record<string, unknown>;
  expect(firstKillReplay.replayed).toBe(true);
  expect(firstKillReplay.new_state).toBe("kill_switch_active");

  const secondDuplicateKill = await page.request.post(
    "/api/control/v1/paper/kill-switch/activate",
    {
      headers: { Authorization: `Bearer ${controlOperatorToken()}` },
      data: { schema_version: 1, request_id: killRequestId },
    },
  );
  expect(secondDuplicateKill.status()).toBe(200);
  const secondKillReplay = (await secondDuplicateKill.json()) as Record<string, unknown>;
  expect(secondKillReplay.replayed).toBe(true);
  expect(secondKillReplay.completed_at).toBe(firstKillReplay.completed_at);

  await expect(page.getByRole("button", { name: /activate kill switch/i })).toBeDisabled();
  await expect(page.getByRole("button", { name: /reset \/ recover/i })).toBeEnabled();
  await confirmClick(page, /reset \/ recover/i);
  await expect(page.locator(".paper-lifecycle-receipt")).toContainText(
    "RESET_KILL_SWITCH accepted",
  );
  await expect(health).toContainText("running");
  await expect(health).toContainText("inactive");

  await confirmClick(page, /^Stop$/i);
  await expect(page.locator(".paper-lifecycle-receipt")).toContainText("STOP accepted");
  await expect(health).toContainText("stopped");
  await expect(page.getByRole("button", { name: /^Stop$/i })).toBeDisabled();

  const orders = page.getByRole("list", { name: "Paper orders" });
  await expect(orders).toContainText("browser-paper-order");
  await expect(orders).toContainText("browser-core-order");
  await expect(page.getByText("Orders 1–100", { exact: true })).toBeVisible();
  const nextOrders = page.getByRole("button", { name: "Next", exact: true });
  const previousOrders = page.getByRole("button", { name: "Previous", exact: true });
  await expect(previousOrders).toBeDisabled();
  await expect(nextOrders).toBeEnabled();
  const orderOffsets: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/control/v1/paper/orders") {
      orderOffsets.push(url.searchParams.get("offset") ?? "");
    }
  });
  await nextOrders.click();
  await expect(page.getByText("Orders 101–101", { exact: true })).toBeVisible();
  await expect(orders).toContainText("browser-paper-order-100");
  await expect(nextOrders).toBeDisabled();
  await expect.poll(() => orderOffsets).toContain("100");
  await previousOrders.click();
  await expect(page.getByText("Orders 1–100", { exact: true })).toBeVisible();
  await expect(orders).toContainText("browser-paper-order");
  await expect.poll(() => orderOffsets.filter((offset) => offset === "0").length).toBeGreaterThan(0);
  await expect(orders).toContainText("0 / 0.010");
  await expect(page.getByText("portfolio-1", { exact: true }).last()).toBeVisible();
  await expect(page.getByText("BTCUSDT", { exact: true }).last()).toBeVisible();

  const body = page.locator("body");
  for (const forbidden of [
    controlReaderToken(),
    controlOperatorToken(),
    "BROWSER-PRIVATE-KILL-SWITCH-REASON",
    "/Users/private/paper-runtime.json",
    "BROWSER-PRIVATE-RECOVERY-PAYLOAD",
    "BROWSER-PRIVATE-ORDER-METADATA",
  ]) {
    await expect(body).not.toContainText(forbidden);
  }
  await expect(page.getByRole("button", { name: /live trading/i })).toHaveCount(0);
  expect(await browserStorage(page)).toEqual({ local: 0, session: 0 });

  await page.reload();
  await expect(page.getByLabel("Control reader token")).toHaveValue("");
  await expect(page.getByLabel("Control operator token")).toHaveValue("");
  await expect(page.getByRole("list", { name: "Paper orders" })).toHaveCount(0);
  expect(await browserStorage(page)).toEqual({ local: 0, session: 0 });
});

test("projects owner positions read-only and clears rows when reader auth changes", async ({ page }) => {
  test.skip(!PAPER_RUNTIME_E2E, "focused embedded Paper Runtime fixture is not configured");

  let authorizedReads = 0;
  await page.route("**/api/control/v1/paper/positions", async (route) => {
    const authorization = route.request().headers().authorization;
    if (authorization !== `Bearer ${controlReaderToken()}`) {
      await route.fulfill({ status: 401, json: { error: { code: "CONTROL_AUTH_REQUIRED" } } });
      return;
    }
    authorizedReads += 1;
    if (authorizedReads > 1) {
      await route.fulfill({
        status: 503,
        json: { error: { code: "CONTROL_PAPER_POSITIONS_UNAVAILABLE" } },
      });
      return;
    }
    await route.continue();
  });

  await page.goto("/#/paper");
  await page.getByLabel("Control reader token").fill(controlReaderToken());
  const positions = page.getByRole("list", { name: "PAPER positions" });
  await expect(positions).toContainText("owner-position-e2e");
  await expect(positions).toContainText("0.125000000000000001 contracts");
  await expect(positions).toContainText("Unknown");
  await expect(positions).toContainText("browser-strategy-2");
  await expect(page.getByText("browser-membership-3", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /close position|flatten position/i })).toHaveCount(0);
  await expect(
    page.getByText("The latest position refresh failed. The rows below are the last-known owner snapshot.", {
      exact: true,
    }),
  ).toBeVisible({ timeout: 12_000 });
  await expect(positions).toContainText("owner-position-e2e");

  await page.getByLabel("Control reader token").fill("invalid-reader-token");
  await expect(page.getByText("Owner positions could not be read safely.", { exact: true })).toBeVisible();
  await expect(positions).toHaveCount(0);
});

for (const [status, message] of [
  ["unavailable", "Positions unavailable (owner_not_ready)."],
  ["recovering", "Positions are recovering (owner_not_ready); no flat-state inference is available."],
  ["stale", "The owner marks this positions snapshot stale (owner_not_ready)."],
] as const) {
  test(`renders owner positions ${status} state without rows`, async ({ page }) => {
    test.skip(!PAPER_RUNTIME_E2E, "focused embedded Paper Runtime fixture is not configured");
    await page.route("**/api/control/v1/paper/positions", async (route) => {
      if (route.request().headers().authorization !== `Bearer ${controlReaderToken()}`) {
        await route.fulfill({ status: 401, json: { error: { code: "CONTROL_AUTH_REQUIRED" } } });
        return;
      }
      await route.fulfill({
        status: 200,
        json: { schema_version: 1, status, reason_code: "owner_not_ready" },
      });
    });
    await page.goto("/#/paper");
    await page.getByLabel("Control reader token").fill(controlReaderToken());
    await expect(page.getByRole("region", { name: "Positions" })).toContainText(message);
    await expect(page.getByRole("list", { name: "PAPER positions" })).toHaveCount(0);
    await expect(page.getByRole("article", { name: "Position snapshot status" })).toContainText(status);
  });
}

test("renders complete-empty as flat only for an available owner snapshot", async ({ page }) => {
  test.skip(!PAPER_RUNTIME_E2E, "focused embedded Paper Runtime fixture is not configured");
  await page.route("**/api/control/v1/paper/positions", async (route) => {
    if (route.request().headers().authorization !== `Bearer ${controlReaderToken()}`) {
      await route.fulfill({ status: 401, json: { error: { code: "CONTROL_AUTH_REQUIRED" } } });
      return;
    }
    await route.fulfill({
      status: 200,
      json: {
        schema_version: 1,
        status: "available",
        owner_id: "paper-execution",
        composition_id: "composition-1",
        portfolio_id: "portfolio-1",
        owner_generation: "generation-1",
        membership_version: "membership-1",
        snapshot_version: "snapshot-1",
        state_version: "state-1",
        as_of: "2026-10-23T05:00:00Z",
        coverage: "complete",
        positions: [],
      },
    });
  });
  await page.goto("/#/paper");
  await page.getByLabel("Control reader token").fill(controlReaderToken());
  await expect(page.getByText("No open positions.", { exact: true })).toBeVisible();
  await expect(page.getByRole("article", { name: "Position snapshot status" })).toContainText(
    "complete-empty",
  );
  await expect(page.getByRole("list", { name: "PAPER positions" })).toHaveCount(0);
});

test("reports blocked-scope read failure instead of claiming no scopes", async ({ page }) => {
  test.skip(!PAPER_RUNTIME_E2E, "focused embedded Paper Runtime fixture is not configured");

  await page.route("**/api/control/v1/paper/blocked-scopes", async (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: "fixture projection unavailable" }),
    }),
  );
  await page.goto("/#/paper");
  await expect(page.getByRole("heading", { name: "Paper Trading", exact: true })).toBeVisible();
  await page.getByLabel("Control reader token").fill(controlReaderToken());

  await expect(page.getByRole("alert")).toContainText("Blocked scopes could not be read safely.");
  await expect(
    page.getByText("No blocked Paper Execution scopes are reported.", { exact: true }),
  ).toHaveCount(0);
});

test("can return from a failed orders page to the previous page", async ({ page }) => {
  test.skip(!PAPER_RUNTIME_E2E, "focused embedded Paper Runtime fixture is not configured");

  const orderOffsets: string[] = [];
  let failSecondPage = true;
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/control/v1/paper/orders") {
      orderOffsets.push(url.searchParams.get("offset") ?? "");
    }
  });
  await page.route("**/api/control/v1/paper/orders?*", async (route) => {
    const url = new URL(route.request().url());
    if (url.searchParams.get("offset") === "100" && failSecondPage) {
      failSecondPage = false;
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "fixture page unavailable" }),
      });
      return;
    }
    const offset = Number(url.searchParams.get("offset") ?? "0");
    const orders = Array.from({ length: 100 }, (_, index) => ({
      schema_version: 1,
      client_order_id: `fixture-order-${offset + index}`,
      core_order_id: `fixture-core-order-${offset + index}`,
      portfolio_id: "fixture-portfolio",
      instrument: "BTCUSDT",
      intent_role: "entry",
      native_state: "open",
      requested_quantity: "1",
      filled_quantity: "0",
      remaining_quantity: "1",
      average_fill_price: null,
      fees_paid: "0",
      last_sequence: offset + index,
      last_event_timestamp: null,
    }));
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: 1,
        orders,
        limit: 100,
        offset,
        has_more: true,
      }),
    });
  });

  await page.goto("/#/paper");
  await page.getByLabel("Control reader token").fill(controlReaderToken());
  await expect(page.getByText("Orders 1–100", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Order projection unavailable." })).toBeVisible();
  const previous = page.getByRole("button", { name: "Previous", exact: true });
  await expect(previous).toBeEnabled();
  await previous.click();
  await expect(page.getByText("Orders 1–100", { exact: true })).toBeVisible();
  await expect(page.getByRole("alert").filter({ hasText: "Order projection unavailable." })).toHaveCount(0);
  await expect.poll(() => orderOffsets).toContain("0");
});

test("marks cached orders stale when a refresh fails", async ({ page }) => {
  test.skip(!PAPER_RUNTIME_E2E, "focused embedded Paper Runtime fixture is not configured");

  let orderReads = 0;
  await page.route("**/api/control/v1/paper/orders?*", async (route) => {
    if (route.request().headers().authorization !== `Bearer ${controlReaderToken()}`) {
      await route.continue();
      return;
    }
    orderReads += 1;
    if (orderReads > 1) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "fixture refresh unavailable" }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: 1,
        orders: [
          {
            schema_version: 1,
            client_order_id: "cached-order-001",
            core_order_id: "cached-core-order-001",
            portfolio_id: "fixture-portfolio",
            instrument: "BTCUSDT",
            intent_role: "entry",
            native_state: "open",
            requested_quantity: "1",
            filled_quantity: "0",
            remaining_quantity: "1",
            average_fill_price: null,
            fees_paid: "0",
            last_sequence: 1,
            last_event_timestamp: null,
          },
        ],
        limit: 100,
        offset: 0,
        has_more: false,
      }),
    });
  });

  await page.goto("/#/paper");
  await page.getByLabel("Control reader token").fill(controlReaderToken());
  const orders = page.getByRole("list", { name: "Paper orders" });
  await expect(orders).toContainText("cached-order-001");
  await expect(page.getByText("Showing the last successful order projection; these rows may be stale.")).toHaveCount(0);

  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Order projection unavailable." })).toBeVisible();
  await expect(
    page.getByText("Showing the last successful order projection; these rows may be stale.", { exact: true }),
  ).toBeVisible();
  await expect(orders).toContainText("cached-order-001");
  expect(orderReads).toBeGreaterThan(1);
});

test("locks lifecycle controls when a runtime refresh fails with a cached snapshot", async ({ page }) => {
  test.skip(!PAPER_RUNTIME_E2E, "focused embedded Paper Runtime fixture is not configured");

  let failRuntimeRefresh = false;
  let runtimeReads = 0;
  const lifecyclePosts: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().includes("/api/control/v1/paper/")) {
      lifecyclePosts.push(request.url());
    }
  });
  await page.route("**/api/control/v1/paper/runtime", async (route) => {
    runtimeReads += 1;
    if (failRuntimeRefresh) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "fixture runtime projection unavailable" }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: 1,
        runtime_id: "fixture-runtime",
        portfolio_id: "fixture-portfolio",
        strategy_id: "fixture-strategy",
        execution_mode: "paper",
        native_state: "running",
        base_timeframe: "5m",
        market: "usdt_perpetual",
        instruments: ["BTCUSDT"],
        configuration_hash: "a".repeat(64),
        kill_switch: {
          schema_version: 1,
          active: true,
          scope: "global",
          activated_at: "2026-10-21T00:00:00Z",
          cancellation_pending: false,
        },
        health_ready: true,
        queue_depth: 0,
        queue_capacity: 100,
        queue_high_water_mark: 0,
        paper_execution_lifecycle: "ready",
        order_count: 0,
        active_order_count: 0,
        blocked_scope_count: 0,
        position_projection_available: false,
        lifecycle_mutations_available: false,
      }),
    });
  });
  await page.route("**/api/control/v1/paper/lifecycle-capability", async (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ schema_version: 1, available: true, paper_only: true }),
    }),
  );
  await page.route("**/api/control/v1/paper/stop", async (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({
        error: {
          code: "CONTROL_PAPER_RUNTIME_OWNER_UNAVAILABLE",
          message: "The PAPER lifecycle owner is temporarily unavailable.",
          action: "Retry the same lifecycle request.",
          error_id: "fixture_retryable_stop_failure",
          component: "paper_runtime",
          operation: "stop_paper_runtime",
          occurred_at: "2026-10-21T00:00:00Z",
          retryable: true,
          technical_details: {},
        },
      }),
    }),
  );

  await page.goto("/#/paper");
  await page.getByLabel("Control reader token").fill(controlReaderToken());
  await page.getByLabel("Control operator token").fill(controlOperatorToken());
  await expect(page.getByText("fixture-runtime", { exact: true })).toBeVisible();
  const stop = page.getByRole("button", { name: /^Stop$/i });
  const reset = page.getByRole("button", { name: /reset \/ recover/i });
  await expect(stop).toBeEnabled();
  await expect(reset).toBeEnabled();
  await confirmClick(page, /^Stop$/i);
  await expect(
    page.getByRole("alert").filter({ hasText: "CONTROL_PAPER_RUNTIME_OWNER_UNAVAILABLE" }),
  ).toBeVisible();
  const retry = page.getByRole("button", { name: "Retry same idempotency request", exact: true });
  await expect(retry).toBeEnabled();
  expect(lifecyclePosts).toHaveLength(1);

  failRuntimeRefresh = true;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(
    page
      .getByRole("alert")
      .filter({ hasText: "Showing the last successful snapshot; lifecycle controls are locked" }),
  ).toBeVisible();
  await expect(page.getByText("READ ONLY", { exact: true })).toBeVisible();
  await expect(page.getByText("OWNER CONTROL", { exact: true })).toHaveCount(0);
  await expect(page.getByText("read-only", { exact: true })).toBeVisible();
  await expect(page.getByText("owner-backed", { exact: true })).toHaveCount(0);
  await expect(stop).toBeDisabled();
  await expect(reset).toBeDisabled();
  await expect(retry).toBeDisabled();
  expect(runtimeReads).toBeGreaterThanOrEqual(2);
  expect(lifecyclePosts).toHaveLength(1);
});

test("locks lifecycle controls when cached capability cannot be refreshed", async ({ page }) => {
  test.skip(!PAPER_RUNTIME_E2E, "focused embedded Paper Runtime fixture is not configured");

  let failCapabilityRefresh = false;
  let capabilityReads = 0;
  const lifecyclePosts: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().includes("/api/control/v1/paper/")) {
      lifecyclePosts.push(request.url());
    }
  });
  await page.route("**/api/control/v1/paper/runtime", async (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: 1,
        runtime_id: "fixture-runtime",
        portfolio_id: "fixture-portfolio",
        strategy_id: "fixture-strategy",
        execution_mode: "paper",
        native_state: "running",
        base_timeframe: "5m",
        market: "usdt_perpetual",
        instruments: ["BTCUSDT"],
        configuration_hash: "b".repeat(64),
        kill_switch: {
          schema_version: 1,
          active: true,
          scope: "global",
          activated_at: "2026-10-21T00:00:00Z",
          cancellation_pending: false,
        },
        health_ready: true,
        queue_depth: 0,
        queue_capacity: 100,
        queue_high_water_mark: 0,
        paper_execution_lifecycle: "ready",
        order_count: 0,
        active_order_count: 0,
        blocked_scope_count: 0,
        position_projection_available: false,
        lifecycle_mutations_available: false,
      }),
    }),
  );
  await page.route("**/api/control/v1/paper/lifecycle-capability", async (route) => {
    capabilityReads += 1;
    if (failCapabilityRefresh) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "fixture lifecycle capability unavailable" }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ schema_version: 1, available: true, paper_only: true }),
    });
  });
  await page.route("**/api/control/v1/paper/stop", async (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({
        error: {
          code: "CONTROL_PAPER_RUNTIME_OWNER_UNAVAILABLE",
          message: "The PAPER lifecycle owner is temporarily unavailable.",
          action: "Retry the same lifecycle request.",
          error_id: "fixture_retryable_capability_failure",
          component: "paper_runtime",
          operation: "stop_paper_runtime",
          occurred_at: "2026-10-21T00:00:00Z",
          retryable: true,
          technical_details: {},
        },
      }),
    }),
  );

  await page.goto("/#/paper");
  await page.getByLabel("Control reader token").fill(controlReaderToken());
  await page.getByLabel("Control operator token").fill(controlOperatorToken());
  await expect(page.getByText("fixture-runtime", { exact: true })).toBeVisible();
  await expect(page.getByText("OWNER CONTROL", { exact: true })).toBeVisible();
  const stop = page.getByRole("button", { name: /^Stop$/i });
  const reset = page.getByRole("button", { name: /reset \/ recover/i });
  await expect(stop).toBeEnabled();
  await expect(reset).toBeEnabled();
  await confirmClick(page, /^Stop$/i);
  await expect(
    page.getByRole("alert").filter({ hasText: "CONTROL_PAPER_RUNTIME_OWNER_UNAVAILABLE" }),
  ).toBeVisible();
  const retry = page.getByRole("button", { name: "Retry same idempotency request", exact: true });
  await expect(retry).toBeEnabled();
  expect(lifecyclePosts).toHaveLength(1);

  const readsBeforeRefresh = capabilityReads;
  failCapabilityRefresh = true;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect.poll(() => capabilityReads).toBeGreaterThan(readsBeforeRefresh);
  await expect(
    page
      .getByRole("alert")
      .filter({ hasText: "The last-known lifecycle capability is stale; controls are read-only" }),
  ).toBeVisible();
  await expect(page.getByText("READ ONLY", { exact: true })).toBeVisible();
  await expect(page.getByText("OWNER CONTROL", { exact: true })).toHaveCount(0);
  await expect(stop).toBeDisabled();
  await expect(reset).toBeDisabled();
  await expect(retry).toBeDisabled();
  const readsAfterManualRefresh = capabilityReads;
  await expect
    .poll(() => capabilityReads, { timeout: 15_000 })
    .toBeGreaterThan(readsAfterManualRefresh);
  await expect(
    page
      .getByRole("alert")
      .filter({ hasText: "The last-known lifecycle capability is stale; controls are read-only" }),
  ).toBeVisible();
  await expect(page.getByText("READ ONLY", { exact: true })).toBeVisible();
  await expect(retry).toBeDisabled();
  expect(lifecyclePosts).toHaveLength(1);
});
