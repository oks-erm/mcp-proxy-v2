import { useQuery } from "@tanstack/react-query";
import { apiFetchWithRefresh } from "../api/client";

/** Activity / MCP usage for the signed-in user (Log Explorer, scoped by user id). */
export function UsageLogsPage() {
  const q = useQuery({
    queryKey: ["me", "activity", "log-explorer"],
    queryFn: async () => {
      const r = await apiFetchWithRefresh("/me/activity/log-explorer");
      if (!r.ok) throw new Error(await r.text());
      return r.json() as Promise<{ url: string }>;
    },
  });

  return (
    <div className="space-y-6 max-w-2xl">
      <h1 className="text-2xl font-semibold text-white">
        Activity &amp; usage
      </h1>
      <p className="text-slate-400 text-sm leading-relaxed">
        MCP connections, tool calls, and related audit events for your account
        are logged in Google Cloud Logging. Open Log Explorer with a query
        scoped to your user id. You need{" "}
        <span className="text-slate-300">logging viewer</span> (or broader)
        access on the GCP project to see results.
      </p>
      {q.isLoading && <p className="text-slate-500">Loading link…</p>}
      {q.data?.url && (
        <a
          href={q.data.url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-2 px-5 py-3 rounded-xl bg-accent text-surface font-medium hover:opacity-90 focus-ring"
        >
          Open Log Explorer (my activity)
        </a>
      )}
      {q.error && <p className="text-red-400">{String(q.error)}</p>}
    </div>
  );
}
