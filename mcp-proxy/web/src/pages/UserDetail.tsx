import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { apiFetchWithRefresh } from "../api/client";
import type {
  PermissionItem,
  ServerConfig,
  UserProfile,
  UserUsageSummary,
} from "../types";

export function UserDetailPage() {
  const { id } = useParams<{ id: string }>();
  const qc = useQueryClient();

  const user = useQuery({
    queryKey: ["users", id],
    queryFn: async () => {
      const r = await apiFetchWithRefresh("/users");
      if (!r.ok) throw new Error(await r.text());
      const all = (await r.json()) as UserProfile[];
      return all.find((u) => u.id === id) || null;
    },
    enabled: Boolean(id),
  });

  const servers = useQuery({
    queryKey: ["admin", "servers"],
    queryFn: async () => {
      const r = await apiFetchWithRefresh("/admin/servers");
      if (!r.ok) throw new Error(await r.text());
      return r.json() as Promise<ServerConfig[]>;
    },
  });

  const perms = useQuery({
    queryKey: ["users", id, "permissions"],
    queryFn: async () => {
      const r = await apiFetchWithRefresh(
        `/users/${encodeURIComponent(id || "")}/permissions`,
      );
      if (!r.ok) throw new Error(await r.text());
      return r.json() as Promise<PermissionItem[]>;
    },
    enabled: Boolean(id),
  });

  const usageSummary = useQuery({
    queryKey: ["users", id, "usage-summary"],
    queryFn: async () => {
      const r = await apiFetchWithRefresh(
        `/users/${encodeURIComponent(id || "")}/usage-summary`,
      );
      if (!r.ok) throw new Error(await r.text());
      return r.json() as Promise<UserUsageSummary>;
    },
    enabled: Boolean(id),
  });

  const savePerms = useMutation({
    mutationFn: async (permissions: PermissionItem[]) => {
      const r = await apiFetchWithRefresh(
        `/users/${encodeURIComponent(id || "")}/permissions`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ permissions }),
        },
      );
      if (!r.ok) throw new Error(await r.text());
      return r.json();
    },
    onSuccess: () =>
      void qc.invalidateQueries({ queryKey: ["users", id, "permissions"] }),
  });

  const setRole = useMutation({
    mutationFn: async (role: string) => {
      const r = await apiFetchWithRefresh(
        `/users/${encodeURIComponent(id || "")}/role`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ role }),
        },
      );
      if (!r.ok) throw new Error(await r.text());
      return r.json();
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["users"] });
      void qc.invalidateQueries({ queryKey: ["users", id] });
    },
  });

  const regenKey = useMutation({
    mutationFn: async () => {
      const r = await apiFetchWithRefresh(
        `/users/${encodeURIComponent(id || "")}/mcp-credentials/regenerate`,
        { method: "POST" },
      );
      if (!r.ok) throw new Error(await r.text());
      return r.json() as Promise<{ user_key?: string; proxy_url?: string }>;
    },
  });

  const [editAgentName, setEditAgentName] = useState("");
  const [editAgentUrl, setEditAgentUrl] = useState("");

  useEffect(() => {
    if (user.data?.kind === "agent") {
      setEditAgentName(user.data.agent_name ?? "");
      setEditAgentUrl(user.data.agent_url ?? "");
    }
  }, [user.data?.kind, user.data?.agent_name, user.data?.agent_url]);

  const saveAgentMeta = useMutation({
    mutationFn: async () => {
      const r = await apiFetchWithRefresh(
        `/users/${encodeURIComponent(id || "")}/agent`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            agent_name: editAgentName.trim(),
            agent_url: editAgentUrl.trim(),
          }),
        },
      );
      if (!r.ok) throw new Error(await r.text());
      return r.json() as Promise<UserProfile>;
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["users"] });
      void qc.invalidateQueries({ queryKey: ["users", id] });
    },
  });

  if (!user.data && user.isSuccess) {
    return (
      <p className="text-slate-500">
        User not found. <Link to="/users">Back</Link>
      </p>
    );
  }

  const u = user.data;
  const permMap = new Map((perms.data || []).map((p) => [p.server_id, p]));

  return (
    <div className="space-y-8 max-w-4xl">
      <div className="flex items-center gap-4">
        <Link to="/users" className="text-slate-500 hover:text-accent text-sm">
          ← Users
        </Link>
        <h1 className="text-2xl font-semibold text-white font-mono truncate">
          {u?.kind === "agent"
            ? u.agent_name?.trim() || "Service agent"
            : u?.email}
        </h1>
      </div>
      {user.isLoading && <p className="text-slate-500">Loading…</p>}
      {u && (
        <>
          {u.kind === "agent" ? (
            <div className="rounded-xl border border-surface-border bg-surface-raised p-4 space-y-3 max-w-xl">
              <h2 className="text-white font-medium text-sm">Agent details</h2>
              <p className="text-slate-500 text-xs">
                Role is fixed to <code className="text-slate-400">user</code>{" "}
                for service agents.
              </p>
              <div>
                <label className="block text-xs text-slate-500 mb-1">
                  Name
                </label>
                <input
                  className="w-full bg-surface border border-surface-border rounded-lg px-3 py-2 text-sm text-white"
                  value={editAgentName}
                  onChange={(e) => setEditAgentName(e.target.value)}
                />
              </div>
              <div>
                <label className="block text-xs text-slate-500 mb-1">
                  Page URL (optional)
                </label>
                <input
                  className="w-full bg-surface border border-surface-border rounded-lg px-3 py-2 text-sm text-white"
                  value={editAgentUrl}
                  onChange={(e) => setEditAgentUrl(e.target.value)}
                  placeholder="https://…"
                />
              </div>
              {saveAgentMeta.error && (
                <p className="text-red-400 text-sm">
                  {String(saveAgentMeta.error)}
                </p>
              )}
              <button
                type="button"
                disabled={saveAgentMeta.isPending || !editAgentName.trim()}
                onClick={() => saveAgentMeta.mutate()}
                className="px-4 py-2 rounded-lg bg-accent text-surface text-sm font-medium disabled:opacity-50"
              >
                {saveAgentMeta.isPending ? "Saving…" : "Save agent details"}
              </button>
            </div>
          ) : (
            <div className="flex flex-wrap gap-3 items-center">
              <span className="text-slate-500 text-sm">Role</span>
              <select
                className="bg-surface border border-surface-border rounded-lg px-3 py-2 text-sm"
                value={u.role}
                onChange={(e) => setRole.mutate(e.target.value)}
              >
                <option value="user">user</option>
                <option value="power_user">power_user</option>
                <option value="admin">admin</option>
              </select>
            </div>
          )}
          {u.status === "active" && (
            <div className="rounded-xl border border-surface-border bg-surface-raised p-4 space-y-3">
              <h2 className="text-white font-medium">MCP key (admin)</h2>
              <button
                type="button"
                disabled={regenKey.isPending}
                onClick={() => regenKey.mutate()}
                className="px-4 py-2 rounded-lg bg-amber-900/40 text-amber-200 text-sm hover:bg-amber-900/60"
              >
                Regenerate user MCP key
              </button>
              {regenKey.data?.user_key && (
                <p className="text-sm text-amber-300 font-mono break-all">
                  New key: {regenKey.data.user_key}
                </p>
              )}
            </div>
          )}
          <section className="rounded-xl border border-surface-border bg-surface-raised p-4 space-y-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h2 className="text-white font-medium">
                  Usage summary (last 30 days)
                </h2>
                <p className="text-slate-500 text-sm">
                  High-level proxy usage recorded by the MCP proxy.
                </p>
              </div>
              {usageSummary.data?.log_explorer_url && (
                <a
                  href={usageSummary.data.log_explorer_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="px-3 py-2 rounded-lg border border-surface-border text-sm text-accent hover:bg-surface"
                >
                  Open Log Explorer
                </a>
              )}
            </div>

            {usageSummary.isLoading && (
              <p className="text-slate-500 text-sm">Loading usage summary…</p>
            )}

            {usageSummary.error && (
              <div className="rounded-lg border border-red-900/60 bg-red-950/20 p-3">
                <p className="text-red-300 text-sm">
                  Could not load usage summary right now.
                </p>
                <p className="text-red-200/80 text-xs mt-1">
                  {extractErrorMessage(usageSummary.error)}
                </p>
              </div>
            )}

            {usageSummary.data && (
              <UsageSummaryCard summary={usageSummary.data} />
            )}
          </section>
          <div className="space-y-3">
            <h2 className="text-white font-medium">Server permissions</h2>
            <p className="text-slate-500 text-sm">
              Admin and power_user bypass these in the proxy; they apply to role{" "}
              <code>user</code>.
            </p>
            <PermissionEditor
              servers={servers.data || []}
              value={
                servers.data?.map(
                  (s) =>
                    permMap.get(s.id) || {
                      server_id: s.id,
                      read: true,
                      write: false,
                    },
                ) || []
              }
              onSave={(p) => savePerms.mutate(p)}
              busy={savePerms.isPending}
            />
          </div>
        </>
      )}
    </div>
  );
}

function UsageSummaryCard({ summary }: { summary: UserUsageSummary }) {
  const hasActivity =
    summary.authorized_request_count > 0 || summary.tool_call_count > 0;

  if (!hasActivity) {
    return (
      <div className="rounded-lg border border-surface-border bg-surface p-4">
        <p className="text-slate-300 text-sm">
          No proxy activity in the last {summary.window_days} days.
        </p>
      </div>
    );
  }

  const metrics = [
    {
      label: "Authorized requests",
      value: summary.authorized_request_count.toLocaleString(),
    },
    {
      label: "Tool calls",
      value: summary.tool_call_count.toLocaleString(),
    },
    {
      label: "Success / error / denied",
      value: `${summary.result_counts.success} / ${summary.result_counts.error} / ${summary.result_counts.denied}`,
    },
    {
      label: "Last activity",
      value: formatUsageDate(summary.last_activity_at),
    },
    {
      label: "Unique servers",
      value: summary.unique_servers_count.toLocaleString(),
    },
    {
      label: "Unique tools",
      value: summary.unique_tools_count.toLocaleString(),
    },
  ];

  return (
    <div className="space-y-4">
      {summary.truncated && (
        <div className="rounded-lg border border-amber-900/60 bg-amber-950/20 p-3 text-amber-200 text-sm">
          The summary hit the current processing cap and may be incomplete.
        </div>
      )}
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {metrics.map((metric) => (
          <div
            key={metric.label}
            className="rounded-lg border border-surface-border bg-surface p-3"
          >
            <p className="text-xs uppercase tracking-wide text-slate-500">
              {metric.label}
            </p>
            <p className="mt-1 text-lg font-mono text-slate-100">
              {metric.value}
            </p>
          </div>
        ))}
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <UsageBreakdownList
          title="Top servers"
          items={summary.top_servers}
          emptyLabel="No upstream server calls logged."
        />
        <UsageBreakdownList
          title="Top tools"
          items={summary.top_tools}
          emptyLabel="No tool calls logged."
        />
      </div>
    </div>
  );
}

function UsageBreakdownList({
  title,
  items,
  emptyLabel,
}: {
  title: string;
  items: UserUsageSummary["top_servers"];
  emptyLabel: string;
}) {
  return (
    <div className="rounded-lg border border-surface-border bg-surface overflow-hidden">
      <div className="px-4 py-3 border-b border-surface-border">
        <h3 className="text-sm font-medium text-white">{title}</h3>
      </div>
      {items.length === 0 ? (
        <p className="px-4 py-3 text-sm text-slate-500">{emptyLabel}</p>
      ) : (
        <div className="divide-y divide-surface-border">
          {items.map((item) => (
            <div
              key={item.name}
              className="flex items-center justify-between gap-3 px-4 py-3"
            >
              <div className="min-w-0">
                <p className="font-mono text-sm text-slate-200 truncate">
                  {item.name}
                </p>
                <p className="text-xs text-slate-500">
                  {item.result_counts.success} success ·{" "}
                  {item.result_counts.error} error · {item.result_counts.denied}{" "}
                  denied
                </p>
              </div>
              <p className="font-mono text-sm text-accent">{item.total}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function formatUsageDate(value?: string | null): string {
  if (!value) return "—";
  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) return "—";
  return dt.toLocaleString();
}

function extractErrorMessage(error: unknown): string {
  const text = String(error);
  const match = text.match(/\{.*\}$/);
  if (!match) return text;
  try {
    const parsed = JSON.parse(match[0]) as { detail?: string };
    return parsed.detail || text;
  } catch {
    return text;
  }
}

function PermissionEditor({
  servers,
  value,
  onSave,
  busy,
}: {
  servers: ServerConfig[];
  value: PermissionItem[];
  onSave: (p: PermissionItem[]) => void;
  busy: boolean;
}) {
  const rows = servers.map((s) => {
    const row = value.find((v) => v.server_id === s.id) || {
      server_id: s.id,
      read: true,
      write: false,
    };
    return { server: s, row };
  });

  return (
    <form
      className="rounded-xl border border-surface-border overflow-hidden"
      onSubmit={(e) => {
        e.preventDefault();
        const fd = new FormData(e.currentTarget);
        const out: PermissionItem[] = servers.map((s) => ({
          server_id: s.id,
          read: fd.get(`read-${s.id}`) === "on",
          write: fd.get(`write-${s.id}`) === "on",
        }));
        onSave(out);
      }}
    >
      <table className="w-full text-sm">
        <thead className="bg-surface-raised text-slate-400 text-xs uppercase">
          <tr>
            <th className="px-4 py-2 text-left">Server</th>
            <th className="px-4 py-2">Read</th>
            <th className="px-4 py-2">Write</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-surface-border">
          {rows.map(({ server, row }) => (
            <tr key={server.id}>
              <td className="px-4 py-2 font-mono text-accent">{server.id}</td>
              <td className="px-4 py-2 text-center">
                <input
                  type="checkbox"
                  name={`read-${server.id}`}
                  defaultChecked={row.read}
                />
              </td>
              <td className="px-4 py-2 text-center">
                <input
                  type="checkbox"
                  name={`write-${server.id}`}
                  defaultChecked={row.write}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="p-4 bg-surface-raised">
        <button
          type="submit"
          disabled={busy}
          className="px-4 py-2 rounded-lg bg-accent text-surface text-sm font-medium disabled:opacity-50"
        >
          {busy ? "Saving…" : "Save permissions"}
        </button>
      </div>
    </form>
  );
}
