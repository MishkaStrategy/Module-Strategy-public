import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { ControlApiClientError } from "../control/client";
import { SystemCapacityApiClient } from "./client";
import type {
  CapacityTierId,
  ExternalArtifactCapacitySnapshot,
  SystemCapacityTier,
} from "./contracts";

const TIER_LABELS: Record<CapacityTierId, string> = {
  system_acceptance: "System Acceptance",
  market_data_registry_storage: "Market Data registry",
  market_data_application_control_plane_storage: "Market Data control state",
  research_platform: "Research platform",
  core_paper_trading_durable_state: "Core / Paper / Trading",
};

export function SystemCapacityPanel({
  readerToken,
  authRevision,
  currentDiskAvailableBytes,
}: {
  readerToken: string;
  authRevision: number;
  currentDiskAvailableBytes: number | null;
}) {
  const api = useMemo(
    () => new SystemCapacityApiClient("", () => readerToken),
    [readerToken],
  );
  const capacity = useQuery({
    queryKey: ["control-system-capacity", authRevision],
    queryFn: ({ signal }) => api.getCapacity(signal),
    retry: false,
    refetchInterval: 10_000,
  });
  const artifactCapacity = useQuery({
    queryKey: ["control-external-artifact-capacity", authRevision],
    queryFn: ({ signal }) => api.getExternalArtifactCapacity(signal),
    retry: false,
    refetchInterval: 10_000,
  });

  return (
    <section className="control-panel" aria-label="Storage capacity policy">
      <div className="control-panel-heading">
        <div>
          <h3>Storage capacity policy</h3>
          <p>
            Owner-backed GAP-013 projection. Measurements are not converted into invented local
            disk watermarks or deletion authority.
          </p>
        </div>
        {capacity.data ? <small>Policy {capacity.data.policy_status}</small> : null}
      </div>

      {capacity.isPending ? (
        <div className="control-notice" aria-live="polite">
          <strong>Loading capacity policy…</strong>
          <span>CPU/RAM/disk observations remain independent of this policy read.</span>
        </div>
      ) : null}

      {capacity.error ? <CapacityError error={capacity.error} label="Capacity policy" /> : null}

      {capacity.data ? (
        <div>
          <div className="control-notice" role="status">
            <strong>No global local-disk watermark</strong>
            <span>
              Current disk available: {formatOptionalBytes(currentDiskAvailableBytes)}. This is an
              observation only; the canonical policy does not define a universal high/critical
              threshold for the local Control Plane host.
            </span>
            <small>
              Unknown means {capacity.data.unknown_means}. Source policy digest{" "}
              {capacity.data.source_digest_sha256.slice(0, 12)}…
            </small>
          </div>

          <section className="control-summary-grid" aria-label="CI runner capacity watermarks">
            {capacity.data.runner_watermarks.map((watermark) => (
              <article className="control-metric-card" key={watermark.role}>
                <span>{watermark.role} runner</span>
                <strong>{formatBytes(watermark.critical_free_bytes)} critical</strong>
                <small>
                  High-water {formatBytes(watermark.high_water_free_bytes)} free · CI runner policy,
                  not a local-host forecast
                </small>
              </article>
            ))}
          </section>

          <div className="component-health-grid" aria-label="Owner storage capacity tiers">
            {capacity.data.tiers.map((tier) => (
              <CapacityTierCard tier={tier} key={tier.tier_id} />
            ))}
          </div>
        </div>
      ) : null}

      <ExternalArtifactCapacitySection
        data={artifactCapacity.data}
        pending={artifactCapacity.isPending}
        error={artifactCapacity.error}
      />
    </section>
  );
}

function ExternalArtifactCapacitySection({
  data,
  pending,
  error,
}: {
  data: ExternalArtifactCapacitySnapshot | undefined;
  pending: boolean;
  error: unknown;
}) {
  return (
    <section aria-label="External CI artifact capacity">
      <div className="control-panel-heading">
        <div>
          <h3>External CI artifact quota</h3>
          <p>
            GitHub Actions quota is external to local disk. Control UI exposes only the accepted
            signal and maintenance ceiling; it cannot delete artifacts.
          </p>
        </div>
        {data ? <small>{data.status}</small> : null}
      </div>
      {pending ? (
        <div className="control-notice" aria-live="polite">
          <strong>Loading external quota policy…</strong>
          <span>Local resource and owner-tier capacity reads remain independent.</span>
        </div>
      ) : null}
      {error ? <CapacityError error={error} label="External artifact quota" /> : null}
      {data ? <ExternalArtifactCapacityCard capacity={data} /> : null}
    </section>
  );
}

function ExternalArtifactCapacityCard({
  capacity,
}: {
  capacity: ExternalArtifactCapacitySnapshot;
}) {
  return (
    <article className="component-health-card">
      <div className="component-health-heading">
        <strong>GitHub Actions artifacts</strong>
        <span>{capacity.status}</span>
      </div>
      <dl className="control-details">
        <div>
          <dt>Proactive usage</dt>
          <dd>{capacity.proactive_usage_status}</dd>
        </div>
        <div>
          <dt>Critical signal</dt>
          <dd>{capacity.critical_signal}</dd>
        </div>
        <div>
          <dt>Maintenance ceiling</dt>
          <dd>
            {capacity.maintenance_max_count} artifacts · {formatBytes(capacity.maintenance_max_bytes)}
          </dd>
        </div>
        <div>
          <dt>Rate-limit reserve</dt>
          <dd>{capacity.rate_limit_reserve_requests} requests</dd>
        </div>
        <div>
          <dt>Local disk watermark</dt>
          <dd>NOT APPLICABLE</dd>
        </div>
        <div>
          <dt>Control UI cleanup</dt>
          <dd>UNAVAILABLE</dd>
        </div>
      </dl>
      <small>
        Maintenance ceiling is a safety bound, not a quota watermark. A cleanup mutation is not
        exposed by this Control UI.
      </small>
    </article>
  );
}

function CapacityTierCard({ tier }: { tier: SystemCapacityTier }) {
  return (
    <article className="component-health-card">
      <div className="component-health-heading">
        <strong>{TIER_LABELS[tier.tier_id]}</strong>
        <span>{tier.status}</span>
      </div>
      <dl className="control-details">
        <div>
          <dt>Physical boundary</dt>
          <dd>{tier.physical_free_disk.status}</dd>
        </div>
        <div>
          <dt>High-water free</dt>
          <dd>{formatOptionalBytes(tier.physical_free_disk.high_water_free_bytes)}</dd>
        </div>
        <div>
          <dt>Critical free</dt>
          <dd>{formatOptionalBytes(tier.physical_free_disk.critical_free_bytes)}</dd>
        </div>
        <div>
          <dt>Deletion authority</dt>
          <dd>{tier.deletion_authority}</dd>
        </div>
        {tier.next_publication_admission ? (
          <div>
            <dt>Next publication</dt>
            <dd>
              Owner admission accepted · recovery reserve{" "}
              {formatOptionalBytes(tier.recovery_reserve_bytes)}
            </dd>
          </div>
        ) : null}
        {tier.owner_hard_limit_bytes !== null ? (
          <div>
            <dt>Owner hard bound</dt>
            <dd>{formatBytes(tier.owner_hard_limit_bytes)}</dd>
          </div>
        ) : null}
        {tier.owner_payload_limit_bytes !== null ? (
          <div>
            <dt>Owner payload bound</dt>
            <dd>{formatBytes(tier.owner_payload_limit_bytes)}</dd>
          </div>
        ) : null}
      </dl>
      {tier.measurements.length ? (
        <details>
          <summary>Accepted measurements · never aggregate</summary>
          <dl className="control-details">
            {tier.measurements.map((measurement) => (
              <div key={measurement.metric}>
                <dt>{measurement.metric}</dt>
                <dd>
                  {measurement.unit === "bytes"
                    ? formatBytes(measurement.value)
                    : `${measurement.value.toFixed(3)} B/run`}
                </dd>
              </div>
            ))}
          </dl>
        </details>
      ) : null}
    </article>
  );
}

function CapacityError({ error, label }: { error: unknown; label: string }) {
  if (error instanceof ControlApiClientError) {
    return (
      <div className="control-notice control-notice-error" role="status">
        <strong>
          {label} unavailable · {error.code}
        </strong>
        <span>{error.message}</span>
        <span>
          Other resource projections remain readable independently. No capacity, pruning or
          deletion claim is inferred from this failure.
        </span>
        {error.action ? <small>Action: {error.action}</small> : null}
      </div>
    );
  }
  return (
    <div className="control-notice control-notice-error" role="status">
      <strong>{label} unavailable</strong>
      <span>Other resource projections remain readable independently. Unknown remains unknown.</span>
    </div>
  );
}

function formatOptionalBytes(value: number | null): string {
  return value === null ? "UNKNOWN" : formatBytes(value);
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "invalid size";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let amount = value;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024;
    unit += 1;
  }
  const digits = unit === 0 || amount >= 100 ? 0 : amount >= 10 ? 1 : 2;
  return `${amount.toFixed(digits)} ${units[unit]}`;
}
