"""Built-in MCP resources under mcp_proxy://help/* — loaded from help/catalog.yaml."""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml
from users.schemas import UserInDB

logger = logging.getLogger(__name__)

HELP_URI_PREFIX = "mcp_proxy://help"
_CATALOG_PATH = Path(__file__).resolve().parent / "help" / "catalog.yaml"
_N8N_AUTHORING_PROMPT = "mcp_proxy_n8n_workflow_assistant"
_N8N_AUTHORING_RULES_PROMPT = "mcp_proxy_n8n_workflow_rules"
_INVESTIGATION_PROMPT = "mcp_proxy_investigation_assistant"
_INVESTIGATION_RULES_PROMPT = "mcp_proxy_investigation_rules"
_INVESTIGATION_DOMAIN_KEYS = ("guesty", "zendesk", "breezeway", "sql", "firestore")

# Defaults if catalog.yaml omits resource_templates (MCP resources/templates/list).
# Appended to template descriptions so clients see permission scope without opening the index.
_RESOURCE_TEMPLATE_PERM_NOTE = (
    " Same visibility as tools/list (Firestore server_id permissions); reads fail when you lack access."
)

_DEFAULT_RESOURCE_TEMPLATES: List[Dict[str, Any]] = [
    {
        "uriTemplate": f"{HELP_URI_PREFIX}/tool/{{tool_name}}",
        "name": "help-tool",
        "description": "Per-tool discovery: substitute proxied tool name (e.g. guesty_find_listing).",
        "mimeType": "text/markdown",
    },
    {
        "uriTemplate": f"{HELP_URI_PREFIX}/by-task/{{slug}}",
        "name": "help-by-task",
        "description": "Task help: slug matches a catalog task id or journey id (hyphens ok).",
        "mimeType": "text/markdown",
    },
    {
        "uriTemplate": f"{HELP_URI_PREFIX}/workflow/{{domain}}/{{goal}}",
        "name": "help-workflow",
        "description": "Ordered tool sequence for domain + goal (see capabilities.json workflows).",
        "mimeType": "text/markdown",
    },
    {
        "uriTemplate": f"{HELP_URI_PREFIX}/by-domain/{{domain}}",
        "name": "help-by-domain",
        "description": "Same as domain guide; domain is a catalog key (guesty, sql, n8n, …).",
        "mimeType": "text/markdown",
    },
    {
        "uriTemplate": f"{HELP_URI_PREFIX}/{{domain}}",
        "name": "help-domain-direct",
        "description": "Domain guide URI (catalog domain key). Not for index/capabilities — use static resources for those.",
        "mimeType": "text/markdown",
    },
]

_LOCAL_PROMPTS: List[Dict[str, Any]] = [
    {
        "name": _N8N_AUTHORING_PROMPT,
        "description": (
            "Read before calling `n8n_*` workflow-authoring tools. Mirrors the Host Wise n8n workflow "
            "guardrails as MCP-native prompt guidance."
        ),
        "arguments": [],
        "domains": ["n8n"],
        "variant": "full",
    },
    {
        "name": _N8N_AUTHORING_RULES_PROMPT,
        "description": ("Short rules-only version of the Host Wise n8n workflow authoring guardrails for MCP clients."),
        "arguments": [],
        "domains": ["n8n"],
        "variant": "rules",
    },
    {
        "name": _INVESTIGATION_PROMPT,
        "description": (
            "Read before investigating a vague business or operational question across Host Wise MCP tools. "
            "Mirrors the Host Wise investigation skill as MCP-native prompt guidance."
        ),
        "arguments": [],
        "any_domains": list(_INVESTIGATION_DOMAIN_KEYS),
        "variant": "full",
    },
    {
        "name": _INVESTIGATION_RULES_PROMPT,
        "description": "Short rules-only version of the Host Wise investigation guidance for MCP clients.",
        "arguments": [],
        "any_domains": list(_INVESTIGATION_DOMAIN_KEYS),
        "variant": "rules",
    },
]


@lru_cache(maxsize=1)
def _load_catalog_raw() -> Dict[str, Any]:
    if not _CATALOG_PATH.is_file():
        logger.error("Help catalog missing at %s", _CATALOG_PATH)
        return {}
    with _CATALOG_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def clear_catalog_cache() -> None:
    """Test hook."""
    _load_catalog_raw.cache_clear()


def _norm_segment(s: str) -> str:
    return (s or "").strip().lower().replace("_", "-")


def user_sees_all_domains(user: Optional[UserInDB]) -> bool:
    return bool(user and user.status == "active" and user.role in ("admin", "power_user"))


def _allowed_server_set(user: Optional[UserInDB], allowed_server_ids: List[str]) -> Set[str]:
    return set(allowed_server_ids or [])


def _domain_visible(
    domain_meta: Dict[str, Any],
    *,
    user: Optional[UserInDB],
    allowed_server_ids: List[str],
) -> bool:
    if user_sees_all_domains(user):
        return True
    ids = domain_meta.get("server_ids") or []
    if not ids:
        return True
    allow = _allowed_server_set(user, allowed_server_ids)
    return bool(allow.intersection(set(ids)))


def _domain_key_for_tool(tool_name: str, domains: Dict[str, Any]) -> Optional[str]:
    prefix = tool_name.split("_", 1)[0] if tool_name else ""
    if not prefix:
        return None
    # sql_gateway -> sql domain key
    if prefix == "sql" and "sql" in domains:
        return "sql"
    if prefix == "firestore" and "firestore" in domains:
        return "firestore"
    for key, meta in domains.items():
        sids = meta.get("server_ids") or []
        norm_ids = {_norm_segment(x).replace("-", "_") for x in sids}
        p = prefix.replace("-", "_")
        if p in norm_ids or _norm_segment(key).replace("-", "_") == p:
            return key
    # Heuristic: domain key matches first segment (guesty_find -> guesty not in server_ids as guesty)
    if prefix.replace("-", "_") == _norm_segment(prefix):
        cand = prefix.lower()
        if cand in domains:
            return cand
    return None


def _tool_help_merged(cat: Dict[str, Any], tool_name: str) -> Dict[str, Any]:
    """Merge one-line ``tool_blurbs`` with optional ``tool_help_rich`` overlay."""
    blurbs = cat.get("tool_blurbs") or {}
    rich = cat.get("tool_help_rich") or {}
    out: Dict[str, Any] = {}
    b = blurbs.get(tool_name)
    if isinstance(b, str):
        out["summary"] = b
    elif isinstance(b, dict):
        out = dict(b)
        if "blurb" in out and "summary" not in out:
            out["summary"] = out.pop("blurb")
    r = rich.get(tool_name)
    if isinstance(r, dict):
        out = {**out, **r}
    return out


def _append_authoring_style(lines: List[str], value: Any) -> None:
    """Render optional ## Authoring style from a list of bullets or a markdown string."""
    if isinstance(value, list) and value:
        lines.extend(["## Authoring style", ""])
        for item in value:
            lines.append(f"- {item}")
        lines.append("")
    elif isinstance(value, str) and value.strip():
        lines.extend(["## Authoring style", "", value.strip(), ""])


def _append_routing_sections(lines: List[str], meta: Dict[str, Any], *, include_summary: bool = True) -> None:
    """Append markdown sections from merged tool help (required inputs, failures, empty result, next steps)."""
    if include_summary:
        summary = meta.get("summary") or meta.get("blurb")
        if summary:
            lines.append(str(summary))
            lines.append("")
    _append_authoring_style(lines, meta.get("authoring_style"))
    ri = meta.get("required_inputs")
    if isinstance(ri, list) and ri:
        lines.extend(["## Required inputs", ""])
        for item in ri:
            lines.append(f"- {item}")
        lines.append("")
    fm = meta.get("failure_modes")
    if isinstance(fm, list) and fm:
        lines.extend(["## Common failure modes", ""])
        for item in fm:
            lines.append(f"- {item}")
        lines.append("")
    ie = meta.get("if_empty")
    if ie:
        lines.extend(["## If the result is empty", "", str(ie), ""])
    ns = meta.get("next_steps")
    if isinstance(ns, list) and ns:
        lines.extend(["## Typical next steps", ""])
        for item in ns:
            lines.append(f"- {item}")
        lines.append("")
    dd = meta.get("deep_dive")
    if isinstance(dd, str) and dd.strip():
        lines.extend(["## Deep dive", "", dd.strip(), ""])
    cookbooks = meta.get("cookbooks")
    if isinstance(cookbooks, list) and cookbooks:
        lines.extend(["## Cookbook examples", ""])
        for cb in cookbooks:
            if not isinstance(cb, dict):
                continue
            title = cb.get("title") or "Example"
            body = cb.get("body")
            lines.append(f"### {title}")
            lines.append("")
            if isinstance(body, str) and body.strip():
                lines.append(body.strip())
                lines.append("")


def format_tool_help_page(
    tool_name: str,
    cat: Dict[str, Any],
    *,
    domains: Dict[str, Any],
    domain_keys_visible: Set[str],
) -> str:
    """Full markdown for ``mcp_proxy://help/tool/{tool_name}``."""
    meta = _tool_help_merged(cat, tool_name)
    lines: List[str] = [f"# Tool: `{tool_name}`", ""]
    if not meta:
        lines.append("No extended help in catalog; see `tools/list` for the full schema and description.")
    else:
        _append_routing_sections(lines, meta, include_summary=True)
    # Intent-based routing (prefer / fallback)
    intents = cat.get("intents") or []
    primary_intents = [i for i in intents if i.get("recommended_tool") == tool_name]
    if primary_intents:
        fb_all: List[str] = []
        rid_all: List[str] = []
        for intent in primary_intents:
            fb_all.extend(intent.get("fallback_tools") or [])
            rid_all.extend(intent.get("required_identifiers") or [])
        if fb_all:
            fb_u = list(dict.fromkeys(fb_all))
            lines.extend(
                [
                    "## Intent fallbacks",
                    "",
                    "If this tool is not enough, try: " + ", ".join(f"`{x}`" for x in fb_u),
                    "",
                ]
            )
        if rid_all:
            rid_u = list(dict.fromkeys(rid_all))
            lines.extend(["## Identifiers these intents expect", ""])
            for x in rid_u:
                lines.append(f"- `{x}`")
            lines.append("")
    for intent in intents:
        if tool_name in (intent.get("fallback_tools") or []):
            rec = intent.get("recommended_tool")
            if rec:
                lines.extend(
                    [
                        "## Prefer first",
                        "",
                        f"This tool is a fallback; when appropriate start with `{rec}`.",
                        "",
                    ]
                )
                break
    lines.extend(
        [
            "## Permissions",
            "",
            "This page is only available when your account may use this integration (same **`server_id`** allow list "
            "as **`tools/list`**). Individual tools may still require **write** permission if they mutate data.",
            "",
        ]
    )
    dom_guess = _domain_key_for_tool(tool_name, domains)
    if dom_guess and dom_guess in domain_keys_visible:
        lines.extend(["## Domain guide", "", f"`mcp_proxy://help/{dom_guess}`", ""])
    return "\n".join(lines).rstrip() + "\n"


def _tools_from_example(ex: Dict[str, Any]) -> List[str]:
    if ex.get("tool"):
        return [str(ex["tool"])]
    t = ex.get("tools")
    if isinstance(t, list):
        return [str(x) for x in t]
    return []


def _example_domain_keys(ex: Dict[str, Any], domains: Dict[str, Any]) -> Set[str]:
    keys: Set[str] = set()
    for tool in _tools_from_example(ex):
        d = _domain_key_for_tool(tool, domains)
        if d:
            keys.add(d)
    return keys


def _example_allowed_for_user(ex: Dict[str, Any], domains: Dict[str, Any], allowed_domain_keys: Set[str]) -> bool:
    need = _example_domain_keys(ex, domains)
    if not need:
        return True
    return need.issubset(allowed_domain_keys)


def help_path_from_uri(uri: str) -> Optional[str]:
    if not uri.startswith(f"{HELP_URI_PREFIX}/"):
        if uri == HELP_URI_PREFIX:
            return "index"
        return None
    return uri[len(HELP_URI_PREFIX) + 1 :]


def render_index_markdown(
    cat: Dict[str, Any],
    *,
    allowed_domain_keys: Set[str],
    user: Optional[UserInDB],
    allowed_server_ids: List[str],
) -> str:
    lines: List[str] = [
        "# MCP proxy help index",
        "",
        "Start with **`mcp_proxy://help/index`** (this page) and **`mcp_proxy://help/capabilities.json`** for machine-readable routing.",
        "",
    ]
    domains = cat.get("domains") or {}
    all_domain_keys = set(domains.keys())
    hidden_keys = sorted(all_domain_keys - allowed_domain_keys)
    lines.extend(
        [
            "## Permissions & discoverability",
            "",
            "**Help tracks the same access as tools:** only MCP upstreams you are allowed to use (Firestore "
            "permissions: read or write per `server_id`) appear in **`tools/list`** and in the domain sections below. "
            "If you open `mcp_proxy://help/tool/...` or a domain guide for an integration you do not have, "
            "**`resources/read` returns an error** — that is expected.",
            "",
        ]
    )
    lines.extend(
        [
            "## Continuous improvement",
            "",
            "If a proxied tool repeatedly fails, is missing a needed field, or an upstream should expose more tools, "
            "call **`mcp_proxy_request_improvement`** from `tools/list`.",
            "",
            "Agent guidance:",
            "- Always include at least one affected `tool_names` entry and/or `server_ids` entry.",
            "- When the request comes from a failure, include `error_context` with the failing tool name and latest error.",
            "- The request queue is rate limited and reviewed in the proxy Approvals UI. Approved requests stay on record; declined requests are deleted.",
            "",
        ]
    )
    lines.extend(
        [
            "## Shared skill catalog",
            "",
            "The proxy also publishes installable Codex skills.",
            "",
            "- Use `mcp_proxy_list_skills` to browse the catalog.",
            "- Use `mcp_proxy_install_skill` to get the tar.gz bundle URL plus safe install commands for `~/.codex/skills/<skill-name>`.",
            "- If a shared skill should change for everyone, submit `mcp_proxy_request_skill_update`.",
            "",
        ]
    )
    if user_sees_all_domains(user):
        lines.extend(
            [
                "**Your role:** admin or power_user — you may call tools on **all enabled** upstreams "
                "(subject to each server’s OAuth / headers). This index lists every catalog domain.",
                "",
            ]
        )
    else:
        sid = ", ".join(f"`{x}`" for x in sorted(set(allowed_server_ids or [])))
        lines.append(
            f"**Your allowed upstream `server_id`s (from permissions):** {sid or '*(none — you will see no tools)*'}."
        )
        lines.append("")
        if hidden_keys:
            lines.append(
                "**Catalog domains you do not have permission for** (omit these in plans; they are not in `tools/list`):"
            )
            lines.append("")
            for key in hidden_keys:
                meta = domains.get(key) or {}
                title = meta.get("title", key)
                sids = ", ".join(f"`{x}`" for x in (meta.get("server_ids") or []))
                lines.append(f"- **{title}** (`{key}`) — grant access to server id(s): {sids or '*(see ops)*'}")
            lines.append("")
    lines.extend(["## Verb ladder", ""])
    glossary = cat.get("verb_glossary") or {}
    for k, v in glossary.items():
        lines.append(f"- **{k}** — {v}")
    lines.extend(["", "## Domains you can use", ""])
    for key in sorted(domains.keys()):
        if key not in allowed_domain_keys:
            continue
        meta = domains[key]
        title = meta.get("title", key)
        desc = meta.get("description", "")
        lines.append(f"- **{title}** (`mcp_proxy://help/{key}`) — {desc}")
    if not allowed_domain_keys:
        lines.append("- *(none — request access to an MCP upstream in the proxy admin UI.)*")
    ex_available: List[Dict[str, Any]] = []
    ex_other: List[Dict[str, Any]] = []
    for ex in cat.get("examples") or []:
        if _example_allowed_for_user(ex, domains, allowed_domain_keys):
            ex_available.append(ex)
        else:
            ex_other.append(ex)
    lines.extend(["", "## If you want X, use Y (integrations you can use)", ""])
    if ex_available:
        for ex in ex_available:
            want = ex.get("want", "")
            if ex.get("tool"):
                lines.append(f"- **{want}** → `{ex['tool']}`" + (f" — _{ex['note']}_" if ex.get("note") else ""))
            elif ex.get("tools"):
                chain = " → ".join(f"`{t}`" for t in ex["tools"])
                lines.append(f"- **{want}** → {chain}" + (f" — _{ex['note']}_" if ex.get("note") else ""))
    else:
        lines.append("- *(no examples fully covered by your current permissions)*")
    if ex_other and not user_sees_all_domains(user):
        lines.extend(
            [
                "",
                "## Examples for other integrations (you likely lack `tools/list` access)",
                "",
                "_These chains reference upstreams outside your permissions. Following them will fail until an admin "
                "grants the corresponding `server_id`._",
                "",
            ]
        )
        for ex in ex_other:
            want = ex.get("want", "")
            doms = sorted(_example_domain_keys(ex, domains))
            dom_note = f" — _needs domain(s): {', '.join(doms)}_" if doms else ""
            if ex.get("tool"):
                lines.append(
                    f"- **{want}** → `{ex['tool']}`{dom_note}" + (f" — _{ex['note']}_" if ex.get("note") else "")
                )
            elif ex.get("tools"):
                chain = " → ".join(f"`{t}`" for t in ex["tools"])
                lines.append(f"- **{want}** → {chain}{dom_note}" + (f" — _{ex['note']}_" if ex.get("note") else ""))
    elif ex_other:
        lines.extend(["", "## More examples (other integrations)", ""])
        for ex in ex_other:
            want = ex.get("want", "")
            if ex.get("tool"):
                lines.append(f"- **{want}** → `{ex['tool']}`" + (f" — _{ex['note']}_" if ex.get("note") else ""))
            elif ex.get("tools"):
                chain = " → ".join(f"`{t}`" for t in ex["tools"])
                lines.append(f"- **{want}** → {chain}" + (f" — _{ex['note']}_" if ex.get("note") else ""))
    lines.extend(["", "## Journeys (workflow-first)", ""])
    for j in cat.get("journeys") or []:
        jid = j.get("id", "")
        title = j.get("title", "")
        doms = j.get("domains") or []
        if doms and not set(doms).issubset(allowed_domain_keys):
            continue
        lines.append(f"- **{title}** (`mcp_proxy://help/by-task/{_norm_segment(jid)}`)")
    lines.extend(
        [
            "",
            "## More URIs",
            "",
            "- `mcp_proxy://help/capabilities.json` — full intent / domain map (JSON).",
            "- **Parameterized discovery:** call MCP method `resources/templates/list` — returns `uriTemplate` patterns "
            "(e.g. `mcp_proxy://help/tool/{tool_name}`) so clients can build URIs at runtime.",
            "",
            "Examples (substitute path segments):",
            "- `mcp_proxy://help/by-task/{slug}` — task or journey id.",
            "- `mcp_proxy://help/by-domain/{domain}` — domain guide.",
            "- `mcp_proxy://help/workflow/{domain}/{goal}` — ordered steps.",
            "- `mcp_proxy://help/tool/{prefixed_tool_name}` — tool blurb and fallbacks.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_domain_markdown(domain_key: str, meta: Dict[str, Any]) -> str:
    title = meta.get("title", domain_key)
    lines = [f"# {title} (`{domain_key}`)", "", meta.get("description", ""), "", "## Start here", ""]
    for label, tool in (meta.get("start_here") or {}).items():
        lines.append(f"- **{label}**: `{tool}` — deep help: `mcp_proxy://help/tool/{tool}`")
    lines.extend(
        [
            "",
            "For each tool above, open the `mcp_proxy://help/tool/...` URI for **required inputs**, "
            "**failure modes**, and **what to do if results are empty**.",
            "",
            "**Permissions:** If your user loses access to this `server_id`, these tools disappear from `tools/list` "
            "and help reads for this domain will fail.",
        ]
    )
    _append_authoring_style(lines, meta.get("authoring_style"))
    mistakes = meta.get("common_mistakes") or []
    if mistakes:
        lines.extend(["", "## Common mistakes", ""])
        for m in mistakes:
            lines.append(f"- {m}")
    if meta.get("escalation"):
        lines.extend(["", "## Escalation", "", meta["escalation"]])
    nt = meta.get("naming_tags")
    if isinstance(nt, str) and nt.strip():
        lines.extend(["", "## Naming and tags", "", nt.strip()])
    aliases = meta.get("aliases") or []
    if aliases:
        lines.extend(["", "## Prefer / avoid", ""])
        for a in aliases:
            lines.append(f"- Prefer `{a.get('prefer')}` over `{a.get('over')}` when: {a.get('when', '')}".strip())
    return "\n".join(lines) + "\n"


def build_capabilities_json(
    cat: Dict[str, Any],
    *,
    allowed_domain_keys: Set[str],
    user: Optional[UserInDB],
    allowed_server_ids: List[str],
) -> str:
    domains = cat.get("domains") or {}
    all_keys = set(domains.keys())
    filtered_domains = {k: v for k, v in domains.items() if k in allowed_domain_keys}
    tasks_obj = cat.get("tasks") or {}
    task_list: List[Dict[str, Any]] = []
    if isinstance(tasks_obj, dict):
        task_list = [{"id": k, **v} for k, v in tasks_obj.items() if (v.get("domain") or "") in allowed_domain_keys]
    journeys_all = cat.get("journeys") or []
    journeys_f: List[Dict[str, Any]] = []
    for j in journeys_all:
        if not isinstance(j, dict):
            continue
        doms = j.get("domains") or []
        if doms and set(doms).issubset(allowed_domain_keys):
            journeys_f.append(j)
    access: Dict[str, Any] = {
        "help_matches_tools_list": True,
        "note": "Domains and intents in this JSON are filtered to match tools you may call; "
        "request missing server_id access if you need more.",
        "allowed_server_ids": sorted(set(allowed_server_ids or [])),
        "visible_domain_keys": sorted(allowed_domain_keys),
        "hidden_domain_keys": sorted(all_keys - allowed_domain_keys),
    }
    if user:
        access["user_role"] = user.role
        access["full_catalog_help"] = user_sees_all_domains(user)
    out = {
        "access": access,
        "proxy_features": {
            "improvement_request_tool": "mcp_proxy_request_improvement",
            "skill_catalog_tools": {
                "list": "mcp_proxy_list_skills",
                "install": "mcp_proxy_install_skill",
                "request_update": "mcp_proxy_request_skill_update",
            },
            "agent_guidance": [
                "If you hit repeated errors or missing functionality, submit mcp_proxy_request_improvement.",
                "Each request must name affected tool_names and/or server_ids.",
                "Include error_context when the request is motivated by a recent tool failure.",
                "Requests are Firestore-backed, rate limited, reviewed in the Approvals UI, approved requests are kept, and declined requests are deleted.",
                "Use mcp_proxy_list_skills to browse published Codex skills before installing one.",
                "Use mcp_proxy_install_skill to get the tar.gz bundle URL and local install commands for ~/.codex/skills/<skill-name>.",
                "If a shared skill should change for everyone, submit mcp_proxy_request_skill_update with the exact skill_name.",
            ],
        },
        "verb_glossary": cat.get("verb_glossary") or {},
        "domains": filtered_domains,
        "intents": [i for i in (cat.get("intents") or []) if i.get("domain") in allowed_domain_keys],
        "tasks": task_list,
        "journeys": journeys_f,
        "workflows": [w for w in (cat.get("workflows") or []) if (w.get("domain") or "") in allowed_domain_keys],
    }
    workflow_guidance: Dict[str, Any] = {}
    if "n8n" in allowed_domain_keys:
        workflow_guidance["n8n"] = {
            "tool_prefixes": ["n8n_"],
            "read_resources_first": [
                "mcp_proxy://help/n8n",
                "mcp_proxy://help/by-task/n8n-create-workflow",
                "mcp_proxy://help/tool/{tool_name}",
            ],
            "prompts": [_N8N_AUTHORING_PROMPT, _N8N_AUTHORING_RULES_PROMPT],
            "notes": [
                "Treat these prompts/resources as the canonical authoring guidance when using n8n tools through MCP.",
                "A local Codex skill may mirror the same rules, but clients should not depend on local skill installation before using n8n tools.",
            ],
        }
    if workflow_guidance:
        out["proxy_features"]["workflow_guidance"] = workflow_guidance
    return json.dumps(out, indent=2) + "\n"


def _visible_domain_keys(cat: Dict[str, Any], *, user: Optional[UserInDB], allowed_server_ids: List[str]) -> Set[str]:
    domains = cat.get("domains") or {}
    out: Set[str] = set()
    for key, meta in domains.items():
        if _domain_visible(meta, user=user, allowed_server_ids=allowed_server_ids):
            out.add(key)
    return out


def list_help_resource_descriptors(
    *,
    user: Optional[UserInDB],
    allowed_server_ids: List[str],
) -> List[Dict[str, Any]]:
    """Static resources only: index, capabilities, per-domain guides.

    Parameterized help (per-tool, tasks, workflows) is advertised via ``resources/templates/list``.
    """
    cat = _load_catalog_raw()
    if not cat:
        return []
    keys = _visible_domain_keys(cat, user=user, allowed_server_ids=allowed_server_ids)
    resources: List[Dict[str, Any]] = [
        {
            "uri": f"{HELP_URI_PREFIX}/index",
            "name": "help-index",
            "description": (
                "MCP proxy discovery (domains, verbs, examples, journeys). "
                "Scoped to your server_id permissions — same allow list as tools/list."
            ),
            "mimeType": "text/markdown",
        },
        {
            "uri": f"{HELP_URI_PREFIX}/capabilities.json",
            "name": "help-capabilities-json",
            "description": (
                "Machine-readable domain / intent / workflow map; JSON is filtered to match tools you may call."
            ),
            "mimeType": "application/json",
        },
    ]
    domains = cat.get("domains") or {}
    for key in sorted(keys):
        meta = domains.get(key) or {}
        resources.append(
            {
                "uri": f"{HELP_URI_PREFIX}/{key}",
                "name": f"help-domain-{key}",
                "description": str(meta.get("description", ""))[:200],
                "mimeType": "text/markdown",
            }
        )
    return resources


def list_help_resource_templates(
    *,
    user: Optional[UserInDB],
    allowed_server_ids: List[str],
) -> List[Dict[str, Any]]:
    """URI templates (RFC 6570-style ``{param}``) for runtime discovery."""
    cat = _load_catalog_raw()
    raw = cat.get("resource_templates") if cat else None
    if isinstance(raw, list) and raw:
        templates: List[Dict[str, Any]] = [dict(x) for x in raw if isinstance(x, dict)]
    else:
        templates = [dict(x) for x in _DEFAULT_RESOURCE_TEMPLATES]
    # Drop domain-direct template if user has no visible domains (still has tool/task/workflow templates).
    keys = _visible_domain_keys(cat or {}, user=user, allowed_server_ids=allowed_server_ids)
    if not keys and not user_sees_all_domains(user):
        templates = [t for t in templates if t.get("name") != "help-domain-direct"]
    for t in templates:
        desc = t.get("description")
        if isinstance(desc, str) and _RESOURCE_TEMPLATE_PERM_NOTE.strip() not in desc:
            t["description"] = desc.rstrip() + _RESOURCE_TEMPLATE_PERM_NOTE
    return templates


def list_proxy_prompt_descriptors(
    *,
    user: Optional[UserInDB],
    allowed_server_ids: List[str],
) -> List[Dict[str, Any]]:
    """Built-in MCP prompts that encode proxy guidance without requiring client-local skills."""
    cat = _load_catalog_raw()
    if not cat:
        return []
    keys = _visible_domain_keys(cat, user=user, allowed_server_ids=allowed_server_ids)
    prompts: List[Dict[str, Any]] = []
    for meta in _LOCAL_PROMPTS:
        domains = set(meta.get("domains") or [])
        if domains and not domains.issubset(keys):
            continue
        any_domains = set(meta.get("any_domains") or [])
        if any_domains and not any_domains.intersection(keys):
            continue
        prompt = {k: v for k, v in meta.items() if k not in {"domains", "any_domains", "variant"}}
        prompts.append(prompt)
    return prompts


def _prompt_result(name: str, description: str, text: str) -> Dict[str, Any]:
    return {
        "description": description,
        "messages": [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": text,
                },
            }
        ],
    }


def _render_n8n_authoring_prompt(cat: Dict[str, Any], *, concise: bool) -> str:
    domains = cat.get("domains") or {}
    n8n = domains.get("n8n") or {}
    style = [str(x) for x in (n8n.get("authoring_style") or []) if str(x).strip()]
    if concise:
        style = style[:6]
    journey_steps: List[str] = []
    for journey in cat.get("journeys") or []:
        if _norm_segment(str(journey.get("id", ""))) == "n8n-create-workflow":
            journey_steps = [str(x) for x in (journey.get("steps") or []) if str(x).strip()]
            break
    create_meta = _tool_help_merged(cat, "n8n_create_workflow")
    update_meta = _tool_help_merged(cat, "n8n_update_workflow")
    lines = [
        "Use this prompt whenever you are about to call `n8n_*` workflow tools through the MCP proxy.",
        "",
        "Treat these MCP-native instructions as the canonical Host Wise guidance for n8n workflow authoring. "
        "An optional local Codex skill may mirror the same rules, but clients must not depend on local skill installation.",
        "",
        "Read first:",
        "- `mcp_proxy://help/n8n`",
        "- `mcp_proxy://help/by-task/n8n-create-workflow`",
        "- `mcp_proxy://help/tool/n8n_create_workflow` and `mcp_proxy://help/tool/n8n_update_workflow` before writes.",
        "",
        "Workflow discipline:",
    ]
    for item in style:
        lines.append(f"- {item}")
    if journey_steps:
        lines.extend(["", "Canonical sequence:"])
        for idx, step in enumerate(journey_steps, 1):
            lines.append(f"{idx}. {step}")
    required_inputs = create_meta.get("required_inputs") or []
    failure_modes = update_meta.get("failure_modes") or []
    if not concise and required_inputs:
        lines.extend(["", "Create workflow minimum inputs:"])
        for item in required_inputs:
            lines.append(f"- {item}")
    if not concise and failure_modes:
        lines.extend(["", "Update workflow failure checks:"])
        for item in failure_modes:
            lines.append(f"- {item}")
    lines.extend(
        [
            "",
            "If a tool description or workflow state is unclear, open the corresponding `mcp_proxy://help/tool/...` page before proceeding.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _journey_steps(cat: Dict[str, Any], journey_id: str) -> List[str]:
    for journey in cat.get("journeys") or []:
        if _norm_segment(str(journey.get("id", ""))) == _norm_segment(journey_id):
            return [str(x) for x in (journey.get("steps") or []) if str(x).strip()]
    return []


def _render_investigation_prompt(cat: Dict[str, Any], *, visible_domain_keys: Set[str], concise: bool) -> str:
    visible_domains = [key for key in _INVESTIGATION_DOMAIN_KEYS if key in visible_domain_keys]
    complaint_ready = {"zendesk", "guesty", "breezeway"}.issubset(visible_domain_keys)
    vacancy_ready = {"guesty", "sql", "breezeway"}.issubset(visible_domain_keys)
    complaint_steps = _journey_steps(cat, "ops-guest-complaint-triage")
    vacancy_steps = _journey_steps(cat, "vacancy-diagnosis")

    lines = [
        "Use this prompt whenever you are investigating a vague business or operational question through the MCP proxy.",
        "",
        "Treat these MCP-native instructions as the canonical Host Wise investigation guidance. "
        "An optional local Codex skill may mirror the same rules, but clients must not depend on local skill installation.",
        "",
        "Read first:",
        "- `mcp_proxy://help/index`",
        "- `mcp_proxy://help/capabilities.json`",
        "- the relevant domain guide such as `mcp_proxy://help/guesty`, `mcp_proxy://help/zendesk`, `mcp_proxy://help/breezeway`, `mcp_proxy://help/sql`, or `mcp_proxy://help/firestore`",
        "- the relevant journey page such as `mcp_proxy://help/by-task/ops-guest-complaint-triage` or `mcp_proxy://help/by-task/vacancy-diagnosis` when the question matches those flows.",
        "",
    ]
    if visible_domains:
        lines.extend(
            [
                "Visible investigation domains right now:",
                "- " + ", ".join(f"`{key}`" for key in visible_domains),
                "",
            ]
        )
    lines.extend(
        [
            "Investigation loop:",
            "1. Rewrite the request into a short brief: question, subject, time window, likely systems, and deliverable.",
            "2. Resolve names into exact ids before broad retrieval.",
            "3. Gather the minimum evidence needed from each system.",
            "4. Separate facts from hypotheses.",
            "5. Return a short answer first, then supporting evidence, then next actions or gaps.",
            "",
            "Translate fuzzy prompts safely:",
            "- `last week` -> trailing 7 days unless the user gives a stricter range.",
            "- `not getting bookings` or `future reservations are low` -> next 60 days compared with the previous 60 days.",
            "- `anything unresolved` -> open, overdue, blocked, pending, flagged, or otherwise unfinished work in the relevant systems.",
            "- Ask at most 2 short clarifying questions only when a safe entity resolution or time window is impossible.",
            "",
            "Tool discipline:",
            "- Prefer resolver tools such as `find_*` before broad search when the user gave human text instead of an exact id.",
            "- Prefer search tools when you already know the filters you need.",
            "- Prefer curated summary tools when you have an exact id and need agent-friendly detail.",
            "- Use raw `get` tools only when summary or search results omit a required field.",
            "- For SQL, never guess table or column names; start with `sql_gateway_get_schema` or `sql_gateway_lookup_table`, then run a tight read-only query.",
            "- For Breezeway task work, use `breezeway_list_tasks` for one resolved property and `breezeway_triage_tasks` only for a true portfolio-wide urgency queue.",
            "",
        ]
    )
    if not concise:
        lines.extend(
            [
                "Output discipline:",
                "- For triage requests, build one row per business object and flag the highest-priority rows.",
                "- For diagnosis requests, rank the top 3 hypotheses by likelihood and give one suggested action per hypothesis.",
                "- State important assumptions briefly.",
                "- Do not dump raw tool output without interpretation.",
                "",
            ]
        )
    if complaint_ready:
        lines.extend(
            [
                "Complaint triage pattern:",
                "- Use `mcp_proxy://help/by-task/ops-guest-complaint-triage` as the primary guide.",
            ]
        )
        if not concise and complaint_steps:
            for idx, step in enumerate(complaint_steps, 1):
                lines.append(f"{idx}. {step}")
        lines.append("")
    if vacancy_ready:
        lines.extend(
            [
                "Vacancy diagnosis pattern:",
                "- Use `mcp_proxy://help/by-task/vacancy-diagnosis` as the primary guide.",
            ]
        )
        if not concise and vacancy_steps:
            for idx, step in enumerate(vacancy_steps, 1):
                lines.append(f"{idx}. {step}")
        lines.append("")
    lines.append(
        "If a tool path or result shape is unclear, open the corresponding `mcp_proxy://help/tool/...` page before proceeding."
    )
    return "\n".join(lines).rstrip() + "\n"


def read_proxy_prompt(
    name: str,
    *,
    user: Optional[UserInDB],
    allowed_server_ids: List[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Return a built-in prompt payload when the user is allowed to see it."""
    cat = _load_catalog_raw()
    if not cat:
        return None, {"code": -32603, "message": "Help catalog unavailable"}
    keys = _visible_domain_keys(cat, user=user, allowed_server_ids=allowed_server_ids)
    visible = {
        prompt["name"]: prompt
        for prompt in list_proxy_prompt_descriptors(user=user, allowed_server_ids=allowed_server_ids)
    }
    if name not in visible:
        return None, {"code": -32602, "message": f"Unknown prompt: {name}"}
    if name == _N8N_AUTHORING_PROMPT and "n8n" in keys:
        prompt = visible[name]
        return (
            _prompt_result(name, str(prompt.get("description", "")), _render_n8n_authoring_prompt(cat, concise=False)),
            None,
        )
    if name == _N8N_AUTHORING_RULES_PROMPT and "n8n" in keys:
        prompt = visible[name]
        return (
            _prompt_result(name, str(prompt.get("description", "")), _render_n8n_authoring_prompt(cat, concise=True)),
            None,
        )
    if name == _INVESTIGATION_PROMPT and set(_INVESTIGATION_DOMAIN_KEYS).intersection(keys):
        prompt = visible[name]
        return (
            _prompt_result(
                name,
                str(prompt.get("description", "")),
                _render_investigation_prompt(cat, visible_domain_keys=keys, concise=False),
            ),
            None,
        )
    if name == _INVESTIGATION_RULES_PROMPT and set(_INVESTIGATION_DOMAIN_KEYS).intersection(keys):
        prompt = visible[name]
        return (
            _prompt_result(
                name,
                str(prompt.get("description", "")),
                _render_investigation_prompt(cat, visible_domain_keys=keys, concise=True),
            ),
            None,
        )
    return None, {"code": -32602, "message": f"Unknown prompt: {name}"}


def read_help_resource(
    uri: str,
    *,
    user: Optional[UserInDB],
    allowed_server_ids: List[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Returns (result_body, error_obj) where error_obj is JSON-RPC error dict."""
    path = help_path_from_uri(uri)
    if path is None:
        return None, {"code": -32602, "message": f"Unknown resource: {uri}"}

    cat = _load_catalog_raw()
    if not cat:
        return None, {"code": -32603, "message": "Help catalog unavailable"}

    keys = _visible_domain_keys(cat, user=user, allowed_server_ids=allowed_server_ids)
    domains = cat.get("domains") or {}

    def ok(text: str, mime: str) -> Tuple[Dict[str, Any], None]:
        return (
            {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": mime,
                        "text": text,
                    }
                ]
            },
            None,
        )

    norm_path = path.strip()
    if norm_path == "index" or norm_path == "":
        return ok(
            render_index_markdown(
                cat,
                allowed_domain_keys=keys,
                user=user,
                allowed_server_ids=allowed_server_ids,
            ),
            "text/markdown",
        )

    if norm_path == "capabilities.json":
        return ok(
            build_capabilities_json(
                cat,
                allowed_domain_keys=keys,
                user=user,
                allowed_server_ids=allowed_server_ids,
            ),
            "application/json",
        )

    # by-domain/{domain}
    if norm_path.startswith("by-domain/"):
        dk = norm_path.split("/", 1)[1]
        if dk not in domains or dk not in keys:
            return None, {"code": -32602, "message": f"Unknown resource: {uri}"}
        return ok(render_domain_markdown(dk, domains[dk]), "text/markdown")

    # by-task/{slug}
    if norm_path.startswith("by-task/"):
        raw_slug = norm_path.split("/", 1)[1]
        tasks = cat.get("tasks") or {}
        journey_md: Optional[str] = None
        task_key: Optional[str] = None
        if isinstance(tasks, dict):
            for candidate in (raw_slug, raw_slug.replace("-", "_"), _norm_segment(raw_slug)):
                if candidate in tasks:
                    task_key = candidate
                    break
        if isinstance(tasks, dict) and task_key is not None:
            tdef = tasks[task_key]
            dom = tdef.get("domain", "")
            if dom not in keys:
                return None, {"code": -32602, "message": f"Unknown resource: {uri}"}
            lines = [
                f"# {tdef.get('title', task_key)}",
                "",
                f"**Domain:** `{dom}`",
                "",
                f"**Primary tool:** `{tdef.get('primary_tool', '')}`",
            ]
            also = tdef.get("also") or []
            if also:
                lines.extend(["", "**Also consider:** " + ", ".join(f"`{x}`" for x in also)])
            ptool: Optional[str] = tdef.get("primary_tool") if isinstance(tdef.get("primary_tool"), str) else None
            if ptool and ptool.strip():
                th = _tool_help_merged(cat, ptool)
                hint_keys = ("required_inputs", "failure_modes", "if_empty", "next_steps")
                if any(th.get(k) for k in hint_keys):
                    lines.extend(["", "---", "", f"### Routing hints (`{ptool}`)", ""])
                    hint_meta = {k: th[k] for k in hint_keys if th.get(k)}
                    _append_routing_sections(lines, hint_meta, include_summary=False)
            more = f"`mcp_proxy://help/{dom}`, `mcp_proxy://help/capabilities.json`"
            if ptool and ptool.strip():
                more += f", `mcp_proxy://help/tool/{ptool}`"
            lines.extend(["", f"**More:** {more}."])
            journey_md = "\n".join(lines) + "\n"
        else:
            for j in cat.get("journeys") or []:
                if _norm_segment(str(j.get("id", ""))) == _norm_segment(raw_slug):
                    doms = set(j.get("domains") or [])
                    if doms and not doms.issubset(keys):
                        return None, {"code": -32602, "message": f"Unknown resource: {uri}"}
                    lines = [f"# {j.get('title', raw_slug)}", ""]
                    for step in j.get("steps") or []:
                        lines.append(f"- {step}")
                    journey_md = "\n".join(lines) + "\n"
                    break
        if journey_md:
            return ok(journey_md, "text/markdown")
        return None, {"code": -32602, "message": f"Unknown resource: {uri}"}

    # workflow/{domain}/{goal}
    if norm_path.startswith("workflow/"):
        parts = norm_path.split("/")
        if len(parts) >= 3:
            dom, goal = parts[1], parts[2]
            if dom not in keys:
                return None, {"code": -32602, "message": f"Unknown resource: {uri}"}
            for w in cat.get("workflows") or []:
                if w.get("domain") == dom and _norm_segment(str(w.get("goal", ""))) == _norm_segment(goal):
                    lines = [f"# Workflow: {dom} / {w.get('goal', goal)}", "", "Recommended sequence:", ""]
                    for i, step in enumerate(w.get("steps") or [], 1):
                        tool = step.get("tool", "")
                        when = step.get("when", "")
                        suffix = ""
                        if tool:
                            suffix = f" — help: `mcp_proxy://help/tool/{tool}`"
                        lines.append(f"{i}. `{tool}`" + (f" — {when}" if when else "") + suffix)
                    lines.extend(
                        [
                            "",
                            "Expand any step with **`resources/read`** on `mcp_proxy://help/tool/<name>` "
                            "for inputs, failures, and empty-result routing.",
                        ]
                    )
                    return ok("\n".join(lines) + "\n", "text/markdown")
        return None, {"code": -32602, "message": f"Unknown resource: {uri}"}

    # tool/{name}
    if norm_path.startswith("tool/"):
        tool_name = norm_path.split("/", 1)[1]
        dom_guess = _domain_key_for_tool(tool_name, domains)
        if dom_guess is not None and dom_guess not in keys:
            return None, {"code": -32602, "message": f"Unknown resource: {uri}"}
        page = format_tool_help_page(tool_name, cat, domains=domains, domain_keys_visible=keys)
        return ok(page, "text/markdown")

    # domain top-level key
    if norm_path in domains:
        if norm_path not in keys:
            return None, {"code": -32602, "message": f"Unknown resource: {uri}"}
        return ok(render_domain_markdown(norm_path, domains[norm_path]), "text/markdown")

    return None, {"code": -32602, "message": f"Unknown resource: {uri}"}
