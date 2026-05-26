import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetchWithRefresh } from "../api/client";
import type {
  AccessRequest,
  ImprovementRequest,
  SkillUpdateRequest,
  UserProfile,
} from "../types";

export function ApprovalsPage() {
  const qc = useQueryClient();
  const pendingUsers = useQuery({
    queryKey: ["users", "pending"],
    queryFn: async () => {
      const r = await apiFetchWithRefresh("/users/pending");
      if (!r.ok) throw new Error(await r.text());
      return r.json() as Promise<UserProfile[]>;
    },
  });
  const accessReqs = useQuery({
    queryKey: ["admin", "access-requests"],
    queryFn: async () => {
      const r = await apiFetchWithRefresh("/admin/access-requests/pending");
      if (!r.ok) throw new Error(await r.text());
      return r.json() as Promise<AccessRequest[]>;
    },
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
  });
  const skillUpdateReqs = useQuery({
    queryKey: ["admin", "skill-update-requests"],
    queryFn: async () => {
      const r = await apiFetchWithRefresh(
        "/admin/skill-update-requests/pending",
      );
      if (!r.ok) throw new Error(await r.text());
      return r.json() as Promise<SkillUpdateRequest[]>;
    },
  });

  const approve = useMutation({
    mutationFn: async (userId: string) => {
      const r = await apiFetchWithRefresh(
        `/users/${encodeURIComponent(userId)}/approve`,
        {
          method: "POST",
        },
      );
      if (!r.ok) throw new Error(await r.text());
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["users"] });
      void qc.invalidateQueries({ queryKey: ["users", "pending"] });
      void qc.invalidateQueries({ queryKey: ["admin", "access-requests"] });
      void qc.invalidateQueries({
        queryKey: ["admin", "improvement-requests"],
      });
      void qc.invalidateQueries({
        queryKey: ["admin", "skill-update-requests"],
      });
    },
  });

  const reject = useMutation({
    mutationFn: async (userId: string) => {
      const r = await apiFetchWithRefresh(
        `/users/${encodeURIComponent(userId)}/reject`,
        {
          method: "POST",
        },
      );
      if (!r.ok) throw new Error(await r.text());
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["users"] });
      void qc.invalidateQueries({ queryKey: ["users", "pending"] });
      void qc.invalidateQueries({ queryKey: ["admin", "access-requests"] });
      void qc.invalidateQueries({
        queryKey: ["admin", "improvement-requests"],
      });
      void qc.invalidateQueries({
        queryKey: ["admin", "skill-update-requests"],
      });
    },
  });
  const approveImprovement = useMutation({
    mutationFn: async (requestId: string) => {
      const r = await apiFetchWithRefresh(
        `/admin/improvement-requests/${encodeURIComponent(requestId)}/approve`,
        {
          method: "POST",
        },
      );
      if (!r.ok) throw new Error(await r.text());
    },
    onSuccess: () => {
      void qc.invalidateQueries({
        queryKey: ["admin", "improvement-requests"],
      });
    },
  });
  const declineImprovement = useMutation({
    mutationFn: async (requestId: string) => {
      const r = await apiFetchWithRefresh(
        `/admin/improvement-requests/${encodeURIComponent(requestId)}/decline`,
        {
          method: "POST",
        },
      );
      if (!r.ok) throw new Error(await r.text());
    },
    onSuccess: () => {
      void qc.invalidateQueries({
        queryKey: ["admin", "improvement-requests"],
      });
    },
  });
  const approveSkillUpdate = useMutation({
    mutationFn: async (requestId: string) => {
      const r = await apiFetchWithRefresh(
        `/admin/skill-update-requests/${encodeURIComponent(requestId)}/approve`,
        {
          method: "POST",
        },
      );
      if (!r.ok) throw new Error(await r.text());
    },
    onSuccess: () => {
      void qc.invalidateQueries({
        queryKey: ["admin", "skill-update-requests"],
      });
    },
  });
  const declineSkillUpdate = useMutation({
    mutationFn: async (requestId: string) => {
      const r = await apiFetchWithRefresh(
        `/admin/skill-update-requests/${encodeURIComponent(requestId)}/decline`,
        {
          method: "POST",
        },
      );
      if (!r.ok) throw new Error(await r.text());
    },
    onSuccess: () => {
      void qc.invalidateQueries({
        queryKey: ["admin", "skill-update-requests"],
      });
    },
  });

  return (
    <div className="space-y-8">
      <h1 className="text-2xl font-semibold text-white">Approvals</h1>
      <section>
        <h2 className="text-slate-400 text-sm uppercase tracking-wider mb-3">
          Pending users
        </h2>
        {pendingUsers.isLoading && <p className="text-slate-500">Loading…</p>}
        <ul className="space-y-2">
          {pendingUsers.data?.map((u) => (
            <li
              key={u.id}
              className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-surface-border bg-surface-raised px-4 py-3"
            >
              <span className="font-mono text-slate-300">{u.email}</span>
              <div className="flex gap-2">
                <button
                  type="button"
                  disabled={approve.isPending || reject.isPending}
                  onClick={() => approve.mutate(u.id)}
                  className="px-3 py-1.5 rounded bg-emerald-900/50 text-emerald-300 text-sm"
                >
                  Approve
                </button>
                <button
                  type="button"
                  disabled={approve.isPending || reject.isPending}
                  onClick={() => reject.mutate(u.id)}
                  className="px-3 py-1.5 rounded bg-red-900/40 text-red-300 text-sm"
                >
                  Reject
                </button>
              </div>
            </li>
          ))}
        </ul>
        {pendingUsers.data?.length === 0 && (
          <p className="text-slate-500 text-sm">No pending users.</p>
        )}
      </section>
      <section>
        <h2 className="text-slate-400 text-sm uppercase tracking-wider mb-3">
          Access request records
        </h2>
        {accessReqs.isLoading && <p className="text-slate-500">Loading…</p>}
        <ul className="space-y-2">
          {accessReqs.data?.map((a) => (
            <li
              key={a.id}
              className="rounded-lg border border-surface-border bg-surface-raised px-4 py-2 text-sm"
            >
              <span className="font-mono text-slate-400">{a.id}</span> —{" "}
              {a.email}
            </li>
          ))}
        </ul>
      </section>
      <section>
        <h2 className="text-slate-400 text-sm uppercase tracking-wider mb-3">
          MCP Improvement Requests
        </h2>
        {improvementReqs.isLoading && (
          <p className="text-slate-500">Loading…</p>
        )}
        <ul className="space-y-3">
          {improvementReqs.data?.map((req) => (
            <li
              key={req.id}
              className="rounded-lg border border-surface-border bg-surface-raised px-4 py-3 text-sm text-slate-300"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium text-white">
                      {req.summary}
                    </span>
                    <span className="rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[11px] uppercase tracking-wide text-amber-200">
                      MCP
                    </span>
                    <span className="font-mono text-xs text-slate-500">
                      {req.id}
                    </span>
                  </div>
                  <p className="text-slate-400">
                    From{" "}
                    {req.requester_label ||
                      req.requester_email ||
                      req.requester_user_id}
                  </p>
                  {req.details && <p>{req.details}</p>}
                  <p className="text-slate-400">
                    Tools:{" "}
                    {req.tool_names.length
                      ? req.tool_names.join(", ")
                      : "none specified"}
                  </p>
                  <p className="text-slate-400">
                    Servers:{" "}
                    {req.server_ids.length
                      ? req.server_ids.join(", ")
                      : "none specified"}
                  </p>
                  {req.error_context?.error_message && (
                    <p className="text-amber-200/90">
                      Error:{" "}
                      {req.error_context.failed_tool_name || "unknown tool"}
                      {req.error_context.error_code != null
                        ? ` (${req.error_context.error_code})`
                        : ""}
                      {" — "}
                      {req.error_context.error_message}
                    </p>
                  )}
                </div>
                <div className="flex gap-2">
                  <button
                    type="button"
                    disabled={
                      approveImprovement.isPending ||
                      declineImprovement.isPending
                    }
                    onClick={() => approveImprovement.mutate(req.id)}
                    className="px-3 py-1.5 rounded bg-emerald-900/50 text-emerald-300 text-sm"
                  >
                    Approve
                  </button>
                  <button
                    type="button"
                    disabled={
                      approveImprovement.isPending ||
                      declineImprovement.isPending
                    }
                    onClick={() => declineImprovement.mutate(req.id)}
                    className="px-3 py-1.5 rounded bg-red-900/40 text-red-300 text-sm"
                  >
                    Decline
                  </button>
                </div>
              </div>
            </li>
          ))}
        </ul>
        {improvementReqs.data?.length === 0 && (
          <p className="text-slate-500 text-sm">
            No pending improvement requests.
          </p>
        )}
      </section>
      <section>
        <h2 className="text-slate-400 text-sm uppercase tracking-wider mb-3">
          Skill Update Requests
        </h2>
        {skillUpdateReqs.isLoading && (
          <p className="text-slate-500">Loading…</p>
        )}
        <ul className="space-y-3">
          {skillUpdateReqs.data?.map((req) => (
            <li
              key={req.id}
              className="rounded-lg border border-surface-border bg-surface-raised px-4 py-3 text-sm text-slate-300"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium text-white">
                      {req.summary}
                    </span>
                    <span className="rounded-full border border-sky-500/40 bg-sky-500/10 px-2 py-0.5 text-[11px] uppercase tracking-wide text-sky-200">
                      Skill
                    </span>
                    <span className="font-mono text-xs text-slate-500">
                      {req.id}
                    </span>
                  </div>
                  <p className="text-slate-400">
                    From{" "}
                    {req.requester_label ||
                      req.requester_email ||
                      req.requester_user_id}
                  </p>
                  <p className="text-slate-400">
                    Skill: <span className="font-mono">{req.skill_name}</span>
                  </p>
                  {req.details && <p>{req.details}</p>}
                  {req.desired_outcome && (
                    <p className="text-sky-100/90">
                      Desired outcome: {req.desired_outcome}
                    </p>
                  )}
                </div>
                <div className="flex gap-2">
                  <button
                    type="button"
                    disabled={
                      approveSkillUpdate.isPending ||
                      declineSkillUpdate.isPending
                    }
                    onClick={() => approveSkillUpdate.mutate(req.id)}
                    className="px-3 py-1.5 rounded bg-emerald-900/50 text-emerald-300 text-sm"
                  >
                    Approve
                  </button>
                  <button
                    type="button"
                    disabled={
                      approveSkillUpdate.isPending ||
                      declineSkillUpdate.isPending
                    }
                    onClick={() => declineSkillUpdate.mutate(req.id)}
                    className="px-3 py-1.5 rounded bg-red-900/40 text-red-300 text-sm"
                  >
                    Decline
                  </button>
                </div>
              </div>
            </li>
          ))}
        </ul>
        {skillUpdateReqs.data?.length === 0 && (
          <p className="text-slate-500 text-sm">
            No pending skill update requests.
          </p>
        )}
      </section>
    </div>
  );
}
