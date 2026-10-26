export type AuditOutcome =
  | "APPROVED_FOR_PAPER"
  | "RESEARCH_ONLY"
  | "REJECTED"
  | "UNAVAILABLE";

export type ResultsEvidenceStatus =
  | "PASS"
  | "FAIL"
  | "PARTIAL"
  | "MISSING"
  | "CONTAMINATED"
  | "INCOMPATIBLE";

export type EvidenceSeverity = "INFO" | "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";

export interface AuditLedgerIntegrity {
  schema_version: 1;
  evidence_count: number;
  decision_count: number;
  chain_head: string | null;
  hash_chain_verified: true;
  hmac_signatures_reverified: false;
}

export interface EvidenceOverview {
  schema_version: 1;
  audit_configured: boolean;
  results_registry_configured: boolean;
  audit_integrity: AuditLedgerIntegrity | null;
  results_registry_revision: number | null;
  results_evidence_count: number | null;
}

export interface AuditDecisionSummary {
  schema_version: 1;
  sequence: number;
  audit_id: string;
  candidate_id: string;
  outcome: AuditOutcome;
  evidence_reference: string;
  evidence_hash: string;
  reason_codes: string[];
  issued_at: string;
  auditor_id: string;
  contract_version: "strategy-audit-port-v1";
  decision_hash: string;
  hash_chain_verified: true;
  hmac_signature_present: true;
  hmac_signature_reverified: false;
}

export interface AuditDecisionList {
  schema_version: 1;
  decisions: AuditDecisionSummary[];
  offset: number;
  limit: number;
  has_more: boolean;
}

export interface AuditEvidenceSummary {
  schema_version: 1;
  reference: string;
  evidence_hash: string;
  candidate_id: string;
  first_seen_at: string;
  decision_count: number;
  content_integrity_verified: true;
}

export interface AuditEvidenceList {
  schema_version: 1;
  evidence: AuditEvidenceSummary[];
  offset: number;
  limit: number;
  has_more: boolean;
}

export interface ResultsEvidenceSummary {
  schema_version: 1;
  evidence_id: string;
  experiment_id: string;
  evidence_type: string;
  producer: string;
  producer_version: string;
  status: ResultsEvidenceStatus;
  severity: EvidenceSeverity;
  created_at: string;
  evidence_hash: string;
  content_hash: string;
  registered_revision: number;
  run_count: number;
  artifact_count: number;
  limitations_count: number;
  content_integrity_verified: true;
}

export interface ResultsEvidenceList {
  schema_version: 1;
  evidence: ResultsEvidenceSummary[];
  registry_revision: number;
  offset: number;
  limit: number;
  has_more: boolean;
}

const AUDIT_OUTCOMES = new Set<AuditOutcome>([
  "APPROVED_FOR_PAPER",
  "RESEARCH_ONLY",
  "REJECTED",
  "UNAVAILABLE",
]);
const RESULTS_STATUSES = new Set<ResultsEvidenceStatus>([
  "PASS",
  "FAIL",
  "PARTIAL",
  "MISSING",
  "CONTAMINATED",
  "INCOMPATIBLE",
]);
const SEVERITIES = new Set<EvidenceSeverity>(["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"]);
const HEX_64 = /^[0-9a-f]{64}$/;

export function parseEvidenceOverview(value: unknown): EvidenceOverview {
  const record = asRecord(value, "Evidence overview");
  const auditConfigured = booleanValue(record.audit_configured, "audit_configured");
  const resultsConfigured = booleanValue(
    record.results_registry_configured,
    "results_registry_configured",
  );
  const auditIntegrity =
    record.audit_integrity === null ? null : parseAuditLedgerIntegrity(record.audit_integrity);
  const registryRevision = nullableNonNegativeInteger(
    record.results_registry_revision,
    "results_registry_revision",
  );
  const resultsCount = nullableNonNegativeInteger(
    record.results_evidence_count,
    "results_evidence_count",
  );
  if (auditConfigured !== (auditIntegrity !== null)) {
    throw new Error("Evidence audit source shape is inconsistent");
  }
  if (resultsConfigured !== (registryRevision !== null && resultsCount !== null)) {
    throw new Error("Evidence Results source shape is inconsistent");
  }
  return {
    schema_version: literalOne(record.schema_version, "schema_version"),
    audit_configured: auditConfigured,
    results_registry_configured: resultsConfigured,
    audit_integrity: auditIntegrity,
    results_registry_revision: registryRevision,
    results_evidence_count: resultsCount,
  };
}

export function parseAuditDecisionList(value: unknown): AuditDecisionList {
  const record = asRecord(value, "Audit decision list");
  const values = arrayValue(record.decisions, "decisions");
  return {
    schema_version: literalOne(record.schema_version, "schema_version"),
    decisions: values.map(parseAuditDecision),
    offset: nonNegativeInteger(record.offset, "offset"),
    limit: pageLimit(record.limit),
    has_more: booleanValue(record.has_more, "has_more"),
  };
}

export function parseAuditEvidenceList(value: unknown): AuditEvidenceList {
  const record = asRecord(value, "Audit evidence list");
  const values = arrayValue(record.evidence, "evidence");
  return {
    schema_version: literalOne(record.schema_version, "schema_version"),
    evidence: values.map(parseAuditEvidence),
    offset: nonNegativeInteger(record.offset, "offset"),
    limit: pageLimit(record.limit),
    has_more: booleanValue(record.has_more, "has_more"),
  };
}

export function parseResultsEvidenceList(value: unknown): ResultsEvidenceList {
  const record = asRecord(value, "Results evidence list");
  const values = arrayValue(record.evidence, "evidence");
  return {
    schema_version: literalOne(record.schema_version, "schema_version"),
    evidence: values.map(parseResultsEvidence),
    registry_revision: nonNegativeInteger(record.registry_revision, "registry_revision"),
    offset: nonNegativeInteger(record.offset, "offset"),
    limit: pageLimit(record.limit),
    has_more: booleanValue(record.has_more, "has_more"),
  };
}

function parseAuditLedgerIntegrity(value: unknown): AuditLedgerIntegrity {
  const record = asRecord(value, "Audit integrity");
  const decisions = nonNegativeInteger(record.decision_count, "decision_count");
  const chainHead = nullableDigest(record.chain_head, "chain_head");
  if ((decisions === 0) !== (chainHead === null)) {
    throw new Error("Audit chain head does not match decision count");
  }
  trueLiteral(record.hash_chain_verified, "hash_chain_verified");
  falseLiteral(record.hmac_signatures_reverified, "hmac_signatures_reverified");
  return {
    schema_version: literalOne(record.schema_version, "schema_version"),
    evidence_count: nonNegativeInteger(record.evidence_count, "evidence_count"),
    decision_count: decisions,
    chain_head: chainHead,
    hash_chain_verified: true,
    hmac_signatures_reverified: false,
  };
}

function parseAuditDecision(value: unknown): AuditDecisionSummary {
  const record = asRecord(value, "Audit decision");
  const reasons = arrayValue(record.reason_codes, "reason_codes").map((item) =>
    boundedString(item, "reason_code", 128),
  );
  if (reasons.length < 1 || reasons.length > 64 || new Set(reasons).size !== reasons.length) {
    throw new Error("Audit reason_codes are invalid");
  }
  if ([...reasons].sort().some((item, index) => item !== reasons[index])) {
    throw new Error("Audit reason_codes must be sorted");
  }
  trueLiteral(record.hash_chain_verified, "hash_chain_verified");
  trueLiteral(record.hmac_signature_present, "hmac_signature_present");
  falseLiteral(record.hmac_signature_reverified, "hmac_signature_reverified");
  return {
    schema_version: literalOne(record.schema_version, "schema_version"),
    sequence: positiveInteger(record.sequence, "sequence"),
    audit_id: boundedString(record.audit_id, "audit_id", 128),
    candidate_id: boundedString(record.candidate_id, "candidate_id", 128),
    outcome: enumValue(record.outcome, AUDIT_OUTCOMES, "outcome"),
    evidence_reference: boundedString(record.evidence_reference, "evidence_reference", 512),
    evidence_hash: digest(record.evidence_hash, "evidence_hash"),
    reason_codes: reasons,
    issued_at: timestamp(record.issued_at, "issued_at"),
    auditor_id: boundedString(record.auditor_id, "auditor_id", 128),
    contract_version: exactString(record.contract_version, "strategy-audit-port-v1"),
    decision_hash: digest(record.decision_hash, "decision_hash"),
    hash_chain_verified: true,
    hmac_signature_present: true,
    hmac_signature_reverified: false,
  };
}

function parseAuditEvidence(value: unknown): AuditEvidenceSummary {
  const record = asRecord(value, "Audit evidence");
  trueLiteral(record.content_integrity_verified, "content_integrity_verified");
  return {
    schema_version: literalOne(record.schema_version, "schema_version"),
    reference: boundedString(record.reference, "reference", 512),
    evidence_hash: digest(record.evidence_hash, "evidence_hash"),
    candidate_id: boundedString(record.candidate_id, "candidate_id", 128),
    first_seen_at: timestamp(record.first_seen_at, "first_seen_at"),
    decision_count: nonNegativeInteger(record.decision_count, "decision_count"),
    content_integrity_verified: true,
  };
}

function parseResultsEvidence(value: unknown): ResultsEvidenceSummary {
  const record = asRecord(value, "Results evidence");
  trueLiteral(record.content_integrity_verified, "content_integrity_verified");
  return {
    schema_version: literalOne(record.schema_version, "schema_version"),
    evidence_id: boundedString(record.evidence_id, "evidence_id", 192),
    experiment_id: boundedString(record.experiment_id, "experiment_id", 192),
    evidence_type: boundedString(record.evidence_type, "evidence_type", 256),
    producer: boundedString(record.producer, "producer", 256),
    producer_version: boundedString(record.producer_version, "producer_version", 128),
    status: enumValue(record.status, RESULTS_STATUSES, "status"),
    severity: enumValue(record.severity, SEVERITIES, "severity"),
    created_at: timestamp(record.created_at, "created_at"),
    evidence_hash: digest(record.evidence_hash, "evidence_hash"),
    content_hash: digest(record.content_hash, "content_hash"),
    registered_revision: nonNegativeInteger(record.registered_revision, "registered_revision"),
    run_count: nonNegativeInteger(record.run_count, "run_count"),
    artifact_count: nonNegativeInteger(record.artifact_count, "artifact_count"),
    limitations_count: nonNegativeInteger(record.limitations_count, "limitations_count"),
    content_integrity_verified: true,
  };
}

function asRecord(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function arrayValue(value: unknown, label: string): unknown[] {
  if (!Array.isArray(value)) throw new Error(`${label} must be an array`);
  return value;
}

function literalOne(value: unknown, label: string): 1 {
  if (value !== 1) throw new Error(`${label} must equal 1`);
  return 1;
}

function trueLiteral(value: unknown, label: string): true {
  if (value !== true) throw new Error(`${label} must be true`);
  return true;
}

function falseLiteral(value: unknown, label: string): false {
  if (value !== false) throw new Error(`${label} must be false`);
  return false;
}

function booleanValue(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") throw new Error(`${label} must be boolean`);
  return value;
}

function boundedString(value: unknown, label: string, maximum: number): string {
  if (typeof value !== "string" || value.length < 1 || value.length > maximum) {
    throw new Error(`${label} must contain 1..${maximum} characters`);
  }
  return value;
}

function exactString<T extends string>(value: unknown, expected: T): T {
  if (value !== expected) throw new Error(`value must equal ${expected}`);
  return expected;
}

function timestamp(value: unknown, label: string): string {
  const result = boundedString(value, label, 64);
  if (!Number.isFinite(Date.parse(result))) throw new Error(`${label} must be a timestamp`);
  return result;
}

function digest(value: unknown, label: string): string {
  const result = boundedString(value, label, 64);
  if (!HEX_64.test(result)) throw new Error(`${label} must be a lowercase SHA-256`);
  return result;
}

function nullableDigest(value: unknown, label: string): string | null {
  return value === null ? null : digest(value, label);
}

function nonNegativeInteger(value: unknown, label: string): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
    throw new Error(`${label} must be a non-negative integer`);
  }
  return value;
}

function positiveInteger(value: unknown, label: string): number {
  const result = nonNegativeInteger(value, label);
  if (result < 1) throw new Error(`${label} must be positive`);
  return result;
}

function nullableNonNegativeInteger(value: unknown, label: string): number | null {
  return value === null ? null : nonNegativeInteger(value, label);
}

function pageLimit(value: unknown): number {
  const result = positiveInteger(value, "limit");
  if (result > 200) throw new Error("limit must be <= 200");
  return result;
}

function enumValue<T extends string>(value: unknown, allowed: Set<T>, label: string): T {
  if (typeof value !== "string" || !allowed.has(value as T)) {
    throw new Error(`${label} is unsupported`);
  }
  return value as T;
}
