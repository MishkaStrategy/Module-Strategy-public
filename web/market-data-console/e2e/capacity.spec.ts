import { expect, test } from "@playwright/test";

const enabled = process.env.MODULAR_CAPACITY_E2E === "1";

test.describe("System Capacity · owner-backed policy", () => {
  test.skip(!enabled, "focused System Capacity owner E2E only");

  test("keeps resource, owner capacity and external quota semantics separate", async ({ page }) => {
    const readerToken = process.env.MODULAR_CAPACITY_E2E_READER_TOKEN;
    if (!readerToken) throw new Error("MODULAR_CAPACITY_E2E_READER_TOKEN is required");

    await page.goto("/#/system/resources");
    await page.getByLabel("Control reader token").fill(readerToken);

    await expect(page.getByRole("heading", { name: "System Resources" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Storage capacity policy" })).toBeVisible();
    await expect(page.getByText("No global local-disk watermark")).toBeVisible();
    await expect(page.getByText(/PARTIAL_MEASURED_FAIL_CLOSED/)).toBeVisible();
    await expect(page.getByText(/NO_DESTRUCTIVE_OR_CAPACITY_CLAIM/)).toBeVisible();
    await expect(page.getByText("System Acceptance")).toBeVisible();
    await expect(page.getByText("Market Data registry")).toBeVisible();
    await expect(page.getByText("Core / Paper / Trading")).toBeVisible();
    await expect(page.getByText("UNKNOWN", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("NONE", { exact: true }).first()).toBeVisible();
    await expect(page.getByText(/Owner admission accepted/)).toBeVisible();

    await expect(page.getByRole("heading", { name: "External CI artifact quota" })).toBeVisible();
    const artifactCard = page
      .locator("article.component-health-card")
      .filter({ hasText: "GitHub Actions artifacts" });
    await expect(artifactCard).toBeVisible();
    await expect(artifactCard.getByText("PARTIAL_EXTERNAL_QUOTA", { exact: true })).toBeVisible();
    await expect(
      artifactCard.getByText("UNKNOWN_NO_ACCEPTED_USAGE_SOURCE", { exact: true }),
    ).toBeVisible();
    await expect(
      artifactCard.getByText("CREATE_ARTIFACT_QUOTA_FAILURE", { exact: true }),
    ).toBeVisible();
    await expect(artifactCard.getByText("NOT APPLICABLE", { exact: true })).toBeVisible();
    await expect(artifactCard.getByText("UNAVAILABLE", { exact: true })).toBeVisible();
    await expect(artifactCard.getByText(/Maintenance ceiling is a safety bound/)).toBeVisible();

    const body = await page.locator("body").innerText();
    expect(body).not.toContain("MODULAR_CONTROL_STORAGE_PRESSURE_POLICY_PATH");
    expect(body).not.toContain("STORAGE_PRESSURE_THRESHOLDS.json");
    expect(body).not.toContain("selected_full_rc_persistent_backend");
    expect(body).not.toContain("actions_artifact_prune_batch");
    expect(body.toLowerCase()).not.toContain("time-to-full");
  });
});
