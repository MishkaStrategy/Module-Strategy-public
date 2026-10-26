import { afterEach, describe, expect, it, vi } from "vitest";

import { EvidenceControlApiClient, EvidenceControlClientError } from "./client";

const jsonResponse = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });

const overview = {
  schema_version: 1,
  audit_configured: true,
  results_registry_configured: true,
  audit_integrity: {
    schema_version: 1,
    evidence_count: 1,
    decision_count: 1,
    chain_head: "a".repeat(64),
    hash_chain_verified: true,
    hmac_signatures_reverified: false,
  },
  results_registry_revision: 4,
  results_evidence_count: 2,
};

const decisionList = {
  schema_version: 1,
  decisions: [
    {
      schema_version: 1,
      sequence: 1,
      audit_id: "audit-1",
      candidate_id: "candidate-1",
      outcome: "APPROVED_FOR_PAPER",
      evidence_reference: "evidence://candidate-1/package-1",
      evidence_hash: "b".repeat(64),
      reason_codes: ["PAPER_ADMISSION_APPROVED"],
      issued_at: "2026-08-07T12:00:00Z",
      auditor_id: "auditor-1",
      contract_version: "strategy-audit-port-v1",
      decision_hash: "c".repeat(64),
      hash_chain_verified: true,
      hmac_signature_present: true,
      hmac_signature_reverified: false,
    },
  ],
  offset: 0,
  limit: 50,
  has_more: false,
};

const auditEvidence = {
  schema_version: 1,
  evidence: [
    {
      schema_version: 1,
      reference: "evidence://candidate-1/package-1",
      evidence_hash: "b".repeat(64),
      candidate_id: "candidate-1",
      first_seen_at: "2026-08-07T11:59:00Z",
      decision_count: 1,
      content_integrity_verified: true,
    },
  ],
  offset: 0,
  limit: 50,
  has_more: false,
};

const resultsEvidence = {
  schema_version: 1,
  evidence: [
    {
      schema_version: 1,
      evidence_id: "evidence-results-1",
      experiment_id: "experiment-1",
      evidence_type: "robustness-summary",
      producer: "results-aggregator",
      producer_version: "1.0",
      status: "PASS",
      severity: "HIGH",
      created_at: "2026-08-07T11:30:00Z",
      evidence_hash: "d".repeat(64),
      content_hash: "e".repeat(64),
      registered_revision: 4,
      run_count: 3,
      artifact_count: 2,
      limitations_count: 1,
      content_integrity_verified: true,
    },
  ],
  registry_revision: 4,
  offset: 0,
  limit: 50,
  has_more: false,
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("EvidenceControlApiClient", () => {
  it("uses only the memory reader token and parses all evidence sources", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const headers = new Headers(init?.headers);
      expect(headers.get("Authorization")).toBe("Bearer reader-token");
      expect(init?.cache).toBe("no-store");
      expect(init?.credentials).toBe("omit");
      if (path.includes("/overview")) return jsonResponse(overview);
      if (path.includes("/audit/decisions")) return jsonResponse(decisionList);
      if (path.includes("/audit/evidence")) return jsonResponse(auditEvidence);
      if (path.includes("/evidence/results")) return jsonResponse(resultsEvidence);
      throw new Error(`unexpected path: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new EvidenceControlApiClient("", () => "reader-token");

    expect((await client.overview()).audit_integrity?.hmac_signatures_reverified).toBe(false);
    expect((await client.auditDecisions()).decisions[0]?.outcome).toBe("APPROVED_FOR_PAPER");
    expect((await client.auditEvidence()).evidence[0]?.content_integrity_verified).toBe(true);
    expect((await client.resultsEvidence()).evidence[0]?.status).toBe("PASS");
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("fails closed if a server pretends HMAC signatures were reverified", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({
          ...overview,
          audit_integrity: {
            ...overview.audit_integrity,
            hmac_signatures_reverified: true,
          },
        }),
      ),
    );
    const client = new EvidenceControlApiClient();

    await expect(client.overview()).rejects.toMatchObject({
      code: "CONTROL_EVIDENCE_CONTRACT_INVALID",
    });
  });

  it("rejects invalid page arguments before fetch", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const client = new EvidenceControlApiClient();

    await expect(client.auditDecisions(201, 0)).rejects.toMatchObject({
      code: "CONTROL_EVIDENCE_CLIENT_INVALID_LIMIT",
    });
    await expect(client.resultsEvidence(50, -1)).rejects.toMatchObject({
      code: "CONTROL_EVIDENCE_CLIENT_INVALID_OFFSET",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("uses only typed server errors and does not leak raw response fields", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(
          {
            error: {
              code: "CONTROL_EVIDENCE_STATE_INVALID",
              message: "Evidence durable state failed read-only integrity validation.",
              action: "Inspect producer.",
              error_id: "ctrl-safe",
              retryable: false,
              technical_details: { secret: "PRIVATE-PATH" },
            },
            raw_secret: "DO-NOT-LEAK",
          },
          409,
        ),
      ),
    );
    const client = new EvidenceControlApiClient();

    let captured: unknown;
    try {
      await client.auditEvidence();
    } catch (error) {
      captured = error;
    }
    expect(captured).toBeInstanceOf(EvidenceControlClientError);
    expect(String(captured)).not.toContain("PRIVATE-PATH");
    expect(String(captured)).not.toContain("DO-NOT-LEAK");
    expect(captured).toMatchObject({
      code: "CONTROL_EVIDENCE_STATE_INVALID",
      status: 409,
      errorId: "ctrl-safe",
    });
  });
});
