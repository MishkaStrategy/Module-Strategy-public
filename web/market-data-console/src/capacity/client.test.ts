import { afterEach, describe, expect, it, vi } from "vitest";

import { ControlApiClientError } from "../control/client";
import { SystemCapacityApiClient } from "./client";
import type {
  CapacityTierId,
  ExternalArtifactCapacitySnapshot,
  SystemCapacitySnapshot,
  SystemCapacityTier,
} from "./contracts";
import { isExternalArtifactCapacitySnapshot, isSystemCapacitySnapshot } from "./contracts";

afterEach(() => {
  vi.unstubAllGlobals();
});

const TIER_IDS: CapacityTierId[] = [
  "system_acceptance",
  "market_data_registry_storage",
  "market_data_application_control_plane_storage",
  "research_platform",
  "core_paper_trading_durable_state",
];

function tier(tierId: CapacityTierId): SystemCapacityTier {
  return {
    schema_version: 1,
    tier_id: tierId,
    kind: "measurement_bound",
    status: "UNKNOWN_DO_NOT_PRUNE",
    physical_free_disk: {
      schema_version: 1,
      status: "UNKNOWN_DO_NOT_PRUNE",
      high_water_free_bytes: null,
      critical_free_bytes: null,
    },
    deletion_authority: "NONE",
    next_publication_admission: tierId === "market_data_registry_storage",
    recovery_reserve_bytes: tierId === "market_data_registry_storage" ? 24_576 : null,
    owner_hard_limit_bytes: tierId === "system_acceptance" ? 1_073_741_824 : null,
    owner_payload_limit_bytes: tierId === "system_acceptance" ? 256_000 : null,
    measurements_aggregatable: false,
    measurements: [],
  };
}

function capacitySnapshot(): SystemCapacitySnapshot {
  return {
    schema_version: 1,
    observed_at: "2026-08-12T00:00:00Z",
    source_schema_version: 2,
    source_digest_sha256: "a".repeat(64),
    policy_status: "PARTIAL_MEASURED_FAIL_CLOSED",
    unknown_means: "NO_DESTRUCTIVE_OR_CAPACITY_CLAIM",
    global_local_disk_watermark_available: false,
    runner_watermarks: [
      {
        schema_version: 1,
        role: "fast",
        high_water_free_bytes: 2 * 1024 ** 3,
        critical_free_bytes: 1024 ** 3,
      },
      {
        schema_version: 1,
        role: "docker",
        high_water_free_bytes: 15 * 1024 ** 3,
        critical_free_bytes: 10 * 1024 ** 3,
      },
      {
        schema_version: 1,
        role: "macos",
        high_water_free_bytes: 12 * 1024 ** 3,
        critical_free_bytes: 8 * 1024 ** 3,
      },
      {
        schema_version: 1,
        role: "backtester",
        high_water_free_bytes: 30 * 1024 ** 3,
        critical_free_bytes: 20 * 1024 ** 3,
      },
    ],
    tiers: TIER_IDS.map(tier),
  };
}

function externalArtifactCapacity(): ExternalArtifactCapacitySnapshot {
  return {
    schema_version: 1,
    observed_at: "2026-08-12T00:00:00Z",
    source_schema_version: 2,
    source_digest_sha256: "b".repeat(64),
    kind: "external_quota_and_maintenance_bound",
    status: "PARTIAL_EXTERNAL_QUOTA",
    proactive_usage_status: "UNKNOWN_NO_ACCEPTED_USAGE_SOURCE",
    critical_signal: "CREATE_ARTIFACT_QUOTA_FAILURE",
    maintenance_max_count: 400,
    maintenance_max_bytes: 536_870_912,
    rate_limit_reserve_requests: 100,
    local_disk_watermark_applicable: false,
    operator_cleanup_available: false,
  };
}

describe("System Capacity contract", () => {
  it("accepts the bounded fail-closed projection", () => {
    expect(isSystemCapacitySnapshot(capacitySnapshot())).toBe(true);
  });

  it("rejects privacy-expanded top-level contracts and digest coercion", () => {
    expect(
      isSystemCapacitySnapshot({
        ...capacitySnapshot(),
        private_policy_path: "/private/policy.json",
      }),
    ).toBe(false);
    expect(
      isSystemCapacitySnapshot({
        ...capacitySnapshot(),
        source_digest_sha256: 1111111111111111111111111111111111111111111111111111111111111111,
      }),
    ).toBe(false);
  });

  it("rejects deletion authority escalation", () => {
    const snapshot = capacitySnapshot();
    const unsafe = {
      ...snapshot,
      tiers: snapshot.tiers.map((item, index) =>
        index === 0 ? { ...item, deletion_authority: "AUTO" } : item,
      ),
    };
    expect(isSystemCapacitySnapshot(unsafe)).toBe(false);
  });

  it("rejects partial physical thresholds and duplicate identities", () => {
    const snapshot = capacitySnapshot();
    const partial = {
      ...snapshot,
      tiers: snapshot.tiers.map((item, index) =>
        index === 0
          ? {
              ...item,
              physical_free_disk: {
                ...item.physical_free_disk,
                high_water_free_bytes: 10_000,
              },
            }
          : item,
      ),
    };
    expect(isSystemCapacitySnapshot(partial)).toBe(false);

    const duplicates = {
      ...snapshot,
      runner_watermarks: [
        snapshot.runner_watermarks[0],
        snapshot.runner_watermarks[0],
        snapshot.runner_watermarks[2],
        snapshot.runner_watermarks[3],
      ],
    };
    expect(isSystemCapacitySnapshot(duplicates)).toBe(false);
  });
});

describe("External artifact capacity contract", () => {
  it("accepts only unknown proactive usage with no cleanup authority", () => {
    expect(isExternalArtifactCapacitySnapshot(externalArtifactCapacity())).toBe(true);
    expect(
      isExternalArtifactCapacitySnapshot({
        ...externalArtifactCapacity(),
        proactive_usage_status: "KNOWN",
      }),
    ).toBe(false);
    expect(
      isExternalArtifactCapacitySnapshot({
        ...externalArtifactCapacity(),
        operator_cleanup_available: true,
      }),
    ).toBe(false);
  });

  it("rejects private or mutable transport expansion", () => {
    expect(
      isExternalArtifactCapacitySnapshot({
        ...externalArtifactCapacity(),
        cleanup_endpoint: "/api/delete-artifacts",
      }),
    ).toBe(false);
  });
});

describe("SystemCapacityApiClient", () => {
  it("uses the fixed reader-authenticated path without ambient credentials or cache", async () => {
    const signal = new AbortController().signal;
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(capacitySnapshot()), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const client = new SystemCapacityApiClient("", () => "reader-secret");

    const capacity = await client.getCapacity(signal);

    expect(capacity.policy_status).toBe("PARTIAL_MEASURED_FAIL_CLOSED");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/control/v1/system/capacity");
    expect(init.signal).toBe(signal);
    expect(init.cache).toBe("no-store");
    expect(init.credentials).toBe("omit");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer reader-secret");
  });

  it("reads external artifacts from a separate fixed non-mutating route", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(externalArtifactCapacity()), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new SystemCapacityApiClient("", () => "reader-secret");

    const capacity = await client.getExternalArtifactCapacity();

    expect(capacity.operator_cleanup_available).toBe(false);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/control/v1/system/capacity/artifacts");
    expect(init.method).toBe("GET");
    expect(init.cache).toBe("no-store");
    expect(init.credentials).toBe("omit");
  });

  it("rejects incompatible success payloads", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ ...capacitySnapshot(), hostname: "private-host" }), {
          status: 200,
        }),
      ),
    );
    const client = new SystemCapacityApiClient();

    await expect(client.getCapacity()).rejects.toMatchObject({
      code: "CONTROL_RESPONSE_INVALID",
      operation: "system_capacity",
    });
  });

  it("maps typed API errors without surfacing technical details", async () => {
    const secret = "must-not-render";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            schema_version: 1,
            error: {
              schema_version: 1,
              code: "CONTROL_SYSTEM_CAPACITY_UNAVAILABLE",
              message: "Storage capacity policy could not be projected safely.",
              action: "Verify policy attachment.",
              error_id: "ctrl_capacity_1",
              component: null,
              operation: "get_system_capacity",
              occurred_at: "2026-08-12T00:00:00Z",
              retryable: true,
              technical_details: { raw_secret: secret },
            },
          }),
          { status: 503 },
        ),
      ),
    );
    const client = new SystemCapacityApiClient();

    let captured: unknown;
    try {
      await client.getCapacity();
    } catch (error) {
      captured = error;
    }
    expect(captured).toBeInstanceOf(ControlApiClientError);
    expect(captured).toMatchObject({
      code: "CONTROL_SYSTEM_CAPACITY_UNAVAILABLE",
      errorId: "ctrl_capacity_1",
      retryable: true,
    });
    expect(String(captured)).not.toContain(secret);
  });
});
