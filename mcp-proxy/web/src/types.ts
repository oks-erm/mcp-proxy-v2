export type UserRole = "admin" | "power_user" | "user";
export type UserStatus = "active" | "pending" | "rejected";
export type UserKind = "human" | "agent";

export interface UserProfile {
  id: string;
  email: string;
  kind?: UserKind;
  agent_name?: string;
  agent_url?: string;
  role: UserRole;
  status: UserStatus;
  created_at?: string | null;
  updated_at?: string | null;
}

export type UpstreamAuth = "headers" | "cloud_run_iam" | "oauth2";

export interface OAuth2UpstreamConfig {
  authorization_endpoint: string;
  token_endpoint: string;
  client_id: string;
  scopes?: string;
  client_secret_secret_id?: string;
  use_pkce?: boolean;
  audience?: string;
  extra_token_params?: Record<string, string>;
}

export interface ServerConfig {
  id: string;
  url: string;
  credentials_secret_id: string;
  credentials_header: string;
  upstream_auth: UpstreamAuth;
  oauth?: OAuth2UpstreamConfig | null;
  enabled: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface UpstreamOAuthConnectionStatus {
  server_id: string;
  connected: boolean;
}

export interface UpstreamOAuthConnectionsResponse {
  servers: UpstreamOAuthConnectionStatus[];
  /** Enabled servers with oauth2 upstream auth (normalized); used for empty-state copy */
  deployment_oauth2_server_count?: number;
}

export interface ServerConfigBatchResponse {
  created: ServerConfig[];
  errors: { index?: number; id?: string; detail?: string }[];
}

export interface ManagedWorkflowRecord {
  workflow_id: string;
  name: string;
  summary: string;
  editor_url?: string | null;
  server_id: string;
  created_by_user_id: string;
  created_by_email: string;
  created_by_kind: "human" | "agent";
  created_by_label: string;
  created_at?: string | null;
  updated_at?: string | null;
  deleted_at?: string | null;
}

export type ManagedAppStatus =
  | "pending_review"
  | "approved"
  | "deleted"
  | "delete_failed";

export interface ManagedAppRecord {
  app_id: string;
  name: string;
  summary: string;
  data_access_summary: string;
  data_connections: Record<string, unknown>[];
  service_name: string;
  service_url?: string | null;
  project_id: string;
  region: string;
  runtime_service_account: string;
  framework?: string;
  repo_url?: string;
  approved_url?: string | null;
  version?: string;
  commit_sha?: string;
  build_id?: string;
  image_digest?: string;
  cloud_run_revision?: string;
  status: ManagedAppStatus;
  created_by_user_id: string;
  created_by_email: string;
  created_by_kind: "human" | "agent";
  created_by_label: string;
  created_at?: string | null;
  updated_at?: string | null;
  approved_by?: string | null;
  approved_at?: string | null;
  deleted_at?: string | null;
  delete_mode?: "cloud_run_only" | "full_cleanup" | null;
}

export interface PermissionItem {
  server_id: string;
  read: boolean;
  write: boolean;
}

export interface UsageResultCounts {
  success: number;
  error: number;
  denied: number;
}

export interface UsageBreakdownItem {
  name: string;
  total: number;
  result_counts: UsageResultCounts;
}

export interface UserUsageSummary {
  window_days: number;
  authorized_request_count: number;
  tool_call_count: number;
  result_counts: UsageResultCounts;
  last_activity_at?: string | null;
  unique_servers_count: number;
  unique_tools_count: number;
  top_servers: UsageBreakdownItem[];
  top_tools: UsageBreakdownItem[];
  log_explorer_url: string;
  truncated: boolean;
}

export interface UserUsageRankingItem {
  user_id: string;
  identity_label: string;
  kind: UserKind;
  email: string;
  tool_call_count: number;
  authorized_request_count: number;
  result_counts: UsageResultCounts;
  last_activity_at?: string | null;
  log_explorer_url: string;
}

export interface UserUsageLeaderboard {
  window_days: number;
  truncated: boolean;
  users: UserUsageRankingItem[];
}

export interface AccessRequest {
  id: string;
  user_id: string;
  email: string;
  requested_at: string | null;
  status: string;
}

export interface ImprovementRequestErrorContext {
  failed_tool_name?: string | null;
  error_message?: string | null;
  error_code?: number | null;
}

export interface ImprovementRequest {
  id: string;
  requester_user_id: string;
  requester_email: string;
  requester_kind: "human" | "agent";
  requester_label: string;
  summary: string;
  details: string;
  tool_names: string[];
  server_ids: string[];
  status: "pending" | "approved";
  error_context?: ImprovementRequestErrorContext | null;
  created_at?: string | null;
  updated_at?: string | null;
  reviewed_by?: string | null;
  reviewed_at?: string | null;
}

export interface SkillUpdateRequest {
  id: string;
  skill_name: string;
  requester_user_id: string;
  requester_email: string;
  requester_kind: "human" | "agent";
  requester_label: string;
  summary: string;
  details: string;
  desired_outcome: string;
  status: "pending" | "approved";
  created_at?: string | null;
  updated_at?: string | null;
  reviewed_by?: string | null;
  reviewed_at?: string | null;
}

export interface AdminMetricsSnapshot {
  process_id: number;
  boot_id: string;
  counters: {
    mcp_denied_missing_key: number;
    mcp_denied_invalid_user: number;
    mcp_requests_authorized: number;
    mcp_errors: number;
  };
  by_method: Record<string, number>;
}

declare global {
  interface Window {
    google?: {
      accounts: {
        id: {
          initialize: (cfg: Record<string, unknown>) => void;
          renderButton: (el: HTMLElement, cfg: Record<string, unknown>) => void;
          prompt: () => void;
        };
      };
    };
  }
}
