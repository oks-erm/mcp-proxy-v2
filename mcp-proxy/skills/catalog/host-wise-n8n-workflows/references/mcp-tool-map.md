# MCP Tool Map

Use this order when building or changing an automation.

## Reuse First

1. `n8n_find_workflow_by_name`
   Use this first. Search with the business name and 1 or 2 close variants.

2. `n8n_get_workflow`
   Use this after you find a likely match. Review the current workflow before changing anything.

3. `n8n_update_workflow`
   Use this when the automation already exists.

4. `n8n_create_workflow`
   Use this only if no matching workflow exists.

## Validate Before Write

1. `n8n_validate_workflow_definition`
   Use this before `n8n_update_workflow` or `n8n_create_workflow`.

## Credentials

1. `n8n_get_credentials`
   Check existing credentials first.
   For Slack nodes, look for `Slack n8n Bot` first and reuse it by default unless the user explicitly asked for another Slack credential.

2. `n8n_get_credential_schema`
   Use this only when a new credential is truly needed.

3. `n8n_create_credential`
   Create a credential only after confirming no suitable one already exists.
   If `Slack n8n Bot` is missing, do not create or choose a different Slack credential silently. Ask first unless the user already named the replacement.

## Node Discovery

1. `n8n_find_node_by_name_or_capability`
   Find the likely node folder.

2. `n8n_list_node_files`
   Find the source file.

3. `n8n_get_node_source`
   Read the node source when parameters or behavior are unclear.

## Data Discovery

1. `sql_gateway_get_schema`
   Use this first for Guesty-related reporting or internal data questions.

Rule:
If the needed data is available internally, do not design a direct Guesty connection.

## Execution Tools

- `n8n_stop_execution`
- `n8n_retry_execution`

Use these only for an existing workflow. Do not create a new workflow for debugging.
