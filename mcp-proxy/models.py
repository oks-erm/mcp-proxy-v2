"""Pydantic models for MCP Proxy."""

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

UpstreamAuth = Literal["headers", "cloud_run_iam", "oauth2"]


class OAuth2UpstreamConfig(BaseModel):
    """OAuth 2.0 settings for per-user upstream authorization (authorization code + PKCE)."""

    authorization_endpoint: str = Field(..., min_length=1)
    token_endpoint: str = Field(..., min_length=1)
    client_id: str = Field(..., min_length=1)
    scopes: str = Field("", description="Space-separated OAuth scopes")
    client_secret_secret_id: Optional[str] = Field(
        None,
        min_length=1,
        description="Optional Secret Manager secret id whose string value is the client secret",
    )
    use_pkce: bool = True
    audience: Optional[str] = Field(None, description="Optional token audience (e.g. Auth0 resource server)")
    extra_token_params: Optional[dict[str, str]] = Field(
        None,
        description="Extra form fields for token requests (vendor-specific)",
    )


class ServerConfigCreate(BaseModel):
    """Payload for creating a new MCP server config."""

    id: str = Field(..., min_length=1, pattern=r"^[a-zA-Z0-9_-]+$")
    url: str = Field(..., min_length=1)
    credentials_secret_id: Optional[str] = Field(
        None,
        min_length=1,
        description="Secret Manager secret ID containing JSON headers",
    )
    credentials_header: Optional[str] = Field(
        None,
        min_length=1,
        description='Inline header in mcp.json format, e.g. "X-API-Key: abc123" or "xc-mcp-token: token"',
    )
    upstream_auth: UpstreamAuth = Field(
        "headers",
        description='Use "headers" with secret or inline header; "cloud_run_iam" for OIDC from proxy SA; "oauth2" for per-user OAuth',
    )
    oauth: Optional[OAuth2UpstreamConfig] = None
    enabled: bool = True

    @model_validator(mode="after")
    def validate_credentials(self):
        if self.upstream_auth == "oauth2":
            if not self.oauth:
                raise ValueError(
                    'upstream_auth "oauth2" requires an "oauth" object with authorization and token endpoints'
                )
            if self.credentials_secret_id and self.credentials_header:
                raise ValueError("Provide only one of credentials_secret_id or credentials_header")
            return self
        if self.upstream_auth == "cloud_run_iam":
            if self.credentials_secret_id and self.credentials_header:
                raise ValueError("Provide only one of credentials_secret_id or credentials_header")
            if self.oauth:
                raise ValueError("oauth config is only valid when upstream_auth is oauth2")
            return self
        if self.credentials_secret_id and self.credentials_header:
            raise ValueError("Provide only one of credentials_secret_id or credentials_header")
        if self.oauth:
            raise ValueError("oauth config is only valid when upstream_auth is oauth2")
        return self

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "id": "server-a",
                    "url": "https://mcp.example.com/mcp",
                    "credentials_header": "X-API-Key: your-api-key",
                    "enabled": True,
                },
                {
                    "id": "server-b",
                    "url": "https://another.example.com/endpoint",
                    "credentials_header": "Authorization: Bearer your-token",
                    "enabled": True,
                },
                {
                    "id": "server-c",
                    "url": "https://mcp.example.com/mcp",
                    "credentials_secret_id": "my-secret-id",
                    "enabled": True,
                },
                {
                    "id": "notion",
                    "url": "https://notion-bridge-XXXX.run.app/mcp",
                    "enabled": True,
                },
            ]
        }
    }


class ServerConfigUpdate(BaseModel):
    """Payload for updating an MCP server config (partial)."""

    url: Optional[str] = Field(None, min_length=1)
    credentials_secret_id: Optional[str] = Field(None, min_length=1)
    credentials_header: Optional[str] = Field(
        None,
        min_length=1,
        description='Inline header in mcp.json format, e.g. "X-API-Key: abc123"',
    )
    upstream_auth: Optional[UpstreamAuth] = None
    oauth: Optional[OAuth2UpstreamConfig] = None
    reset_oauth: bool = Field(False, description="If true, clear stored oauth config on the server document")
    reset_credentials: bool = Field(
        False,
        description="If true, clear credentials_secret_id and credentials_header on the server document",
    )
    enabled: Optional[bool] = None
    reset_credentials: bool = Field(
        False,
        description="If true, clear stored credentials before applying new credential fields (if any).",
    )

    @model_validator(mode="after")
    def not_both_credentials(self):
        if self.credentials_secret_id is not None and self.credentials_header is not None:
            raise ValueError("Provide only one of credentials_secret_id or credentials_header")
        if self.upstream_auth == "cloud_run_iam":
            if self.credentials_secret_id is not None and self.credentials_header is not None:
                raise ValueError("Provide only one of credentials_secret_id or credentials_header")
        return self

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"enabled": False},
                {"url": "https://mcp.example.com/mcp"},
                {"credentials_header": "X-API-Key: your-new-key"},
            ]
        }
    }


class ServerConfig(BaseModel):
    """Full MCP server config as stored/returned."""

    id: str
    url: str
    credentials_secret_id: str = ""
    credentials_header: str = ""
    upstream_auth: UpstreamAuth = "headers"
    oauth: Optional[OAuth2UpstreamConfig] = None
    enabled: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ServerConfigBatchResponse(BaseModel):
    """Response for batch create."""

    created: list[ServerConfig] = Field(default_factory=list)
    errors: list[dict] = Field(default_factory=list)


class UpstreamOAuthConnectionStatus(BaseModel):
    server_id: str
    connected: bool


class UpstreamOAuthConnectionsResponse(BaseModel):
    servers: list[UpstreamOAuthConnectionStatus]
    deployment_oauth2_server_count: int = Field(
        0,
        description="Enabled servers with normalized upstream_auth oauth2 (for empty-state hints)",
    )


class ManagedWorkflowRecord(BaseModel):
    """Metadata for n8n workflows created through the MCP proxy."""

    workflow_id: str
    name: str
    summary: str
    editor_url: Optional[str] = None
    server_id: str = "n8n"
    created_by_user_id: str
    created_by_email: str = ""
    created_by_kind: Literal["human", "agent"] = "human"
    created_by_label: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None


ManagedAppStatus = Literal["pending_review", "approved", "deleted", "delete_failed"]
ManagedAppDeleteMode = Literal["cloud_run_only", "full_cleanup"]


class ManagedAppRecord(BaseModel):
    """Metadata for dashboard apps deployed through the Cloud Run deployer MCP."""

    app_id: str
    name: str
    summary: str
    data_access_summary: str
    data_connections: list[dict[str, Any]] = Field(default_factory=list)
    service_name: str
    service_url: Optional[str] = None
    project_id: str
    region: str
    runtime_service_account: str = ""
    framework: str = ""
    repo_url: str = ""
    approved_url: Optional[str] = None
    version: str = ""
    commit_sha: str = ""
    build_id: str = ""
    image_digest: str = ""
    cloud_run_revision: str = ""
    status: ManagedAppStatus = "pending_review"
    created_by_user_id: str
    created_by_email: str = ""
    created_by_kind: Literal["human", "agent"] = "human"
    created_by_label: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None
    delete_mode: Optional[ManagedAppDeleteMode] = None


class ManagedAppSecretSetRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    value: str = Field(..., min_length=1)


class ManagedAppSecretMetadata(BaseModel):
    app_id: str
    name: str
    key_version: str = "v1"
    created_by_user_id: str = ""
    updated_by_user_id: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
