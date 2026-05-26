# Safety Checklist

Run this checklist before every workflow write.

## User Context

- Did you ask several short questions first?
- Is the goal clear?
- Is the trigger clear?
- Is the destination clear?
- Is the data source clear?
- Is there already a workflow for this?

## Duplicate Guard

- Did you search with `n8n_find_workflow_by_name` first?
- Did you inspect likely matches with `n8n_get_workflow`?
- Are you reusing the existing workflow when the automation is the same?
- Are you avoiding `test`, `debug`, `tmp`, `copy`, `v2`, and similar names?

## Naming Guard

- Does the workflow name follow `[<input SaaS>] <O que faz o workflow> [<Output SaaS>]`?
- Do the workflow tags include `FDE` from the authenticated MCP creator identity plus `Area`?
- If the user already has a `Notion Ticket`, did you treat it as optional metadata instead of a requirement?
- Does the credential name follow `<SaaS>-<Type Credential>-<FDE>`?
- Did you avoid asking the user to identify the FDE when MCP can derive it automatically?
- If `Area` is missing, did you ask the user?
- If tags cannot be written by the current MCP tool, did you say that clearly to the user?

## Data Guard

- Is this Guesty-related?
- Did you check internal data first with `sql_gateway_get_schema`?
- Are you avoiding direct Guesty access unless the data is missing internally?

## Load Guard

- Is the workflow scheduled?
- Is the interval at least 2 hours?
- Does it touch Zendesk, Breezeway, or Guesty?
- Did you reduce load with filters, limits, batching, or changed-since logic?

## Error Guard

- Is something missing, such as a Slack channel or recipient?
- Did you ask the user instead of guessing?
- Are you fixing the existing workflow instead of creating a debug workflow?

## Good Short Questions

Use short questions like these:

1. What should this automation do?
2. When should it run?
3. Who should receive it?
4. Which data should it use?
5. Is there already a workflow for this?
6. Do you want me to update the current workflow or create a new one if none exists?
