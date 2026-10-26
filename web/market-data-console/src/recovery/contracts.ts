export interface RecoveryCapability {
  status: "NOT_CONFIGURED" | "RESOLUTION_ONLY";
  resolution_enabled: boolean;
  profile_catalog_configured: boolean;
  owner_readiness_source_configured: boolean;
  recovery_authorization_policy_configured: boolean;
  backup_package_proposal_source_configured: boolean;
  existing_package_evidence_source_configured: boolean;
  durable_intent_store_configured: false;
  recovery_effects_enabled: false;
  execution_admission_enabled: false;
}

export type RecoveryOperation = "backup" | "verify" | "restore";

export interface RecoveryResolutionRequest {
  operation: RecoveryOperation;
  request_id: string;
  profile_id: string;
  package_id?: string;
}

export interface RecoveryResolutionSummary {
  status: "RESOLUTION_ONLY";
  operation: RecoveryOperation;
  fingerprint: string;
}

const OPAQUE_IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const SHA256 = /^[0-9a-f]{64}$/;

export function validateRecoveryResolutionRequest(
  value: RecoveryResolutionRequest,
): RecoveryResolutionRequest {
  if (value.operation !== "backup" && value.operation !== "verify" && value.operation !== "restore") {
    throw new Error("Recovery operation is invalid");
  }
  if (!OPAQUE_IDENTIFIER.test(value.request_id) || !OPAQUE_IDENTIFIER.test(value.profile_id)) {
    throw new Error("Recovery identifiers are invalid");
  }
  if (value.operation === "backup") {
    if (value.package_id !== undefined) throw new Error("Backup cannot specify a package ID");
  } else if (value.package_id === undefined || !OPAQUE_IDENTIFIER.test(value.package_id)) {
    throw new Error("Verify and restore require a valid package ID");
  }
  return value;
}

export function parseRecoveryResolutionSummary(
  value: unknown,
  expectedOperation: RecoveryOperation,
): RecoveryResolutionSummary {
  if (!isRecord(value)) throw new Error("Recovery resolution must be an object");
  const keys = Object.keys(value).sort();
  if (keys.join(",") !== "fingerprint,operation,status") {
    throw new Error("Recovery resolution fields are invalid");
  }
  if (
    value.status !== "RESOLUTION_ONLY" ||
    value.operation !== expectedOperation ||
    typeof value.fingerprint !== "string" ||
    !SHA256.test(value.fingerprint)
  ) {
    throw new Error("Recovery resolution response is invalid");
  }
  return {
    status: "RESOLUTION_ONLY",
    operation: expectedOperation,
    fingerprint: value.fingerprint,
  };
}

export function parseRecoveryCapability(value: unknown): RecoveryCapability {
  if (!isRecord(value)) throw new Error("Recovery capability must be an object");
  if (value.status !== "NOT_CONFIGURED" && value.status !== "RESOLUTION_ONLY") {
    throw new Error("Recovery capability status is invalid");
  }
  const capability: RecoveryCapability = {
    status: value.status,
    resolution_enabled: booleanValue(value.resolution_enabled),
    profile_catalog_configured: booleanValue(value.profile_catalog_configured),
    owner_readiness_source_configured: booleanValue(value.owner_readiness_source_configured),
    recovery_authorization_policy_configured: booleanValue(
      value.recovery_authorization_policy_configured,
    ),
    backup_package_proposal_source_configured: booleanValue(
      value.backup_package_proposal_source_configured,
    ),
    existing_package_evidence_source_configured: booleanValue(
      value.existing_package_evidence_source_configured,
    ),
    durable_intent_store_configured: falseLiteral(value.durable_intent_store_configured),
    recovery_effects_enabled: falseLiteral(value.recovery_effects_enabled),
    execution_admission_enabled: falseLiteral(value.execution_admission_enabled),
  };
  if (capability.status === "NOT_CONFIGURED" && capability.resolution_enabled) {
    throw new Error("Unconfigured recovery cannot enable resolution");
  }
  if (
    capability.status === "RESOLUTION_ONLY" &&
    (!capability.resolution_enabled ||
      !capability.profile_catalog_configured ||
      !capability.owner_readiness_source_configured ||
      !capability.recovery_authorization_policy_configured ||
      !capability.backup_package_proposal_source_configured ||
      !capability.existing_package_evidence_source_configured)
  ) {
    throw new Error("Resolution-only recovery requires all configured read ports");
  }
  return capability;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function booleanValue(value: unknown): boolean {
  if (typeof value !== "boolean") throw new Error("Recovery capability flag is invalid");
  return value;
}

function falseLiteral(value: unknown): false {
  if (value !== false) throw new Error("Recovery effect capability must remain disabled");
  return false;
}
