import { expect, test } from "@playwright/test";

const AUDIT_ID = "audit-browser-evidence-1";
const CANDIDATE_ID = "candidate-browser-evidence";
const RESULTS_EVIDENCE_ID = "browser-results-evidence";
const EVIDENCE_STATE_CONFIGURED = Boolean(
  process.env.MODULAR_CONTROL_STRATEGY_AUDIT_STATE_PATH ||
    process.env.MODULAR_CONTROL_RESULTS_REGISTRY_PATH,
);

function controlReaderToken(): string {
  const value = process.env.MODULAR_CONTROL_E2E_READER_TOKEN;
  if (!value) throw new Error("MODULAR_CONTROL_E2E_READER_TOKEN is required");
  return value;
}

async function browserStorage(page: import("@playwright/test").Page) {
  return page.evaluate(() => ({
    local: window.localStorage.length,
    session: window.sessionStorage.length,
  }));
}

test("reads Evidence provenance without mutation and preserves the cryptographic boundary", async ({
  page,
}) => {
  test.skip(!EVIDENCE_STATE_CONFIGURED, "Evidence durable-state fixtures are not configured");

  await page.goto("/#/evidence");
  await expect(page.getByRole("heading", { name: "Evidence", exact: true })).toBeVisible();
  await page.getByLabel("Control reader token").fill(controlReaderToken());

  const health = page.getByRole("region", { name: "Evidence health" });
  await expect(health).toContainText("HEALTHY");
  await expect(health).toContainText("configured");

  await expect(page.getByText("hash chain verified", { exact: true })).toBeVisible();
  await expect(page.getByText("no · auditor key not held", { exact: true })).toBeVisible();

  const decisions = page.getByRole("list", { name: "Audit decisions" });
  await expect(decisions).toContainText(AUDIT_ID);
  await expect(decisions).toContainText(CANDIDATE_ID);
  await expect(decisions).toContainText("APPROVED_FOR_PAPER");
  await expect(decisions).toContainText("PAPER_ADMISSION_APPROVED");

  const auditEvidence = page.getByRole("list", { name: "Audit evidence" });
  await expect(auditEvidence).toContainText(CANDIDATE_ID);
  await expect(auditEvidence).toContainText("content verified");

  const resultsEvidence = page.getByRole("list", { name: "Results evidence" });
  await expect(resultsEvidence).toContainText(RESULTS_EVIDENCE_ID);
  await expect(resultsEvidence).toContainText("PASS · INFO");
  await expect(resultsEvidence).toContainText("results-aggregator · 0.1.0.dev0");
  await expect(resultsEvidence).toContainText("0 runs · 0 artifacts");
  await expect(resultsEvidence).toContainText("1 limitation(s); raw text is not projected.");

  const body = page.locator("body");
  for (const forbidden of [
    "BROWSER-AUDIT-SECRET-DO-NOT-PROJECT",
    "/Users/private/browser-audit-evidence",
    "BROWSER-RESULTS-SECRET-DO-NOT-PROJECT",
    "/Users/private/browser-results-evidence",
    "BROWSER-RESULTS-LIMITATION-DO-NOT-PROJECT",
    "hmac-sha256:",
    "payload_json",
    "private_secret",
  ]) {
    await expect(body).not.toContainText(forbidden);
  }

  for (const mutation of [/approve/i, /reject/i, /re-audit/i, /register/i, /annotate/i, /delete/i]) {
    await expect(page.getByRole("button", { name: mutation })).toHaveCount(0);
  }

  await expect(page.getByRole("link", { name: "Acceptance", exact: true })).toBeVisible();
  expect(await browserStorage(page)).toEqual({ local: 0, session: 0 });

  await page.reload();
  await expect(page.getByLabel("Control reader token")).toHaveValue("");
  await expect(page.getByRole("list", { name: "Audit decisions" })).toHaveCount(0);
  await expect(page.getByRole("list", { name: "Results evidence" })).toHaveCount(0);

  await page.getByLabel("Control reader token").fill(controlReaderToken());
  await expect(page.getByRole("list", { name: "Audit decisions" })).toContainText(AUDIT_ID);
  await expect(page.getByRole("list", { name: "Results evidence" })).toContainText(
    RESULTS_EVIDENCE_ID,
  );
  expect(await browserStorage(page)).toEqual({ local: 0, session: 0 });
});
