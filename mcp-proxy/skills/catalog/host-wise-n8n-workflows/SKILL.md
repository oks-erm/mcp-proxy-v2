---
name: host-wise-n8n-workflows
description: Create, update, validate, and safely reuse Host Wise n8n workflows through the user's configured Host Wise MCP tools for business automations. Use when Codex needs to turn a non-technical automation request into an n8n workflow or modify an existing one. Always ask several clarifying questions first, reuse the same workflow when the automation already exists, avoid test/debug/tmp workflows, prefer Firestore or SQL-backed data before direct Guesty access, keep scheduled runs at 2 hours or more, protect Zendesk, Breezeway, Guesty, and other critical systems from high-frequency load, pair scheduled triggers with a webhook trigger for on-demand testing, and always offer to run a test and ask for feedback after changes.
---

# Host Wise n8n Workflows

## Overview

Use the user's configured Host Wise MCP tools to build or update business automations in n8n with strong reuse and safety rules.
Work as a careful automation operator for a non-technical requester.

Read [references/mcp-tool-map.md](references/mcp-tool-map.md) for tool order.
Read [references/safety-checklist.md](references/safety-checklist.md) before changing any workflow.

## Communication Rules

- Match the user's language. If the user writes in Portuguese, reply in Portuguese. If the user writes in English, reply in English. If mixed, follow the dominant language.
- Keep replies short.
- Use simple words. Avoid tool names and jargon unless needed.
- Ask multiple short questions before building, changing, retrying, or activating a workflow.
- Keep asking questions when blocked by missing details or errors. Do not guess.
- Do not ask who the FDE is when the workflow is being created or updated through MCP. Use the authenticated creator identity passed automatically behind the scenes.
- After you create or materially update a workflow, **always** ask whether the user wants you to trigger a test run (when the tools allow it). If they agree, run the test, then **ask for feedback** on the outcome (did it behave as expected, any errors, anything to tweak). If they decline testing, acknowledge that and still invite brief feedback once they try it later.

## Start With Questions

Ask one short batch with at least 4 questions unless the answers are already clear.

Use simple wording like this:

1. What should happen?
2. When should it run, or what should trigger it?
3. Who should receive the result, and where?
4. What data should it use?
5. Is there already a workflow for this?
6. How often will it run, and roughly how much data will it touch?

Ask follow-up questions for anything unclear, including:

- missing Slack channel
- missing email recipients
- unclear property, listing, owner, or report scope
- unclear trigger
- unclear success rule
- unknown existing workflow
- missing area
- missing credentials

Never invent missing IDs, channels, emails, or filters.

## Core Workflow

1. Restate the request in one short sentence in the user's language.
2. Ask the question batch.
3. Search for an existing workflow first with `n8n_find_workflow_by_name`.
4. If a likely match exists, inspect it with `n8n_get_workflow` and update it instead of creating a new one.
5. If several workflows look similar, ask the user which one should be reused.
6. Only create a new workflow when no real match exists.
7. Validate the workflow structure with `n8n_validate_workflow_definition` before any write.
8. Before `n8n_create_workflow`, collect one short summary sentence for the automation because the MCP tool now requires a sticky-note summary inside every new workflow.
9. Reuse existing credentials with `n8n_get_credentials` before creating any new credential.
10. For any Slack node, default to the credential named `Slack n8n Bot` unless the user explicitly asks for a different Slack credential or the existing workflow already uses another approved Slack credential.
11. If `Slack n8n Bot` is missing and no other Slack credential was specified, stop and ask before creating or choosing a different Slack credential.
12. After the change, explain the result in short, simple language.
13. **Test and feedback:** Ask if the user wants a test execution. If yes, trigger it (webhook or the tool your MCP provides for test runs). After the run, ask for feedback: success, errors, and any changes they want.

## Duplicate Prevention Rules

- Treat one business automation as one workflow.
- Never create a second workflow for the same automation just to test, debug, compare versions, or work around an error.
- Never create workflows named `test`, `debug`, `tmp`, `copy`, `v2`, or `my workflow`.
- If you find an archived or inactive workflow that appears to be the same automation, ask the user before replacing or reactivating it.
- If the user asks for the same automation again, reuse the same workflow and update it.

## Naming Rules

- Workflow name format: `[<input SaaS>] <O que faz o workflow> [<Output SaaS>]`
- Workflow tags must include:
  - `FDE` derived automatically from the authenticated MCP creator identity
  - `Area` such as `CS`, `Sales`, or another business area
- `Notion Ticket` is optional metadata when the user already has one, but it is not required to create or rename a workflow.
- Credential name format: `<SaaS>-<Type Credential>-<FDE>`
- When you need an FDE value for naming or tagging, use the creator identity passed automatically by the MCP proxy instead of asking the user.
- If `Area` is missing, ask the user before creating or renaming anything.
- If the current MCP workflow tool cannot write tags directly, still collect the tag values and tell the user clearly that the tags were not saved by the tool.

## Data Source Rules

- For Guesty-related data, prefer Firestore or SQL-backed internal data first.
- Use `sql_gateway_get_schema` to check whether the needed data is already available in the internal data layer.
- Avoid direct Guesty connections whenever the data can be obtained from Firestore or the SQL gateway.
- Use direct Guesty access only when the needed data is not available internally, and say that reason clearly.

## Schedule and Load Rules

- Any scheduled workflow must run every 2 hours or slower. Never create a scheduled workflow below 2 hours.
- If the user asks for less than 2 hours, explain the limit simply and offer either:
  - every 2 hours
  - an event-based trigger
- Do not "bombar" critical systems such as Zendesk, Breezeway, or Guesty.
- For workflows that read or write critical systems, reduce load with filters, date windows, changed-since logic, batching, and limits.
- Stop and ask the user before creating high-volume writes, bulk updates, or broad polling loops.

### Scheduled workflows: add a webhook for testing

- When the primary trigger is **Schedule** (or any time-based trigger), **also add a Webhook node** as a second entry point. Wire **both** the Schedule trigger and the Webhook to the **same next node** (merge into one branch) so production behavior is identical regardless of how the run started.
- Configure the webhook for **manual / agent testing only**: document that scheduled runs stay on the cron; the webhook exists so you or the agent can fire the workflow on demand without waiting for the next schedule tick.
- Prefer **POST** and a **non-guessable path** when n8n allows it; if authentication options exist (header, query secret), use them when the instance supports it. Tell the user clearly that the webhook URL is sensitive.
- The first nodes after the merge should treat **webhook payload** the same as **empty or schedule-shaped input** where needed (defaults, optional fields) so a simple test POST does not break the flow.
- If the user refuses a second trigger for security or policy reasons, note that in your summary and still offer schedule-only testing guidance (e.g. wait for next run, or temporary narrower schedule with explicit approval).

## Error Handling Rules

- If a workflow fails, inspect the existing workflow first.
- Ask focused questions before changing it.
- Do not create a separate troubleshooting workflow.
- Do not create dummy Slack channels, placeholder credentials, or fake destinations.
- If the destination is missing, ask for it.

Example:

`I’m missing the Slack channel for this. Which channel should receive the message?`

## Build Rules

- Use `n8n_find_node_by_name_or_capability`, `n8n_list_node_files`, and `n8n_get_node_source` when node behavior is unclear.
- Keep flows simple and readable.
- Prefer idempotent reads and bounded updates.
- Reuse credentials when possible.
- For Slack nodes, first try to reuse the credential named `Slack n8n Bot`.
- Do not silently swap a workflow away from `Slack n8n Bot` to another Slack credential unless the user asked for that change or the workflow already depends on another credential.
- Validate before create or update.
- Prefer updating an existing workflow with `n8n_update_workflow`.
- Use `n8n_create_workflow` only after the duplicate checks are done.

## Finish

End with a short summary in plain language:

- what the workflow does
- whether you reused or created it
- the trigger (and, for scheduled workflows, that a **webhook mirror** exists for on-demand tests, unless the user opted out)
- any important limit or open question
- whether a **test run** was done or offered, and **any user feedback** captured (or a reminder to share feedback after they try it)
