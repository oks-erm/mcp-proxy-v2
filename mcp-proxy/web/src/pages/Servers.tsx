import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { apiFetchWithRefresh } from "../api/client";
import { EditServerModal } from "../components/EditServerModal";
import { ServerUpstreamFields } from "../components/ServerUpstreamFields";
import type {
  OAuth2UpstreamConfig,
  ServerConfig,
  ServerConfigBatchResponse,
  UpstreamAuth,
} from "../types";

async function fetchServers() {
  const r = await apiFetchWithRefresh("/admin/servers");
  if (!r.ok) throw new Error(await r.text());
  return r.json() as Promise<ServerConfig[]>;
}

export function ServersPage() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["admin", "servers"], queryFn: fetchServers });

  const [editTarget, setEditTarget] = useState<ServerConfig | null>(null);
  const [editModalOpen, setEditModalOpen] = useState(false);
  const [editSaveSucceeded, setEditSaveSucceeded] = useState(false);

  const toggle = useMutation({
    mutationFn: async ({ id, enabled }: { id: string; enabled: boolean }) => {
      const r = await apiFetchWithRefresh(
        `/admin/servers/${encodeURIComponent(id)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled }),
        },
      );
      if (!r.ok) throw new Error(await r.text());
      return r.json() as Promise<ServerConfig>;
    },
    onSuccess: () =>
      void qc.invalidateQueries({ queryKey: ["admin", "servers"] }),
  });

  const addServer = useMutation({
    mutationFn: async (body: {
      id: string;
      url: string;
      upstream_auth: UpstreamAuth;
      credentials_secret_id?: string;
      credentials_header?: string;
      oauth?: OAuth2UpstreamConfig;
    }) => {
      const r = await apiFetchWithRefresh("/admin/servers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify([{ ...body, enabled: true }]),
      });
      if (!r.ok) throw new Error(await r.text());
      return r.json() as Promise<ServerConfigBatchResponse>;
    },
    onSuccess: () =>
      void qc.invalidateQueries({ queryKey: ["admin", "servers"] }),
  });

  const patchServer = useMutation({
    mutationFn: async ({
      id,
      body,
    }: {
      id: string;
      body: Record<string, unknown>;
    }) => {
      const r = await apiFetchWithRefresh(
        `/admin/servers/${encodeURIComponent(id)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      if (!r.ok) throw new Error(await r.text());
      return r.json() as Promise<ServerConfig>;
    },
    onSuccess: () =>
      void qc.invalidateQueries({ queryKey: ["admin", "servers"] }),
  });

  function openEdit(s: ServerConfig) {
    patchServer.reset();
    setEditTarget(s);
    setEditSaveSucceeded(false);
    setEditModalOpen(true);
  }

  function closeEdit() {
    setEditModalOpen(false);
    setEditTarget(null);
    setEditSaveSucceeded(false);
    patchServer.reset();
  }

  return (
    <div className="space-y-8">
      <h1 className="text-2xl font-semibold text-white">MCP servers</h1>
      <AddServerForm
        onSubmit={(data) => addServer.mutate(data)}
        busy={addServer.isPending}
        error={addServer.error?.message}
      />
      {q.isLoading && <p className="text-slate-500">Loading…</p>}
      {q.error && <p className="text-red-400">{String(q.error)}</p>}
      <div className="overflow-x-auto rounded-xl border border-surface-border">
        <table className="w-full text-sm text-left">
          <thead className="bg-surface-raised text-slate-400 uppercase text-xs">
            <tr>
              <th className="px-4 py-3 font-medium">ID</th>
              <th className="px-4 py-3 font-medium">URL</th>
              <th className="px-4 py-3 font-medium">Auth</th>
              <th className="px-4 py-3 font-medium">Enabled</th>
              <th className="px-4 py-3 font-medium" />
            </tr>
          </thead>
          <tbody className="divide-y divide-surface-border">
            {q.data?.map((s) => (
              <tr key={s.id} className="hover:bg-surface-raised/50">
                <td className="px-4 py-3 font-mono text-accent">{s.id}</td>
                <td className="px-4 py-3 font-mono text-slate-400 max-w-md truncate">
                  {s.url}
                </td>
                <td className="px-4 py-3 font-mono text-xs text-slate-500">
                  {s.upstream_auth ?? "headers"}
                </td>
                <td className="px-4 py-3">
                  <button
                    type="button"
                    disabled={toggle.isPending}
                    onClick={() =>
                      toggle.mutate({ id: s.id, enabled: !s.enabled })
                    }
                    className={`text-xs px-2 py-1 rounded ${
                      s.enabled
                        ? "bg-emerald-900/50 text-emerald-300"
                        : "bg-slate-700 text-slate-400"
                    }`}
                  >
                    {s.enabled ? "on" : "off"}
                  </button>
                </td>
                <td className="px-4 py-3 flex flex-wrap gap-3">
                  <button
                    type="button"
                    onClick={() => openEdit(s)}
                    className="text-accent hover:underline bg-transparent border-0 p-0 cursor-pointer text-sm font-inherit"
                  >
                    Edit
                  </button>
                  <Link
                    className="text-accent hover:underline"
                    to={`/servers/${encodeURIComponent(s.id)}`}
                  >
                    Details
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <EditServerModal
        server={editTarget}
        open={editModalOpen}
        onClose={closeEdit}
        busy={patchServer.isPending}
        error={patchServer.error?.message}
        saveSucceeded={editSaveSucceeded}
        onSaveSucceededAck={closeEdit}
        onSave={async (body) => {
          if (!editTarget) return;
          await patchServer.mutateAsync({ id: editTarget.id, body });
          setEditSaveSucceeded(true);
        }}
      />
    </div>
  );
}

function AddServerForm({
  onSubmit,
  busy,
  error,
}: {
  onSubmit: (b: {
    id: string;
    url: string;
    upstream_auth: UpstreamAuth;
    credentials_secret_id?: string;
    credentials_header?: string;
    oauth?: OAuth2UpstreamConfig;
  }) => void;
  busy: boolean;
  error?: string;
}) {
  const [localError, setLocalError] = useState<string | undefined>();

  return (
    <form
      className="rounded-xl border border-surface-border bg-surface-raised p-5 space-y-3 max-w-xl"
      onSubmit={(e) => {
        e.preventDefault();
        const fd = new FormData(e.currentTarget);
        const id = String(fd.get("id") || "").trim();
        const url = String(fd.get("url") || "").trim();
        const upstreamAuth = String(
          fd.get("upstream_auth") || "headers",
        ) as UpstreamAuth;
        const secret = String(fd.get("credentials_secret_id") || "").trim();
        const cred = String(fd.get("credentials_header") || "").trim();
        if (!id || !url) return;
        if (upstreamAuth !== "oauth2" && secret && cred) {
          setLocalError(
            "Provide only one of Secret Manager ID or inline header",
          );
          return;
        }
        setLocalError(undefined);
        const payload: {
          id: string;
          url: string;
          upstream_auth: UpstreamAuth;
          credentials_secret_id?: string;
          credentials_header?: string;
          oauth?: OAuth2UpstreamConfig;
        } = { id, url, upstream_auth: upstreamAuth };
        if (upstreamAuth === "oauth2") {
          const authorization_endpoint = String(
            fd.get("oauth_authorization_endpoint") || "",
          ).trim();
          const token_endpoint = String(
            fd.get("oauth_token_endpoint") || "",
          ).trim();
          const client_id = String(fd.get("oauth_client_id") || "").trim();
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
            scopes: String(fd.get("oauth_scopes") || "").trim(),
            use_pkce: String(fd.get("oauth_use_pkce") || "true") !== "false",
          };
          const audience = String(fd.get("oauth_audience") || "").trim();
          if (audience) oauth.audience = audience;
          const csid = String(
            fd.get("oauth_client_secret_secret_id") || "",
          ).trim();
          if (csid) oauth.client_secret_secret_id = csid;
          if (secret) payload.credentials_secret_id = secret;
          else if (cred) payload.credentials_header = cred;
          payload.oauth = oauth;
        } else {
          if (secret) payload.credentials_secret_id = secret;
          else if (cred) payload.credentials_header = cred;
        }
        onSubmit(payload);
        e.currentTarget.reset();
      }}
    >
      <h2 className="text-white font-medium">Add server</h2>
      <div className="grid gap-2 sm:grid-cols-2">
        <input
          name="id"
          placeholder="server id"
          className="bg-surface border border-surface-border rounded-lg px-3 py-2 font-mono text-sm"
          required
        />
        <ServerUpstreamFields />
      </div>
      {(localError || error) && (
        <p className="text-red-400 text-sm">{localError || error}</p>
      )}
      <button
        type="submit"
        disabled={busy}
        className="px-4 py-2 rounded-lg bg-accent text-surface font-medium text-sm hover:opacity-90 disabled:opacity-50"
      >
        {busy ? "Adding…" : "Validate & add"}
      </button>
    </form>
  );
}
