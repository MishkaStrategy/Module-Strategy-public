import { useQuery, useQueryClient } from "@tanstack/react-query";

import type { ControlPlaneApiClient } from "../control/client";
import { EvidenceControlApiClient } from "./client";
import "./evidence.css";

export function EvidencePage({
  controlApi,
  evidenceApi,
  readerAuthRevision,
}: {
  controlApi: ControlPlaneApiClient;
  evidenceApi: EvidenceControlApiClient;
  readerAuthRevision: number;
}) {
  const queryClient = useQueryClient();
  const health = useQuery({
    queryKey: ["control-health", "evidence", readerAuthRevision],
    queryFn: ({ signal }) => controlApi.getComponentHealth("evidence", signal),
    retry: false,
  });
  const overview = useQuery({
    queryKey: ["evidence-overview", readerAuthRevision],
    queryFn: ({ signal }) => evidenceApi.overview(signal),
    retry: false,
  });
  const auditDecisions = useQuery({
    queryKey: ["evidence-audit-decisions", readerAuthRevision],
    queryFn: ({ signal }) => evidenceApi.auditDecisions(50, 0, signal),
    enabled: overview.data?.audit_configured === true,
    retry: false,
  });
  const auditEvidence = useQuery({
    queryKey: ["evidence-audit-packages", readerAuthRevision],
    queryFn: ({ signal }) => evidenceApi.auditEvidence(50, 0, signal),
    enabled: overview.data?.audit_configured === true,
    retry: false,
  });
  const resultsEvidence = useQuery({
    queryKey: ["evidence-results", readerAuthRevision],
    queryFn: ({ signal }) => evidenceApi.resultsEvidence(50, 0, signal),
    enabled: overview.data?.results_registry_configured === true,
    retry: false,
  });

  const reload = () => {
    void queryClient.invalidateQueries({ queryKey: ["control-health", "evidence"] });
    void queryClient.invalidateQueries({ queryKey: ["evidence-overview"] });
    void queryClient.invalidateQueries({ queryKey: ["evidence-audit-decisions"] });
    void queryClient.invalidateQueries({ queryKey: ["evidence-audit-packages"] });
    void queryClient.invalidateQueries({ queryKey: ["evidence-results"] });
  };

  return (
    <section className="evidence-workspace" aria-label="Evidence workspace">
      <header className="evidence-header">
        <div>
          <p className="eyebrow">Read-only provenance</p>
          <h1>Evidence</h1>
          <p>
            Независимый Strategy Audit ledger и immutable Results Aggregator evidence. Control
            Plane только читает существующие producer-owned базы и не сканирует host filesystem.
          </p>
        </div>
        <button type="button" onClick={reload} className="secondary-action">
          Refresh
        </button>
      </header>

      <div className="evidence-boundary-note" role="note">
        <strong>Cryptographic boundary</strong>
        <span>
          SHA-256 evidence content и append-only audit chain перепроверяются read-only observer.
          Historical HMAC signatures не перепроверяются: symmetric signing key остаётся только у
          independent auditor. Recorded outcome не следует путать с новой keyed re-authentication.
        </span>
      </div>

      <section className="evidence-health" aria-label="Evidence health">
        <div>
          <span>Component</span>
          <strong>{health.data?.status ?? (health.isError ? "UNAVAILABLE" : "LOADING")}</strong>
        </div>
        <div>
          <span>Audit source</span>
          <strong>{overview.data?.audit_configured ? "configured" : "not configured"}</strong>
        </div>
        <div>
          <span>Results source</span>
          <strong>
            {overview.data?.results_registry_configured ? "configured" : "not configured"}
          </strong>
        </div>
      </section>

      {overview.isError ? (
        <p className="status-error" role="alert">
          Evidence overview is unavailable. Configure the local reader token/source and retry.
        </p>
      ) : null}

      {overview.data?.audit_integrity ? (
        <section className="evidence-section" aria-labelledby="audit-integrity-heading">
          <div className="evidence-section-heading">
            <div>
              <p className="eyebrow">Independent auditor</p>
              <h2 id="audit-integrity-heading">Strategy Audit ledger</h2>
            </div>
            <span className="evidence-integrity-badge">hash chain verified</span>
          </div>
          <dl className="evidence-summary-grid">
            <div>
              <dt>Evidence packages</dt>
              <dd>{overview.data.audit_integrity.evidence_count}</dd>
            </div>
            <div>
              <dt>Decisions</dt>
              <dd>{overview.data.audit_integrity.decision_count}</dd>
            </div>
            <div>
              <dt>Chain head</dt>
              <dd className="mono-wrap">{overview.data.audit_integrity.chain_head ?? "empty"}</dd>
            </div>
            <div>
              <dt>HMAC reverified</dt>
              <dd>no · auditor key not held</dd>
            </div>
          </dl>

          <h3>Recorded paper-admission decisions</h3>
          {auditDecisions.data?.decisions.length ? (
            <div className="evidence-card-list" role="list" aria-label="Audit decisions">
              {auditDecisions.data.decisions.map((decision) => (
                <article className="evidence-card" role="listitem" key={decision.audit_id}>
                  <div className="evidence-card-title">
                    <strong>{decision.candidate_id}</strong>
                    <span>{decision.outcome}</span>
                  </div>
                  <dl>
                    <div>
                      <dt>Audit</dt>
                      <dd>{decision.audit_id}</dd>
                    </div>
                    <div>
                      <dt>Auditor</dt>
                      <dd>{decision.auditor_id}</dd>
                    </div>
                    <div>
                      <dt>Issued</dt>
                      <dd>{formatUtc(decision.issued_at)}</dd>
                    </div>
                    <div>
                      <dt>Evidence hash</dt>
                      <dd className="mono-wrap">{decision.evidence_hash}</dd>
                    </div>
                    <div>
                      <dt>Decision hash</dt>
                      <dd className="mono-wrap">{decision.decision_hash}</dd>
                    </div>
                  </dl>
                  <p className="evidence-reasons">{decision.reason_codes.join(" · ")}</p>
                </article>
              ))}
            </div>
          ) : (
            <p className="muted-copy">No audit decisions in the current page.</p>
          )}

          <h3>Audit evidence index</h3>
          {auditEvidence.data?.evidence.length ? (
            <div className="evidence-card-list" role="list" aria-label="Audit evidence">
              {auditEvidence.data.evidence.map((item) => (
                <article className="evidence-card" role="listitem" key={item.reference}>
                  <div className="evidence-card-title">
                    <strong>{item.candidate_id}</strong>
                    <span>content verified</span>
                  </div>
                  <p>{item.reference}</p>
                  <p className="mono-wrap">{item.evidence_hash}</p>
                  <small>{item.decision_count} linked decision(s)</small>
                </article>
              ))}
            </div>
          ) : (
            <p className="muted-copy">No audit evidence in the current page.</p>
          )}
        </section>
      ) : null}

      {overview.data?.results_registry_configured ? (
        <section className="evidence-section" aria-labelledby="results-evidence-heading">
          <div className="evidence-section-heading">
            <div>
              <p className="eyebrow">Results Aggregator</p>
              <h2 id="results-evidence-heading">Immutable evidence registry</h2>
            </div>
            <span className="evidence-integrity-badge">
              revision {overview.data.results_registry_revision ?? "—"}
            </span>
          </div>
          {resultsEvidence.data?.evidence.length ? (
            <div className="evidence-card-list" role="list" aria-label="Results evidence">
              {resultsEvidence.data.evidence.map((item) => (
                <article className="evidence-card" role="listitem" key={item.evidence_id}>
                  <div className="evidence-card-title">
                    <strong>{item.evidence_id}</strong>
                    <span>
                      {item.status} · {item.severity}
                    </span>
                  </div>
                  <dl>
                    <div>
                      <dt>Experiment</dt>
                      <dd>{item.experiment_id}</dd>
                    </div>
                    <div>
                      <dt>Type</dt>
                      <dd>{item.evidence_type}</dd>
                    </div>
                    <div>
                      <dt>Producer</dt>
                      <dd>
                        {item.producer} · {item.producer_version}
                      </dd>
                    </div>
                    <div>
                      <dt>Relations</dt>
                      <dd>
                        {item.run_count} runs · {item.artifact_count} artifacts
                      </dd>
                    </div>
                    <div>
                      <dt>Evidence hash</dt>
                      <dd className="mono-wrap">{item.evidence_hash}</dd>
                    </div>
                    <div>
                      <dt>Content hash</dt>
                      <dd className="mono-wrap">{item.content_hash}</dd>
                    </div>
                  </dl>
                  <small>{item.limitations_count} limitation(s); raw text is not projected.</small>
                </article>
              ))}
            </div>
          ) : (
            <p className="muted-copy">No Results Aggregator evidence in the current page.</p>
          )}
        </section>
      ) : null}
    </section>
  );
}

function formatUtc(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "invalid timestamp";
  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "medium",
    timeStyle: "medium",
    timeZone: "UTC",
  }).format(date);
}
