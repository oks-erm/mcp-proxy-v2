import { useQuery } from "@tanstack/react-query";
import { Link, NavLink, Outlet } from "react-router-dom";
import { apiFetchWithRefresh } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import type { AccessRequest, ImprovementRequest, UserProfile } from "../types";

const adminNav = [
  { to: "/dashboard", label: "Dashboard" },
  { to: "/servers", label: "Servers" },
  { to: "/apps", label: "Apps" },
  { to: "/workflows", label: "Workflows" },
  { to: "/users", label: "Users" },
  { to: "/approvals", label: "Approvals" },
  { to: "/audit", label: "Audit" },
  { to: "/account", label: "MCP setup" },
  { to: "/usage", label: "Activity logs" },
];

/** Non-admins only use MCP setup + their own usage logs in Log Explorer. */
const standardUserNav = [
  { to: "/apps", label: "Apps" },
  { to: "/account", label: "MCP setup" },
  { to: "/usage", label: "Activity logs" },
];

function HeaderNavLink({
  to,
  label,
  compact,
  showApprovalsDot,
}: {
  to: string;
  label: string;
  compact: boolean;
  showApprovalsDot: boolean;
}) {
  const approvalsAria =
    to === "/approvals" && showApprovalsDot
      ? `${label} (pending requests)`
      : undefined;

  return (
    <NavLink
      to={to}
      aria-label={approvalsAria}
      className={({ isActive }) =>
        `relative ${compact ? "px-2 py-1 rounded text-xs" : "px-3 py-1.5 rounded-md text-sm font-medium transition-colors"} ${
          isActive
            ? "bg-surface-border text-accent"
            : compact
              ? "text-slate-500"
              : "text-slate-400 hover:text-slate-200"
        }`
      }
    >
      <span className="inline-flex items-center">{label}</span>
      {to === "/approvals" && showApprovalsDot && (
        <span
          className={`absolute rounded-full bg-amber-400 ring-2 ring-surface-raised ${
            compact ? "top-0.5 right-0.5 h-1.5 w-1.5" : "top-1 right-1 h-2 w-2"
          }`}
          title="Pending approval requests"
          aria-hidden
        />
      )}
    </NavLink>
  );
}

export function Layout() {
  const { state, logout } = useAuth();
  const isAdmin = state.user?.role === "admin";
  const nav = isAdmin ? adminNav : standardUserNav;
  const homeLink = isAdmin ? "/dashboard" : "/account";

  const pendingUsers = useQuery({
    queryKey: ["users", "pending"],
    queryFn: async () => {
      const r = await apiFetchWithRefresh("/users/pending");
      if (!r.ok) throw new Error(await r.text());
      return r.json() as Promise<UserProfile[]>;
    },
    enabled: isAdmin,
    refetchInterval: 45_000,
  });
  const accessReqs = useQuery({
    queryKey: ["admin", "access-requests"],
    queryFn: async () => {
      const r = await apiFetchWithRefresh("/admin/access-requests/pending");
      if (!r.ok) throw new Error(await r.text());
      return r.json() as Promise<AccessRequest[]>;
    },
    enabled: isAdmin,
    refetchInterval: 45_000,
  });
  const improvementReqs = useQuery({
    queryKey: ["admin", "improvement-requests"],
    queryFn: async () => {
      const r = await apiFetchWithRefresh(
        "/admin/improvement-requests/pending",
      );
      if (!r.ok) throw new Error(await r.text());
      return r.json() as Promise<ImprovementRequest[]>;
    },
    enabled: isAdmin,
    refetchInterval: 45_000,
  });

  const approvalsPendingCount =
    (pendingUsers.data?.length ?? 0) +
    (accessReqs.data?.length ?? 0) +
    (improvementReqs.data?.length ?? 0);
  const showApprovalsDot = isAdmin && approvalsPendingCount > 0;

  return (
    <div className="min-h-screen flex flex-col">
      <header className="border-b border-surface-border bg-surface-raised/80 backdrop-blur-sm sticky top-0 z-10">
        <div className="max-w-7xl mx-auto px-4 py-3 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-6">
            <Link
              to={homeLink}
              className="font-semibold text-accent tracking-tight"
            >
              MCP Proxy
            </Link>
            <nav className="hidden md:flex gap-1">
              {nav.map(({ to, label }) => (
                <HeaderNavLink
                  key={to}
                  to={to}
                  label={label}
                  compact={false}
                  showApprovalsDot={showApprovalsDot}
                />
              ))}
            </nav>
          </div>
          <div className="flex items-center gap-3 text-sm">
            {state.user && (
              <span className="text-slate-500 font-mono truncate max-w-[14rem]">
                {state.user.email}
              </span>
            )}
            <button
              type="button"
              onClick={() => void logout()}
              className="text-slate-400 hover:text-white focus-ring rounded px-2 py-1"
            >
              Sign out
            </button>
          </div>
        </div>
        <div className="md:hidden border-t border-surface-border px-2 pb-2 flex flex-wrap gap-1">
          {nav.map(({ to, label }) => (
            <HeaderNavLink
              key={to}
              to={to}
              label={label}
              compact
              showApprovalsDot={showApprovalsDot}
            />
          ))}
        </div>
      </header>
      <main className="flex-1 max-w-7xl w-full mx-auto px-4 py-8">
        <Outlet />
      </main>
    </div>
  );
}
