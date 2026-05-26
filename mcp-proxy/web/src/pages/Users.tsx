import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { apiFetchWithRefresh } from "../api/client";
import type { UserProfile } from "../types";

function displayIdentity(u: UserProfile): string {
  if (u.kind === "agent") {
    return u.agent_name?.trim() || "(agent)";
  }
  return u.email || "(no email)";
}

export function UsersPage() {
  const qc = useQueryClient();
  const [modalOpen, setModalOpen] = useState(false);
  const [createMode, setCreateMode] = useState<"agent" | "user">("agent");
  const [agentName, setAgentName] = useState("");
  const [agentUrl, setAgentUrl] = useState("");
  const [userEmail, setUserEmail] = useState("");
  const [createdKey, setCreatedKey] = useState<string | null>(null);

  const q = useQuery({
    queryKey: ["users"],
    queryFn: async () => {
      const r = await apiFetchWithRefresh("/users");
      if (!r.ok) throw new Error(await r.text());
      return r.json() as Promise<UserProfile[]>;
    },
  });

  const createAgent = useMutation({
    mutationFn: async () => {
      const r = await apiFetchWithRefresh("/users/agents", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          agent_name: agentName.trim(),
          agent_url: agentUrl.trim(),
        }),
      });
      if (!r.ok) throw new Error(await r.text());
      return r.json() as Promise<{
        user: UserProfile;
        user_key: string;
        proxy_url: string;
      }>;
    },
    onSuccess: (data) => {
      setCreatedKey(data.user_key);
      void qc.invalidateQueries({ queryKey: ["users"] });
    },
  });

  const createAdHocUser = useMutation({
    mutationFn: async () => {
      const r = await apiFetchWithRefresh("/users/ad-hoc", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: userEmail.trim(),
        }),
      });
      if (!r.ok) throw new Error(await r.text());
      return r.json() as Promise<{
        user: UserProfile;
        user_key: string;
        proxy_url: string;
      }>;
    },
    onSuccess: (data) => {
      setCreatedKey(data.user_key);
      void qc.invalidateQueries({ queryKey: ["users"] });
    },
  });

  const openAgentModal = () => {
    setCreateMode("agent");
    setAgentName("");
    setAgentUrl("");
    setUserEmail("");
    setCreatedKey(null);
    createAgent.reset();
    createAdHocUser.reset();
    setModalOpen(true);
  };

  const openUserModal = () => {
    setCreateMode("user");
    setAgentName("");
    setAgentUrl("");
    setUserEmail("");
    setCreatedKey(null);
    createAgent.reset();
    createAdHocUser.reset();
    setModalOpen(true);
  };

  const closeModal = () => {
    setModalOpen(false);
    setCreatedKey(null);
    createAgent.reset();
    createAdHocUser.reset();
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <h1 className="text-2xl font-semibold text-white">Users</h1>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={openUserModal}
            className="px-4 py-2 rounded-lg bg-accent text-surface text-sm font-medium hover:opacity-90"
          >
            Add ad-hoc user
          </button>
          <button
            type="button"
            onClick={openAgentModal}
            className="px-4 py-2 rounded-lg border border-surface-border text-white text-sm font-medium hover:bg-surface-raised"
          >
            Add service agent
          </button>
        </div>
      </div>

      {modalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div
            className="w-full max-w-md rounded-xl border border-surface-border bg-surface p-6 shadow-xl space-y-4"
            role="dialog"
            aria-labelledby={
              createMode === "agent" ? "add-agent-title" : "add-user-title"
            }
          >
            <h2
              id={createMode === "agent" ? "add-agent-title" : "add-user-title"}
              className="text-lg font-medium text-white"
            >
              {createMode === "agent" ? "New service agent" : "New ad-hoc user"}
            </h2>
            <p className="text-slate-500 text-sm">
              {createMode === "agent" ? (
                <>
                  No Google login. Role is always{" "}
                  <code className="text-slate-400">user</code>. Permissions and
                  MCP key work like other users.
                </>
              ) : (
                <>
                  Create an active human user directly from email and issue an
                  MCP key immediately. If they later use Google login with the
                  same email, it will attach to this user record.
                </>
              )}
            </p>
            {createdKey ? (
              <div className="space-y-3">
                <p className="text-emerald-400 text-sm">
                  {createMode === "agent"
                    ? "Agent created. Copy this MCP key now (shown once):"
                    : "User created. Copy this MCP key now (shown once):"}
                </p>
                <p className="text-xs font-mono text-amber-200 break-all bg-surface-raised p-3 rounded-lg">
                  {createdKey}
                </p>
                <button
                  type="button"
                  onClick={closeModal}
                  className="w-full py-2 rounded-lg bg-surface-border text-white text-sm"
                >
                  Close
                </button>
              </div>
            ) : (
              <form
                className="space-y-3"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (createMode === "agent") {
                    if (!agentName.trim()) return;
                    createAgent.mutate();
                    return;
                  }
                  if (!userEmail.trim()) return;
                  createAdHocUser.mutate();
                }}
              >
                {createMode === "agent" ? (
                  <>
                    <div>
                      <label className="block text-xs text-slate-500 mb-1">
                        Agent name
                      </label>
                      <input
                        className="w-full bg-surface-raised border border-surface-border rounded-lg px-3 py-2 text-sm text-white"
                        value={agentName}
                        onChange={(e) => setAgentName(e.target.value)}
                        placeholder="e.g. Cursor background agent"
                        autoComplete="off"
                      />
                    </div>
                    <div>
                      <label className="block text-xs text-slate-500 mb-1">
                        Agent page URL (optional)
                      </label>
                      <input
                        className="w-full bg-surface-raised border border-surface-border rounded-lg px-3 py-2 text-sm text-white"
                        value={agentUrl}
                        onChange={(e) => setAgentUrl(e.target.value)}
                        placeholder="https://…"
                        autoComplete="off"
                      />
                    </div>
                  </>
                ) : (
                  <div>
                    <label className="block text-xs text-slate-500 mb-1">
                      User email
                    </label>
                    <input
                      className="w-full bg-surface-raised border border-surface-border rounded-lg px-3 py-2 text-sm text-white"
                      value={userEmail}
                      onChange={(e) => setUserEmail(e.target.value)}
                      placeholder="name@example.com"
                      autoComplete="off"
                      type="email"
                    />
                  </div>
                )}
                {(createMode === "agent"
                  ? createAgent.error
                  : createAdHocUser.error) && (
                  <p className="text-red-400 text-sm">
                    {String(
                      createMode === "agent"
                        ? createAgent.error
                        : createAdHocUser.error,
                    )}
                  </p>
                )}
                <div className="flex gap-2 justify-end pt-2">
                  <button
                    type="button"
                    onClick={closeModal}
                    className="px-4 py-2 rounded-lg text-slate-400 text-sm hover:text-white"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={
                      createMode === "agent"
                        ? createAgent.isPending || !agentName.trim()
                        : createAdHocUser.isPending || !userEmail.trim()
                    }
                    className="px-4 py-2 rounded-lg bg-accent text-surface text-sm font-medium disabled:opacity-50"
                  >
                    {createMode === "agent"
                      ? createAgent.isPending
                        ? "Creating…"
                        : "Create"
                      : createAdHocUser.isPending
                        ? "Creating…"
                        : "Create"}
                  </button>
                </div>
              </form>
            )}
          </div>
        </div>
      )}

      {q.isLoading && <p className="text-slate-500">Loading…</p>}
      {q.error && <p className="text-red-400">{String(q.error)}</p>}
      <div className="overflow-x-auto rounded-xl border border-surface-border">
        <table className="w-full text-sm text-left">
          <thead className="bg-surface-raised text-slate-400 uppercase text-xs">
            <tr>
              <th className="px-4 py-3 font-medium">Type</th>
              <th className="px-4 py-3 font-medium">Identity</th>
              <th className="px-4 py-3 font-medium">Role</th>
              <th className="px-4 py-3 font-medium">Status</th>
              <th className="px-4 py-3 font-medium" />
            </tr>
          </thead>
          <tbody className="divide-y divide-surface-border">
            {q.data?.map((u) => (
              <tr key={u.id} className="hover:bg-surface-raised/50">
                <td className="px-4 py-3 text-slate-400">
                  {u.kind === "agent" ? "Agent" : "Human"}
                </td>
                <td className="px-4 py-3 text-slate-300">
                  {u.kind === "agent" && u.agent_url?.trim() ? (
                    <a
                      href={u.agent_url.trim()}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="font-mono text-accent hover:underline"
                    >
                      {displayIdentity(u)}
                    </a>
                  ) : (
                    <span className="font-mono">{displayIdentity(u)}</span>
                  )}
                </td>
                <td className="px-4 py-3">{u.role}</td>
                <td className="px-4 py-3">
                  <span
                    className={`text-xs px-2 py-0.5 rounded ${
                      u.status === "active"
                        ? "bg-emerald-900/40 text-emerald-300"
                        : u.status === "pending"
                          ? "bg-amber-900/40 text-amber-300"
                          : "bg-red-900/40 text-red-300"
                    }`}
                  >
                    {u.status}
                  </span>
                </td>
                <td className="px-4 py-3">
                  <Link
                    className="text-accent hover:underline"
                    to={`/users/${encodeURIComponent(u.id)}`}
                  >
                    Manage
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
