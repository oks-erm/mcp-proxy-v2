---
name: host-wise-investigation
description: Investigate vague business questions across Host Wise MCP tools and turn them into a short, evidence-based answer. Use when Codex needs to diagnose why something is happening, surface what is unresolved, or build a cross-platform report across Zendesk, Guesty, Breezeway, SQL warehouse, Firestore, or other available MCP integrations. Best for non-technical prompts such as guest complaint triage, property vacancy diagnosis, booking drop investigations, maintenance blocker checks, and other read-first operational questions where the user does not know the right tools or prompt structure.
---

# Host Wise Investigation

Use this skill to bridge the gap between a non-technical business question and a disciplined MCP investigation.
Translate a vague request into a small investigation brief, resolve entities safely, gather only the minimum evidence needed, and answer in business language instead of tool language.

Read [references/investigation-playbook.md](references/investigation-playbook.md) for request shaping, assumptions, evidence rules, and response templates.
Read [references/mcp-routing.md](references/mcp-routing.md) for tool order and the cross-platform demo flows.

## Start Here

- Match the user's language.
- Treat the request as read-only unless the user explicitly asks to change data.
- When the relevant integrations or tool names are unclear, inspect MCP help first:
  - `mcp_proxy://help/index`
  - `mcp_proxy://help/capabilities.json`
  - the relevant domain guides such as `mcp_proxy://help/guesty`, `mcp_proxy://help/zendesk`, `mcp_proxy://help/breezeway`, `mcp_proxy://help/sql`
  - the relevant journey pages such as `mcp_proxy://help/by-task/ops-guest-complaint-triage` and `mcp_proxy://help/by-task/vacancy-diagnosis`
- Prefer the smallest useful investigation. Do not scan an entire system when the user asked about one ticket, reservation, or property.

## Investigation Loop

1. Rewrite the user request into a short internal brief:
   - what the user wants to know
   - which business object is being investigated
   - what time window applies
   - which systems are likely relevant
   - what output shape will help the user most
2. Resolve names into exact ids before broad retrieval:
   - listing/property: `guesty_find_listing`, `breezeway_find_property_by_name_or_external_id`
   - reservation: `guesty_find_reservation`
   - reservation-linked ticket: `zendesk_find_ticket_by_reservation_id`
3. Gather concise evidence from each system:
   - summaries and scoped searches first
   - full/raw detail only when a summary is missing a field you need
4. Separate facts from hypotheses:
   - facts come from tool output
   - hypotheses explain the facts and must be labeled as likely / possible
5. Return a short answer first, then the supporting evidence, then any gaps or next actions.

## Translate Fuzzy User Prompts

- Interpret `last week` as the trailing 7 days unless the user gives a calendar range.
- Interpret `not getting bookings` or `future reservations are low` as:
  - forward look: next 60 days
  - comparison window: previous 60 days
  - state that assumption briefly in the answer
- Interpret `anything unresolved` as open, overdue, blocked, pending, flagged, or otherwise unfinished work in the relevant systems.
- If the user names one property, listing, reservation, or ticket, investigate that object first instead of doing a portfolio scan.
- Ask at most 2 short clarifying questions only when a safe resolution is impossible, such as:
  - the property name cannot be resolved
  - the date window materially changes the answer
  - several records are equally plausible matches

## Tool Selection Rules

- Prefer resolver tools over broad search when the user gave human text instead of an id.
- Prefer search tools when you already know the filters you need.
- Prefer curated summary tools when you have an exact id and need agent-friendly detail.
- Use raw `get` tools only when the summary/search result omits a required field.
- For SQL, never guess table or column names. Start with `sql_gateway_get_schema` or `sql_gateway_lookup_table`, then run a tight read-only query.
- For Breezeway task work:
  - use `breezeway_list_tasks` for one resolved property
  - use `breezeway_triage_tasks` only for a true portfolio-wide urgency queue

## Default Output Shape

- Start with a short direct answer for the user.
- For triage requests, show one row per business object and flag the highest-priority rows.
- For diagnosis requests, rank the top 3 hypotheses by likelihood.
- For each hypothesis, include one suggested next action.
- End with explicit assumptions, missing data, or confidence limits.

## Examples

User request:
`Show me the guest complaints from the last week and if there's anything unresolved in those properties.`

What to do:

- Use the complaint triage flow from [references/mcp-routing.md](references/mcp-routing.md).
- Resolve the reservation and property context per ticket.
- Return one row per complaint with Zendesk, Guesty, and Breezeway evidence side by side.
- Flag rows where an open complaint and an overdue Breezeway task exist together.

User request:
`Why is T0 Maternidade 45 7 not getting bookings?`

What to do:

- Use the vacancy diagnosis flow from [references/mcp-routing.md](references/mcp-routing.md).
- Confirm listing status in Guesty.
- Compare the next 60 days with the previous 60 days.
- Check reviews and Breezeway blockers before forming hypotheses.
- Return the top 3 hypotheses plus one action for each.
