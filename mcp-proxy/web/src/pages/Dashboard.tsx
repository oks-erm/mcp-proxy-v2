import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { apiFetchWithRefresh } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import type {
  AdminMetricsSnapshot,
  ManagedAppRecord,
  ServerConfig,
  UserProfile,
  UserUsageLeaderboard,
  UserUsageRankingItem,
} from "../types";

async function fetchJson<T>(path: string): Promise<T> {
  const r = await apiFetchWithRefresh(path);
  if (!r.ok) throw new Error(await r.text());
  return r.json() as Promise<T>;
}

function hostFromUrl(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url.length > 48 ? `${url.slice(0, 45)}…` : url;
  }
}

export function DashboardPage() {
  const { state } = useAuth();
  const queryClient = useQueryClient();
  const isAdmin = state.user?.role === "admin";
  const [serversOpen, setServersOpen] = useState(false);

  const servers = useQuery({
    queryKey: ["admin", "servers"],
    queryFn: () => fetchJson<ServerConfig[]>("/admin/servers"),
    enabled: isAdmin,
  });
  const users = useQuery({
    queryKey: ["users"],
    queryFn: () => fetchJson<UserProfile[]>("/users"),
    enabled: isAdmin,
  });
  const pending = useQuery({
    queryKey: ["users", "pending"],
    queryFn: () => fetchJson<UserProfile[]>("/users/pending"),
    enabled: isAdmin,
  });
  const apps = useQuery({
    queryKey: ["admin", "apps"],
    queryFn: () => fetchJson<ManagedAppRecord[]>("/admin/apps"),
    enabled: isAdmin,
  });
  const metrics = useQuery({
    queryKey: ["admin", "metrics"],
    queryFn: () => fetchJson<AdminMetricsSnapshot>("/admin/metrics"),
    enabled: isAdmin,
    refetchInterval: 20_000,
  });
  const auditLink = useQuery({
    queryKey: ["admin", "audit", "log-explorer"],
    queryFn: () => fetchJson<{ url: string }>("/admin/audit/log-explorer"),
    enabled: isAdmin,
  });
  const usageRanking = useQuery({
    queryKey: ["users", "usage-ranking"],
    queryFn: () => fetchJson<UserUsageLeaderboard>("/users/usage-ranking"),
    enabled: isAdmin,
    refetchInterval: 60_000,
  });

  const inventory = useMemo(() => {
    const list = servers.data ?? [];
    const ulist = users.data ?? [];
    const enabled = list.filter((s) => s.enabled).length;
    const withSecret = list.filter((s) =>
      Boolean(s.credentials_secret_id?.trim()),
    ).length;
    const withHeader = list.filter((s) =>
      Boolean(s.credentials_header?.trim()),
    ).length;
    const neitherCred = list.filter(
      (s) => !s.credentials_secret_id?.trim() && !s.credentials_header?.trim(),
    ).length;
    const byRole = { admin: 0, power_user: 0, user: 0 } as Record<
      string,
      number
    >;
    const byStatus = { active: 0, pending: 0, rejected: 0 } as Record<
      string,
      number
    >;
    for (const u of ulist) {
      byRole[u.role] = (byRole[u.role] ?? 0) + 1;
      byStatus[u.status] = (byStatus[u.status] ?? 0) + 1;
    }
    return {
      totalServers: list.length,
      enabledServers: enabled,
      disabledServers: list.length - enabled,
      withSecret,
      withHeader,
      neitherCred,
      byRole,
      byStatus,
    };
  }, [servers.data, users.data]);

  const methodRows = useMemo(() => {
    const bm = metrics.data?.by_method ?? {};
    return Object.entries(bm)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 12);
  }, [metrics.data?.by_method]);

  const appCounts = useMemo(() => {
    const list = apps.data ?? [];
    return {
      total: list.length,
      pending: list.filter((app) => app.status === "pending_review").length,
      approved: list.filter((app) => app.status === "approved").length,
    };
  }, [apps.data]);

  const refreshAll = () => {
    void queryClient.invalidateQueries({ queryKey: ["admin"] });
    void queryClient.invalidateQueries({ queryKey: ["users"] });
  };

  const loading =
    servers.isLoading ||
    users.isLoading ||
    pending.isLoading ||
    apps.isLoading ||
    metrics.isLoading;

  if (!isAdmin) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold text-white">Welcome</h1>
        <p className="text-slate-400 max-w-xl">
          You are signed in as{" "}
          <span className="font-mono text-slate-300">{state.user?.email}</span>{" "}
          ({state.user?.role}). Use{" "}
          <Link className="text-accent hover:underline" to="/account">
            Account
          </Link>{" "}
          for MCP connection details.
        </p>
      </div>
    );
  }

  return (
    <div className="relative -mx-4 px-4 pb-4">
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 -top-2 overflow-hidden rounded-2xl opacity-90"
        style={{
          background:
            "radial-gradient(ellipse 80% 60% at 50% -20%, rgba(62, 230, 176, 0.12), transparent 55%), radial-gradient(ellipse 60% 40% at 100% 0%, rgba(30, 36, 48, 0.9), transparent 50%), linear-gradient(180deg, #13161d 0%, #0c0e12 45%)",
        }}
      />
      <div className="relative space-y-8 pt-2">
        <header className="dashboard-enter dashboard-enter-delay-1 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight text-white">
              Dashboard
            </h1>
            <p className="mt-1 text-sm text-slate-500">
              Instance{" "}
              <span className="font-mono text-slate-400">
                {metrics.data
                  ? `${metrics.data.process_id} · ${metrics.data.boot_id}`
                  : "—"}
              </span>
              <span className="text-slate-600">
                {" "}
                · per-replica counters; resets on deploy
              </span>
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            {metrics.isFetching && (
              <span className="inline-flex items-center gap-2 text-xs text-slate-500">
                <span className="dashboard-live-dot inline-block h-2 w-2 rounded-full bg-accent" />
                Updating…
              </span>
            )}
            <span className="text-xs text-slate-600">
              Last sync:{" "}
              {metrics.dataUpdatedAt
                ? new Date(metrics.dataUpdatedAt).toLocaleTimeString()
                : "—"}
            </span>
            <button
              type="button"
              onClick={() => refreshAll()}
              className="focus-ring rounded-lg border border-surface-border bg-surface-raised px-3 py-1.5 text-sm text-slate-300 transition hover:border-accent/40 hover:text-white"
            >
              Refresh
            </button>
          </div>
        </header>

        <div className="dashboard-enter dashboard-enter-delay-2 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Link
            to="/servers"
            className="group block rounded-xl border border-surface-border bg-surface-raised/90 p-5 shadow-lg shadow-black/25 backdrop-blur-sm transition-all duration-200 hover:-translate-y-0.5 hover:border-accent/35 hover:shadow-xl"
          >
            <p className="text-xs uppercase tracking-wider text-slate-500">
              Servers
            </p>
            <p className="mt-1 font-mono text-3xl text-accent">
              {servers.isLoading ? "—" : inventory.enabledServers}
            </p>
            <p className="mt-1 text-sm text-slate-500">
              enabled of {servers.isLoading ? "—" : inventory.totalServers}
              {inventory.disabledServers > 0 && (
                <span className="text-amber-400/90">
                  {" "}
                  · {inventory.disabledServers} off
                </span>
              )}
            </p>
          </Link>
          <Link
            to="/users"
            className="group block rounded-xl border border-surface-border bg-surface-raised/90 p-5 shadow-lg shadow-black/25 backdrop-blur-sm transition-all duration-200 hover:-translate-y-0.5 hover:border-accent/35 hover:shadow-xl"
          >
            <p className="text-xs uppercase tracking-wider text-slate-500">
              Users
            </p>
            <p className="mt-1 font-mono text-3xl text-accent">
              {users.isLoading ? "—" : (users.data?.length ?? 0)}
            </p>
            <p className="mt-1 text-sm text-slate-500">total</p>
          </Link>
          <Link
            to="/apps"
            className="group block rounded-xl border border-surface-border bg-surface-raised/90 p-5 shadow-lg shadow-black/25 backdrop-blur-sm transition-all duration-200 hover:-translate-y-0.5 hover:border-accent/35 hover:shadow-xl"
          >
            <p className="text-xs uppercase tracking-wider text-slate-500">
              Apps
            </p>
            <p className="mt-1 font-mono text-3xl text-accent">
              {apps.isLoading ? "—" : appCounts.approved}
            </p>
            <p className="mt-1 text-sm text-slate-500">
              approved of {apps.isLoading ? "—" : appCounts.total}
              {appCounts.pending > 0 && (
                <span className="text-amber-400/90">
                  {" "}
                  · {appCounts.pending} pending
                </span>
              )}
            </p>
          </Link>
          <Link
            to="/approvals"
            className="group block rounded-xl border border-surface-border bg-surface-raised/90 p-5 shadow-lg shadow-black/25 backdrop-blur-sm transition-all duration-200 hover:-translate-y-0.5 hover:border-accent/35 hover:shadow-xl"
          >
            <p className="text-xs uppercase tracking-wider text-slate-500">
              Pending
            </p>
            <p className="mt-1 font-mono text-3xl text-amber-400">
              {pending.isLoading ? "—" : (pending.data?.length ?? 0)}
            </p>
            <p className="mt-1 text-sm text-slate-500">approvals</p>
          </Link>
        </div>

        <section className="dashboard-enter dashboard-enter-delay-3 grid gap-4 lg:grid-cols-2">
          <div className="rounded-xl border border-surface-border bg-surface-raised/80 p-5 shadow-inner shadow-black/20 backdrop-blur-sm">
            <h2 className="text-sm font-medium uppercase tracking-wide text-slate-500">
              Inventory
            </h2>
            <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2">
              <div>
                <dt className="text-slate-500">Users by role</dt>
                <dd className="mt-1 font-mono text-slate-200">
                  admin {inventory.byRole.admin ?? 0} · power{" "}
                  {inventory.byRole.power_user ?? 0} · user{" "}
                  {inventory.byRole.user ?? 0}
                </dd>
              </div>
              <div>
                <dt className="text-slate-500">Users by status</dt>
                <dd className="mt-1 font-mono text-slate-200">
                  active {inventory.byStatus.active ?? 0} · pending{" "}
                  {inventory.byStatus.pending ?? 0} · rejected{" "}
                  {inventory.byStatus.rejected ?? 0}
                </dd>
              </div>
              <div className="sm:col-span-2">
                <dt className="text-slate-500">Upstream credentials</dt>
                <dd className="mt-1 font-mono text-slate-200">
                  Secret Manager {inventory.withSecret} · inline header{" "}
                  {inventory.withHeader} · none {inventory.neitherCred}
                </dd>
              </div>
            </dl>
          </div>

          <div className="rounded-xl border border-surface-border bg-surface-raised/80 p-5 shadow-inner shadow-black/20 backdrop-blur-sm">
            <div className="flex items-start justify-between gap-2">
              <h2 className="text-sm font-medium uppercase tracking-wide text-slate-500">
                MCP pulse
              </h2>
              {metrics.isFetching && !loading && (
                <span
                  className="dashboard-live-dot inline-block h-2 w-2 shrink-0 rounded-full bg-accent"
                  title="Refreshing"
                />
              )}
            </div>
            <p className="mt-1 text-xs text-slate-600">
              Counts for this running instance only; use Log Explorer for full
              traffic history.
            </p>
            <dl className="mt-4 grid gap-2 font-mono text-sm sm:grid-cols-2">
              <div className="flex justify-between gap-2 border-b border-surface-border/60 py-1 text-slate-300">
                <dt className="text-slate-500">Authorized requests</dt>
                <dd>{metrics.data?.counters.mcp_requests_authorized ?? "—"}</dd>
              </div>
              <div className="flex justify-between gap-2 border-b border-surface-border/60 py-1 text-slate-300">
                <dt className="text-slate-500">Denied (no key)</dt>
                <dd>{metrics.data?.counters.mcp_denied_missing_key ?? "—"}</dd>
              </div>
              <div className="flex justify-between gap-2 border-b border-surface-border/60 py-1 text-slate-300">
                <dt className="text-slate-500">Denied (bad key)</dt>
                <dd>{metrics.data?.counters.mcp_denied_invalid_user ?? "—"}</dd>
              </div>
              <div className="flex justify-between gap-2 border-b border-surface-border/60 py-1 text-rose-300/90">
                <dt className="text-slate-500">Errors</dt>
                <dd>{metrics.data?.counters.mcp_errors ?? "—"}</dd>
              </div>
            </dl>
            {methodRows.length > 0 && (
              <div className="mt-4">
                <p className="text-xs uppercase tracking-wider text-slate-600">
                  JSON-RPC methods
                </p>
                <ul className="mt-2 max-h-32 space-y-1 overflow-y-auto font-mono text-xs text-slate-400">
                  {methodRows.map(([name, n]) => (
                    <li key={name} className="flex justify-between gap-2">
                      <span className="truncate text-slate-500">{name}</span>
                      <span className="text-slate-300">{n}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </section>

        <section className="dashboard-enter dashboard-enter-delay-4 space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-sm font-medium uppercase tracking-wide text-slate-500">
              Servers
            </h2>
            <button
              type="button"
              onClick={() => setServersOpen((o) => !o)}
              className="text-xs text-accent hover:underline"
            >
              {serversOpen ? "Hide list" : "Quick list"}
            </button>
          </div>
          {serversOpen && (
            <div className="overflow-hidden rounded-xl border border-surface-border bg-surface-raised/60 shadow-md shadow-black/20">
              <table className="w-full text-left text-sm">
                <thead>
                  <tr className="border-b border-surface-border text-xs uppercase tracking-wider text-slate-500">
                    <th className="px-4 py-2 font-medium">ID</th>
                    <th className="px-4 py-2 font-medium">Host</th>
                    <th className="px-4 py-2 font-medium">State</th>
                  </tr>
                </thead>
                <tbody>
                  {(servers.data ?? []).slice(0, 12).map((s) => (
                    <tr
                      key={s.id}
                      className="border-b border-surface-border/80 last:border-0"
                    >
                      <td className="px-4 py-2">
                        <Link
                          to={`/servers/${encodeURIComponent(s.id)}`}
                          className="font-mono text-accent hover:underline"
                        >
                          {s.id}
                        </Link>
                      </td>
                      <td
                        className="max-w-[12rem] truncate px-4 py-2 font-mono text-slate-400"
                        title={s.url}
                      >
                        {hostFromUrl(s.url)}
                      </td>
                      <td className="px-4 py-2">
                        {s.enabled ? (
                          <span className="text-emerald-400/90">on</span>
                        ) : (
                          <span className="text-slate-500">off</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {(servers.data?.length ?? 0) > 12 && (
                <p className="border-t border-surface-border px-4 py-2 text-xs text-slate-600">
                  Showing 12 of {servers.data?.length}.{" "}
                  <Link to="/servers" className="text-accent hover:underline">
                    View all
                  </Link>
                </p>
              )}
            </div>
          )}
        </section>

        <section className="dashboard-enter dashboard-enter-delay-5 rounded-xl border border-surface-border bg-surface-raised/80 p-5 shadow-inner shadow-black/20 backdrop-blur-sm">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="text-sm font-medium uppercase tracking-wide text-slate-500">
                Usage by user
              </h2>
              <p className="mt-1 text-sm text-slate-500">
                Recent proxy activity ranking for the last{" "}
                {usageRanking.data?.window_days ?? 30} days.
              </p>
            </div>
            {usageRanking.data?.truncated && (
              <span className="rounded-full border border-amber-800/60 bg-amber-950/30 px-3 py-1 text-xs text-amber-200">
                Truncated
              </span>
            )}
          </div>
          {usageRanking.isLoading ? (
            <p className="mt-4 text-sm text-slate-500">
              Loading usage ranking…
            </p>
          ) : usageRanking.error ? (
            <p className="mt-4 text-sm text-rose-400">
              {extractErrorMessage(usageRanking.error)}
            </p>
          ) : usageRanking.data?.users.length ? (
            <div className="mt-4 overflow-hidden rounded-xl border border-surface-border">
              <table className="w-full text-left text-sm">
                <thead className="bg-surface-raised text-xs uppercase tracking-wider text-slate-500">
                  <tr>
                    <th className="px-4 py-3 font-medium">User</th>
                    <th className="px-4 py-3 font-medium">Tool calls</th>
                    <th className="px-4 py-3 font-medium">Authorized</th>
                    <th className="px-4 py-3 font-medium">
                      Success / error / denied
                    </th>
                    <th className="px-4 py-3 font-medium">Last activity</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-surface-border">
                  {usageRanking.data.users.map((item) => (
                    <UsageRankingRow key={item.user_id} item={item} />
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="mt-4 text-sm text-slate-500">
              No recent proxy usage found.
            </p>
          )}
        </section>

        <section className="dashboard-enter flex flex-col gap-3 rounded-xl border border-surface-border border-accent/20 bg-surface-raised/70 p-5 shadow-lg shadow-black/30 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="text-sm font-medium text-white">Audit &amp; logs</h2>
            <p className="mt-1 max-w-xl text-sm text-slate-500">
              Structured events in Cloud Logging — full history, alerts, and
              cross-replica views.
            </p>
          </div>
          {auditLink.data?.url ? (
            <a
              href={auditLink.data.url}
              target="_blank"
              rel="noopener noreferrer"
              className="focus-ring inline-flex shrink-0 items-center justify-center rounded-xl bg-accent px-5 py-3 text-sm font-medium text-surface transition hover:opacity-90"
            >
              Open Log Explorer
            </a>
          ) : (
            <span className="text-sm text-slate-500">
              {auditLink.isLoading ? "Loading…" : "—"}
            </span>
          )}
        </section>

        {(servers.error || users.error || metrics.error) && (
          <p className="text-sm text-rose-400">
            {extractErrorMessage(servers.error || users.error || metrics.error)}
          </p>
        )}
      </div>
    </div>
  );
}

function UsageRankingRow({ item }: { item: UserUsageRankingItem }) {
  return (
    <tr className="hover:bg-surface-raised/50">
      <td className="px-4 py-3">
        <Link
          to={`/users/${encodeURIComponent(item.user_id)}`}
          className="font-mono text-accent hover:underline"
        >
          {item.identity_label}
        </Link>
        <p className="mt-1 text-xs text-slate-500">
          {item.kind === "agent" ? "Agent" : "Human"}
          {item.email ? ` · ${item.email}` : ""}
        </p>
      </td>
      <td className="px-4 py-3 font-mono text-slate-200">
        {item.tool_call_count}
      </td>
      <td className="px-4 py-3 font-mono text-slate-200">
        {item.authorized_request_count}
      </td>
      <td className="px-4 py-3 text-xs text-slate-400">
        <span className="font-mono text-slate-200">
          {item.result_counts.success}
        </span>{" "}
        /{" "}
        <span className="font-mono text-rose-300">
          {item.result_counts.error}
        </span>{" "}
        /{" "}
        <span className="font-mono text-amber-300">
          {item.result_counts.denied}
        </span>
      </td>
      <td className="px-4 py-3 text-slate-400">
        {formatDashboardDate(item.last_activity_at)}
      </td>
    </tr>
  );
}

function formatDashboardDate(value?: string | null): string {
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
