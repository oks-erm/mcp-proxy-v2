# SQL Gateway Agent Schema Guide

This file is the **agent-first schema super prompt** for SQL work in the portal database.

Use this document for:

- business meaning and query intent,
- trusted join paths,
- high-signal filters and metrics,
- common pitfalls and ambiguity guards.

Use `resources/schema.yaml` for:

- exact table/column names,
- reference fields,
- search-first structured lookup.

## Mandatory Query Workflow

1. Confirm the business question and metric definition.
2. Identify domain tables from this guide.
3. Validate exact column names in `schema.yaml`.
4. Build **SELECT-only** SQL.
5. Paginate results and only return required columns.
6. If unsure about joins, validate keys using row samples before aggregating.

## Output Quality Rules

- Always scope timeframe (`date`, `created_at`, `check_in/check_out`, or month field).
- Avoid `SELECT *` in final answers.
- Prefer explicit joins on canonical keys listed below.
- Normalize IDs as text when combining heterogeneous sources.
- If two similarly named fields exist, document which one you used.

---

## Canonical Domain Map

### Accounting

- `accounting_transaction`: transactional revenue/cost lines (fees, refunds, payouts).
- `accounting_payout`: payout batches by platform.
- `accounting_reconciliation`: matching between bank and payout.
- `accounting_nonreservationinvoice`: product-only invoices (non-stay flow).
- `accounting_invoiceabletransaction`, `accounting_invoice`, `accounting_creditnote`.

### Listings and Operations

- `listings_listing`, `listings_unit`, `listings_listingunit`.
- `listings_listingchannel`, `listings_regiongroup`, `listings_serviceconnection`.
- `operations_operationtask`, `operations_operationuser` and relation tables.

### Reservations and Revenue

- `reservations_reservation` (master booking row).
- `reservations_reservationsingle` (unit split).
- `reservations_reservationdailyrevenue`, `reservations_reservationmonthlyrevenue`.
- `reservations_fee`, `reservations_reservationvalidation`, `reservations_transaction`.

### Owners and Contracts

- `owners_owner`, `owners_ownerspayout`, `owners_ownerpayoutvalidation`.
- `properties_property`, `properties_contract`, `properties_contracttemplate`,
  `properties_contractrule`, `properties_contractfee`, `properties_feestructure`.

### Reviews

- `reviews_review`, `reviews_airbnbreview`, `reviews_bookingreview`,
  `reviews_housekeepingreview`.
- Aggregates: `airbnb_avg_ratings`, `booking_avg_ratings`.

### Users and Tasks

- `users_user` (auth + permissions).
- `tasks_taskcache` (cached task payloads).

---

## Trusted Join Keys (High Confidence)

- Reservation core:
  - `reservations_reservation.id = reservations_fee.reservation_id`
  - `reservations_reservation.id = reservations_reservationdailyrevenue.reservation_id`
  - `reservations_reservation.id = reservations_reservationmonthlyrevenue.reservation_id`
  - `reservations_reservation.id = reviews_review.reservation_id`
- Listing core:
  - `listings_listing.id = reservations_reservation.listing_id`
  - `listings_listing.id = properties_property.listing_id`
  - `listings_listing.id = reviews_review.listing_id`
- Unit mapping:
  - `listings_unit.id = listings_listingunit.unit_id`
  - `listings_listing.id = listings_listingunit.listing_id`
  - `listings_unit.id = reservations_reservationsingle.unit_id`
- Accounting flow:
  - `accounting_payout.id = accounting_transaction.payout_id`
  - `accounting_bankentry.id = accounting_reconciliation.bank_entry_id`
  - `accounting_payout.id = accounting_reconciliation.payout_id`
  - `accounting_transaction.id = accounting_nonreservationinvoice.transaction_id`
- Owner payout validation:
  - `owners_owner.id = owners_ownerpayoutvalidation.owner_id`
  - `properties_property.id = owners_ownerpayoutvalidation.property_id`
  - `reservations_reservation.id = owners_ownerpayoutvalidation.reservation_id`

---

## Per-Table Agent Template (Reference Pattern)

Use this structure when reasoning about any table:

1. **Purpose and grain**: what one row represents.
2. **Primary key and uniqueness**: PK + unique constraints.
3. **Business-critical columns**: columns used in filters, joins, metrics.
4. **Inbound/outbound joins**: trusted FK paths.
5. **Query patterns**: common grouped metrics and slices.
6. **Pitfalls**: nullable keys, duplicate identifiers, platform-specific semantics.

---

## High-Value Table Playbooks

### `reservations_reservation`

- **Purpose/grain:** one reservation/booking record from PMS.
- **Primary key:** `id` (UUID).
- **Critical columns:** `listing_id`, `status`, `source`, `check_in`, `check_out`, `nights`,
  `subtotal`, `cleaning_fee`, `channel_commission`, `revenue`, `owner_revenue`, `net_revenue`,
  `validation_status`.
- **Common joins:** listings, fees, daily/monthly revenue, reviews.
- **Pitfalls:** some financial fields may be null; `pms_reservation_id` and `channel_reservation_id`
  are external identifiers, not guaranteed globally stable across providers.

### `reservations_reservationdailyrevenue`

- **Purpose/grain:** one reservation revenue row per night (or unit-night in multi-unit).
- **Critical columns:** `nightdate`, `reservation_id`, `listing_id`, `unit_id`,
  `base_price`, `cleaning_fee_daily`, `channel_commission_daily`, `revenue`, `month`.
- **Pitfalls:** both `nightdate` and deprecated `date` can exist; prefer `nightdate`.

### `accounting_transaction`

- **Purpose/grain:** platform transaction lines feeding reconciliation/invoicing.
- **Critical columns:** `payout_id`, `platform`, `type`, `date`, `confirmation_code`,
  `gross_earnings`, `transaction_amount`, `amount`, `service_fee`, `cleaning_fee`,
  `occupancy_taxes`, `issue`.
- **Pitfalls:** `type` values are platform-specific and multilingual; normalize before grouping.

### `accounting_nonreservationinvoice`

- **Purpose/grain:** product-only invoice source linked to one accounting transaction.
- **Critical columns:** `transaction_id`, `moloni_product_id`, `moloni_customer_id`,
  `gross_earnings`, `invoice_date`, `platform`.
- **Pitfalls:** does not represent accommodation invoices; do not merge blindly with reservation
  invoice flows.

### `owners_ownerpayoutvalidation`

- **Purpose/grain:** validation line for owner payout at reservation+property+listing scope.
- **Critical columns:** `owner_id`, `property_id`, `listing_id`, `reservation_id`,
  `amount`, `reservation_amount`, `platform_commission`, `status`.
- **Pitfalls:** avoid double counting when joining to reservation fees or revenue tables.

### `reviews_review`

- **Purpose/grain:** base review entity across providers.
- **Critical columns:** `integration_type`, `rating`, `review_date`, `listing_id`,
  `reservation_id`, `unit_id`, `validation_status`.
- **Pitfalls:** provider-specific category scores are in `reviews_airbnbreview` and
  `reviews_bookingreview`; do not expect them in base table.

### `listings_listing`

- **Purpose/grain:** listing entity with topology, capacity, price metadata and state.
- **Critical columns:** `listing_type`, `status`, `is_active`, `city`, `country`,
  `base_price`, `currency`, `quality_score`, `location_score`, `validation_status`.
- **Pitfalls:** cluster/multi listings require `listings_listingunit` joins for unit-level detail.

### `users_user`

- **Purpose/grain:** one platform user account.
- **Critical columns:** `id`, `email`, `google_id`, `is_invited`, `permissions`,
  `is_active`, `is_staff`, `is_superuser`, `date_joined`.
- **Pitfalls:** email is login identity; username exists for Django compatibility only.

### `tasks_taskcache`

- **Purpose/grain:** cached JSON payload for background tasks.
- **Critical columns:** `task_id`, `cache_data`, `created_at`, `updated_at`.
- **Pitfalls:** cache payload schema may evolve; treat `cache_data` as semi-structured.

---

## Curated SQL Examples (Executable)

### 1) Reservation Revenue by Listing and Month

```sql
SELECT
  rmr.month,
  l.id AS listing_id,
  COALESCE(l.nickname, l.title) AS listing_name,
  SUM(COALESCE(rmr.revenue, 0)) AS total_revenue,
  SUM(COALESCE(rmr.total_hostwise, 0)) AS total_hostwise,
  SUM(COALESCE(rmr.channel_commission, 0)) AS total_channel_commission
FROM reservations_reservationmonthlyrevenue rmr
JOIN listings_listing l ON l.id = rmr.listing_id
WHERE rmr.month >= DATE '2025-01-01'
  AND rmr.month < DATE '2026-01-01'
GROUP BY rmr.month, l.id, COALESCE(l.nickname, l.title)
ORDER BY rmr.month, total_revenue DESC;
```

### 2) Accounting Issue Monitoring

```sql
SELECT
  at.platform,
  at.issue,
  COUNT(*) AS transaction_count,
  SUM(COALESCE(at.amount, 0)) AS total_amount
FROM accounting_transaction at
WHERE at.date >= DATE '2025-01-01'
  AND at.issue IS NOT NULL
GROUP BY at.platform, at.issue
ORDER BY transaction_count DESC;
```

### 3) Reconciliation Coverage

```sql
SELECT
  ar.status,
  COUNT(*) AS reconciliations,
  COUNT(ar.bank_entry_id) AS with_bank_entry,
  COUNT(ar.payout_id) AS with_payout
FROM accounting_reconciliation ar
GROUP BY ar.status
ORDER BY reconciliations DESC;
```

### 4) Non-Reservation Invoice Pipeline

```sql
SELECT
  ani.invoice_date,
  ani.platform,
  ani.moloni_product_name,
  COUNT(*) AS invoices,
  SUM(COALESCE(ani.gross_earnings, 0)) AS gross_earnings
FROM accounting_nonreservationinvoice ani
WHERE ani.invoice_date >= DATE '2025-01-01'
GROUP BY ani.invoice_date, ani.platform, ani.moloni_product_name
ORDER BY ani.invoice_date DESC, gross_earnings DESC;
```

### 5) Owner Payout Validation Exceptions

```sql
SELECT
  opv.status,
  o.id AS owner_id,
  CONCAT(COALESCE(o.first_name, ''), ' ', COALESCE(o.last_name, '')) AS owner_name,
  COUNT(*) AS rows_count,
  SUM(COALESCE(opv.amount, 0)) AS total_amount
FROM owners_ownerpayoutvalidation opv
JOIN owners_owner o ON o.id = opv.owner_id
WHERE opv.status IS NOT NULL
GROUP BY opv.status, o.id, CONCAT(COALESCE(o.first_name, ''), ' ', COALESCE(o.last_name, ''))
ORDER BY rows_count DESC;
```

### 6) Review Quality by Platform and Listing

```sql
SELECT
  rr.integration_type,
  rr.listing_id,
  COUNT(*) AS reviews_count,
  ROUND(AVG(rr.rating), 2) AS avg_rating
FROM reviews_review rr
WHERE rr.review_date >= DATE '2025-01-01'
  AND rr.rating IS NOT NULL
GROUP BY rr.integration_type, rr.listing_id
HAVING COUNT(*) >= 5
ORDER BY avg_rating DESC, reviews_count DESC;
```

### 7) Operations Task Throughput

```sql
SELECT
  ot.department,
  ot.priority,
  COUNT(*) AS tasks_total,
  COUNT(ot.operation_finished_at) AS tasks_finished
FROM operations_operationtask ot
WHERE ot.operation_created_at >= DATE '2025-01-01'
GROUP BY ot.department, ot.priority
ORDER BY tasks_total DESC;
```

### 8) Listing Coverage by Unit Mapping

```sql
SELECT
  l.id AS listing_id,
  COALESCE(l.nickname, l.title) AS listing_name,
  l.listing_type,
  COUNT(DISTINCT lu.unit_id) AS mapped_units
FROM listings_listing l
LEFT JOIN listings_listingunit lu ON lu.listing_id = l.id
GROUP BY l.id, COALESCE(l.nickname, l.title), l.listing_type
ORDER BY mapped_units DESC, listing_name;
```

---

## Ambiguity and Risk Guards

- `date` fields have multiple semantics (transaction date, booking date, invoice date).
  Always label the date meaning in query output aliases.
- Some tables mix platform enums with free-text historical values.
  Use normalization CASE expressions before KPI grouping.
- Revenue fields may overlap (`revenue`, `owner_revenue`, `net_revenue`, `total_hostwise`).
  Confirm business definition before aggregating.
- Validation tables (`*_validation*`) represent data quality checks, not final financial truth.

---

Keep this guide narrative and execution-focused, and keep `schema.yaml` precise and machine-parseable.

# SQL Gateway Agent Schema Guide

This file is the **agent-first schema super prompt** for SQL work in the portal database.

Use this document for:

- business meaning and query intent,
- trusted join paths,
- high-signal filters and metrics,
- common pitfalls and ambiguity guards.

Use `resources/schema.yaml` for:

- exact table/column names,
- reference fields,
- search-first structured lookup.

## Mandatory Query Workflow

1. Confirm the business question and metric definition.
2. Identify domain tables from this guide.
3. Validate exact column names in `schema.yaml`.
4. Build **SELECT-only** SQL.
5. Paginate results and only return required columns.
6. If unsure about joins, validate keys using row samples before aggregating.

## Output Quality Rules

- Always scope timeframe (`date`, `created_at`, `check_in/check_out`, or month field).
- Avoid `SELECT *` in final answers.
- Prefer explicit joins on canonical keys listed below.
- Normalize IDs as text when combining heterogeneous sources.
- If two similarly named fields exist, document which one you used.

---

## Canonical Domain Map

### Accounting

- `accounting_transaction`: transactional revenue/cost lines (fees, refunds, payouts).
- `accounting_payout`: payout batches by platform.
- `accounting_reconciliation`: matching between bank and payout.
- `accounting_nonreservationinvoice`: product-only invoices (non-stay flow).
- `accounting_invoiceabletransaction`, `accounting_invoice`, `accounting_creditnote`.

### Listings and Operations

- `listings_listing`, `listings_unit`, `listings_listingunit`.
- `listings_listingchannel`, `listings_regiongroup`, `listings_serviceconnection`.
- `operations_operationtask`, `operations_operationuser` and relation tables.

### Reservations and Revenue

- `reservations_reservation` (master booking row).
- `reservations_reservationsingle` (unit split).
- `reservations_reservationdailyrevenue`, `reservations_reservationmonthlyrevenue`.
- `reservations_fee`, `reservations_reservationvalidation`, `reservations_transaction`.

### Owners and Contracts

- `owners_owner`, `owners_ownerspayout`, `owners_ownerpayoutvalidation`.
- `properties_property`, `properties_contract`, `properties_contracttemplate`,
  `properties_contractrule`, `properties_contractfee`, `properties_feestructure`.

### Reviews

- `reviews_review`, `reviews_airbnbreview`, `reviews_bookingreview`,
  `reviews_housekeepingreview`.
- Aggregates: `airbnb_avg_ratings`, `booking_avg_ratings`.

### Users and Tasks

- `users_user` (auth + permissions).
- `tasks_taskcache` (cached task payloads).

---

## Trusted Join Keys (High Confidence)

- Reservation core:
  - `reservations_reservation.id = reservations_fee.reservation_id`
  - `reservations_reservation.id = reservations_reservationdailyrevenue.reservation_id`
  - `reservations_reservation.id = reservations_reservationmonthlyrevenue.reservation_id`
  - `reservations_reservation.id = reviews_review.reservation_id`
- Listing core:
  - `listings_listing.id = reservations_reservation.listing_id`
  - `listings_listing.id = properties_property.listing_id`
  - `listings_listing.id = reviews_review.listing_id`
- Unit mapping:
  - `listings_unit.id = listings_listingunit.unit_id`
  - `listings_listing.id = listings_listingunit.listing_id`
  - `listings_unit.id = reservations_reservationsingle.unit_id`
- Accounting flow:
  - `accounting_payout.id = accounting_transaction.payout_id`
  - `accounting_bankentry.id = accounting_reconciliation.bank_entry_id`
  - `accounting_payout.id = accounting_reconciliation.payout_id`
  - `accounting_transaction.id = accounting_nonreservationinvoice.transaction_id`
- Owner payout validation:
  - `owners_owner.id = owners_ownerpayoutvalidation.owner_id`
  - `properties_property.id = owners_ownerpayoutvalidation.property_id`
  - `reservations_reservation.id = owners_ownerpayoutvalidation.reservation_id`

---

## Per-Table Agent Template (Reference Pattern)

Use this structure when reasoning about any table:

1. **Purpose and grain**: what one row represents.
2. **Primary key and uniqueness**: PK + unique constraints.
3. **Business-critical columns**: columns used in filters, joins, metrics.
4. **Inbound/outbound joins**: trusted FK paths.
5. **Query patterns**: common grouped metrics and slices.
6. **Pitfalls**: nullable keys, duplicate identifiers, platform-specific semantics.

---

## High-Value Table Playbooks

### `reservations_reservation`

- **Purpose/grain:** one reservation/booking record from PMS.
- **Primary key:** `id` (UUID).
- **Critical columns:** `listing_id`, `status`, `source`, `check_in`, `check_out`, `nights`,
  `subtotal`, `cleaning_fee`, `channel_commission`, `revenue`, `owner_revenue`, `net_revenue`,
  `validation_status`.
- **Common joins:** listings, fees, daily/monthly revenue, reviews.
- **Pitfalls:** some financial fields may be null; `pms_reservation_id` and `channel_reservation_id`
  are external identifiers, not guaranteed globally stable across providers.

### `reservations_reservationdailyrevenue`

- **Purpose/grain:** one reservation revenue row per night (or unit-night in multi-unit).
- **Critical columns:** `nightdate`, `reservation_id`, `listing_id`, `unit_id`,
  `base_price`, `cleaning_fee_daily`, `channel_commission_daily`, `revenue`, `month`.
- **Pitfalls:** both `nightdate` and deprecated `date` can exist; prefer `nightdate`.

### `accounting_transaction`

- **Purpose/grain:** platform transaction lines feeding reconciliation/invoicing.
- **Critical columns:** `payout_id`, `platform`, `type`, `date`, `confirmation_code`,
  `gross_earnings`, `transaction_amount`, `amount`, `service_fee`, `cleaning_fee`,
  `occupancy_taxes`, `issue`.
- **Pitfalls:** `type` values are platform-specific and multilingual; normalize before grouping.

### `accounting_nonreservationinvoice`

- **Purpose/grain:** product-only invoice source linked to one accounting transaction.
- **Critical columns:** `transaction_id`, `moloni_product_id`, `moloni_customer_id`,
  `gross_earnings`, `invoice_date`, `platform`.
- **Pitfalls:** does not represent accommodation invoices; do not merge blindly with reservation
  invoice flows.

### `owners_ownerpayoutvalidation`

- **Purpose/grain:** validation line for owner payout at reservation+property+listing scope.
- **Critical columns:** `owner_id`, `property_id`, `listing_id`, `reservation_id`,
  `amount`, `reservation_amount`, `platform_commission`, `status`.
- **Pitfalls:** avoid double counting when joining to reservation fees or revenue tables.

### `reviews_review`

- **Purpose/grain:** base review entity across providers.
- **Critical columns:** `integration_type`, `rating`, `review_date`, `listing_id`,
  `reservation_id`, `unit_id`, `validation_status`.
- **Pitfalls:** provider-specific category scores are in `reviews_airbnbreview` and
  `reviews_bookingreview`; do not expect them in base table.

### `listings_listing`

- **Purpose/grain:** listing entity with topology, capacity, price metadata and state.
- **Critical columns:** `listing_type`, `status`, `is_active`, `city`, `country`,
  `base_price`, `currency`, `quality_score`, `location_score`, `validation_status`.
- **Pitfalls:** cluster/multi listings require `listings_listingunit` joins for unit-level detail.

### `users_user`

- **Purpose/grain:** one platform user account.
- **Critical columns:** `id`, `email`, `google_id`, `is_invited`, `permissions`,
  `is_active`, `is_staff`, `is_superuser`, `date_joined`.
- **Pitfalls:** email is login identity; username exists for Django compatibility only.

### `tasks_taskcache`

- **Purpose/grain:** cached JSON payload for background tasks.
- **Critical columns:** `task_id`, `cache_data`, `created_at`, `updated_at`.
- **Pitfalls:** cache payload schema may evolve; treat `cache_data` as semi-structured.

---

## Curated SQL Examples (Executable)

### 1) Reservation Revenue by Listing and Month

```sql
SELECT
  rmr.month,
  l.id AS listing_id,
  COALESCE(l.nickname, l.title) AS listing_name,
  SUM(COALESCE(rmr.revenue, 0)) AS total_revenue,
  SUM(COALESCE(rmr.total_hostwise, 0)) AS total_hostwise,
  SUM(COALESCE(rmr.channel_commission, 0)) AS total_channel_commission
FROM reservations_reservationmonthlyrevenue rmr
JOIN listings_listing l ON l.id = rmr.listing_id
WHERE rmr.month >= DATE '2025-01-01'
  AND rmr.month < DATE '2026-01-01'
GROUP BY rmr.month, l.id, COALESCE(l.nickname, l.title)
ORDER BY rmr.month, total_revenue DESC;
```

### 2) Accounting Issue Monitoring

```sql
SELECT
  at.platform,
  at.issue,
  COUNT(*) AS transaction_count,
  SUM(COALESCE(at.amount, 0)) AS total_amount
FROM accounting_transaction at
WHERE at.date >= DATE '2025-01-01'
  AND at.issue IS NOT NULL
GROUP BY at.platform, at.issue
ORDER BY transaction_count DESC;
```

### 3) Reconciliation Coverage

```sql
SELECT
  ar.status,
  COUNT(*) AS reconciliations,
  COUNT(ar.bank_entry_id) AS with_bank_entry,
  COUNT(ar.payout_id) AS with_payout
FROM accounting_reconciliation ar
GROUP BY ar.status
ORDER BY reconciliations DESC;
```

### 4) Non-Reservation Invoice Pipeline

```sql
SELECT
  ani.invoice_date,
  ani.platform,
  ani.moloni_product_name,
  COUNT(*) AS invoices,
  SUM(COALESCE(ani.gross_earnings, 0)) AS gross_earnings
FROM accounting_nonreservationinvoice ani
WHERE ani.invoice_date >= DATE '2025-01-01'
GROUP BY ani.invoice_date, ani.platform, ani.moloni_product_name
ORDER BY ani.invoice_date DESC, gross_earnings DESC;
```

### 5) Owner Payout Validation Exceptions

```sql
SELECT
  opv.status,
  o.id AS owner_id,
  CONCAT(COALESCE(o.first_name, ''), ' ', COALESCE(o.last_name, '')) AS owner_name,
  COUNT(*) AS rows_count,
  SUM(COALESCE(opv.amount, 0)) AS total_amount
FROM owners_ownerpayoutvalidation opv
JOIN owners_owner o ON o.id = opv.owner_id
WHERE opv.status IS NOT NULL
GROUP BY opv.status, o.id, CONCAT(COALESCE(o.first_name, ''), ' ', COALESCE(o.last_name, ''))
ORDER BY rows_count DESC;
```

### 6) Review Quality by Platform and Listing

```sql
SELECT
  rr.integration_type,
  rr.listing_id,
  COUNT(*) AS reviews_count,
  ROUND(AVG(rr.rating), 2) AS avg_rating
FROM reviews_review rr
WHERE rr.review_date >= DATE '2025-01-01'
  AND rr.rating IS NOT NULL
GROUP BY rr.integration_type, rr.listing_id
HAVING COUNT(*) >= 5
ORDER BY avg_rating DESC, reviews_count DESC;
```

### 7) Operations Task Throughput

```sql
SELECT
  ot.department,
  ot.priority,
  COUNT(*) AS tasks_total,
  COUNT(ot.operation_finished_at) AS tasks_finished
FROM operations_operationtask ot
WHERE ot.operation_created_at >= DATE '2025-01-01'
GROUP BY ot.department, ot.priority
ORDER BY tasks_total DESC;
```

### 8) Listing Coverage by Unit Mapping

```sql
SELECT
  l.id AS listing_id,
  COALESCE(l.nickname, l.title) AS listing_name,
  l.listing_type,
  COUNT(DISTINCT lu.unit_id) AS mapped_units
FROM listings_listing l
LEFT JOIN listings_listingunit lu ON lu.listing_id = l.id
GROUP BY l.id, COALESCE(l.nickname, l.title), l.listing_type
ORDER BY mapped_units DESC, listing_name;
```

---

## Ambiguity and Risk Guards

- `date` fields have multiple semantics (transaction date, booking date, invoice date).
  Always label the date meaning in query output aliases.
- Some tables mix platform enums with free-text historical values.
  Use normalization CASE expressions before KPI grouping.
- Revenue fields may overlap (`revenue`, `owner_revenue`, `net_revenue`, `total_hostwise`).
  Confirm business definition before aggregating.
- Validation tables (`*_validation*`) represent data quality checks, not final financial truth.

---

Keep this guide narrative and execution-focused, and keep `schema.yaml` precise and machine-parseable.
