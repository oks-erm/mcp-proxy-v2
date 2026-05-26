# MCP Routing

Use this file when choosing tool order for an investigation.

## Resolver-First Ladder

Use this ladder unless a more specific journey below says otherwise:

1. Resolve the entity from human text.
2. Fetch a safe summary or tightly scoped search result.
3. Join the matching entity in the next system.
4. Pull only the fields needed for the answer.
5. Escalate to raw detail only when a key field is missing.

Domain shortcuts:

- Guesty:
  - property/listing from text -> `guesty_find_listing`
  - reservation from human context -> `guesty_find_reservation`
  - listing detail -> `guesty_get_listing_summary`
  - forward availability -> `guesty_get_listing_calendar`
  - reviews -> `guesty_search_reviews`
- Zendesk:
  - reservation-linked ticket -> `zendesk_find_ticket_by_reservation_id`
  - filtered queue -> `zendesk_search_tickets`
  - exact ticket -> `zendesk_get_ticket`
- Breezeway:
  - property from name or external id -> `breezeway_find_property_by_name_or_external_id`
  - property detail -> `breezeway_get_property_summary`
  - one-property task view -> `breezeway_list_tasks`
  - portfolio urgency queue -> `breezeway_triage_tasks`
- SQL:
  - schema and joins -> `sql_gateway_get_schema`
  - exact table columns -> `sql_gateway_lookup_table`
  - read-only query -> `sql_gateway_run_query`

## Cross-Platform Ops Triage

User-style prompt:
`Show me the guest complaints from the last week and if there's anything unresolved in those properties.`

Recommended sequence:

1. Use `zendesk_search_tickets` to find open guest-complaint tickets in the trailing 7 days.
2. For each ticket, extract the reservation identifier or use the reservation bridge available in Zendesk.
3. Resolve the reservation in Guesty with `guesty_find_reservation` or a tight `guesty_search_reservations`.
4. Fetch the listing/property context with `guesty_get_listing_summary`.
5. Resolve the matching Breezeway property with `breezeway_find_property_by_name_or_external_id`.
6. Use `breezeway_list_tasks` scoped to that property to find open or overdue tasks.
7. Return one row per ticket with:
   - Zendesk ticket id, subject, status, assignee
   - Guesty reservation and property context
   - Breezeway open/overdue task summary
8. Flag rows where an open complaint and an overdue Breezeway task exist together.

Important rules:

- Prefer one-property task lookups over `breezeway_triage_tasks` here because the user asked about the properties tied to known complaints, not the whole portfolio.
- If Zendesk returns tickets without a clean reservation link, say which rows could not be joined safely.

## Vacancy Diagnosis

User-style prompt:
`Why is T0 Maternidade 45 7 not getting bookings?`

Recommended sequence:

1. Resolve the listing with `guesty_find_listing`.
2. Confirm listing status and key identifiers with `guesty_get_listing_summary`.
3. Pull the forward calendar for the next 60 days with `guesty_get_listing_calendar`.
4. Calculate or summarize open, blocked, and reserved days.
5. Read recent reviews with `guesty_search_reviews`.
6. Inspect SQL schema first with `sql_gateway_get_schema` or `sql_gateway_lookup_table`.
7. Run a tight historical occupancy query with `sql_gateway_run_query` for the previous 60 days.
8. Resolve the property in Breezeway and inspect open tasks with `breezeway_find_property_by_name_or_external_id` plus `breezeway_list_tasks`.
9. Rank the top 3 hypotheses by likelihood and give one action per hypothesis.

Important rules:

- Do not guess the SQL model or join key. Discover it first.
- Treat blocked calendar days and open maintenance issues as stronger operational signals than generic speculation.
- Use reviews to support a hypothesis, not as the only explanation.

## Common Bridges Between Systems

- Zendesk <-> Guesty:
  - reservation id is the cleanest bridge when present
- Guesty <-> Breezeway:
  - listing/property name
  - PMS or external property id when available
- Guesty/Operations <-> SQL:
  - discover the warehouse model and canonical listing key before querying

## Anti-Patterns

- Starting with a broad `list_*` call when a resolver exists.
- Using `breezeway_triage_tasks` for a single resolved property.
- Running warehouse SQL before schema discovery.
- Joining across systems on a guessed name when an exact id is available.
