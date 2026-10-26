import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { FormEvent } from "react";
import { RecoveryClientError } from "./client";
import type { RecoveryControlApiClient } from "./client";
import type {
  RecoveryCapability,
  RecoveryOperation,
  RecoveryResolutionRequest,
} from "./contracts";

export function RecoveryPage({
  api,
  readerAuthRevision,
  operatorReady,
}: {
  api: RecoveryControlApiClient;
  readerAuthRevision: number;
  operatorReady: boolean;
}) {
  const capability = useQuery({
    queryKey: ["recovery-capability", readerAuthRevision],
    queryFn: ({ signal }) => api.capability(signal),
    retry: false,
  });

  return (
    <section className="dashboard-page recovery-page" aria-label="Recovery workspace">
      <header className="dashboard-page-header">
        <p className="eyebrow">Operator resolution preview</p>
        <h2>Recovery</h2>
        <p>Показывается только конфигурация capability Control Plane.</p>
      </header>
      {capability.isPending ? <p role="status">Loading recovery capability…</p> : null}
      {capability.error ? <RecoveryFailure error={capability.error} /> : null}
      {capability.data ? (
        <RecoveryContent
          capability={capability.data}
          api={api}
          operatorReady={operatorReady}
        />
      ) : null}
    </section>
  );
}

export function RecoveryContent({
  capability,
  api,
  operatorReady,
}: {
  capability: RecoveryCapability;
  api: RecoveryControlApiClient;
  operatorReady: boolean;
}) {
  return (
    <>
      <CapabilitySummary capability={capability} />
      {capability.status === "RESOLUTION_ONLY" && capability.resolution_enabled ? (
        <ResolutionRequestForm api={api} operatorReady={operatorReady} />
      ) : null}
    </>
  );
}

export function ResolutionRequestForm({
  api,
  operatorReady,
}: {
  api: RecoveryControlApiClient;
  operatorReady: boolean;
}) {
  const [operation, setOperation] = useState<RecoveryOperation>("backup");
  const [requestId, setRequestId] = useState("");
  const [profileId, setProfileId] = useState("");
  const [packageId, setPackageId] = useState("");
  const resolution = useMutation({
    mutationFn: (request: RecoveryResolutionRequest) => api.resolveRequest(request),
    retry: false,
  });
  const packageRequired = operation !== "backup";

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!operatorReady) return;
    const request: RecoveryResolutionRequest = {
      operation,
      request_id: requestId.trim(),
      profile_id: profileId.trim(),
      ...(packageRequired ? { package_id: packageId.trim() } : {}),
    };
    resolution.mutate(request);
  }

  return (
    <section className="control-panel" aria-label="Recovery resolution preview">
      <h3>Resolve a named recovery request</h3>
      <p role="status">
        Preview only. This submits an operator-authorized resolution request; it does not create a
        backup, verify or restore a package, persist an intent, or admit execution.
      </p>
      <p>Use opaque IDs only. Do not enter file paths or manifest contents.</p>
      <form onSubmit={submit}>
        <label>
          Operation
          <select
            value={operation}
            disabled={resolution.isPending}
            onChange={(event) => {
              setOperation(event.target.value as RecoveryOperation);
              resolution.reset();
            }}
          >
            <option value="backup">Backup</option>
            <option value="verify">Verify</option>
            <option value="restore">Restore</option>
          </select>
        </label>
        <label>
          Request ID
          <input
            required
            minLength={1}
            maxLength={128}
            pattern="[A-Za-z0-9][A-Za-z0-9_-]{0,127}"
            value={requestId}
            disabled={resolution.isPending}
            onChange={(event) => {
              setRequestId(event.target.value);
              resolution.reset();
            }}
            autoComplete="off"
          />
        </label>
        <label>
          Profile ID
          <input
            required
            minLength={1}
            maxLength={128}
            pattern="[A-Za-z0-9][A-Za-z0-9_-]{0,127}"
            value={profileId}
            disabled={resolution.isPending}
            onChange={(event) => {
              setProfileId(event.target.value);
              resolution.reset();
            }}
            autoComplete="off"
          />
        </label>
        {packageRequired ? (
          <label>
            Package ID
            <input
              required
              minLength={1}
              maxLength={128}
              pattern="[A-Za-z0-9][A-Za-z0-9_-]{0,127}"
              value={packageId}
              disabled={resolution.isPending}
              onChange={(event) => {
                setPackageId(event.target.value);
                resolution.reset();
              }}
              autoComplete="off"
            />
          </label>
        ) : null}
        <button
          type="submit"
          disabled={
            !operatorReady || !requestId.trim() || !profileId.trim() ||
            (packageRequired && !packageId.trim()) || resolution.isPending
          }
        >
          {resolution.isPending ? "Resolving…" : "Preview resolution"}
        </button>
      </form>
      {!operatorReady ? (
        <p role="status">Enter the Control operator token in the dashboard sidebar to submit.</p>
      ) : null}
      {resolution.error ? <ResolutionFailure error={resolution.error} /> : null}
      {resolution.data ? (
        <section aria-label="Validated resolution result" role="status">
          <h4>Resolution only</h4>
          <dl>
            <dt>Status</dt>
            <dd>{resolution.data.status}</dd>
            <dt>Operation</dt>
            <dd>{resolution.data.operation}</dd>
            <dt>Fingerprint</dt>
            <dd><code>{resolution.data.fingerprint}</code></dd>
          </dl>
          <p>No backup, verification, restore, durable intent, or execution admission occurred.</p>
        </section>
      ) : null}
    </section>
  );
}

export function ResolutionFailure({ error }: { error: unknown }) {
  const resolutionError = error instanceof RecoveryClientError ? error : null;
  const status = resolutionError?.status;
  if (status === 401 || status === 403) {
    return <p role="alert">Recovery operator authorization failed (HTTP {status}).</p>;
  }
  const code = resolutionError && /^[A-Z0-9_]{1,128}$/.test(resolutionError.code)
    ? resolutionError.code
    : null;
  const http = typeof status === "number" && Number.isInteger(status) && status >= 100 && status <= 599
    ? `HTTP ${status}`
    : null;
  const details = [http, code].filter((value): value is string => value !== null);
  return (
    <p role="alert">
      {details.length > 0
        ? `Recovery resolution failed (${details.join(", ")}).`
        : "Recovery resolution failed."}
    </p>
  );
}

export function RecoveryFailure({ error }: { error: unknown }) {
  const capabilityError = error instanceof RecoveryClientError ? error : null;
  if (
    capabilityError?.status === 503 &&
    capabilityError.code === "CONTROL_RECOVERY_NOT_CONFIGURED"
  ) {
    return (
      <div className="empty-state" role="status">
        <strong>Not configured</strong>
        <p>Recovery readiness and the profile catalog are unknown.</p>
      </div>
    );
  }
  if (capabilityError?.status === 401 || capabilityError?.status === 403) {
    return (
      <p className="status-error" role="alert">
        Recovery authorization failed (HTTP {capabilityError.status}). Check the Control reader token.
      </p>
    );
  }
  return <p className="status-error" role="alert">{failureText(capabilityError)}</p>;
}

export function CapabilitySummary({ capability }: { capability: RecoveryCapability }) {
  const flags: Array<[string, boolean, string, string]> = [
    ["Resolution enabled", capability.resolution_enabled, "Enabled", "Disabled"],
    ["Profile catalog configured", capability.profile_catalog_configured, "Configured", "Not configured"],
    ["Owner readiness source configured", capability.owner_readiness_source_configured, "Configured", "Not configured"],
    ["Recovery authorization policy configured", capability.recovery_authorization_policy_configured, "Configured", "Not configured"],
    ["Backup package proposal source configured", capability.backup_package_proposal_source_configured, "Configured", "Not configured"],
    ["Existing package evidence source configured", capability.existing_package_evidence_source_configured, "Configured", "Not configured"],
    ["Durable intent store configured", capability.durable_intent_store_configured, "Configured", "Not configured"],
    ["Recovery effects enabled", capability.recovery_effects_enabled, "Enabled", "Disabled"],
    ["Execution admission enabled", capability.execution_admission_enabled, "Enabled", "Disabled"],
  ];
  return (
    <section className="control-panel" aria-label="Recovery capability configuration">
      <h3>{capability.status}</h3>
      <p role="status">
        Effectful recovery actions are unavailable. This view does not provide backup, verification,
        restore, or execution.
      </p>
      <p>
        Configuration only. This does not report profile readiness or provide a profile inventory.
      </p>
      <dl className="acceptance-details-grid single">
        {flags.map(([label, enabled, enabledText, disabledText]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{enabled ? enabledText : disabledText}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function failureText(error: RecoveryClientError | null): string {
  if (!error) return "Recovery capability request failed.";
  const status = Number.isInteger(error.status) && error.status >= 100 && error.status <= 599
    ? `HTTP ${error.status}`
    : null;
  const code = /^[A-Z0-9_]{1,128}$/.test(error.code) ? error.code : null;
  const details = [status, code].filter((value): value is string => value !== null);
  return details.length > 0
    ? `Recovery capability request failed (${details.join(", ")}).`
    : "Recovery capability request failed.";
}
