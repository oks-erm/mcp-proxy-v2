import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { useAuth } from "../auth/AuthContext";
import { apiFetchWithRefresh } from "../api/client";
import type { UpstreamOAuthConnectionsResponse } from "../types";

/** Shown only if the API cannot return a stored key (legacy account or decrypt failure). */
const PLACEHOLDER_KEY = "YOUR_SAVED_API_KEY_OR_REGENERATE";

export function buildMcpEndpointUrl(proxyUrl: string): string {
  const base = (proxyUrl || "").replace(/\/$/, "");
  if (!base) return "";
  const path = "/mcp-server/mcp";
  return base.endsWith(path) ? base : `${base}${path}`;
}

/** Escape a value for use inside TOML double-quoted strings. */
function tomlEscapeDoubleQuoted(s: string): string {
  return s.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
}

/** OpenAI Codex `config.toml` snippet for streamable HTTP + static API key header. */
export function buildCodexMcpToml(proxyUrl: string, apiKey: string): string {
  const url = buildMcpEndpointUrl(proxyUrl);
  return `[mcp_servers.mcp-proxy]
url = "${tomlEscapeDoubleQuoted(url)}"
http_headers = { "X-API-Key" = "${tomlEscapeDoubleQuoted(apiKey)}" }
`;
}

/** Cursor-style MCP config; merge into ~/.cursor/mcp.json or project .cursor/mcp.json */
export function buildCursorMcpConfig(proxyUrl: string, apiKey: string) {
  return {
    mcpServers: {
      "mcp-proxy": {
        url: buildMcpEndpointUrl(proxyUrl),
        headers: {
          "X-API-Key": apiKey,
        },
      },
    },
  };
}

function buildClaudeCodeCliLine(endpointUrl: string, apiKey: string): string {
  return `claude mcp add --transport http mcp-proxy ${JSON.stringify(endpointUrl)} --header ${JSON.stringify(`X-API-Key: ${apiKey}`)}`;
}

/** Gemini CLI: merge into `~/.gemini/settings.json` or `.gemini/settings.json` under `mcpServers`. */
export function buildGeminiMcpSettingsSnippet(
  proxyUrl: string,
  apiKey: string,
): string {
  const httpUrl = buildMcpEndpointUrl(proxyUrl);
  return JSON.stringify(
    {
      mcpServers: {
        "mcp-proxy": {
          httpUrl,
          headers: {
            "X-API-Key": apiKey,
          },
        },
      },
    },
    null,
    2,
  );
}

function buildGeminiMcpAddCliLine(endpointUrl: string, apiKey: string): string {
  return `gemini mcp add --transport http --scope user mcp-proxy ${JSON.stringify(endpointUrl)} --header ${JSON.stringify(`X-API-Key: ${apiKey}`)}`;
}

type CopiedSection =
  | "json"
  | "codex"
  | "claude-cli"
  | "gemini-json"
  | "gemini-cli"
  | null;

export function AccountPage() {
  const queryClient = useQueryClient();
  const [copiedSection, setCopiedSection] = useState<CopiedSection>(null);
  const { state, reloadMe } = useAuth();
  const creds = useQuery({
    queryKey: ["me", "mcp-credentials"],
    queryFn: async () => {
      const r = await apiFetchWithRefresh("/me/mcp-credentials");
      if (!r.ok) throw new Error("Could not load credentials");
      return r.json() as Promise<{
        user_key?: string;
        proxy_url?: string;
        message?: string;
      }>;
    },
    enabled: state.user?.status === "active",
  });

  const oauthConnections = useQuery({
    queryKey: ["me", "upstream-oauth", "connections"],
    queryFn: async () => {
      const r = await apiFetchWithRefresh("/me/upstream-oauth/connections");
      if (!r.ok) throw new Error("Could not load OAuth connections");
      return r.json() as Promise<UpstreamOAuthConnectionsResponse>;
    },
    enabled: state.user?.status === "active",
  });

  const [oauthFlash, setOauthFlash] = useState<string | null>(null);
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    if (q.get("upstream_oauth_connected")) {
      setOauthFlash(
        `Connected: ${q.get("upstream_oauth_connected")}. Reconnect your MCP client if it was already open.`,
      );
      q.delete("upstream_oauth_connected");
      const next =
        window.location.pathname +
        (q.toString() ? `?${q.toString()}` : "") +
        window.location.hash;
      window.history.replaceState({}, "", next);
    } else if (q.get("upstream_oauth_error")) {
      setOauthFlash(
        "OAuth linking failed. Check redirect URI and client settings, then try again.",
      );
      q.delete("upstream_oauth_error");
      q.delete("reason");
      const next =
        window.location.pathname +
        (q.toString() ? `?${q.toString()}` : "") +
        window.location.hash;
      window.history.replaceState({}, "", next);
    }
  }, []);

  const disconnectOauth = useMutation({
    mutationFn: async (serverId: string) => {
      const r = await apiFetchWithRefresh(
        `/me/upstream-oauth/${encodeURIComponent(serverId)}`,
        { method: "DELETE" },
      );
      if (!r.ok) throw new Error(await r.text());
    },
    onSuccess: () => {
      void oauthConnections.refetch();
      setOauthFlash("Disconnected.");
    },
  });

  const regen = useMutation({
    mutationFn: async () => {
      const r = await apiFetchWithRefresh("/me/mcp-credentials/regenerate", {
        method: "POST",
      });
      if (!r.ok) throw new Error(await r.text());
      return r.json() as Promise<{ user_key?: string; proxy_url?: string }>;
    },
    onSuccess: () => {
      void reloadMe();
      void queryClient.invalidateQueries({
        queryKey: ["me", "mcp-credentials"],
      });
    },
  });

  const rawKey = regen.data?.user_key ?? creds.data?.user_key;
  const apiKeyForConfig = rawKey ?? PLACEHOLDER_KEY;
  const cursorConfigText = useMemo(() => {
    const proxy = creds.data?.proxy_url ?? "";
    if (!proxy) return "";
    return JSON.stringify(
      buildCursorMcpConfig(proxy, apiKeyForConfig),
      null,
      2,
    );
  }, [creds.data?.proxy_url, apiKeyForConfig]);

  const codexTomlText = useMemo(() => {
    const proxy = creds.data?.proxy_url ?? "";
    if (!proxy) return "";
    return buildCodexMcpToml(proxy, apiKeyForConfig);
  }, [creds.data?.proxy_url, apiKeyForConfig]);

  const claudeCliLine = useMemo(() => {
    const proxy = creds.data?.proxy_url ?? "";
    if (!proxy) return "";
    return buildClaudeCodeCliLine(buildMcpEndpointUrl(proxy), apiKeyForConfig);
  }, [creds.data?.proxy_url, apiKeyForConfig]);

  const geminiSettingsText = useMemo(() => {
    const proxy = creds.data?.proxy_url ?? "";
    if (!proxy) return "";
    return buildGeminiMcpSettingsSnippet(proxy, apiKeyForConfig);
  }, [creds.data?.proxy_url, apiKeyForConfig]);

  const geminiCliLine = useMemo(() => {
    const proxy = creds.data?.proxy_url ?? "";
    if (!proxy) return "";
    return buildGeminiMcpAddCliLine(
      buildMcpEndpointUrl(proxy),
      apiKeyForConfig,
    );
  }, [creds.data?.proxy_url, apiKeyForConfig]);

  async function copyToClipboard(
    text: string,
    section: Exclude<CopiedSection, null>,
  ) {
    if (!text) return;
    await navigator.clipboard.writeText(text);
    setCopiedSection(section);
    window.setTimeout(() => setCopiedSection(null), 2000);
  }

  const detailsClass =
    "rounded-lg border border-slate-800/80 bg-slate-950/40 [&_summary]:cursor-pointer [&_summary]:list-none [&_summary::-webkit-details-marker]:hidden";

  return (
    <div className="max-w-2xl space-y-6">
      <h1 className="text-2xl font-semibold text-white">Account</h1>
      <div className="rounded-xl border border-surface-border bg-surface-raised p-5 space-y-3 text-sm">
        <p>
          <span className="text-slate-500">Email</span>
          <br />
          <span className="font-mono text-slate-200">{state.user?.email}</span>
        </p>
        <p>
          <span className="text-slate-500">Role</span>
          <br />
          <span className="font-mono text-slate-200">{state.user?.role}</span>
        </p>
      </div>
      {state.user?.status === "active" && (
        <div className="rounded-xl border border-surface-border bg-surface-raised p-5 space-y-4">
          <h2 className="text-white font-medium">MCP connection</h2>
          {creds.isLoading && <p className="text-slate-500">Loading…</p>}
          {creds.data?.proxy_url && (
            <p className="text-sm">
              <span className="text-slate-500">Proxy URL</span>
              <br />
              <code className="font-mono text-accent break-all">
                {buildMcpEndpointUrl(creds.data.proxy_url)}
              </code>
            </p>
          )}
          {creds.data?.user_key && (
            <p className="text-sm">
              <span className="text-slate-500">API key</span>
              <br />
              <code className="font-mono text-amber-300 break-all">
                {creds.data.user_key}
              </code>
            </p>
          )}
          {creds.data?.message && !creds.data?.user_key && (
            <p className="text-slate-400 text-sm">{creds.data.message}</p>
          )}
          <button
            type="button"
            disabled={regen.isPending}
            onClick={() => regen.mutate()}
            className="px-4 py-2 rounded-lg bg-surface-border text-slate-200 hover:bg-slate-700 focus-ring text-sm"
          >
            Regenerate API key
          </button>
          {oauthFlash && (
            <p className="text-sm text-amber-200/90 border border-amber-900/50 rounded-lg px-3 py-2">
              {oauthFlash}
            </p>
          )}
          <div className="pt-4 border-t border-surface-border space-y-2">
            <h3 className="text-white font-medium text-sm">
              Upstream OAuth2 (per server)
            </h3>
            <p className="text-slate-500 text-xs leading-relaxed">
              For MCP servers configured with{" "}
              <code className="text-slate-400">oauth2</code> upstream auth, link
              your provider account once. Tools for unlinked servers stay hidden
              until you connect.
            </p>
            {oauthConnections.isLoading && (
              <p className="text-slate-500 text-sm">Loading connections…</p>
            )}
            {oauthConnections.data?.servers &&
            oauthConnections.data.servers.length > 0 ? (
              <ul className="space-y-2 text-sm">
                {oauthConnections.data.servers.map((row) => (
                  <li
                    key={row.server_id}
                    className="flex flex-wrap items-center gap-2 justify-between rounded-lg border border-surface-border px-3 py-2"
                  >
                    <span className="font-mono text-accent">
                      {row.server_id}
                    </span>
                    <span
                      className={
                        row.connected ? "text-emerald-400" : "text-slate-500"
                      }
                    >
                      {row.connected ? "connected" : "not connected"}
                    </span>
                    <div className="flex gap-2 w-full sm:w-auto justify-end">
                      {!row.connected ? (
                        <button
                          type="button"
                          className="px-3 py-1.5 rounded-md bg-accent/20 text-accent text-xs font-medium hover:bg-accent/30"
                          onClick={async () => {
                            const r = await apiFetchWithRefresh(
                              `/me/upstream-oauth/${encodeURIComponent(row.server_id)}/authorize?next=${encodeURIComponent("/app/account")}`,
                              { redirect: "manual" },
                            );
                            if (r.status >= 300 && r.status < 400) {
                              const loc = r.headers.get("Location");
                              if (loc) window.location.href = loc;
                            }
                          }}
                        >
                          Connect
                        </button>
                      ) : (
                        <button
                          type="button"
                          disabled={disconnectOauth.isPending}
                          onClick={() => disconnectOauth.mutate(row.server_id)}
                          className="px-3 py-1.5 rounded-md border border-surface-border text-slate-300 text-xs hover:bg-surface-raised"
                        >
                          Disconnect
                        </button>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              oauthConnections.data && (
                <p className="text-slate-500 text-sm leading-relaxed">
                  {(oauthConnections.data.deployment_oauth2_server_count ??
                    0) === 0 ? (
                    <>
                      No MCP server is configured for{" "}
                      <code className="text-slate-400">oauth2</code> upstream
                      auth yet, or the stored{" "}
                      <code className="text-slate-400">oauth</code> block is
                      invalid so it was ignored.
                      {state.user?.role === "admin" ||
                      state.user?.role === "power_user" ? (
                        <>
                          {" "}
                          Add or edit a server under{" "}
                          <span className="text-slate-400">Servers</span>,
                          choose OAuth2, and save authorization / token URLs
                          plus client id.
                        </>
                      ) : null}
                    </>
                  ) : (
                    <>
                      OAuth2 MCP servers exist, but none are shared with your
                      account. Ask an admin to grant{" "}
                      <span className="text-slate-400">read</span> or{" "}
                      <span className="text-slate-400">write</span> on the
                      relevant server.
                    </>
                  )}
                </p>
              )
            )}
          </div>
          {regen.data?.user_key && (
            <p className="text-sm text-amber-300 font-mono break-all">
              New key: {regen.data.user_key}
            </p>
          )}
          {cursorConfigText && (
            <div className="space-y-3 pt-2 border-t border-surface-border">
              <div>
                <h3 className="text-white font-medium text-sm">
                  Connect from your tool
                </h3>
                <p className="text-slate-500 text-xs leading-relaxed mt-1">
                  Use the proxy URL and API key above. Each product stores MCP
                  settings in a different place; pick yours and merge or paste
                  the snippets below.
                </p>
              </div>
              <div className="space-y-2">
                <details className={detailsClass}>
                  <summary className="px-3 py-2.5 text-sm font-medium text-slate-200 hover:bg-slate-900/60 rounded-lg">
                    Cursor
                  </summary>
                  <div className="px-3 pb-3 pt-0 space-y-2 text-xs text-slate-400 leading-relaxed border-t border-slate-800/80 mt-0 pt-3">
                    <p>
                      Open or create{" "}
                      <code className="text-slate-300">~/.cursor/mcp.json</code>{" "}
                      (user-wide) or{" "}
                      <code className="text-slate-300">.cursor/mcp.json</code>{" "}
                      in a project. Merge the{" "}
                      <code className="text-slate-300">mcp-proxy</code> block
                      from the JSON below into{" "}
                      <code className="text-slate-300">mcpServers</code>. Reload
                      or restart Cursor if tools do not appear.
                    </p>
                    <p className="text-slate-500">
                      Windows:{" "}
                      <code className="text-slate-400">
                        %USERPROFILE%\.cursor\mcp.json
                      </code>
                    </p>
                  </div>
                </details>
                <details className={detailsClass}>
                  <summary className="px-3 py-2.5 text-sm font-medium text-slate-200 hover:bg-slate-900/60 rounded-lg">
                    Claude Desktop
                  </summary>
                  <div className="px-3 pb-3 pt-0 space-y-2 text-xs text-slate-400 leading-relaxed border-t border-slate-800/80 mt-0 pt-3">
                    <p>
                      Claude Desktop has native MCP support: add this proxy as a
                      remote streamable HTTP server via{" "}
                      <code className="text-slate-300">
                        claude_desktop_config.json
                      </code>
                      . Merge the same{" "}
                      <code className="text-slate-300">mcpServers</code> entry
                      as in the shared JSON below (
                      <code className="text-slate-300">url</code> +{" "}
                      <code className="text-slate-300">headers</code>).
                    </p>
                    <ul className="list-disc pl-4 space-y-1 text-slate-500">
                      <li>
                        macOS:{" "}
                        <code className="text-slate-400">
                          ~/Library/Application
                          Support/Claude/claude_desktop_config.json
                        </code>
                      </li>
                      <li>
                        Windows:{" "}
                        <code className="text-slate-400">
                          %APPDATA%\Claude\claude_desktop_config.json
                        </code>
                      </li>
                      <li>
                        Linux:{" "}
                        <code className="text-slate-400">
                          ~/.config/Claude/claude_desktop_config.json
                        </code>
                      </li>
                    </ul>
                    <p className="text-slate-500">
                      Fully quit and reopen Claude Desktop after saving.
                    </p>
                    <p className="text-slate-500">
                      More context:{" "}
                      <a
                        href="https://support.anthropic.com/en/articles/10949351-getting-started-with-model-context-protocol-mcp-on-claude-for-desktop"
                        className="text-accent hover:underline"
                        target="_blank"
                        rel="noreferrer"
                      >
                        Anthropic: MCP on Claude Desktop
                      </a>
                      ,{" "}
                      <a
                        href="https://modelcontextprotocol.io/docs/getting-started/intro"
                        className="text-accent hover:underline"
                        target="_blank"
                        rel="noreferrer"
                      >
                        MCP intro
                      </a>
                      .
                    </p>
                  </div>
                </details>
                <details className={detailsClass}>
                  <summary className="px-3 py-2.5 text-sm font-medium text-slate-200 hover:bg-slate-900/60 rounded-lg">
                    ChatGPT (MCP apps / connectors)
                  </summary>
                  <div className="px-3 pb-3 pt-0 space-y-2 text-xs text-slate-400 leading-relaxed border-t border-slate-800/80 mt-0 pt-3">
                    <p>
                      On{" "}
                      <strong className="text-slate-300">
                        eligible ChatGPT plans
                      </strong>
                      , workspace admins enable{" "}
                      <strong className="text-slate-300">developer mode</strong>{" "}
                      and authorized users create a{" "}
                      <strong className="text-slate-300">custom MCP app</strong>{" "}
                      pointing at this proxy&apos;s{" "}
                      <strong className="text-slate-300">public HTTPS</strong>{" "}
                      MCP URL (path{" "}
                      <code className="text-slate-300">/mcp-server/mcp</code> on
                      your proxy host; see shared JSON below). ChatGPT only
                      supports{" "}
                      <strong className="text-slate-300">remote</strong> MCP
                      servers, not localhost—Cloud Run is appropriate.
                    </p>
                    <p>
                      This proxy authenticates with header{" "}
                      <code className="text-slate-300">X-API-Key</code>. In
                      ChatGPT&apos;s app configuration, use whatever auth option
                      matches your workspace (custom headers or API key, if
                      offered). If only OAuth is available, your admin must
                      configure a flow ChatGPT supports.
                    </p>
                    <p className="text-slate-500">
                      MCP apps work on{" "}
                      <strong className="text-slate-400">ChatGPT web</strong>{" "}
                      only (not mobile). Plan details and roles vary by
                      Business, Enterprise, Edu, and Pro—see OpenAI&apos;s help.
                    </p>
                    <p className="text-slate-500">
                      <a
                        href="https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt-beta"
                        className="text-accent hover:underline"
                        target="_blank"
                        rel="noreferrer"
                      >
                        OpenAI: Developer mode and MCP apps in ChatGPT (beta)
                      </a>
                      .
                    </p>
                  </div>
                </details>
                <details className={detailsClass}>
                  <summary className="px-3 py-2.5 text-sm font-medium text-slate-200 hover:bg-slate-900/60 rounded-lg">
                    Amazon Q Developer
                  </summary>
                  <div className="px-3 pb-3 pt-0 space-y-2 text-xs text-slate-400 leading-relaxed border-t border-slate-800/80 mt-0 pt-3">
                    <p>
                      <strong className="text-slate-300">In the IDE</strong> (VS
                      Code, JetBrains, etc.): open the Amazon Q panel → Chat →{" "}
                      <strong className="text-slate-300">tools</strong> icon →
                      MCP configuration → add server. Choose transport{" "}
                      <code className="text-slate-300">http</code>, set{" "}
                      <strong className="text-slate-300">URL</strong> to this
                      proxy&apos;s MCP endpoint (
                      <code className="text-slate-300">…/mcp-server/mcp</code>
                      ), and under{" "}
                      <strong className="text-slate-300">
                        Headers
                      </strong> add{" "}
                      <code className="text-slate-300">X-API-Key</code> with
                      your key above.
                    </p>
                    <p className="text-slate-500">
                      Settings are stored at{" "}
                      <code className="text-slate-400">
                        ~/.aws/amazonq/default.json
                      </code>{" "}
                      (global) or{" "}
                      <code className="text-slate-400">
                        .amazonq/default.json
                      </code>{" "}
                      (project). Legacy locations{" "}
                      <code className="text-slate-400">
                        ~/.aws/amazonq/mcp.json
                      </code>{" "}
                      /{" "}
                      <code className="text-slate-400">.amazonq/mcp.json</code>{" "}
                      may still apply depending on your setup.
                    </p>
                    <p className="text-slate-500">
                      <strong className="text-slate-400">CLI:</strong> use{" "}
                      <code className="text-slate-400">qchat mcp</code> for HTTP
                      servers—see{" "}
                      <a
                        href="https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/mcp-ide.html"
                        className="text-accent hover:underline"
                        target="_blank"
                        rel="noreferrer"
                      >
                        MCP in the IDE
                      </a>{" "}
                      and{" "}
                      <a
                        href="https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/command-line-mcp-config-CLI.html"
                        className="text-accent hover:underline"
                        target="_blank"
                        rel="noreferrer"
                      >
                        MCP in the CLI
                      </a>
                      .
                    </p>
                  </div>
                </details>
                <details className={detailsClass}>
                  <summary className="px-3 py-2.5 text-sm font-medium text-slate-200 hover:bg-slate-900/60 rounded-lg">
                    Google Gemini (CLI)
                  </summary>
                  <div className="px-3 pb-3 pt-0 space-y-2 text-xs text-slate-400 leading-relaxed border-t border-slate-800/80 mt-0 pt-3">
                    <p>
                      MCP for Gemini is documented for{" "}
                      <strong className="text-slate-300">Gemini CLI</strong>,
                      not the consumer Gemini web app. Add the proxy as a
                      streamable HTTP server with{" "}
                      <code className="text-slate-300">httpUrl</code> and{" "}
                      <code className="text-slate-300">headers</code> in{" "}
                      <code className="text-slate-300">
                        ~/.gemini/settings.json
                      </code>{" "}
                      (user) or{" "}
                      <code className="text-slate-300">
                        .gemini/settings.json
                      </code>{" "}
                      (project), merging the{" "}
                      <code className="text-slate-300">mcpServers</code> block
                      below into your existing file.
                    </p>
                    {geminiSettingsText && (
                      <div className="rounded-lg bg-slate-950 border border-slate-800 p-3">
                        <pre className="text-xs text-slate-300 overflow-x-auto font-mono whitespace-pre-wrap break-all max-h-48 overflow-y-auto">
                          {geminiSettingsText}
                        </pre>
                        <button
                          type="button"
                          onClick={() =>
                            void copyToClipboard(
                              geminiSettingsText,
                              "gemini-json",
                            )
                          }
                          className="mt-2 px-3 py-1.5 rounded-md bg-accent/20 text-accent text-xs font-medium hover:bg-accent/30 focus-ring"
                        >
                          {copiedSection === "gemini-json"
                            ? "Copied"
                            : "Copy JSON"}
                        </button>
                      </div>
                    )}
                    <p>
                      Or run once in a terminal (user-wide config):{" "}
                      <code className="text-slate-400">--scope user</code> adds
                      to{" "}
                      <code className="text-slate-400">
                        ~/.gemini/settings.json
                      </code>
                      .
                    </p>
                    {geminiCliLine && (
                      <div className="rounded-lg bg-slate-950 border border-slate-800 p-3">
                        <pre className="text-xs text-slate-300 overflow-x-auto font-mono whitespace-pre-wrap break-all">
                          {geminiCliLine}
                        </pre>
                        <button
                          type="button"
                          onClick={() =>
                            void copyToClipboard(geminiCliLine, "gemini-cli")
                          }
                          className="mt-2 px-3 py-1.5 rounded-md bg-accent/20 text-accent text-xs font-medium hover:bg-accent/30 focus-ring"
                        >
                          {copiedSection === "gemini-cli"
                            ? "Copied"
                            : "Copy command"}
                        </button>
                      </div>
                    )}
                    <p className="text-slate-500">
                      <a
                        href="https://google-gemini.github.io/gemini-cli/docs/tools/mcp-server.html"
                        className="text-accent hover:underline"
                        target="_blank"
                        rel="noreferrer"
                      >
                        Gemini CLI: MCP servers
                      </a>
                      .
                    </p>
                  </div>
                </details>
                <details className={detailsClass}>
                  <summary className="px-3 py-2.5 text-sm font-medium text-slate-200 hover:bg-slate-900/60 rounded-lg">
                    Claude Code
                  </summary>
                  <div className="px-3 pb-3 pt-0 space-y-2 text-xs text-slate-400 leading-relaxed border-t border-slate-800/80 mt-0 pt-3">
                    <p>
                      In a terminal, run the following once (adds an HTTP MCP
                      server with your key). If you already have a server named{" "}
                      <code className="text-slate-300">mcp-proxy</code>, remove
                      it first or pick another name.
                    </p>
                    {claudeCliLine && (
                      <div className="rounded-lg bg-slate-950 border border-slate-800 p-3">
                        <pre className="text-xs text-slate-300 overflow-x-auto font-mono whitespace-pre-wrap break-all">
                          {claudeCliLine}
                        </pre>
                        <button
                          type="button"
                          onClick={() =>
                            void copyToClipboard(claudeCliLine, "claude-cli")
                          }
                          className="mt-2 px-3 py-1.5 rounded-md bg-accent/20 text-accent text-xs font-medium hover:bg-accent/30 focus-ring"
                        >
                          {copiedSection === "claude-cli"
                            ? "Copied"
                            : "Copy command"}
                        </button>
                      </div>
                    )}
                    <p className="text-slate-500">
                      Alternatively, add an HTTP server with the same URL and{" "}
                      <code className="text-slate-400">X-API-Key</code> header
                      via your project{" "}
                      <code className="text-slate-400">.mcp.json</code> or
                      Claude Code settings—see{" "}
                      <a
                        href="https://code.claude.com/docs/en/mcp"
                        className="text-accent hover:underline"
                        target="_blank"
                        rel="noreferrer"
                      >
                        Claude Code MCP docs
                      </a>
                      .
                    </p>
                  </div>
                </details>
                <details className={detailsClass}>
                  <summary className="px-3 py-2.5 text-sm font-medium text-slate-200 hover:bg-slate-900/60 rounded-lg">
                    OpenAI Codex (CLI / IDE)
                  </summary>
                  <div className="px-3 pb-3 pt-0 space-y-2 text-xs text-slate-400 leading-relaxed border-t border-slate-800/80 mt-0 pt-3">
                    <p>
                      Append this block to{" "}
                      <code className="text-slate-300">
                        ~/.codex/config.toml
                      </code>{" "}
                      or to{" "}
                      <code className="text-slate-300">.codex/config.toml</code>{" "}
                      inside a trusted project. You can also run{" "}
                      <code className="text-slate-400">codex mcp</code> for an
                      interactive setup.
                    </p>
                    {codexTomlText && (
                      <div className="rounded-lg bg-slate-950 border border-slate-800 p-3">
                        <pre className="text-xs text-slate-300 overflow-x-auto font-mono whitespace-pre-wrap break-all max-h-48 overflow-y-auto">
                          {codexTomlText}
                        </pre>
                        <button
                          type="button"
                          onClick={() =>
                            void copyToClipboard(codexTomlText, "codex")
                          }
                          className="mt-2 px-3 py-1.5 rounded-md bg-accent/20 text-accent text-xs font-medium hover:bg-accent/30 focus-ring"
                        >
                          {copiedSection === "codex" ? "Copied" : "Copy TOML"}
                        </button>
                      </div>
                    )}
                    <p className="text-slate-500">
                      Reference:{" "}
                      <a
                        href="https://developers.openai.com/codex/mcp"
                        className="text-accent hover:underline"
                        target="_blank"
                        rel="noreferrer"
                      >
                        Codex MCP
                      </a>
                      .
                    </p>
                  </div>
                </details>
                <details className={detailsClass}>
                  <summary className="px-3 py-2.5 text-sm font-medium text-slate-200 hover:bg-slate-900/60 rounded-lg">
                    Windsurf
                  </summary>
                  <div className="px-3 pb-3 pt-0 space-y-2 text-xs text-slate-400 leading-relaxed border-t border-slate-800/80 mt-0 pt-3">
                    <p>
                      Command Palette →{" "}
                      <span className="text-slate-300 font-medium">
                        Windsurf: Configure MCP Servers
                      </span>
                      . Merge the{" "}
                      <code className="text-slate-300">mcp-proxy</code> entry
                      from the JSON below into{" "}
                      <code className="text-slate-300">mcpServers</code>.
                    </p>
                    <p className="text-slate-500">
                      Typical path:{" "}
                      <code className="text-slate-400">
                        ~/.codeium/windsurf/mcp_config.json
                      </code>{" "}
                      (macOS/Linux) or{" "}
                      <code className="text-slate-400">
                        %USERPROFILE%\.codeium\windsurf\mcp_config.json
                      </code>{" "}
                      (Windows).
                    </p>
                  </div>
                </details>
              </div>
              <div className="space-y-2 pt-1">
                <h3 className="text-white font-medium text-sm">
                  Shared JSON (Cursor, Claude Desktop, Windsurf, …)
                </h3>
                <p className="text-slate-500 text-xs leading-relaxed">
                  For clients that expect streamable HTTP with{" "}
                  <code className="text-slate-400">url</code> +{" "}
                  <code className="text-slate-400">headers</code>. Merge into
                  existing <code className="text-slate-400">mcpServers</code>.
                  Not for Gemini CLI (
                  <code className="text-slate-400">httpUrl</code> snippet above)
                  or ChatGPT / Amazon Q (configure in their app UI). Set{" "}
                  <code className="text-slate-400">X-API-Key</code> to your key
                  above, or regenerate if you don&apos;t have it saved.
                </p>
                <div className="relative rounded-lg bg-slate-950 border border-slate-800 p-3 pr-2">
                  <pre className="text-xs text-slate-300 overflow-x-auto font-mono whitespace-pre-wrap break-all max-h-64 overflow-y-auto">
                    {cursorConfigText}
                  </pre>
                  <button
                    type="button"
                    onClick={() =>
                      void copyToClipboard(cursorConfigText, "json")
                    }
                    className="mt-2 px-3 py-1.5 rounded-md bg-accent/20 text-accent text-xs font-medium hover:bg-accent/30 focus-ring"
                  >
                    {copiedSection === "json" ? "Copied" : "Copy JSON"}
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
