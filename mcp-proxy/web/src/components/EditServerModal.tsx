import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import type {
  OAuth2UpstreamConfig,
  ServerConfig,
  UpstreamAuth,
} from "../types";

export interface EditServerModalProps {
  server: ServerConfig | null;
  open: boolean;
  onClose: () => void;
  onSave: (body: Record<string, unknown>) => Promise<void>;
  busy: boolean;
  error?: string | null;
  saveSucceeded: boolean;
  onSaveSucceededAck: () => void;
}

export function EditServerModal({
  server,
  open,
  onClose,
  onSave,
  busy,
  error,
  saveSucceeded,
  onSaveSucceededAck,
}: EditServerModalProps) {
  const [url, setUrl] = useState("");
  const [upstreamAuth, setUpstreamAuth] = useState<UpstreamAuth>("headers");
  const [secretId, setSecretId] = useState("");
  const [header, setHeader] = useState("");
  const [resetCreds, setResetCreds] = useState(false);
  const [resetOauth, setResetOauth] = useState(false);
  const [oauthAuthz, setOauthAuthz] = useState("");
  const [oauthToken, setOauthToken] = useState("");
  const [oauthClientId, setOauthClientId] = useState("");
  const [oauthScopes, setOauthScopes] = useState("");
  const [oauthClientSecretSid, setOauthClientSecretSid] = useState("");
  const [oauthAudience, setOauthAudience] = useState("");
  const [oauthUsePkce, setOauthUsePkce] = useState(true);
  const [localError, setLocalError] = useState<string | null>(null);

  useEffect(() => {
    if (!server || !open) return;
    setUrl(server.url);
    setUpstreamAuth(server.upstream_auth ?? "headers");
    setSecretId(server.credentials_secret_id ?? "");
    setHeader(server.credentials_header ?? "");
    setResetCreds(false);
    setResetOauth(false);
    const o = server.oauth;
    setOauthAuthz(o?.authorization_endpoint ?? "");
    setOauthToken(o?.token_endpoint ?? "");
    setOauthClientId(o?.client_id ?? "");
    setOauthScopes(o?.scopes ?? "");
    setOauthClientSecretSid(o?.client_secret_secret_id ?? "");
    setOauthAudience(o?.audience ?? "");
    setOauthUsePkce(o?.use_pkce !== false);
    setLocalError(null);
  }, [server, open]);

  if (!open || !server) return null;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!server) return;
    setLocalError(null);
    const u = url.trim();
    if (!u) {
      setLocalError("URL is required");
      return;
    }
    const sid = secretId.trim();
    const hdr = header.trim();
    if (upstreamAuth !== "oauth2" && !resetCreds && sid && hdr) {
      setLocalError("Provide only one of Secret Manager ID or inline header");
      return;
    }
    const body: Record<string, unknown> = {
      url: u,
      upstream_auth: upstreamAuth,
    };
    if (resetCreds) body.reset_credentials = true;
    if (upstreamAuth !== "oauth2" && sid) body.credentials_secret_id = sid;
    if (upstreamAuth !== "oauth2" && hdr) body.credentials_header = hdr;
    if (upstreamAuth === "oauth2") {
      if (resetOauth) {
        body.reset_oauth = true;
      } else {
        const authorization_endpoint = oauthAuthz.trim();
        const token_endpoint = oauthToken.trim();
        const client_id = oauthClientId.trim();
        if (!authorization_endpoint || !token_endpoint || !client_id) {
          setLocalError(
            "OAuth2 requires authorization URL, token URL, and client id",
          );
          return;
        }
        const oauth: OAuth2UpstreamConfig = {
          authorization_endpoint,
          token_endpoint,
          client_id,
          scopes: oauthScopes.trim(),
          use_pkce: oauthUsePkce,
        };
        if (oauthAudience.trim()) oauth.audience = oauthAudience.trim();
        if (oauthClientSecretSid.trim())
          oauth.client_secret_secret_id = oauthClientSecretSid.trim();
        body.oauth = oauth;
        if (sid) body.credentials_secret_id = sid;
        else if (hdr) body.credentials_header = hdr;
      }
    }
    if (upstreamAuth !== "oauth2" && server.upstream_auth === "oauth2") {
      body.reset_oauth = true;
    }
    if (resetOauth) body.reset_oauth = true;
    await onSave(body);
  }

  const err = localError || error;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60">
      <div
        className="w-full max-w-lg max-h-[90vh] overflow-y-auto rounded-xl border border-surface-border bg-surface shadow-xl"
        role="dialog"
        aria-modal="true"
        aria-labelledby="edit-server-title"
      >
        <div className="border-b border-surface-border px-5 py-4 flex justify-between items-center sticky top-0 bg-surface z-10">
          <h2 id="edit-server-title" className="text-white font-medium">
            Edit server{" "}
            <span className="font-mono text-accent">{server.id}</span>
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="text-slate-400 hover:text-white text-sm"
          >
            Close
          </button>
        </div>
        {saveSucceeded ? (
          <div className="p-5 space-y-3">
            <p className="text-emerald-400 text-sm">Saved successfully.</p>
            <p className="text-slate-500 text-sm">
              Run diagnostics to verify the upstream accepts the new settings
              (PATCH does not re-validate like Add).
            </p>
            <div className="flex flex-wrap gap-3">
              <Link
                to={`/servers/${encodeURIComponent(server.id)}`}
                className="text-accent hover:underline text-sm"
                onClick={onSaveSucceededAck}
              >
                Run diagnostics
              </Link>
              <button
                type="button"
                onClick={onSaveSucceededAck}
                className="text-slate-400 hover:text-white text-sm"
              >
                Back to list
              </button>
            </div>
          </div>
        ) : (
          <form className="p-5 space-y-3" onSubmit={handleSubmit}>
            <p className="text-slate-500 text-xs">
              Changing auth mode without updating the credential fields below
              keeps the stored Secret ID / inline header as-is. Use{" "}
              <strong className="text-slate-400">
                Clear stored credentials
              </strong>{" "}
              to remove both, then save optional new credentials.
            </p>
            <label className="block text-slate-400 text-sm">
              URL
              <input
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                className="mt-1 w-full bg-surface border border-surface-border rounded-lg px-3 py-2 font-mono text-sm text-white"
                required
              />
            </label>
            <label className="block text-slate-400 text-sm">
              Upstream auth
              <select
                value={upstreamAuth}
                onChange={(e) =>
                  setUpstreamAuth(e.target.value as UpstreamAuth)
                }
                className="mt-1 w-full bg-surface border border-surface-border rounded-lg px-3 py-2 font-mono text-sm text-white"
              >
                <option value="headers">Headers / Secret Manager</option>
                <option value="cloud_run_iam">
                  Cloud Run IAM (OIDC from proxy SA)
                </option>
                <option value="oauth2">OAuth2 (per-user)</option>
              </select>
            </label>
            {upstreamAuth === "oauth2" ? (
              <>
                <label className="flex items-center gap-2 text-slate-400 text-sm cursor-pointer">
                  <input
                    type="checkbox"
                    checked={resetOauth}
                    onChange={(e) => setResetOauth(e.target.checked)}
                    className="rounded border-surface-border"
                  />
                  Clear stored OAuth configuration
                </label>
                {!resetOauth && (
                  <div className="space-y-2 border border-surface-border rounded-lg p-3">
                    <input
                      value={oauthAuthz}
                      onChange={(e) => setOauthAuthz(e.target.value)}
                      placeholder="Authorization URL"
                      className="w-full bg-surface border border-surface-border rounded-lg px-3 py-2 font-mono text-sm text-white"
                      required
                    />
                    <input
                      value={oauthToken}
                      onChange={(e) => setOauthToken(e.target.value)}
                      placeholder="Token URL"
                      className="w-full bg-surface border border-surface-border rounded-lg px-3 py-2 font-mono text-sm text-white"
                      required
                    />
                    <input
                      value={oauthClientId}
                      onChange={(e) => setOauthClientId(e.target.value)}
                      placeholder="Client id"
                      className="w-full bg-surface border border-surface-border rounded-lg px-3 py-2 font-mono text-sm text-white"
                      required
                    />
                    <input
                      value={oauthScopes}
                      onChange={(e) => setOauthScopes(e.target.value)}
                      placeholder="Scopes (space-separated, optional)"
                      className="w-full bg-surface border border-surface-border rounded-lg px-3 py-2 font-mono text-sm text-white"
                    />
                    <input
                      value={oauthClientSecretSid}
                      onChange={(e) => setOauthClientSecretSid(e.target.value)}
                      placeholder="Secret Manager id for client secret (optional)"
                      className="w-full bg-surface border border-surface-border rounded-lg px-3 py-2 font-mono text-sm text-white"
                    />
                    <input
                      value={oauthAudience}
                      onChange={(e) => setOauthAudience(e.target.value)}
                      placeholder="Audience (optional)"
                      className="w-full bg-surface border border-surface-border rounded-lg px-3 py-2 font-mono text-sm text-white"
                    />
                    <label className="flex items-center gap-2 text-slate-400 text-sm">
                      <input
                        type="checkbox"
                        checked={oauthUsePkce}
                        onChange={(e) => setOauthUsePkce(e.target.checked)}
                        className="rounded border-surface-border"
                      />
                      Use PKCE
                    </label>
                    <p className="text-slate-500 text-xs">
                      Callback:{" "}
                      <code className="text-slate-400 break-all">
                        {"{MCP_PROXY_URL}"}/auth/upstream-oauth/callback
                      </code>
                    </p>
                  </div>
                )}
              </>
            ) : null}
            <label className="flex items-center gap-2 text-slate-400 text-sm cursor-pointer">
              <input
                type="checkbox"
                checked={resetCreds}
                onChange={(e) => setResetCreds(e.target.checked)}
                className="rounded border-surface-border"
              />
              Clear stored credentials
            </label>
            <label className="block text-slate-400 text-sm">
              Secret Manager secret ID
              <input
                value={secretId}
                onChange={(e) => setSecretId(e.target.value)}
                className="mt-1 w-full bg-surface border border-surface-border rounded-lg px-3 py-2 font-mono text-sm text-white"
                placeholder="optional"
              />
            </label>
            <label className="block text-slate-400 text-sm">
              Inline header
              <input
                value={header}
                onChange={(e) => setHeader(e.target.value)}
                className="mt-1 w-full bg-surface border border-surface-border rounded-lg px-3 py-2 font-mono text-sm text-white"
                placeholder='e.g. "X-API-Key: …"'
              />
            </label>
            <p className="text-slate-500 text-xs">
              Omit credential changes by leaving both fields empty (unless clear
              is checked).
            </p>
            {err && <p className="text-red-400 text-sm">{err}</p>}
            <div className="flex gap-3 pt-2">
              <button
                type="submit"
                disabled={busy}
                className="px-4 py-2 rounded-lg bg-accent text-surface font-medium text-sm hover:opacity-90 disabled:opacity-50"
              >
                {busy ? "Saving…" : "Save"}
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={onClose}
                className="px-4 py-2 rounded-lg border border-surface-border text-slate-300 text-sm hover:bg-surface-raised"
              >
                Cancel
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
