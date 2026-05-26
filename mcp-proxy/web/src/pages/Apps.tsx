import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetchWithRefresh } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import type { ManagedAppRecord, ManagedAppStatus } from "../types";

async function readErrorMessage(response: Response) {
  const text = await response.text();
  if (!text) return `Request failed (${response.status})`;
  try {
    const parsed = JSON.parse(text) as { detail?: unknown; message?: unknown };
    const detail = parsed.detail ?? parsed.message;
    if (typeof detail === "string" && detail.trim()) return detail;
  } catch {
    // Fall back to the raw body when the response is not JSON.
  }
  return text;
}

async function fetchManagedApps(isAdmin: boolean) {
  const r = await apiFetchWithRefresh(isAdmin ? "/admin/apps" : "/me/apps");
  if (!r.ok) throw new Error(await readErrorMessage(r));
  return r.json() as Promise<ManagedAppRecord[]>;
}

function formatDate(value?: string | null) {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString();
}

function statusLabel(status: ManagedAppStatus) {
  if (status === "approved") return "Live";
  return status.replace("_", " ");
}

function statusClass(status: ManagedAppStatus) {
  switch (status) {
    case "approved":
      return "border-emerald-500/40 bg-emerald-500/10 text-emerald-200";
    case "pending_review":
      return "border-amber-500/40 bg-amber-500/10 text-amber-200";
    case "delete_failed":
      return "border-red-500/40 bg-red-500/10 text-red-200";
    case "deleted":
      return "border-slate-600 bg-slate-800 text-slate-300";
  }
}

export function AppsPage() {
  const { state } = useAuth();
  const isAdmin = state.user?.role === "admin";
  const qc = useQueryClient();
  const queryKey = isAdmin ? ["admin", "apps"] : ["me", "apps"];
  const q = useQuery({
    queryKey,
    queryFn: () => fetchManagedApps(isAdmin),
  });

  const deleteApp = useMutation({
    mutationFn: async ({
      appId,
      deleteMode,
    }: {
      appId: string;
      deleteMode: "cloud_run_only" | "full_cleanup";
    }) => {
      const r = await apiFetchWithRefresh(
        `/admin/apps/${encodeURIComponent(appId)}?delete_mode=${deleteMode}`,
        { method: "DELETE" },
      );
      if (!r.ok) throw new Error(await readErrorMessage(r));
      return r.json() as Promise<{ app_id: string; deleted: boolean }>;
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey }),
  });

  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <h1 className="text-2xl font-semibold text-white">Managed apps</h1>
        <p className="text-sm text-slate-400">
          Apps deployed through the{" "}
          <span className="text-slate-300">MCP proxy</span> (
          <code className="text-slate-300">cloud_run_deployer</code> with{" "}
          <code className="text-slate-300">execute=true</code>) are reachable
          with Google sign-in (IAP) by default. Use this page to review metadata
          and remove services you no longer need. Admins see every app; other
          roles only see apps tied to their account.
        </p>
      </div>

      {q.isLoading && <p className="text-slate-500">Loading...</p>}
      {q.error && <p className="text-red-400">{String(q.error)}</p>}
      {deleteApp.error && (
        <p className="text-red-400">{String(deleteApp.error)}</p>
      )}

      {!q.isLoading && !q.error && (q.data?.length ?? 0) === 0 && (
        <div className="rounded-xl border border-surface-border bg-surface-raised p-6 text-slate-400">
          No web apps created through MCP yet.
        </div>
      )}

      {(q.data?.length ?? 0) > 0 && (
        <div className="overflow-x-auto rounded-xl border border-surface-border">
          <table className="w-full text-sm text-left">
            <thead className="bg-surface-raised text-slate-400 uppercase text-xs">
              <tr>
                <th className="px-4 py-3 font-medium">App</th>
                <th className="px-4 py-3 font-medium">Purpose</th>
                <th className="px-4 py-3 font-medium">Data</th>
                <th className="px-4 py-3 font-medium">Status</th>
                <th className="px-4 py-3 font-medium">Created by</th>
                <th className="px-4 py-3 font-medium">Dates</th>
                <th className="px-4 py-3 font-medium" />
              </tr>
            </thead>
            <tbody className="divide-y divide-surface-border">
              {q.data?.map((app) => (
                <tr key={app.app_id} className="hover:bg-surface-raised/50">
                  <td className="px-4 py-3 align-top">
                    <div className="font-medium text-white">{app.name}</div>
                    <div className="font-mono text-xs text-slate-500">
                      {app.app_id}
                    </div>
                    <div className="font-mono text-xs text-slate-600">
                      {app.service_name}
                    </div>
                    {app.framework && (
                      <div className="mt-1 text-xs text-slate-500">
                        {app.framework}
                      </div>
                    )}
                    {app.repo_url && (
                      <a
                        href={app.repo_url}
                        target="_blank"
                        rel="noreferrer"
                        className="mt-1 block text-xs text-slate-500 hover:text-slate-300"
                      >
                        Repository
                      </a>
                    )}
                  </td>
                  <td className="px-4 py-3 text-slate-300 max-w-sm align-top">
                    {app.summary}
                  </td>
                  <td className="px-4 py-3 text-slate-300 max-w-sm align-top">
                    <div>{app.data_access_summary}</div>
                    {app.data_connections.length > 0 && (
                      <div className="mt-2 font-mono text-xs text-slate-500">
                        {app.data_connections
                          .map((item) => String(item.id || item.type || "data"))
                          .join(", ")}
                      </div>
                    )}
                  </td>
                  <td className="px-4 py-3 align-top">
                    <span
                      className={`inline-flex rounded-full border px-2 py-1 text-xs ${statusClass(app.status)}`}
                    >
                      {statusLabel(app.status)}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-slate-400 align-top">
                    {app.created_by_label || app.created_by_email || "-"}
                  </td>
                  <td className="px-4 py-3 text-slate-500 align-top">
                    <div>Created {formatDate(app.created_at)}</div>
                    {app.approved_at && (
                      <div>Approved {formatDate(app.approved_at)}</div>
                    )}
                    {app.deleted_at && (
                      <div>Deleted {formatDate(app.deleted_at)}</div>
                    )}
                  </td>
                  <td className="px-4 py-3 align-top">
                    <div className="flex flex-wrap gap-3">
                      {app.approved_url || app.service_url ? (
                        <a
                          href={
                            app.approved_url || app.service_url || undefined
                          }
                          target="_blank"
                          rel="noreferrer"
                          className="text-accent hover:underline"
                        >
                          Open
                        </a>
                      ) : (
                        <span
                          className="text-slate-600"
                          title={
                            app.build_id
                              ? "No service URL was stored. After Cloud Build finishes, run complete_deployment with execute=true via the proxy, or redeploy the same app_id so the deployer can record the Run URL."
                              : "No service URL was stored. Deploy with execute=true through the aggregated MCP proxy so the Run URL is saved to this list."
                          }
                        >
                          No URL on record
                          {app.build_id && (
                            <span className="ml-1 font-mono text-xs text-slate-500">
                              (build {app.build_id.slice(0, 8)}…)
                            </span>
                          )}
                        </span>
                      )}
                      {isAdmin && app.status !== "deleted" && (
                        <>
                          <button
                            type="button"
                            disabled={deleteApp.isPending}
                            onClick={() => {
                              const confirmed = window.confirm(
                                `Full cleanup for "${app.name}"? This removes the app service and managed resources where available.`,
                              );
                              if (!confirmed) return;
                              deleteApp.mutate({
                                appId: app.app_id,
                                deleteMode: "full_cleanup",
                              });
                            }}
                            className="text-red-300 hover:text-red-200"
                          >
                            Delete
                          </button>
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
