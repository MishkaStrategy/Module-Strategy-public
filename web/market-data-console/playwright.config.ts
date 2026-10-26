import { defineConfig } from "@playwright/test";

const baseURL = process.env.MDI_E2E_BASE_URL ?? "http://127.0.0.1:4173";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "*.spec.ts",
  timeout: 45_000,
  expect: { timeout: 12_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: "line",
  outputDir: "test-results",
  use: {
    baseURL,
    headless: true,
    locale: "ru-RU",
    timezoneId: "UTC",
    screenshot: "only-on-failure",
    trace: "off",
    video: "off",
  },
});
