# Breezeway MCP

Read-focused tools for the Breezeway inventory API. **Cloud Run**: deploy with `--no-allow-unauthenticated` and grant `roles/run.invoker` to the MCP proxy service account.

## Tools

| Tool                                             | Parameters                                                                                  | Description                                                                                                                                                                                  |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `breezeway_list_properties_page`                 | `page`, `limit`, `detail_level`                                                             | Paginated property list (default `detail_level="summary"` redacts sensitive keys)                                                                                                            |
| `breezeway_find_property_by_name_or_external_id` | **`query`** _(required)_, `limit`, `max_pages`, `page_size`                                 | Resolver: find properties by name, display name, or external/PMS id. **Primary arg is `query` — not `name`, `id`, or `external_id`.** Returns `{"count", "data", "detail_level": "compact"}` |
| `breezeway_get_property_summary`                 | **`property_id`** _(int, required)_                                                         | Fetch one safe property summary by exact numeric Breezeway id. Returns summary fields with sensitive keys stripped                                                                           |
| `breezeway_list_users`                           | `detail_level`, `limit`, `offset`                                                           | Paginated user/people list                                                                                                                                                                   |
| `breezeway_list_tasks`                           | `page`, `limit`, `home_id` or `reference_property_id`, optional filters, `include_comments` | Paginated task list; set `include_comments=true` to hydrate each task with its comment thread                                                                                                |
| `breezeway_triage_tasks`                         | `limit`, `include_comments`, `max_properties`, `max_tasks_per_property`                     | Portfolio-wide task scan that ranks open work across properties, groups the queue by urgency, and highlights what needs attention now                                                        |
| `breezeway_get_task_comments`                    | **`task_id`** _(int, required)_                                                             | Retrieve comments for a single task                                                                                                                                                          |
| `breezeway_move_task`                            | **`task_id`** _(int, required)_, **`action`**                                               | Move a task through a supported workflow transition: `close`, `approve`, or `reopen`                                                                                                         |
| `breezeway_update_task`                          | **`task_id`** _(int, required)_, **`updates`** _(object)_                                   | Patch a task using the raw Breezeway update payload                                                                                                                                          |
| `breezeway_get_reservation_by_external_id`       | **`external_reservation_id`** _(required)_, `allow_multiple`                                | Fetch a reservation by external PMS/booking id                                                                                                                                               |

> **Resolver tools** (`breezeway_find_property_by_name_or_external_id`) use **`query`** as the single search argument — do not pass `name`, `external_id`, or `id`.

## Agent Guidance

Use the Breezeway task tools in this order:

1. `breezeway_list_tasks` when you need to browse or filter tasks for one property/home.
2. `breezeway_triage_tasks` when you need the urgent queue across many properties and want the MCP to do the portfolio scan + ranking for you.
3. `breezeway_list_tasks(include_comments=true)` when you already need the task rows and want comments inline on that page.
4. `breezeway_get_task_comments` when you already have a specific task id and want just that thread.
5. `breezeway_move_task` when the requested change is a workflow transition: `close`, `approve`, or `reopen`.
6. `breezeway_update_task` when the requested change is to editable task fields, not just workflow state.

### Task scope rule

`breezeway_list_tasks` should always be scoped with either:

- `home_id`, or
- `reference_property_id`

This keeps task retrieval targeted and avoids broad, low-signal task scans.

`breezeway_triage_tasks` is the portfolio-level exception to this rule. It still performs scoped per-property task reads under the hood, but it handles the scan/ranking loop for the caller.

### Comment retrieval rule

If an agent needs tasks and comments together, prefer:

```json
{
  "home_id": 12345,
  "page": 1,
  "limit": 20,
  "include_comments": true
}
```

If it only needs the discussion for one known task, prefer:

```json
{
  "task_id": 12345
}
```

### Write safety rule

`breezeway_move_task` and `breezeway_update_task` mutate Breezeway state:

- Use `breezeway_move_task` for status/workflow changes only.
- Use `breezeway_update_task` for field edits with a minimal PATCH body.
- Do not send speculative or broad update payloads. Patch only the fields the user asked to change.

Common documented `breezeway_update_task` fields from Breezeway's official update-task API are:

- `name`
- `type_department`
- `type_priority`
- `description`
- `template_id`
- `scheduled_date`
- `scheduled_time`
- `assignments`
- `tags`
- `subdepartment_id`
- `rate_paid`
- `rate_type`
- `requested_by`

The tool schema now exposes those fields directly while still allowing extra upstream keys if Breezeway expands the PATCH body.

Examples:

```json
{
  "task_id": 12345,
  "action": "close"
}
```

```json
{
  "task_id": 12345,
  "updates": {
    "assignments": [77]
  }
}
```

### Error handling guidance

- If `breezeway_list_tasks` returns `validation_error`, the agent should add `home_id` or `reference_property_id`.
- If `include_comments=true` is used, comment fetch failures are attached per task as `comments_error`; the overall list still succeeds.
- If `breezeway_move_task` or `breezeway_update_task` returns `upstream_failed`, the agent should verify the task id, the requested transition, and the PATCH fields against Breezeway's task API.

## MCP HTTP API key (Secret Manager)

The `/mcp-server` routes require **either**:

- **`X-API-Key`** matching the latest version of secret **`breezeway-mcp-api-key`** (override ID with `API_KEY_SECRET_ID`), **or**
- A **verified** `Authorization: Bearer` Google ID token from an allowlisted invoker SA (default `mcp-proxy-sa@it-team-hw-project.iam.gserviceaccount.com`), same pattern as Zendesk MCP.

### Create `breezeway-mcp-api-key` (once)

```bash
PROJECT=it-team-hw-project
KEY="$(openssl rand -hex 32)"
echo "Save for mcp-proxy upstream credentials if you use X-API-Key there: $KEY"

gcloud secrets describe breezeway-mcp-api-key --project="$PROJECT" >/dev/null 2>&1 || \
  gcloud secrets create breezeway-mcp-api-key --project="$PROJECT" --replication-policy=automatic

echo -n "$KEY" | gcloud secrets versions add breezeway-mcp-api-key --project="$PROJECT" --data-file=-

gcloud secrets add-iam-policy-binding breezeway-mcp-api-key --project="$PROJECT" \
  --member="serviceAccount:breezeway-mcp-sa@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

**Local:** `ENV=local` and `API_KEY` in the environment (no Secret Manager for the MCP key).

## Breezeway API JWT (Firestore)

JWT is read from Firestore document `tokens/breezeway` field `token`, using database `FIRESTORE_DATABASE` (default `data-warehouse-firestore`) and project `GCP_PROJECT_ID`.

**Local:** `ENV=local` and `BREEZEWAY_TOKEN`.

## Env

- `BREEZEWAY_COMPANY_ID` (default `8617`)
- `BREEZEWAY_API_BASE` (optional override)
- `API_KEY_SECRET_ID` (default `breezeway-mcp-api-key`)
- `ALLOWED_INVOKER_SA_EMAILS` (optional comma-separated list for OIDC-only access)

The Cloud Run service account (`breezeway-mcp-sa@…`) needs **Firestore read** on `data-warehouse-firestore` and **Secret Manager accessor** on `breezeway-mcp-api-key`.

## mcp-proxy

Use **`https://<service-url>/mcp-server/mcp`** with **Upstream auth: Cloud Run IAM** (OIDC only), or add **`credentials_header`** / secret with `X-API-Key: <same key as secret>` if you want both layers.

## Property list sensitivity

`breezeway_list_properties_page` defaults to **`detail_level="summary"`**, which strips access instructions, lock/Wi‑Fi–related fields, media arrays, and other high-sensitivity payload. Use **`detail_level="full"`** only when the full Breezeway property document is required.

## Task writes and permissions

`breezeway_move_task` and `breezeway_update_task` are write tools. In `mcp-proxy`, they must be classified as write operations for server id `breezeway` so users without `permissions.write` cannot see or call them.
