import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { apiFetchWithRefresh } from "../api/client";

interface Diagnostics {
  server_id: string;
  url: string;
  enabled: boolean;
  note?: string;
  sections: {
    initialize: {
      ok: boolean;
      error?: string | null;
      latency_ms?: number | null;
    };
    tools: {
      ok: boolean;
      error?: string | null;
      latency_ms?: number | null;
      items: { name: string; description: string }[];
    };
    resources: {
      ok: boolean;
      error?: string | null;
      latency_ms?: number | null;
      items: { uri: string; name: string; description: string }[];
    };
    prompts: {
      ok: boolean;
      error?: string | null;
      latency_ms?: number | null;
      items: { name: string; description: string }[];
    };
  };
}

export function ServerDetailPage() {
  const { id } = useParams<{ id: string }>();
  const q = useQuery({
    queryKey: ["admin", "servers", id, "diagnostics"],
    queryFn: async () => {
      const r = await apiFetchWithRefresh(
        `/admin/servers/${encodeURIComponent(id || "")}/diagnostics`,
      );
      if (!r.ok) throw new Error(await r.text());
      return r.json() as Promise<Diagnostics>;
    },
    enabled: Boolean(id),
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-4">
        <Link
          to="/servers"
          className="text-slate-500 hover:text-accent text-sm"
        >
          ← Servers
        </Link>
        <h1 className="text-2xl font-semibold text-white font-mono">{id}</h1>
      </div>
      {q.isLoading && <p className="text-slate-500">Running diagnostics…</p>}
      {q.error && <p className="text-red-400">{String(q.error)}</p>}
      {q.data && (
        <div className="space-y-6">
          <p className="text-slate-500 text-sm font-mono break-all">
            {q.data.url}
          </p>
          {q.data.note && (
            <p className="text-amber-200/90 text-sm border border-amber-900/40 rounded-lg px-3 py-2">
              {q.data.note}
            </p>
          )}
          <Section title="Initialize" s={q.data.sections.initialize} />
          <TabbedLists
            tools={q.data.sections.tools}
            resources={q.data.sections.resources}
            prompts={q.data.sections.prompts}
          />
        </div>
      )}
    </div>
  );
}

function Section({
  title,
  s,
}: {
  title: string;
  s: { ok: boolean; error?: string | null; latency_ms?: number | null };
}) {
  return (
    <div className="rounded-xl border border-surface-border bg-surface-raised p-4">
      <div className="flex justify-between items-center mb-2">
        <h2 className="text-white font-medium">{title}</h2>
        <span className="text-xs text-slate-500">
          {s.latency_ms != null ? `${s.latency_ms} ms` : ""}{" "}
          {s.ok ? (
            <span className="text-emerald-400">ok</span>
          ) : (
            <span className="text-red-400">fail</span>
          )}
        </span>
      </div>
      {s.error && <p className="text-red-400 text-sm font-mono">{s.error}</p>}
    </div>
  );
}

function TabbedLists({
  tools,
  resources,
  prompts,
}: {
  tools: Diagnostics["sections"]["tools"];
  resources: Diagnostics["sections"]["resources"];
  prompts: Diagnostics["sections"]["prompts"];
}) {
  return (
    <div className="space-y-4">
      <h2 className="text-white font-medium">Capabilities</h2>
      <div className="grid gap-6 lg:grid-cols-3">
        <ListBlock
          title="Tools"
          section={tools}
          render={(t) => (
            <li
              key={t.name}
              className="border-b border-surface-border py-2 last:border-0"
            >
              <span className="font-mono text-accent text-sm">{t.name}</span>
              <p className="text-slate-500 text-xs mt-1">{t.description}</p>
            </li>
          )}
        />
        <ListBlock
          title="Resources"
          section={resources}
          render={(r) => (
            <li
              key={r.uri}
              className="border-b border-surface-border py-2 last:border-0"
            >
              <span className="font-mono text-slate-300 text-xs break-all">
                {r.uri}
              </span>
              <p className="text-slate-500 text-xs mt-1">{r.name}</p>
            </li>
          )}
        />
        <ListBlock
          title="Prompts"
          section={prompts}
          render={(p) => (
            <li
              key={p.name}
              className="border-b border-surface-border py-2 last:border-0"
            >
              <span className="font-mono text-accent text-sm">{p.name}</span>
              <p className="text-slate-500 text-xs mt-1">{p.description}</p>
            </li>
          )}
        />
      </div>
    </div>
  );
}

function ListBlock<T extends { name?: string; uri?: string }>({
  title,
  section,
  render,
}: {
  title: string;
  section: { ok: boolean; error?: string | null; items: T[] };
  render: (item: T) => ReactNode;
}) {
  return (
    <div className="rounded-xl border border-surface-border bg-surface-raised p-4 min-h-[120px]">
      <h3 className="text-slate-400 text-xs uppercase tracking-wider mb-3">
        {title}
      </h3>
      {!section.ok && section.error && (
        <p className="text-amber-400 text-xs font-mono">{section.error}</p>
      )}
      <ul className="list-none m-0 p-0 max-h-80 overflow-y-auto">
        {section.items.map(render)}
      </ul>
    </div>
  );
}
