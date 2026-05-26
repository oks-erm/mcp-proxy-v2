import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetchWithRefresh } from "../api/client";
import type { ManagedWorkflowRecord } from "../types";

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

async function fetchManagedWorkflows() {
  const r = await apiFetchWithRefresh("/admin/n8n-workflows");
  if (!r.ok) throw new Error(await readErrorMessage(r));
  return r.json() as Promise<ManagedWorkflowRecord[]>;
}

function formatDate(value?: string | null) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString();
}

export function WorkflowsPage() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["admin", "n8n-workflows"],
    queryFn: fetchManagedWorkflows,
  });

  const deleteWorkflow = useMutation({
    mutationFn: async (workflowId: string) => {
      const r = await apiFetchWithRefresh(
        `/admin/n8n-workflows/${encodeURIComponent(workflowId)}`,
        { method: "DELETE" },
      );
      if (!r.ok) throw new Error(await readErrorMessage(r));
      return r.json() as Promise<{ workflow_id: string; deleted: boolean }>;
    },
    onSuccess: () =>
      void qc.invalidateQueries({ queryKey: ["admin", "n8n-workflows"] }),
  });

  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <h1 className="text-2xl font-semibold text-white">Managed workflows</h1>
        <p className="text-sm text-slate-400">
          n8n workflows created through MCP, including their required summary
          note.
        </p>
      </div>

      {q.isLoading && <p className="text-slate-500">Loading…</p>}
      {q.error && <p className="text-red-400">{String(q.error)}</p>}
      {deleteWorkflow.error && (
        <p className="text-red-400">{String(deleteWorkflow.error)}</p>
      )}

      {!q.isLoading && !q.error && (q.data?.length ?? 0) === 0 && (
        <div className="rounded-xl border border-surface-border bg-surface-raised p-6 text-slate-400">
          No workflows created through MCP yet.
        </div>
      )}

      {(q.data?.length ?? 0) > 0 && (
        <div className="overflow-x-auto rounded-xl border border-surface-border">
          <table className="w-full text-sm text-left">
            <thead className="bg-surface-raised text-slate-400 uppercase text-xs">
              <tr>
                <th className="px-4 py-3 font-medium">Workflow</th>
                <th className="px-4 py-3 font-medium">Summary</th>
                <th className="px-4 py-3 font-medium">Created by</th>
                <th className="px-4 py-3 font-medium">Created</th>
                <th className="px-4 py-3 font-medium" />
              </tr>
            </thead>
            <tbody className="divide-y divide-surface-border">
              {q.data?.map((workflow) => (
                <tr
                  key={workflow.workflow_id}
                  className="hover:bg-surface-raised/50"
                >
                  <td className="px-4 py-3">
                    <div className="font-medium text-white">
                      {workflow.name}
                    </div>
                    <div className="font-mono text-xs text-slate-500">
                      {workflow.workflow_id}
                    </div>
                  </td>
                  <td className="px-4 py-3 text-slate-300 max-w-xl">
                    {workflow.summary}
                  </td>
                  <td className="px-4 py-3 text-slate-400">
                    {workflow.created_by_label ||
                      workflow.created_by_email ||
                      "—"}
                  </td>
                  <td className="px-4 py-3 text-slate-500">
                    {formatDate(workflow.created_at)}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex flex-wrap gap-3">
                      {workflow.editor_url ? (
                        <a
                          href={workflow.editor_url}
                          target="_blank"
                          rel="noreferrer"
                          className="text-accent hover:underline"
                        >
                          Open
                        </a>
                      ) : (
                        <span className="text-slate-600">No link</span>
                      )}
                      <button
                        type="button"
                        disabled={deleteWorkflow.isPending}
                        onClick={() => {
                          const confirmed = window.confirm(
                            `Delete workflow "${workflow.name}" from n8n?`,
                          );
                          if (!confirmed) return;
                          deleteWorkflow.mutate(workflow.workflow_id);
                        }}
                        className="text-red-300 hover:text-red-200"
                      >
                        Delete
                      </button>
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
