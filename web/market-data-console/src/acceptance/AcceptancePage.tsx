import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { FormEvent } from "react";

import type { ControlPlaneApiClient } from "../control/client";
import { AcceptanceClientError, AcceptanceControlApiClient } from "./client";
import type {
  AcceptanceMutation,
  AcceptanceMutationResult,
  AcceptanceRunMutationRequest,
  AcceptanceStartRequest,
  AcceptanceStatus,
} from "./contracts";
import "./acceptance.css";

const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$/;
const DIGEST = /^[0-9a-f]{64}$/;
const ACTIVE = new Set<AcceptanceStatus>([
  "QUEUED",
  "STARTING",
  "RUNNING",
  "STOPPING",
  "RECOVERING",
]);

interface LastMutation {
  kind: AcceptanceMutation;
  request: AcceptanceStartRequest | AcceptanceRunMutationRequest;
}

export interface AcceptancePageProps {
  controlApi: ControlPlaneApiClient;
  acceptanceApi: AcceptanceControlApiClient;
  readerAuthRevision: number;
  operatorReady: boolean;
}

export function AcceptancePage({
  controlApi,
  acceptanceApi,
  readerAuthRevision,
  operatorReady,
}: AcceptancePageProps) {
  const queryClient = useQueryClient();
  const [datasetId, setDatasetId] = useState("");
  const [datasetDigest, setDatasetDigest] = useState("");
  const [strategyId, setStrategyId] = useState("");
  const [strategyDigest, setStrategyDigest] = useState("");
  const [configDigest, setConfigDigest] = useState("");
  const [soak24h, setSoak24h] = useState(false);
  const [soak72h, setSoak72h] = useState(false);
  const [lastMutation, setLastMutation] = useState<LastMutation | null>(null);

  const health = useQuery({
    queryKey: ["control-component-health", readerAuthRevision, "acceptance"],
    queryFn: ({ signal }) => controlApi.getComponentHealth("acceptance", signal),
    retry: false,
    refetchInterval: 5_000,
  });
  const current = useQuery({
    queryKey: ["acceptance-current", readerAuthRevision],
    queryFn: ({ signal }) => acceptanceApi.current(signal),
    retry: false,
    refetchInterval: 5_000,
  });
  const mutation = useMutation({
    mutationFn: (entry: LastMutation) => executeMutation(acceptanceApi, entry),
    onSuccess: () => refreshAcceptance(queryClient),
  });

  const run = current.data?.run ?? null;
  const normalized = {
    datasetId: datasetId.trim(),
    datasetDigest: datasetDigest.trim(),
    strategyId: strategyId.trim(),
    strategyDigest: strategyDigest.trim(),
    configDigest: configDigest.trim(),
  };
  const startValid =
    IDENTIFIER.test(normalized.datasetId) &&
    DIGEST.test(normalized.datasetDigest) &&
    IDENTIFIER.test(normalized.strategyId) &&
    DIGEST.test(normalized.strategyDigest) &&
    DIGEST.test(normalized.configDigest) &&
    (!soak72h || soak24h);
  const canStart =
    operatorReady &&
    run === null &&
    startValid &&
    !mutation.isPending;
  const canStopOrCancel =
    operatorReady && run !== null && ACTIVE.has(run.status) && !mutation.isPending;
  const canRetry =
    operatorReady &&
    run !== null &&
    (run.status === "FAILED" || run.status === "BLOCKED") &&
    !mutation.isPending;
  const canRetrySameRequest =
    operatorReady &&
    lastMutation !== null &&
    mutation.error instanceof AcceptanceClientError &&
    mutation.error.retryable &&
    !mutation.isPending;

  const submitStart = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canStart) return;
    queueMutation({
      kind: "START",
      request: {
        schema_version: 1,
        request_id: newRequestId("start"),
        dataset_id: normalized.datasetId,
        dataset_digest: normalized.datasetDigest,
        strategy_id: normalized.strategyId,
        strategy_digest: normalized.strategyDigest,
        config_digest: normalized.configDigest,
        enable_soak_24h: soak24h,
        enable_soak_72h: soak72h,
      },
    });
  };

  const queueRunMutation = (kind: "STOP" | "CANCEL" | "RETRY") => {
    if (run === null) return;
    queueMutation({
      kind,
      request: {
        schema_version: 1,
        request_id: newRequestId(kind.toLowerCase()),
        acceptance_run_id: run.acceptance_run_id,
      },
    });
  };

  const queueMutation = (entry: LastMutation) => {
    setLastMutation(entry);
    mutation.mutate(entry);
  };

  return (
    <section className="dashboard-page acceptance-page" aria-label="System Acceptance workspace">
      <header className="dashboard-page-header">
        <div className="page-title-row">
          <div>
            <p className="eyebrow">Durable owner · browser-independent</p>
            <h2>System Acceptance</h2>
          </div>
          <div className="mode-banner compact" aria-label="Текущий режим: PAPER">
            <span aria-hidden="true">●</span>
            PAPER
          </div>
        </div>
        <p>
          Текущий acceptance/soak читается из durable owner. Закрытие или перезагрузка Dashboard не
          останавливает run и не создаёт новый таймер или state machine.
        </p>
      </header>

      <section className="acceptance-health-grid" aria-label="Acceptance health summary">
        <SummaryCard
          label="Component"
          value={health.data?.status ?? (health.isError ? "UNAVAILABLE" : "LOADING")}
        />
        <SummaryCard label="Run" value={run?.status ?? "NO RUN"} attention={run?.status === "BLOCKED" || run?.status === "FAILED"} />
        <SummaryCard label="Phase" value={run?.phase ?? "—"} />
        <SummaryCard label="Restarts" value={run ? String(run.restart_count) : "—"} />
      </section>

      {!operatorReady ? (
        <Notice
          title="Mutation access is locked"
          text="Введите Control operator token в глобальной панели. Downstream Acceptance credentials остаются только на сервере."
        />
      ) : null}
      {current.error ? <Failure error={current.error} /> : null}
      {mutation.error ? <Failure error={mutation.error} /> : null}
      {mutation.data ? <MutationReceipt result={mutation.data} /> : null}
      {canRetrySameRequest && lastMutation ? (
        <button type="button" className="secondary-action" onClick={() => mutation.mutate(lastMutation)}>
          Retry same idempotency request
        </button>
      ) : null}

      {run ? (
        <>
          <section className="control-panel acceptance-run-panel" aria-labelledby="acceptance-run-heading">
            <div className="control-panel-heading">
              <div>
                <p className="eyebrow">Authoritative durable state</p>
                <h3 id="acceptance-run-heading">Current run</h3>
              </div>
              <span className={`acceptance-status acceptance-status-${run.status.toLowerCase()}`}>
                {run.status}
              </span>
            </div>
            <dl className="acceptance-details-grid">
              <Detail label="Run ID" value={run.acceptance_run_id} mono />
              <Detail label="Phase" value={run.phase} />
              <Detail label="Active test time" value={formatDuration(run.active_test_time)} />
              <Detail label="Wall-clock elapsed" value={formatDuration(run.wall_clock_elapsed)} />
              <Detail label="Updated" value={formatUtc(run.updated_at)} />
              <Detail label="Next action" value={run.next_action ?? "—"} />
              <Detail label="Checkpoint" value={run.checkpoint?.phase ?? "not published"} />
              <Detail label="Restart count" value={String(run.restart_count)} />
            </dl>
            {run.error ? (
              <p className="status-error" role="alert">
                Owner error: <code>{run.error}</code>
              </p>
            ) : null}
            <div className="acceptance-actions" aria-label="Acceptance owner actions">
              <button
                type="button"
                disabled={!canStopOrCancel}
                onClick={() => queueRunMutation("STOP")}
              >
                Stop at safe checkpoint
              </button>
              <button
                type="button"
                className="danger-action"
                disabled={!canStopOrCancel}
                onClick={() => {
                  if (window.confirm("Cancel this durable PAPER Acceptance run at the next safe checkpoint?")) {
                    queueRunMutation("CANCEL");
                  }
                }}
              >
                Cancel run
              </button>
              <button type="button" disabled={!canRetry} onClick={() => queueRunMutation("RETRY")}>
                Retry durable run
              </button>
            </div>
          </section>

          <section className="acceptance-grid-two">
            <section className="control-panel" aria-labelledby="acceptance-candidate-heading">
              <div className="control-panel-heading">
                <div>
                  <p className="eyebrow">Immutable candidate</p>
                  <h3 id="acceptance-candidate-heading">Pinned identity</h3>
                </div>
              </div>
              <dl className="acceptance-details-grid single">
                <Detail label="full-RC" value={run.identity.full_rc_sha} mono />
                <Detail label="Dataset" value={run.identity.dataset_id} mono />
                <Detail label="Dataset SHA-256" value={run.identity.dataset_digest} mono />
                <Detail label="Strategy" value={run.identity.strategy_id} mono />
                <Detail label="Strategy SHA-256" value={run.identity.strategy_digest} mono />
                <Detail label="Config SHA-256" value={run.identity.config_digest} mono />
                <Detail label="24h soak" value={run.identity.enable_soak_24h ? "enabled" : "disabled"} />
                <Detail label="72h soak" value={run.identity.enable_soak_72h ? "enabled" : "disabled"} />
              </dl>
            </section>

            <section className="control-panel" aria-labelledby="acceptance-observability-heading">
              <div className="control-panel-heading">
                <div>
                  <p className="eyebrow">Bounded owner projection</p>
                  <h3 id="acceptance-observability-heading">Resources & evidence</h3>
                </div>
              </div>
              <ResourceSummary run={run} />
              {run.warnings.length ? (
                <ul className="acceptance-warning-list">
                  {run.warnings.map((warning) => <li key={warning}>{warning}</li>)}
                </ul>
              ) : (
                <p className="muted-copy">No owner warnings.</p>
              )}
              <div className="acceptance-evidence-list" role="list" aria-label="Acceptance evidence">
                {run.evidence.map((item) => (
                  <article role="listitem" key={`${item.kind}:${item.digest}`}>
                    <strong>{item.kind}</strong>
                    <code>{item.reference}</code>
                    <small>{item.digest}</small>
                  </article>
                ))}
              </div>
            </section>
          </section>
        </>
      ) : (
        <section className="control-panel acceptance-start-panel" aria-labelledby="acceptance-start-heading">
          <div className="control-panel-heading">
            <div>
              <p className="eyebrow">No durable run</p>
              <h3 id="acceptance-start-heading">Start candidate acceptance</h3>
              <p>
                full-RC SHA и PAPER mode берёт owner на сервере. Browser передаёт только bounded
                registered candidate identities и soak options.
              </p>
            </div>
          </div>
          <form className="acceptance-start-form" onSubmit={submitStart}>
            <TextField label="Dataset ID" value={datasetId} onChange={setDatasetId} maxLength={128} />
            <TextField label="Dataset SHA-256" value={datasetDigest} onChange={setDatasetDigest} maxLength={64} mono />
            <TextField label="Strategy ID" value={strategyId} onChange={setStrategyId} maxLength={128} />
            <TextField label="Strategy SHA-256" value={strategyDigest} onChange={setStrategyDigest} maxLength={64} mono />
            <TextField label="Config SHA-256" value={configDigest} onChange={setConfigDigest} maxLength={64} mono />
            <label className="acceptance-check">
              <input type="checkbox" checked={soak24h} onChange={(event) => {
                setSoak24h(event.target.checked);
                if (!event.target.checked) setSoak72h(false);
              }} />
              Enable 24h soak
            </label>
            <label className="acceptance-check">
              <input type="checkbox" checked={soak72h} disabled={!soak24h} onChange={(event) => setSoak72h(event.target.checked)} />
              Enable 72h soak
            </label>
            <button type="submit" disabled={!canStart}>
              {mutation.isPending ? "Starting…" : "Start durable acceptance"}
            </button>
          </form>
          {!startValid && (datasetId || datasetDigest || strategyId || strategyDigest || configDigest) ? (
            <p className="status-error" role="alert">
              IDs must be portable identifiers; dataset/strategy/config hashes must be lowercase SHA-256.
            </p>
          ) : null}
        </section>
      )}
    </section>
  );
}

async function executeMutation(
  api: AcceptanceControlApiClient,
  entry: LastMutation,
): Promise<AcceptanceMutationResult> {
  if (entry.kind === "START") return api.start(entry.request as AcceptanceStartRequest);
  const request = entry.request as AcceptanceRunMutationRequest;
  if (entry.kind === "STOP") return api.stop(request);
  if (entry.kind === "CANCEL") return api.cancel(request);
  return api.retry(request);
}

export function refreshAcceptance(queryClient: ReturnType<typeof useQueryClient>) {
  void queryClient.invalidateQueries({ queryKey: ["acceptance-current"] });
  void queryClient.invalidateQueries({ queryKey: ["control-component-health"] });
}

function newRequestId(kind: string): string {
  return `dashboard-acceptance-${kind}-${crypto.randomUUID()}`;
}

function SummaryCard({ label, value, attention = false }: { label: string; value: string; attention?: boolean }) {
  return <article className={attention ? "attention" : ""}><span>{label}</span><strong>{value}</strong></article>;
}

function Detail({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return <div><dt>{label}</dt><dd className={mono ? "mono-wrap" : undefined}>{value}</dd></div>;
}

function TextField({
  label,
  value,
  onChange,
  maxLength,
  mono = false,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  maxLength: number;
  mono?: boolean;
}) {
  return (
    <label>
      <span>{label}</span>
      <input
        className={mono ? "mono-input" : undefined}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        autoComplete="off"
        spellCheck={false}
        maxLength={maxLength}
      />
    </label>
  );
}

function ResourceSummary({ run }: { run: NonNullable<Awaited<ReturnType<AcceptanceControlApiClient["current"]>>["run"]> }) {
  const latest = run.resources.at(-1) ?? null;
  if (latest === null) return <p className="muted-copy">No resource sample published.</p>;
  return (
    <dl className="acceptance-resource-grid">
      <Detail label="CPU" value={`${latest.cpu_percent.toFixed(1)}%`} />
      <Detail label="RAM" value={formatBytes(latest.ram_bytes)} />
      <Detail label="Disk free" value={formatBytes(latest.disk_free_bytes)} />
      <Detail label="Sample" value={formatUtc(latest.timestamp)} />
    </dl>
  );
}

function MutationReceipt({ result }: { result: AcceptanceMutationResult }) {
  return (
    <div className="acceptance-receipt" role="status">
      <strong>{result.mutation} accepted</strong>
      <span>{result.acceptance_run_id} · {result.status} · {result.phase}</span>
      <small>{result.replayed ? "durable replay of the same request" : `request ${result.request_id}`}</small>
    </div>
  );
}

function Notice({ title, text }: { title: string; text: string }) {
  return <div className="acceptance-notice" role="note"><strong>{title}</strong><span>{text}</span></div>;
}

function Failure({ error }: { error: Error }) {
  if (error instanceof AcceptanceClientError) {
    return (
      <div className="acceptance-error" role="alert">
        <strong>{error.code}</strong>
        <span>{error.message}</span>
        {error.action ? <small>{error.action}</small> : null}
      </div>
    );
  }
  return <div className="acceptance-error" role="alert"><strong>Acceptance unavailable</strong><span>Owner projection could not be read safely.</span></div>;
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remaining = Math.floor(seconds % 60);
  return `${hours}h ${minutes}m ${remaining}s`;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  return `${(bytes / 1024 ** 3).toFixed(1)} GiB`;
}

function formatUtc(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "invalid timestamp";
  return `${new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "medium",
    timeStyle: "medium",
    timeZone: "UTC",
  }).format(date)} UTC`;
}