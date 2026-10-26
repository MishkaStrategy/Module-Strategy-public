import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import DashboardApp from "../dashboard/DashboardApp";
import { NAVIGATION, isNavigationItemActive } from "../dashboard/navigation";
import { RecoveryClientError, RecoveryControlApiClient } from "./client";
import {
  CapabilitySummary,
  RecoveryContent,
  RecoveryFailure,
  ResolutionFailure,
} from "./RecoveryPage";
import type { RecoveryCapability, RecoveryResolutionRequest } from "./contracts";

const CAPABILITY: RecoveryCapability = {
  status: "RESOLUTION_ONLY",
  resolution_enabled: true,
  profile_catalog_configured: true,
  owner_readiness_source_configured: true,
  recovery_authorization_policy_configured: true,
  backup_package_proposal_source_configured: true,
  existing_package_evidence_source_configured: true,
  durable_intent_store_configured: false,
  recovery_effects_enabled: false,
  execution_admission_enabled: false,
};

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("Recovery capability UI", () => {
  it("shows default not-configured as unknown, without implying an empty catalog", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      error: { code: "CONTROL_RECOVERY_NOT_CONFIGURED", message: "Recovery is not configured." },
    }, 503)));
    const api = new RecoveryControlApiClient();
    let failure: unknown;
    try {
      await api.capability();
    } catch (error) {
      failure = error;
    }

    expect(failure).toBeInstanceOf(RecoveryClientError);
    expect(failure).toMatchObject({ code: "CONTROL_RECOVERY_NOT_CONFIGURED", status: 503 });
    const html = renderToStaticMarkup(createElement(RecoveryFailure, { error: failure }));
    expect(html).toContain("Not configured");
    expect(html).toContain("readiness and the profile catalog are unknown");
    expect(html).not.toContain("No profiles");
  });

  it("reads authenticated capability flags via GET and renders them as configuration only", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe("/api/control/v1/recovery/capability");
      expect(init?.method).toBe("GET");
      expect(new Headers(init?.headers).get("authorization")).toBe("Bearer reader-token");
      expect(init?.cache).toBe("no-store");
      expect(init?.credentials).toBe("omit");
      return jsonResponse(CAPABILITY);
    });
    vi.stubGlobal("fetch", fetchMock);

    const capability = await new RecoveryControlApiClient(
      "",
      () => "reader-token",
      () => "operator-token",
    ).capability();
    const html = renderToStaticMarkup(createElement(CapabilitySummary, { capability }));
    expect(html).toContain("RESOLUTION_ONLY");
    expect(html).toContain("Profile catalog configured");
    expect(html).toContain("Durable intent store configured");
    expect(html).toContain("Effectful recovery actions are unavailable");
    expect(html).toContain("does not provide backup, verification, restore, or execution");
    expect(html).toContain("Configuration only");
    expect(html).toContain("does not report profile readiness or provide a profile inventory");
    expect(html).not.toContain("profile_id");
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("submits a backup resolution preview using only the operator token", async () => {
    const request: RecoveryResolutionRequest = {
      operation: "backup",
      request_id: "request_01",
      profile_id: "profile_main",
    };
    const response = {
      status: "RESOLUTION_ONLY",
      operation: "backup",
      fingerprint: "b".repeat(64),
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe("/api/control/v1/recovery/requests");
      expect(init?.method).toBe("POST");
      expect(new Headers(init?.headers).get("authorization")).toBe("Bearer operator-token");
      expect(new Headers(init?.headers).get("content-type")).toBe("application/json");
      expect(init?.body).toBe(JSON.stringify(request));
      expect(init?.cache).toBe("no-store");
      expect(init?.credentials).toBe("omit");
      return jsonResponse(response);
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await new RecoveryControlApiClient(
      "",
      () => "reader-token",
      () => "operator-token",
    ).resolveRequest(request);

    expect(result).toEqual(response);
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it.each(["verify", "restore"] as const)(
    "%s preview sends the required opaque package ID",
    async (operation) => {
      const request: RecoveryResolutionRequest = {
        operation,
        request_id: "request_02",
        profile_id: "profile_main",
        package_id: "package_42",
      };
      vi.stubGlobal("fetch", vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        expect(JSON.parse(String(init?.body))).toEqual(request);
        return jsonResponse({
          status: "RESOLUTION_ONLY",
          operation,
          fingerprint: "c".repeat(64),
        });
      }));
      const client = new RecoveryControlApiClient("", () => "reader", () => "operator");
      await expect(client.resolveRequest(request)).resolves.toMatchObject({ operation });
    },
  );

  it("rejects invalid identifiers and package combinations before network access", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const client = new RecoveryControlApiClient("", () => "reader", () => "operator");
    await expect(client.resolveRequest({
      operation: "backup",
      request_id: "../private",
      profile_id: "profile_main",
    })).rejects.toThrow("Recovery identifiers are invalid");
    await expect(client.resolveRequest({
      operation: "verify",
      request_id: "request_03",
      profile_id: "profile_main",
    })).rejects.toThrow("Verify and restore require a valid package ID");
    await expect(client.resolveRequest({
      operation: "backup",
      request_id: "request_04",
      profile_id: "profile_main",
      package_id: "package_42",
    })).rejects.toThrow("Backup cannot specify a package ID");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects a response that is not the exact resolution-only contract", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      status: "EXECUTED",
      operation: "backup",
      fingerprint: "d".repeat(64),
    })));
    const client = new RecoveryControlApiClient("", () => "reader", () => "operator");
    await expect(client.resolveRequest({
      operation: "backup",
      request_id: "request_05",
      profile_id: "profile_main",
    })).rejects.toMatchObject({ code: "CONTROL_RECOVERY_CONTRACT_INVALID" });
  });

  it("sanitizes resolution endpoint errors and never renders server details", async () => {
    const privatePath = "/srv/private/recovery/manifest.json";
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      error: {
        code: "CONTROL_RECOVERY_INTERNAL",
        message: `Failed reading ${privatePath}`,
        detail: privatePath,
      },
    }, 500)));
    const client = new RecoveryControlApiClient("", () => "reader", () => "operator");
    let failure: unknown;
    try {
      await client.resolveRequest({
        operation: "backup",
        request_id: "request_06",
        profile_id: "profile_main",
      });
    } catch (error) {
      failure = error;
    }
    expect(failure).toMatchObject({ message: "Recovery resolution request failed." });
    const html = renderToStaticMarkup(createElement(ResolutionFailure, { error: failure }));
    expect(html).not.toContain(privatePath);
  });

  it("offers only a manual resolution preview when configured and stays inert otherwise", () => {
    const api = new RecoveryControlApiClient();
    const queryClient = new QueryClient();
    const unconfiguredHtml = renderToStaticMarkup(createElement(
      QueryClientProvider,
      { client: queryClient },
      createElement(RecoveryContent, {
        capability: { ...CAPABILITY, status: "NOT_CONFIGURED", resolution_enabled: false },
        api,
        operatorReady: true,
      }),
    ));
    expect(unconfiguredHtml).not.toContain('aria-label="Recovery resolution preview"');

    const configuredHtml = renderToStaticMarkup(createElement(
      QueryClientProvider,
      { client: queryClient },
      createElement(RecoveryContent, {
        capability: CAPABILITY,
        api,
        operatorReady: false,
      }),
    ));
    expect(configuredHtml).toContain('aria-label="Recovery resolution preview"');
    expect(configuredHtml).toContain("Preview only");
    expect(configuredHtml).toContain("Request ID");
    expect(configuredHtml).toContain("Profile ID");
    expect(configuredHtml).toContain("does not create a backup");
    expect(configuredHtml).toContain("disabled=");
    expect(configuredHtml).not.toContain("No profiles");
    queryClient.clear();
  });

  it.each([401, 403])("preserves HTTP %s as an authorization error", async (status) => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      error: { code: "CONTROL_READER_FORBIDDEN", message: "Reader authorization failed." },
    }, status)));
    const api = new RecoveryControlApiClient();
    await expect(api.capability()).rejects.toMatchObject({ status });
    let failure: unknown;
    try {
      await api.capability();
    } catch (error) {
      failure = error;
    }
    const html = renderToStaticMarkup(createElement(RecoveryFailure, { error: failure }));
    expect(html).toContain(`HTTP ${status}`);
    expect(html).toContain("authorization failed");
    expect(html).not.toContain("Not configured");
  });

  it("registers an active Recovery route and renders the truthful capability view", () => {
    const recovery = NAVIGATION.flatMap((group) => group.items).find(
      (item) => item.path === "/recovery",
    );
    expect(recovery?.available).toBe(true);
    expect(recovery && isNavigationItemActive(recovery, "/recovery")).toBe(true);
    const html = renderToStaticMarkup(createElement(CapabilitySummary, { capability: CAPABILITY }));
    expect(html).toContain("Recovery capability configuration");
    expect(html).toContain("profile inventory");
  });

  it("renders Recovery through the DashboardApp route", () => {
    vi.stubGlobal("window", { location: { hash: "#/recovery" } });
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const html = renderToStaticMarkup(createElement(
      QueryClientProvider,
      { client: queryClient },
      createElement(DashboardApp),
    ));
    expect(html).toContain('aria-label="Recovery workspace"');
    expect(html).toContain("Recovery</h2>");
    expect(html).toContain('href="#/recovery"');
    queryClient.clear();
  });

  it("does not render server-supplied private details for other failures", async () => {
    const privatePath = "/srv/private/recovery/backup-key.json";
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      error: {
        code: "CONTROL_RECOVERY_INTERNAL",
        message: `Failed opening ${privatePath}`,
        detail: privatePath,
      },
    }, 500)));
    let failure: unknown;
    try {
      await new RecoveryControlApiClient().capability();
    } catch (error) {
      failure = error;
    }
    const html = renderToStaticMarkup(createElement(RecoveryFailure, { error: failure }));
    expect(html).toContain("Recovery capability request failed (HTTP 500, CONTROL_RECOVERY_INTERNAL)");
    expect(html).not.toContain(privatePath);
    expect(failure).toMatchObject({ message: "Recovery capability request failed." });
  });
});
