# Investigation Playbook

Use this file when the user asks a vague operational question and you need to turn it into a concrete, evidence-based investigation.

## 1. Rewrite The Request

Convert the user's message into a short brief with five fields:

- `question`: what business question needs an answer
- `subject`: listing, property, reservation, guest, ticket, owner, workflow, or portfolio
- `window`: time range to inspect
- `systems`: which MCP integrations are likely relevant
- `deliverable`: diagnosis, triage table, ranked hypotheses, summary, or report

Examples:

- `Show me the guest complaints from the last week and if there's anything unresolved in those properties.`

  - `question`: which current guest complaints also have unresolved operational issues
  - `subject`: open guest complaint tickets
  - `window`: trailing 7 days
  - `systems`: Zendesk, Guesty, Breezeway
  - `deliverable`: combined triage table with priority flags

- `Why is T0 Maternidade 45 7 not getting bookings?`
  - `question`: why future reservations are weak
  - `subject`: one listing/property
  - `window`: next 60 days vs previous 60 days
  - `systems`: Guesty, SQL, Breezeway
  - `deliverable`: short diagnosis with ranked hypotheses and actions

## 2. Safe Defaults

Use these defaults unless the user gives a better one:

- `last week` -> trailing 7 days
- `recent` -> trailing 30 days
- `future bookings low` -> next 60 days
- `compare historically` -> previous 60 days
- `unresolved` -> open, overdue, blocked, pending, flagged, or unresolved issues
- `diagnosis` -> short answer first, then evidence, then ranked hypotheses

State any important default briefly in the answer.

## 3. Ask Only When It Matters

Ask a short clarifying question only when the investigation would be unsafe or low quality without it.

Good reasons to ask:

- the named listing/property cannot be resolved
- several entities are equally plausible matches
- the user omitted the only identifier that bridges systems
- the time window would meaningfully change the answer

Do not ask when you can make a safe, common-sense assumption and proceed.

## 4. Evidence Discipline

- Resolve entities before broad retrieval.
- Prefer summary and scoped search over raw full records.
- Use at least two pieces of evidence before making a causal hypothesis when possible.
- Distinguish clearly between:
  - facts: directly observed in tool output
  - hypotheses: your explanation of the facts
- Do not overclaim. If you only have partial evidence, say so.

## 5. Investigation Heuristics

### Triage questions

For prompts like `show me complaints`, `what needs attention`, or `what is unresolved`:

- build one row per business object
- surface status fields from each system
- flag rows where more than one problem overlaps
- rank the queue by urgency, not by narrative detail

### Diagnosis questions

For prompts like `why is this not getting bookings` or `what is causing this problem`:

- confirm object status first
- compare forward-looking state with recent historical baseline
- inspect quality signals such as reviews, flags, maintenance issues, or operational blockers
- rank the top 3 hypotheses by likelihood
- give one recommended action per hypothesis

## 6. Response Templates

### Short diagnosis

Use when the user asks `why` or `what is going on`.

1. One short paragraph with the direct answer.
2. `Evidence:` 3-5 bullets with the strongest observations.
3. `Top hypotheses:` 3 bullets ranked by likelihood, each with one action.
4. `Assumptions or gaps:` 1-3 bullets if needed.

### Triage report

Use when the user asks `show me`, `list`, or `what is unresolved`.

1. One-sentence summary of the queue.
2. A combined row-per-item report.
3. A short highlight of the highest-priority rows.
4. Assumptions or missing links if some rows could not be joined cleanly.

## 7. Anti-Patterns

- Do not dump raw tool output without interpretation.
- Do not query whole portfolios when the user named one object.
- Do not run SQL before checking schema and join keys.
- Do not present hypotheses as facts.
- Do not mention many internal tool details unless they help the user understand a limitation or a recommendation.
