export type ApiStatus = "ok" | "degraded" | "error";

export interface VersionResponse {
  schema_version: 1;
  component: "market_data_application";
  api_version: "v1";
  build_version: string;
}

export interface HealthResponse {
  schema_version: 1;
  status: ApiStatus;
  component: "market_data_api";
  api_version: "v1";
  build_version: string;
  checks: Record<string, string>;
}

export interface Pagination {
  schema_version: 1;
  limit: number;
  offset: number;
  total: number;
}

export interface DatasetDetails {
  schema_version: 1;
  dataset_id: string;
  version: number;
  dataset_hash: string;
  manifest_uri: string;
  storage_uri: string;
  exchange_id: string;
  market_type: string;
  instrument_id: string;
  data_types: string[];
  timeframes: string[];
  start: string;
  end: string;
  dataset_schema_version: number;
  validation_status: string;
  unresolved_issues: string[];
  lineage: string[];
  compatible_consumers: string[];
  descriptor_version: number;
  registered_at: string;
}

export interface DatasetListResponse {
  schema_version: 1;
  items: DatasetDetails[];
  pagination: Pagination;
}

export interface ArtifactDescriptor {
  schema_version: 1;
  artifact_id: string;
  dataset_id: string;
  version: number;
  dataset_hash: string;
  filename: string;
  content_type: "application/zip";
  checksum_sha256: string;
  byte_size: number;
  file_count: number;
  created_at: string;
}

export type JobOperation = "download" | "update" | "validate" | "repair" | "backfill" | "export";

export type RunState =
  | "CREATED"
  | "VALIDATING_REQUEST"
  | "DISCOVERING_CAPABILITIES"
  | "RESOLVING_INSTRUMENT"
  | "PLANNING"
  | "DOWNLOADING"
  | "VERIFYING_RAW"
  | "NORMALIZING"
  | "VALIDATING_DATA"
  | "REPAIRING"
  | "BUILDING_DERIVED"
  | "HASHING"
  | "REGISTERING"
  | "EXPORTING"
  | "COMPLETED"
  | "COMPLETED_WITH_WARNINGS"
  | "PAUSED"
  | "CANCELLED"
  | "FAILED"
  | "DATASET_REJECTED";

export type JobEventType = "state" | "progress" | "log" | "warning" | "result" | "error";

export interface JobProgress {
  schema_version: 1;
  phase: RunState;
  determinate: boolean;
  completed_units: number;
  total_units: number | null;
  percent: number | null;
  message: string | null;
  counters: Record<string, number>;
  current_range_start: string | null;
  current_range_end: string | null;
}

export interface JobError {
  schema_version: 1;
  code: string;
  message: string;
  retryable: boolean;
  stage: string | null;
  details: Record<string, unknown>;
  correlation_id: string;
}

export interface JobSummary {
  schema_version: 1;
  job_id: string;
  operation: JobOperation;
  state: RunState;
  request_hash: string;
  backend_run_id: string | null;
  progress: JobProgress | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface JobDetails extends JobSummary {
  request: Record<string, unknown>;
  result: Record<string, unknown> | null;
  error: JobError | null;
  warnings: string[];
  correlation_id: string;
  actor_id: string | null;
}

export interface JobListResponse {
  schema_version: 1;
  items: JobSummary[];
  pagination: Pagination;
}

export interface JobEvent {
  schema_version: 1;
  event_id: number;
  job_id: string;
  event_type: JobEventType;
  state: RunState;
  timestamp: string;
  payload: Record<string, unknown>;
}

export interface ApiErrorDetail {
  schema_version: 1;
  code: string;
  message: string;
  details: Record<string, unknown>;
  correlation_id: string;
}

export interface ApiErrorEnvelope {
  schema_version: 1;
  error: ApiErrorDetail;
}
