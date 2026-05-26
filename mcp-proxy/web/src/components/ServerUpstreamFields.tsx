/** Shared URL / upstream auth / credential inputs for Add and Edit server forms. */

import { useState } from "react";
import type { UpstreamAuth } from "../types";

export function ServerUpstreamFields() {
  const [auth, setAuth] = useState<UpstreamAuth>("headers");

  return (
    <>
      <input
        name="url"
        placeholder="https://…/mcp"
        className="bg-surface border border-surface-border rounded-lg px-3 py-2 font-mono text-sm sm:col-span-2"
        required
      />
      <label className="sm:col-span-2 text-slate-400 text-sm">
        Upstream auth
        <select
          name="upstream_auth"
          value={auth}
          onChange={(e) => setAuth(e.target.value as UpstreamAuth)}
          className="mt-1 w-full bg-surface border border-surface-border rounded-lg px-3 py-2 font-mono text-sm text-white"
        >
          <option value="headers">Headers / Secret Manager (default)</option>
          <option value="cloud_run_iam">
            Cloud Run IAM (OIDC from proxy SA)
          </option>
          <option value="oauth2">OAuth2 (per-user link in Account)</option>
        </select>
      </label>
      {auth === "oauth2" ? (
        <>
          <input
            name="oauth_authorization_endpoint"
            placeholder="OAuth authorization URL"
            className="bg-surface border border-surface-border rounded-lg px-3 py-2 font-mono text-sm sm:col-span-2"
            required={auth === "oauth2"}
          />
          <input
            name="oauth_token_endpoint"
            placeholder="OAuth token URL"
            className="bg-surface border border-surface-border rounded-lg px-3 py-2 font-mono text-sm sm:col-span-2"
            required={auth === "oauth2"}
          />
          <input
            name="oauth_client_id"
            placeholder="OAuth client id"
            className="bg-surface border border-surface-border rounded-lg px-3 py-2 font-mono text-sm sm:col-span-2"
            required={auth === "oauth2"}
          />
          <input
            name="oauth_scopes"
            placeholder="Scopes (space-separated, optional)"
            className="bg-surface border border-surface-border rounded-lg px-3 py-2 font-mono text-sm sm:col-span-2"
          />
          <input
            name="oauth_client_secret_secret_id"
            placeholder="Secret Manager id for client secret (optional)"
            className="bg-surface border border-surface-border rounded-lg px-3 py-2 font-mono text-sm sm:col-span-2"
          />
          <input
            name="oauth_audience"
            placeholder="Audience (optional, e.g. Auth0 API identifier)"
            className="bg-surface border border-surface-border rounded-lg px-3 py-2 font-mono text-sm sm:col-span-2"
          />
          <label className="sm:col-span-2 text-slate-400 text-sm flex items-center gap-2">
            <select
              name="oauth_use_pkce"
              className="mt-1 flex-1 bg-surface border border-surface-border rounded-lg px-3 py-2 font-mono text-sm text-white"
              defaultValue="true"
            >
              <option value="true">PKCE enabled (recommended)</option>
              <option value="false">PKCE disabled</option>
            </select>
          </label>
          <p className="sm:col-span-2 text-slate-500 text-xs">
            Register redirect URI:{" "}
            <code className="text-slate-400">
              {"{MCP_PROXY_URL}"}/auth/upstream-oauth/callback
            </code>
            . Users connect from Account. Optional Secret Manager / inline
            header adds extra headers (not Authorization) merged with the OAuth
            Bearer.
          </p>
        </>
      ) : (
        <>
          <input
            name="credentials_secret_id"
            placeholder="Secret Manager secret ID (optional if using inline header below)"
            className="bg-surface border border-surface-border rounded-lg px-3 py-2 font-mono text-sm sm:col-span-2"
          />
          <input
            name="credentials_header"
            placeholder='Inline header: "X-API-Key: …" — use only one of secret ID or this line'
            className="bg-surface border border-surface-border rounded-lg px-3 py-2 font-mono text-sm sm:col-span-2"
          />
          <p className="sm:col-span-2 text-slate-500 text-xs">
            Provide at most one credential source. Cloud Run IAM: leave both
            empty or add an extra header (e.g. X-API-Key) if the upstream
            expects it.
          </p>
        </>
      )}
    </>
  );
}
