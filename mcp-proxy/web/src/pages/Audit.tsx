import { useQuery } from "@tanstack/react-query";
import { apiFetchWithRefresh } from "../api/client";

export function AuditPage() {
  const q = useQuery({
    queryKey: ["admin", "audit", "log-explorer"],
    queryFn: async () => {
      const r = await apiFetchWithRefresh("/admin/audit/log-explorer");
      if (!r.ok) throw new Error(await r.text());
      return r.json() as Promise<{ url: string }>;
    },
  });

  return (
    <div className="space-y-6 max-w-2xl">
      <h1 className="text-2xl font-semibold text-white">Audit logs</h1>
      <p className="text-slate-400 text-sm leading-relaxed">
        Administrative actions, MCP usage, and tool-level outcomes are written
        to Google Cloud Logging with structured fields (see{" "}
        <code className="font-mono text-accent">audit/logger.py</code>). Open
        Log Explorer with a pre-filtered query to review events for this
        project.
      </p>
      {q.isLoading && <p className="text-slate-500">Loading link…</p>}
      {q.data?.url && (
        <a
          href={q.data.url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-2 px-5 py-3 rounded-xl bg-accent text-surface font-medium hover:opacity-90 focus-ring"
        >
          Open Log Explorer
        </a>
      )}
      {q.error && <p className="text-red-400">{String(q.error)}</p>}
    </div>
  );
}
