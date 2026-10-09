# Yard Inventory & Gate Control System — Design

**Status:** Draft v1 — awaiting approval
**Precedes:** `03-tasks.md`
**Requirements:** `01-requirements.md` (story IDs referenced throughout as `A1`, `F3`, etc.)

---

## 1. Architecture overview

```
                    CloudFront (SPA + static assets)
                            |
                    React 18 + TypeScript SPA
                            |  HTTPS / JSON
                    ALB  ->  ECS Fargate
                            |
        +-------------------+-------------------+
        |                   |                   |
   Django + DRF        Celery workers      Celery beat
   (web requests)      (async work)        (schedules)
        |                   |                   |
        +---------+---------+---------+---------+
                  |                   |
          RDS Postgres 16        ElastiCache Redis
                  |
                  S3  (attachments, generated exports)
```

Single Django project, multiple apps. One React SPA serving all roles — mobile-first, with
desktop-optimised views for the storekeeper and reports (`D5`, `N-1`).

### 1.1 Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12 | Current, well supported on Fargate |
| Backend | Django 5.x + Django REST Framework | Mandated (`D1`); the admin site is a free internal tool |
| Database | PostgreSQL 16 (RDS) | Row-level security, JSONB, window functions for as-of-date ledger queries |
| Async | Celery + Redis | Notifications, exports, overdue sweeps |
| Auth | `djangorestframework-simplejwt` | JWT with revocable refresh (`B1`) |
| Biometrics | `py_webauthn` | WebAuthn step-up on approval (`B5`, `D9`) |
| Files | `django-storages`: local filesystem in development, S3 with pre-signed URLs in production | `N-7` |
| PDF | WeasyPrint | Gate passes, GRNs, waybills (`G4`, `K2`) |
| Excel | openpyxl | Report exports (`M2`) |
| Frontend | React 18, TypeScript, Vite | `D1` |
| Data fetching | TanStack Query | Cache, retry, offline-friendly |
| Forms | react-hook-form + zod | Shared validation shape with API errors |
| UI | Tailwind CSS + shadcn/ui | Fast, mobile-first, no design debt |
| Offline store | Dexie (IndexedDB) + Workbox | `N1`–`N3` |
| Scanning | `BarcodeDetector` API with `@zxing/browser` fallback | `D7`, `G5` |
| Tests | pytest, pytest-django, factory_boy, Playwright | Section 14 |

### 1.2 Django app layout

```
config/            settings, urls, celery
core/              tenancy base classes, audit, numbering, attachments, settings
accounts/          User, Role, Permission, WebAuthn, Delegation
catalogue/         ItemCategory, CategoryCustomField, ItemType
network/           Client, Site, SiteReference, Project, ProjectVariation, Subcontractor
locations/         Location, StockNode
stock/             StockMovement, StockBalance, SerialUnit, Reel, StockCount
receiving/         GateIn and lines
dispatch/          GateOut, lines, release, variances
approvals/         ApprovalRule, ApprovalRequest, ApprovalAction, engine
jobs/              Job, JobCloseout, JobLabour, reconciliation
custody/           CustodyExpectation, transfers, overdue sweeps
disposition/       quarantine decisions, Disposal
notifications/     events, deliveries, channel adapters
commercials/       ExpenseCategory, ProjectExpense, ProjectSnapshot, the costing engine
reporting/         report queries, exports
sync/              idempotency, offline submission handling
platform_admin/    cross-tenant console
```

`Project` lives in `network/` rather than in an app of its own because it **is** the work-order
layer, renamed (`D20`), and moving it would be a second grouping by another name. `JobLabour` sits
in `jobs/` because it is written from a closeout and has no meaning apart from one. `commercials/`
holds what is genuinely new — expenses, snapshots, and the costing engine that reads across the
ledger, the closeouts and the expectations without owning any of them.

---

## 2. Multi-tenancy

Implements `D2`, `A3`. Four independent layers, so one forgotten filter cannot leak data.

### 2.1 Layer 1 — model and manager

```python
class TenantModel(models.Model):
    organization = models.ForeignKey('core.Organization', on_delete=models.PROTECT,
                                     db_index=True, editable=False)
    objects = TenantManager()          # raises if no org in context
    all_objects = models.Manager()     # explicit escape hatch, platform_admin only

    class Meta:
        abstract = True
```

`TenantManager.get_queryset()` reads the organization from a `ContextVar`. If none is set it
raises `TenantContextMissing` rather than returning everything — an unscoped read becomes a crash
in development instead of a silent leak in production. `all_objects` is grep-able, and CI fails the
build if it appears outside `platform_admin/` or migrations.

`TenantModel.save()` stamps `organization` from context when unset, and refuses to save a row whose
organization differs from the active context.

### 2.2 Layer 2 — request middleware

`TenantMiddleware` resolves the organization in this order:

1. Subdomain of the request host (`silvertech.yardflow.co.ke`) — `A1`
2. The authenticated user's organization

If both are present and disagree, the request is rejected with 404. It then sets the `ContextVar`
and executes `SET LOCAL app.current_org = <uuid>` on the connection inside the request's atomic
block. Celery tasks carry `organization_id` explicitly and set the same context via a task base
class.

> **Connection pooling note:** `SET LOCAL` is transaction-scoped, so a pooled connection cannot
> leak the setting into the next request. `ATOMIC_REQUESTS = True` guarantees a transaction exists.
> Never use `SET` without `LOCAL` here.

### 2.3 Layer 3 — Postgres row-level security

Every tenant table gets:

```sql
ALTER TABLE <table> ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON <table> USING (
    organization_id = current_setting('app.current_org', true)::uuid
);
```

The application connects as a non-superuser role, so policies are actually enforced. A migration
helper `enable_rls('app.Model')` applies this, and a Django system check fails startup if any
`TenantModel` subclass lacks a policy.


**Known gap: `accounts_user` carries no policy.** Every other tenant-scoped table does — roles,
role assignments, delegations, WebAuthn credentials — but the table holding the people themselves
relies on the application layer alone. Isolation holds today (the viewsets and the tenant manager
both filter), and it was verified by request: a tenant's user list returns only its own.

Enabling it was attempted and reverted, for a reason worth recording rather than rediscovering. The
policy needs one extra arm (`organization_id IS NULL`) so a platform administrator, who belongs to
no tenant, can still be loaded on a subdomain that resolves none. With that in place the reads are
correct — but `WITH CHECK` then refuses every **insert** made while no organization is in context,
and that pattern is widespread: test factories, and any code that creates a user before a tenant is
resolved. A rebuilt test database gave 34 failures and 359 errors, all of the form "new row violates
row-level security policy".

Closing it properly means auditing every path that creates a user and wrapping it in a context, then
enabling the policy — a piece of work in its own right, not a migration.

### 2.4 Layer 4 — API

`TenantScopedViewSet` scopes `get_queryset()` and returns **404, not 403**, for another tenant's
objects, so IDs are never confirmed to outsiders. `A3`'s acceptance test is a parametrised suite
that walks the DRF router, creates one object per endpoint in tenant A, and asserts every verb
returns 404 when called by tenant B.

---

## 3. The stock ledger — central design decision

Everything that happens to material has one shape: **a double-entry movement between two nodes.**

### 3.1 StockNode

A node is anywhere material can be. Nodes are auto-created and never deleted.

| Node type | Represents | Created for |
|---|---|---|
| `LOCATION` | Yard, store, vehicle, quarantine | Each `Location` |
| `PERSON` | Material in someone's custody (`I1`) | Each user, on first custody |
| `SITE` | Material installed at a site (`H2`) | Each `Site` |
| `CLIENT` | Material returned to a client (`K3`) | Each `Client` |
| `EXTERNAL` | Suppliers and client-issuing stores — the source of receipts | Per supplier/client, plus one generic |
| `CONSUMED` | Bulk material used up on site | One per tenant |
| `SCRAP` | Disposed material (`J3`) | One per tenant |

Why this matters: custody, installation, quarantine, consumption, client returns and transfers all
collapse into the same query. `H4`'s reconciliation — issued vs installed vs returned vs
unaccounted — becomes one aggregation over movements grouped by destination node type, rather than
five bespoke reports that can disagree with each other.

### 3.2 StockMovement — append-only

```
StockMovement
  id, organization, occurred_at, posted_at, posted_by
  movement_type      RECEIPT | ISSUE | TRANSFER | INSTALL | CONSUME | RETURN
                     | QUARANTINE | RESTORE | DISPOSE | ADJUST | REVERSAL
  item_type, tracking_mode, uom
  quantity           Decimal(14,3), ALWAYS POSITIVE
  from_node, to_node
  serial_unit        nullable, set when tracking_mode = SERIALIZED
  reel               nullable, set when tracking_mode = REEL
  owner_type         OWN | CLIENT          -- D3
  owner_client       nullable
  condition          NEW | USED_SERVICEABLE | FAULTY | DAMAGED | SCRAP
  document_type, document_id, document_line_id
  reversal_of        nullable self-FK
  unit_cost          Decimal(14,2) nullable   -- captured at post time, never recomputed (D27)
  unit_cost_source   CATALOGUE | CLIENT_DECLARED | NONE
  note
```

**No UPDATE and no DELETE, ever.** Enforced both by overriding `save()` to reject a second write
and by a Postgres trigger raising on `UPDATE`/`DELETE`. Corrections are `REVERSAL` movements
pointing at the original through `reversal_of` (`M4`).

Invariant, asserted in tests: for any node and item, inbound minus outbound movements equals the
cached balance.

### 3.3 StockBalance — the read cache

`(organization, node, item_type, owner_client, condition) -> quantity`, unique together.

Updated in the **same transaction** as movement insertion, under `select_for_update()` on the
balance row. Never recomputed lazily. A management command `verify_ledger` recomputes from
movements and reports drift; it runs nightly and alerts on any mismatch.

Available stock excludes nodes of type `QUARANTINE`, `SCRAP`, `CONSUMED`, `SITE` and `CLIENT`
(`J1`).

### 3.4 As-of-date stock (`M1`)

Computed from the ledger, not the cache:

```sql
SELECT to_node_id AS node, item_type_id, owner_client_id, condition, SUM(quantity)
  FROM stock_movement
 WHERE organization_id = %s AND occurred_at <= %s
 GROUP BY 1,2,3,4
```

...offset by the symmetric aggregation over `from_node_id`. Indexed on
`(organization_id, occurred_at)` and `(organization_id, item_type_id, occurred_at)`. At roughly 20
movements a day per tenant this is trivially fast, and `N-2`'s 100k-record target is met by the
index alone. Heavy exports run in Celery and land in S3.

### 3.5 Tracking modes

`D11`, `D12`. Three modes, defaulting from `ItemType.default_tracking_mode` and overridable on a
gate-in line.

| Mode | Identity | Movement carries |
|---|---|---|
| `SERIALIZED` | One `SerialUnit` per physical unit | `serial_unit`, quantity always 1 |
| `BULK` | None — a counted quantity | quantity |
| `REEL` | One `Reel` per numbered drum | `reel`, quantity = metres moved |

`SerialUnit` and `Reel` each carry a denormalised `current_node`, `condition` and `owner_client`
for fast lookup, always written in the same transaction as their movement. The ledger stays the
source of truth, and `verify_ledger` checks the denormalisation too.

A `Reel` has `initial_length` and `remaining_length`. Issuing decrements it; reaching zero sets
status `CLOSED` (`E3`). Over-issue is rejected at validation, with the remaining length in the
error message.

---

## 4. Data model

Only non-obvious fields are listed. Every tenant model inherits `TenantModel`; every model carries
`created_at`, `updated_at`, `created_by`.

### 4.1 Platform and settings

**Organization** — `name`, `slug` (subdomain, unique, immutable), `status` (ACTIVE/SUSPENDED),
`logo`, company details. `A1`, `A2`, `A4`.

**OrganizationSettings** — one-to-one, covering all of `C8`:
`money_tracking_enabled` (default false, `D16`), `min_stock_enabled` (false, `E6`),
`qr_labels_enabled`, `asset_tag_enabled`, `asset_tag_prefix_format`
(e.g. `SLV-{category}-{seq:06d}`), `client_waybill_enabled`, `attachments_enabled`,
`attachments_required_gate_in`, `attachments_required_gate_out`,
`signature_required_on_release`, `gate_pass_expiry_hours` (default 24, `D32`),
`allow_self_approval` (default **false**, `F3`), `allow_document_amendment` (default **false**,
`M4`), `retention_months`, `approval_escalation_hours`, `timezone` (Africa/Nairobi),
`currency` (KES), `notification_channels` JSONB, `notification_matrix` JSONB (`L1`, `L2`).

A suspended organization blocks every write via a DRF permission class while reads continue
(`A2`).

### 4.2 Identity

**User** — custom model. `email` and `phone` both nullable, each unique per organization (`B1`); at
least one must be present. `organization` is null for platform admins.

**Role** (`org`, `name`, `is_system`), **Permission** (static codename registry),
**RolePermission**, **UserRole** — `B3`, `B4`.

Permission codenames are grouped: `gate_out.request`, `gate_out.approve`, `gate_out.release`,
`gate_in.post`, `catalogue.manage`, `settings.manage`, `users.manage`, `stock.adjust`,
`disposal.approve`, `report.view_all`, `job.closeout`, `job.close_with_variance`,
`custody.transfer`.

A save/delete guard prevents removing the last user holding `users.manage` together with
`gate_out.approve` (`B4`).

**WebAuthnCredential** — `user`, `credential_id`, `public_key`, `sign_count`, `aaguid`,
`device_label`, `last_used_at`. Multiple per user, admin-revocable (`B5`).

**Delegation** — `from_user`, `to_user`, `role_or_permissions`, `starts_at`, `ends_at`, `reason`.
Permission resolution unions direct roles with active delegations, tagging the source so an
approval is recorded as "X on behalf of Y" (`F5`).

**AuditLog** — `actor`, `action`, `target_type`, `target_id`, `before`/`after` JSONB, `ip`,
`user_agent`, `auth_method`. Append-only by trigger, with no tenant-user delete path (`M3`, `B6`).

### 4.3 Catalogue

**ItemCategory** — self-FK `parent`, `criticality` (NONE/LOW/MEDIUM/HIGH). Criticality drives
approval routing (`C1`, `F3`).

**CategoryCustomField** — `category`, `label`, `field_type`, `required_at_gate_in`, `options`,
`order` (`C2`).

**ItemType** — `category`, `name`, `code`, `default_tracking_mode`, `uom`, `is_returnable`,
`default_return_days` (`I2`), `min_stock_qty`, `unit_cost` (used only when money tracking is on),
`is_archived`, `attributes` JSONB (`C3`). Archived rather than deleted once movements exist.

A data migration seeds the starter telecom catalogue for each new tenant.

### 4.4 Network

**Client** — `name`, `code`, `site_code_pattern` (optional regex, `C6`), contacts.

**Site** — `client`, `internal_ref`, `name`, `region`, `county`, `latitude`, `longitude`,
`site_type` (GREENFIELD/ROOFTOP/INDOOR/OTHER), `status` (ACTIVE/DECOMMISSIONED), plus free-text
`cell_id` and `enodeb_id` that are never validated. Decommissioned sites are retained permanently
because recoveries originate there (`C6`, `D5`).

**SiteReference** — `site`, `label`, `value`, unique on `(site, label)`. This is what makes one
physical site matchable against an operator code, a towerco code and an internal reference at the
same time. Search across references is a single indexed lookup.

**Project** (was **WorkOrder**) — `client`, `reference`, `po_number`, `manager`, `contract_value`,
`cost_budget`, `status`, M2M to sites. Still optional where it always was (`C7`, `D14`); a project
without a `po_number` is the old work order unchanged. Full shape and the commercial layer around it
in §4.14 (`D20`).

### 4.5 Locations

**Location** — `parent`, `name`, `code`, `type` (YARD/STORE/VEHICLE/QUARANTINE), `vehicle_reg`,
`is_active` (`C4`). One QUARANTINE location is auto-created per yard (`J1`).

**StockNode** — `type`, plus exactly one of `location`/`user`/`site`/`client` (nullable FKs), and a
`label`. A check constraint enforces the arity. See section 3.1.

### 4.6 Receiving

**GateIn** — `number`, `source_type` (PURCHASE / CLIENT_ISSUE / RECOVERY / RETURN_FROM_SITE /
WARRANTY_RETURN / TRANSFER), `supplier_name`, `client`, `origin_site`, `to_location`,
`received_at`, `status` (DRAFT/POSTED/VOID), `client_delivery_note_ref`, `related_gate_out` (for
returns, `H3`), `client_uuid` (idempotency, `N2`). Covers `D1`.

**GateInLine** — `item_type`, `tracking_mode` (defaulted, overridable — `D3`), `quantity`, `uom`,
`condition`, `owner_type`, `owner_client`, `custom_field_values` JSONB, `no_serial_reason`.

**GateInSerial** — `line`, `serial_number`, `asset_tag`, `source` (MANUFACTURER/INTERNAL).
**GateInReel** — `line`, `drum_number`, `length`.

Posting a `GateIn` allocates its number, creates `SerialUnit`/`Reel` rows, writes movements from an
`EXTERNAL` node and updates balances — all in one transaction. Faulty, damaged and scrap lines land
on the QUARANTINE node rather than free stock (`D2`, `J1`).

### 4.7 Dispatch

**GateOut** — `number`, `purpose_type` (INSTALLATION / MAINTENANCE / RETURN_TO_CLIENT / TRANSFER /
DISPOSAL / TOOL_ISSUE), destination as exactly one of `site`/`project`/`client`/`location`,
`custody_holder` (User, `F1`), `requested_by`, `status`, `expires_at`, `vehicle_reg`, `driver_name`
(`G2`), `released_by`, `released_at`, `version`, `supersedes` (self-FK, `F6`), `client_uuid`.

Status machine:

```
DRAFT -> PENDING_APPROVAL -> APPROVED -> PARTIALLY_RELEASED -> RELEASED -> CLOSED
  |             |                |               |
  +-> CANCELLED |                +-> EXPIRED     +-> CLOSED (with reason)
                +-> REJECTED -> (amend) -> PENDING_APPROVAL
```

Transitions live in a single `transition()` method with an explicit allowed-transition map. No view
sets `status` directly.

**GateOutLine** — `item_type`, `tracking_mode`, `requested_qty`, `released_qty`, `owner_client`,
`expected_return_date`, `is_returnable`, `no_serial_reason`. Plus `GateOutLineSerial` (FK to
`SerialUnit`) and `GateOutLineReel` (`reel`, `length_requested`, `length_released`).

`no_serial_reason` mirrors the gate-in field of the same name, and for the same reason (D3): a
serialized item may only leave as an anonymous quantity when the yard genuinely holds untagged
units, and then the explanation is part of the record. Added after a released pass put an antenna
into custody with no identity — nothing downstream could say which unit it was, and the gate had
nothing to check the physical load against (G1). A serialized line otherwise names exactly as many
units as it requests.

`released_qty < requested_qty` leaves the document `PARTIALLY_RELEASED` (`F7`).

**ReleaseVariance** — `gate_out_line`, `approved_qty`, `released_qty`, `reason`,
`acknowledged_by`, `acknowledged_at`. Created when the physical load differs from the approved
list. It blocks nothing at the gate, but stays on the exceptions register until acknowledged
(`G1`).

### 4.8 Approvals

**ApprovalRule** — `category` (nullable = all), `criticality`, `required_role`, `sequence`,
`conditions` JSONB (**empty in v1**). The predicate evaluator reads `conditions`, so
client-ownership, quantity and monetary dimensions can be switched on later with no migration —
`F3`'s explicit requirement.

**ApprovalRequest** — generic `document_type`/`document_id`, `level`, `required_role`, `status`,
`due_at`, `escalated_at`.

**ApprovalAction** — `approval_request`, `actor`, `on_behalf_of`, `decision`, `reason`,
`auth_method` (PASSWORD/WEBAUTHN), `webauthn_credential`, `ip`, `user_agent`. This row is the
non-repudiation evidence an ISO auditor asks for (`F4`, `M3`).

### 4.9 Jobs and reconciliation

**Job** — `client`, `site`, `project`, `assignee`, `status`, `closed_by` (`H1`), plus
`delivery_mode`, `subcontractor` and `agreed_price` for work given to a contractor (§4.14, `O3`).

**JobCloseout** — `job`, `submitted_by`, `status` (SUBMITTED/CONFIRMED), `notes` (`H2`).

**JobCloseoutLine** — `action` (INSTALLED / CONSUMED / RETURNING / RECOVERED), `item_type`,
`quantity`, `serial_unit`, `reel`, `length`.

Submitting a closeout immediately posts movements for INSTALLED (to the SITE node) and CONSUMED (to
the CONSUMED node), and creates **expectations** for RETURNING and RECOVERED lines. The
storekeeper's gate-in then matches against those expectations, and any difference creates a
`Variance` (`H3`).

Where the job belongs to a project, the confirmed closeout then goes to the PM for **cost
acceptance** (`O8`). The postings above do **not** wait for it: stock moves when the storekeeper
confirms, exactly as before, and the PM's step accepts what lands on their budget. A ledger that
waited for a financial signature would stop being a record of what happened, and the yard's figures
would lag the yard by however long the PM took. A PM who disagrees rejects the closeout, which asks
for a corrected one; anything already posted in error is undone by a `REVERSAL` (`M4`), not by having
been withheld.

A confirmed closeout also writes `JobLabour` rows from the days captured on it (§4.14, `O15`).

**Variance** — `type` (RETURN/RELEASE/COUNT), related FKs, `expected`, `actual`, `reason`,
`status`, `approval_request`. Drives the exceptions register in `M1`.

A job cannot reach CLOSED while expectations are open, unless a user holding
`job.close_with_variance` closes it with a reason (`H5`).

### 4.10 Custody

Custody balance is simply `StockBalance` at the holder's PERSON node (`I1`) — no separate ledger.

**CustodyExpectation** — `holder`, `gate_out_line`, `item_type`, `serial_unit`/`reel`, `quantity`,
`expected_return_date`, `status` (OPEN/RETURNED/OVERDUE/WRITTEN_OFF). Created on release for
returnable lines (`I2`).

A nightly Celery beat task flags overdue expectations and fires escalating notifications:
holder → storekeeper → owner (`I3`, `D35`). No supervisor role exists, so this is
the chain.

**CustodyTransfer** — `from_holder`, `to_holder`, lines, `acknowledged_at`. Movements post only on
acknowledgement (`I5`).

### 4.11 Disposition and disposal

**Disposition** — `number`, `status`, source quarantine location, `decision` (REPAIR /
RESTORE_TO_SERVICEABLE / RETURN_TO_CLIENT / SCRAP), `reason` (mandatory), `to_location` for a
restore, `vendor_name` for a repair, lines (`J2`).

Only two of the four decisions move anything when the disposition posts. RESTORE_TO_SERVICEABLE
goes to a yard location **and changes condition** — that change is what returns it to availability,
since `stock_on_hand` excludes quarantine locations rather than faulty conditions. REPAIR goes to
the vendor's EXTERNAL node: out of the yard, still ours. RETURN_TO_CLIENT and SCRAP move nothing —
the material leaves on its own document (a gate-out, or an approved Disposal), because deciding
something is scrap and destroying it are two decisions taken at different times, often by different
people.

**Disposal** — `number`, lines, `method`, `approval_request`, `disposed_at`, attachments. Disposal
of client-owned material always requires approval regardless of category criticality — a hardcoded
rule, deliberately not configurable (`J3`).

### 4.12 Client returns

A return to client is a `GateOut` with `purpose_type = RETURN_TO_CLIENT`, moving stock to the
CLIENT node (`K1`). Material sits at that node as "in transit" — still the tenant's exposure.

**ClientReturnAck** — `gate_out`, `acknowledged_ref`, `acknowledged_at`, `acknowledged_by_name`,
`recorded_by`, attachments via `/attachments`. Recording it flips the material to acknowledged,
ending liability on the record (`K3`). A beat task notifies on returns still unacknowledged after
N days (`L2`) — `core.sweeps`, run from `CELERY_BEAT_SCHEDULE`.

**The three states are computed, not stored.** `GET /client-position` splits a client's material
into HELD, IN_TRANSIT and ACKNOWLEDGED. The first comes from balances at our own nodes; the other
two come from *movements into the CLIENT node grouped by the gate-out that caused them*, because a
balance at that node knows how much is there but not which return put it there — and
acknowledgement is a property of the return. Deriving it means there is no second truth about
liability to keep in step.

### 4.13 Counts, attachments, numbering

**StockCount** / **StockCountLine** — `expected_qty`, `counted_qty`, `variance`, `reason`. Posting
writes `ADJUST` movements. Adjustments touching client-owned stock always require approval (`E5`).

**Attachment** — generic owner, `s3_key`, `filename`, `content_type`, `size`, `kind`
(PHOTO/DOCUMENT/SIGNATURE), `uploaded_by`. Direct-to-S3 upload via pre-signed POST; reads via
short-lived pre-signed GET (`D6`, `G3`, `N-7`).

**DocumentSequence** — `(organization, doc_type) -> next_number`. Allocated under
`select_for_update()` **at posting, never at draft creation**, so abandoned drafts leave no gaps.
Voided documents keep their number and are marked void; numbers are never reused (`M6`).

---

### 4.14 Projects and the commercial layer

Epic O. Everything here answers *did the PO make money*, and none of it is allowed to contradict
§3 — so **no figure in this section is stored as a number a person can edit** (`D23`). Cost is a
query over the ledger, the labour entries and the approved expenses.

**Project** — this is `WorkOrder`, renamed and extended (`D20`). One grouping, not two: a project
with no `po_number` is exactly the optional work order that existed before, and nothing that worked
without one stops working.

```
Project  (was WorkOrder)
  id, organization, client
  reference           the tenant's own reference, as WorkOrder had
  po_number           nullable; unique per organization when set
  title, description
  manager             FK User, nullable        -- the PM (O1, O6)
  contract_value      Decimal(14,2) nullable   -- VAT-exclusive (D24)
  cost_budget         Decimal(14,2) nullable   -- what the PM works to, not what the client pays
  starts_on, target_completion_on
  status              OPEN | CLOSED | CANCELLED
  sites               M2M Site
  closed_at, closed_by, closed_with_unreconciled, close_reason
```

`po_number` is separate from `reference` rather than overloading it, because the constraint keys on
it: `a_po_project_is_fully_specified` — `po_number IS NULL OR (manager_id IS NOT NULL AND
contract_value IS NOT NULL AND cost_budget IS NOT NULL)`. A PO without a manager is a PO nobody can
release material against, and it should be refused at creation rather than discovered at the gate.

**Migration.** `RenameModel` WorkOrder → Project, `RenameField` on `Job.work_order` and
`GateOut.work_order`, then drop and recreate `gate_out_has_exactly_one_destination` because it names
the column. Existing rows become projects with no `po_number`, no value and no PM (`D20`), and
continue to route through the criticality rules because they are not project material.

**ProjectVariation** — `project`, `reference`, `description`, `value_delta`, `budget_delta`,
`effective_on`, `raised_by`, `status`, `decided_by`, `decided_at`, `decision_reason`. Deltas may be
negative. Current contract value is `contract_value + sum(approved value_delta)`; the original column
is never written again (`D21`).

`status` (PENDING/APPROVED/REJECTED) rather than a bare `approved_at`, because a rejection has to be
recorded somewhere and deleting the row is not available — the model is append-only once decided,
for the reason §3.2 is. A **pending** variation may still be corrected; nothing has been agreed yet,
so nothing is being rewritten. The guard reads the status the row was loaded with rather than
re-querying, which also keeps `all_objects` out of a model method (§2.1).

**Subcontractor** — `organization`, `name`, `code`, contacts, `is_active`. Unique name per tenant,
`PROTECT` on delete once referenced (`O4`). This is a register of contractors who do work, distinct
from the free-text `supplier_name` on gate-in, which stays as it is.

**Job** gains `project` (the renamed FK), `delivery_mode` (`IN_HOUSE` | `SUBCONTRACTED`),
`subcontractor` and `agreed_price`. A check constraint pairs them: `SUBCONTRACTED` requires both,
`IN_HOUSE` permits neither. Delivery mode and price are immutable once `status = CLOSED` — changing
either would rewrite a cost already counted (`O3`).

**JobLabour** — `job`, `closeout`, `person`, `work_date`, `days` Decimal(4,1), `day_rate`,
`rate_source` (`USER` | `ROLE` | `NONE`), `overlaps_day`, unique on `(job, person, work_date)`.

Written when the closeout is **submitted**, not confirmed. Two reasons, found while building it:
`CloseoutStatus.CONFIRMED` exists on the model but no service ever sets it — H3's confirmation
happens through gate-in return matching — so "on confirmation" had no hook to hang from. And the
days are the technician's report about their own week, not a fact about the returns a storekeeper
checks in; waiting for the storekeeper would not make them truer.

Rate resolution is `User.day_rate` falling back to the **highest** `Role.day_rate` among the roles
they hold, **captured onto the row** at write time (`D27`). Highest rather than first-found, because
someone holding both Technician and Supervisor should not be costed differently depending on which
row happened to be created first. Where neither exists the row is written with a null rate and `rate_source =
NONE`: the job then appears in §10 as *uncosted labour*, which is honest, where a zero would silently
flatter the project.

The one-day check is a query, not a constraint: on submission, sum `days` for that person and date
across every job. Over 1.0 sets `overlaps_day = True` on the row and warns (`O15`) — it does not
refuse, because a late closeout would otherwise be blocked by an earlier one. Storing the flag means
the owner's report finds overlaps with an index rather than recomputing the sum over all history.

**ExpenseCategory** — tenant-configurable, seeded with transport, fuel, equipment hire, wayleaves
and permits, accommodation, other.

**ProjectExpense** — `project`, `job` nullable, `category`, `amount`, `incurred_on`, `description`,
`recorded_by`, `attachment`, `status` (`SUBMITTED` | `APPROVED` | `REJECTED`), `approved_by`,
`approved_at`, `reason`, `reverses` self-FK. Anyone may record; the PM approves; it reaches cost only
on approval (`D29`, `O16`). Once approved the row is append-only and a mistake is corrected by a
reversing entry, the same discipline as the ledger. An expense with no attachment is accepted but
flagged to the PM as unevidenced.

**ProjectSnapshot** — `project`, `taken_at`, `figures` JSONB. Written when the project closes, so a
closed project reports what it reported that day even if a later reversal moves the ledger beneath
it (`O13`).

#### Where each figure comes from

| Figure | Source |
|---|---|
| Material cost | `unit_cost × quantity` over `INSTALL` and `CONSUME` movements whose document resolves to a job on the project |
| Material loss | `CustodyExpectation` rows still open on **closed** jobs, at the captured unit cost |
| Subcontractor | `Job.agreed_price` over closed subcontracted jobs |
| Labour | `JobLabour.days × day_rate` |
| Expenses | approved `ProjectExpense.amount`, net of reversals |
| **Exposure** | balance at `PERSON` nodes for material issued against the project — reported, **never** counted as cost (`O11`) |

Material loss being a *query over open expectations* rather than a posted write-off has a property
worth naming: if the kit turns up two months later and is booked back in, the expectation closes and
the loss disappears on its own. A typed write-off would have to be found and reversed by hand, and
in practice would not be.

#### Valuation on the movement

§3.2 gains `unit_cost` Decimal(14,2) nullable and `unit_cost_source` (`CATALOGUE` |
`CLIENT_DECLARED` | `NONE`), captured when the movement posts and never recomputed (`D27`).

Own material takes `ItemType.unit_cost`. Client-owned material takes the value declared on the
gate-in that brought it in, because the figure that matters for a shortfall is what the operator
will debit, not what the item would cost us (`O11`). `NONE` where neither exists — §10 then reports
the project as partly unvalued instead of stating a confident understatement.

Client-owned material therefore carries a valuation on every movement but contributes **nothing** to
cost while it behaves. It reaches the P&L only through the material-loss query above.

### 4.15 Boxes (Epic P)

> **Status: approved 2026-10-03**, together with stories P9–P11.

#### 4.15.1 The decision: a box groups stock, it is not stock

The ledger (§3) stays exactly as it is. A box adds no quantity, no balance key and no movement
type. `StockBalance` still counts a lot at a node; `SerialUnit` still knows where it is. A box is a
**projection** kept beside the ledger: which units, and which slice of a bulk lot, are currently
grouped under one code. It is written in the same transaction as the movements that change it, and
`verify_ledger` checks it against them, so if they ever disagree the ledger wins and the drift is
reported — the same contract the denormalised `SerialUnit.current_node` already has.

The alternative, a box dimension on the ledger, was rejected. It would touch every balance query,
every report and valuation, and turn "take a unit out of a carton on the shelf" into a stock
movement with nothing physically moving (P7 says it is not one).

**Boxes live inside the perimeter** (`YARD`, `STORE`, `QUARANTINE`). What goes out the gate goes as
units and quantities, never as a box. A unit leaving the box's node leaves the box; a box left with
nothing in it closes (P5). This is how P5 and P6 both hold without a box ever sitting at a site.

#### 4.15.2 Data model (`stock` app)

| Model | Fields | Notes |
|---|---|---|
| **`Box`** | `code`, `source` (LABEL / INTERNAL), `parent` → Box (null), `depth` (1–3), `current_node` → StockNode, `status` (OPEN / CLOSED), `gate_in` → GateIn (null), `label_text` (raw scan, for audit), `closed_at` | `TenantModel` + `TimeStampedModel`. `code` unique per org, case-insensitive, **including closed boxes** (P5: a closed box cannot be reused). `depth` = parent's + 1, check `1 ≤ depth ≤ 3` (P10). |
| **`SerialUnit.box`** | → Box (null, `PROTECT`) | The box the unit is in **now**. Indexed. |
| **`BoxBulkContent`** | `box`, `item_type`, `owner_client` (null), `condition`, `quantity` (> 0) | A claim on the lot at `box.current_node` (P9). Unique per (box, item, owner, condition). Deleted when it reaches zero. |
| **`BoxEvent`** | `box`, `action`, `serial_unit` (null), `child_box` (null), `item_type` / `owner_client` / `condition` / `quantity` (null), `document_type` / `document_id` / `document_number` (blank), `actor`, `occurred_at`, `note` | Append-only (`make_append_only`). Actions: CREATED, UNIT_IN, UNIT_OUT, BULK_IN, BULK_OUT, BOX_IN, BOX_OUT, MOVED, CLOSED. This is P8's trail and the source of a unit's box history (P4). |

Every new table gets `enable_rls(...)` in its own migration and an isolation fixture in
`stock/isolation.py` (§2; `core.E001` and the A3 suite fail otherwise). Box codes generated for
unlabelled boxes (P1) use `allocate_number(DocumentType.BOX)` with prefix `BX`, so they appear in the
number-series admin like every other series.

**Loose bulk** at a node, for a lot, is `balance − Σ claims of open boxes at that node for that lot`.
It is computed, never stored.

#### 4.15.3 Ledger hooks — one place, every path (P5, P7, P9)

`post_movement` (§3) gains two optional fields on `MovementRequest`, and three rules run inside its
existing transaction after the balances are locked. Putting them here, not in each caller, is what
makes custody, closeouts, disposition, counts and gate-out all obey the box rules without each of
them knowing boxes exist.

- `from_box: Box | None` — the box a bulk quantity is drawn from.
- `moving_box: Box | None` — set only by `move_box`, meaning "this movement carries the box intact".

1. **A unit that moves leaves its box**, unless `moving_box` is its box or an ancestor of it. The
   unit's `box` is cleared and a UNIT_OUT event is written with the movement's document refs.
2. **Bulk out of a node:** with `from_box`, the box's claim must cover the quantity and is reduced
   (BULK_OUT). Without it, the movement draws on loose stock. If loose stock is short:
   - for ISSUE, TRANSFER, INSTALL, CONSUME, RETURN, QUARANTINE, DISPOSE it is refused with
     `BoxedStockOnly`, naming the boxes that hold the rest (P9);
   - for ADJUST and REVERSAL, which are corrections, the shortfall is taken from the boxes at that
     node in code order, each reduction written as a BULK_OUT event (P9 edge case).
3. **Empty boxes close.** After any change, a box with no units, no claims and no open child boxes
   is closed (CLOSED event), and the check climbs to its parent (P5, P10).

`move_box(box, to_location, *, actor, request)` (P7 edge case, E4) locks the box subtree, posts one
TRANSFER per unit and per claim with `moving_box` set, then updates `current_node` across the
subtree and writes MOVED. Both ends must be inside the perimeter, as `transfer_stock` requires.

`post_movement` also gains the check it lacks today: a serialized movement's `from_node` must be the
unit's `current_node`. Box rule 1 depends on that being true.

#### 4.15.4 Box services (`stock/boxes.py`)

| Function | Does | Story |
|---|---|---|
| `create_box(code, *, node, parent, gate_in, label_text, actor)` | Validates code (in use, including closed → `BoxCodeInUse` naming where it sits), depth, parent at the same node; generates a code when blank | P1, P10 |
| `put_units / put_bulk / put_box` | Adds contents; units and child boxes must be at the box's node; bulk claim must fit loose stock | P1, P9, P10 |
| `take_out(box, *, units, bulk, boxes, actor)` | Removes contents without moving them; writes events and an audit record | P7 |
| `empty_box(box, *, actor)` | Takes everything out, closes | P7 |
| `move_box(...)` | §4.15.3 | P7, E4 |
| `issuable_contents(box, *, from_node)` | Expands the subtree into proposed gate-out lines, plus exclusions with reasons: already on an open pass, quarantined, held by a person, not at this location | P6, P9, P10 |
| `box_tree(box)` | Contents at every level with counts received vs now | P4 |

Every function locks the box rows it touches (`select_for_update`, parent before child) and refuses
a cycle (`BoxCycle`). They are the only writers of `Box`, `BoxBulkContent` and `SerialUnit.box`.

#### 4.15.5 Gate-in (P1, P2, P9, P10)

The draft gains boxes, so a half-received pallet survives a reload and an offline queue (§8):

- `GateInBox`: `gate_in`, `key` (client-made, stable across edits), `code`, `parent_key`, `source`,
  `label_text`.
- `GateInSerial.box_key` and `GateInLine.box_key` (bulk lines: the whole line's quantity is in that
  box; two boxes of the same item are two lines). Keys, not FKs, because `GateInSerializer.update`
  rewrites lines wholesale today and a key survives that unchanged.

`validate_for_posting` adds: codes unique within the document and the tenant; parents resolve and
form a tree no deeper than three; every box holds something, directly or below (`BoxEmpty`); and
everything in one box lands on **one node** — a box mixing a serviceable and a quarantined line is
refused (`BoxMixedDestinations`), because a box's contents always sit where the box does.

`post_gate_in` creates the units and drums as today, then creates the boxes top-down at the
destination node and puts the contents in, all in its transaction. `void_gate_in` empties and closes
its boxes before posting the reversals.

The offline replay (`sync._apply_gate_in`) needs no change: it already runs the same serializer, and
the queue stores the payload verbatim (§8).

#### 4.15.6 Reading a label (P2, P3)

One reader, written twice with **shared test vectors** (`backend/stock/tests/data/label_vectors.json`,
used by pytest and by the frontend unit tests), so the phone and the server never disagree about
what a label says.

`read_label(raw) → { serials: string[], box_code?: string, document_token?: string, raw }`, tried in
order:

1. A gate-pass address (`…/qr/scan?token=…`) → `document_token`.
2. JSON → `serial` / `serialNumber` / `sn`; a `serials` array; `box` / `carton` / `pallet` code.
3. GS1 → AI 21 (serial), AI 00 (SSCC, used as the box code), split on the group separator.
4. A web address → `serial` / `sn` / `s` parameter, else the last path segment.
5. Labelled text → `SN:` / `S/N` / `Serial:` values, one or many.
6. A list → two or more tokens on separate lines, or separated by commas or semicolons.
7. Otherwise → the raw value, trimmed, as one serial. **A label is never discarded** (P3).

`find_by_identifier` tries the raw value first, then each candidate, and gains `kind: "box"`, so the
stock lookup, gate-out and release all resolve boxes through the one endpoint.

#### 4.15.7 Gate-out (P5, P6, P9, P10)

- `GateOutLine.box` → Box (null): the box the line was picked from. Grouping on the request, the
  approval and the gate pass; for a bulk line, the claim `_release_line` draws on (`from_box`).
- Scanning a box calls `GET /stock/boxes/{code}/issuable?from_location=…`, and the screen adds the
  proposal: **one line per item and lot within each box, each unit named**, with the exclusions
  shown and their reasons. This is how loose multi-unit lines already work, and keeps a carton of
  ten RRUs to one line that lists ten serials, rather than ten lines. *This refines P6's "one line
  per unit"; each unit is still named and released individually.*
- `submit_gate_out` gains the checks it lacks today: each named unit is at the from-location,
  IN_STOCK and not on another open pass (`UnitNotAvailable`, naming the pass that holds it); a box
  line's claim covers its quantity.
- After approval the pass covers units and quantities, not the box (P6). The gate-pass PDF groups
  lines under their box code.

#### 4.15.8 Scan-to-release (P11, G1, G5)

**Opening a pass by scan.** A "Scan a pass" action on the gate-out list and the offline release
page opens `/gate-out/scan`. Online, the token goes to `GET /qr/scan` and the screen opens
`/gate-out/{id}?release=1`, which opens the release sheet. A pass that cannot be released says why
(not approved, expired, released) instead. Offline, the token's payload is read without verifying
the signature and matched against the cached releasable passes; the release is still validated by
the server when it syncs.

**Ticking the load.** Matching runs **on the device** in one pure function,
`matchScan(pass, scannedText) → { ticks, refused }`, using the pass's own data. The gate-out detail
and the offline releasable bundle both carry, per named unit, its serial number, asset tag and box
path, and per box line its box path. So:

- a unit's serial or asset tag ticks that unit;
- a box or pallet code ticks every unit and bulk line on the pass whose box path contains it;
- anything else is refused on screen, naming what was scanned and that the pass does not cover it.

Because it needs no server call, it works the same at a gate with no signal. Every line can still be
confirmed by hand.

The contract both read sides honour (gate-out detail and the releasable bundle): each line carries
`box_path: string[]`, the box codes from outermost to the box it was picked from (empty when
loose); each entry of `line.serials` carries `id`, `serial_unit`, `serial_number`, `asset_tag`,
`released` and its own `box_path` (the unit's current box chain, outermost to innermost, empty when loose). Codes compare
case-insensitively, as the database does.

**Releasing what was ticked.** `release_gate_out` gains `released_serials: {line_id: [unit_id, …]}`.
For a line that has it, exactly those units are issued, ending today's behaviour of issuing the
first N unreleased units whoever was actually loaded. Whatever is unticked or unconfirmed is short,
and a release variance, as G1 already requires. `OrganizationSettings.release_scan_required`
(default off) makes `released_serials` mandatory for serialized lines (`ScanRequiredForRelease`).
The offline release payload carries the same field and `_apply_gate_out_release` passes it on.

#### 4.15.9 API

| Endpoint | Purpose | Permission |
|---|---|---|
| `GET /boxes`, `GET /boxes/{code}` | List and detail with tree, counts, units, bulk | as `/stock` |
| `GET /boxes/{code}/history` | BoxEvents | as `/stock` |
| `GET /stock/boxes/{code}/issuable?from_location=` | Gate-out proposal and exclusions | `gate_out.request` |
| `POST /boxes/{code}/take-out`, `/empty`, `/move` | P7, E4 | `stock.adjust` or `gate_in.post`, as transfers |
| `GET /stock/lookup` | adds `kind: "box"` | unchanged |
| `POST /gate-outs/{id}/release` | adds `released_serials` | unchanged |
| `GateIn` and `GateOut` serializers | add `boxes` / `box_key` / `box`, and per-serial box paths on read | unchanged |

`api-schema.yml` is regenerated with the change; CI's drift test enforces it.

#### 4.15.10 Frontend

| Piece | Where | Story |
|---|---|---|
| `readLabel`, `matchScan` (pure) | `src/features/boxes/` | P2, P3, P11 |
| Gate-in: "Into a box" on the line sheet — choose an open box from the draft or start one by scanning its code; a label listing serials offers them for confirmation; the lines card shows the box tree with counts | `GateInCapturePage` | P1, P2, P9, P10 |
| Gate-out: lookup handles `kind: "box"`, shows the proposal and exclusions, "Add all"; a single unit shows which box it is in | `GateOutRequestPage` | P5, P6, P9, P10 |
| Release: "Scan the load", a continuous scanner ticking lines and units, refusals listed, short lines derived from what is unticked; shared by the online and offline release sheets | `GateOutPages`, `OfflineReleasePage` | P11 |
| `/gate-out/scan` | new route | P11, G5 |
| `/stock/boxes`, `/stock/boxes/:code` — tree, history, take out, empty, move | `StockPages` | P4, P7, P8 |
| Serial history shows the unit's box and its box events | `SerialHistoryPage` | P4 |
| `BarcodeScanner` shows "Scanned X from the label" with the raw text on a tap; its manual input stops hard-coding `id`, since release and gate-in can now mount two scanners | `BarcodeScanner` | P3 |
| Settings: "Require every unit to be scanned at release" | Settings | P11 |

Draft state for boxes lives in `Draft` (localStorage), not in the line sheet, so a half-built box
survives a reload. The IndexedDB schema does not change: queued payloads are stored verbatim.

#### 4.15.11 Errors

All through the existing `{error: {code, message, field_errors, details}}` envelope (§13), each
message naming the thing involved. Codes are upper case, as every `DomainError` here already is.
`UNIT_NOT_AT_ORIGIN` (T11.3) joins them: a serialized movement asked to start where the unit is not.

| Code | When |
|---|---|
| `BOX_CODE_IN_USE` | A code already used in the tenant, open or closed; names where it sits |
| `BOX_TOO_DEEP`, `BOX_CYCLE` | Nesting beyond three, or a box inside itself |
| `BOX_EMPTY`, `BOX_MIXED_DESTINATIONS` | Gate-in validation |
| `BOXED_STOCK_ONLY` | Bulk drawn without naming a box, loose stock short; names the boxes |
| `UNIT_NOT_AVAILABLE` | Submit: a named unit is elsewhere or on another open pass; names it |
| `SCAN_REQUIRED_FOR_RELEASE` | Release without scans when the setting is on |

#### 4.15.12 Testing

- **Backend (pytest):** the three ledger hooks, including a unit issued from a box, a pallet emptied
  by one release closing up its tree, ADJUST reducing claims and ISSUE refused; `move_box`; gate-in
  with nested, mixed and generated-code boxes and every validation; void; `issuable_contents`
  exclusions; submit's new checks; release by named serials and the require-scan setting; the label
  vectors through `find_by_identifier`; `verify_ledger`'s new checks; sync replay of a gate-in with
  boxes and a release with `released_serials`; RLS and the isolation suite for the new tables.
- **`verify_ledger` gains:** units in a box sit at the box's node and the box is open; claims never
  exceed the balance; trees are acyclic, at most three deep, and children sit with their parents.
- **Frontend unit tests:** `readLabel` against the shared vectors and `matchScan` against fixture
  passes. This adds Vitest as a dev dependency; the project has no unit runner today.
- **E2E (Playwright, phone):** receive a box of three units by manual entry; scan the box at
  gate-out and see one line with three serials; release by scanning two units and one stranger, see
  the stranger refused and the third unit short. As with every E2E here, CI does not run these; they
  run against a seeded tenant.

### 4.16 Site earmarks (Epic Q)

> **Status: approved 2026-10-03** with Epic Q.

#### 4.16.1 The decision: an earmark is a projection, like a box

The ledger is unchanged. An earmark says which site a piece of stock is meant for; it adds no
quantity and no movement type. It is kept beside the ledger, written in the same transaction as the
movements that change it, and checked by `verify_ledger` — the contract boxes already have (§4.15.1).

#### 4.16.2 Data model

| Model / field | Shape | Notes |
|---|---|---|
| `SerialUnit.earmark_site` | → Site (null) | Travels with the unit. |
| `Reel.earmark_site` | → Site (null) | A drum is earmarked whole. |
| `BulkEarmark` | `site`, `node`, `item_type`, `owner_client`, `condition`, `quantity` (> 0) | A claim on the lot at `node`, unique per (site, node, lot); deleted at zero. Mirrors `BoxBulkContent`. |
| `EarmarkEvent` | `action`, `site`, `to_site` (null), subject (`serial_unit` / `reel` / bulk lot + `quantity`), document refs, `actor`, `occurred_at`, `reason` | Append-only. Actions: EARMARKED, CHANGED, CLEARED, DELIVERED, DIVERTED, MOVED, REDUCED. The report reads from it. |
| `GateIn.for_site`, `GateInLine.for_site` | → Site (null) | The delivery's default and each line's own. |
| `GateOutLine.divert_reason` | text | Required when the line uses another site's earmark. |

RLS migrations and isolation fixtures as every tenant table (§2). **Free** bulk at a node, for a lot,
is `balance − Σ BulkEarmark` there.

#### 4.16.3 Ledger hook — `post_movement` (Q2, Q3, edge cases)

`MovementRequest` gains `for_sites: frozenset[Site] | None` (the sites the movement is delivering
to; empty = none) and `divert_reason: str = ""`. Rules, run beside the box rules (§4.15.3):

- **A unit or a drum that leaves the perimeter** (ISSUE, INSTALL, CONSUME, DISPOSE, RETURN to a
  client): if it is earmarked and its site is in `for_sites`, the earmark is cleared as DELIVERED;
  if not, the movement must carry `divert_reason` (else `EARMARK_DIVERSION_NEEDS_REASON`), and it is
  cleared as DIVERTED. Moving inside the perimeter keeps the earmark (MOVED event, node only).
- **Bulk out of a node:** drawn in order — earmarks of a site in `for_sites`, then free, then other
  sites' earmarks (by site name). Drawing another site's needs `divert_reason` (else
  `EARMARK_DIVERSION_NEEDS_REASON`, naming the sites). For a movement inside the perimeter
  (TRANSFER, QUARANTINE, RESTORE) the drawn earmarks move with it to the destination node (MOVED),
  never treated as diversions. ADJUST and REVERSAL take free first, then reduce earmarks (REDUCED),
  as corrections do for boxes.
- Every reduction writes its `EarmarkEvent` with the movement's document refs.

#### 4.16.4 Gate-in (Q1)

`for_site` on the header and lines (a line inherits the header's when blank). Posting earmarks the
units and drums it creates and adds a `BulkEarmark` at the destination node for bulk lines, each with
an EARMARKED event. The offline replay needs nothing new: the payload carries the fields.

#### 4.16.5 Gate-out (Q3)

- `destination_sites(gate_out)`: the pass's site; else its job's site; for a project destination,
  the project's sites; for a person with no job, a client, or another location, none. (Another
  location is a transfer inside the perimeter: earmarks move, nothing is diverted.)
- **Submit** works out, line by line, what release would draw, with the rules above: a named unit
  earmarked for a site not in the destination set, a drum likewise, or a bulk quantity that cannot be
  met from own earmarks plus free stock, is a diversion; without `divert_reason` the line is refused
  (`EARMARK_DIVERSION_NEEDS_REASON`, naming the site and quantity). The detail and approval payloads
  carry, per line, `diversions: [{site, quantity or serials}]` and `divert_reason`.
- **Release** passes `for_sites` and the line's `divert_reason` to every movement.

#### 4.16.6 Changing an earmark (Q4)

`POST /stock/earmarks/change` with a subject (a unit, a drum, or a bulk lot at a node with a
quantity and its current site or "free"), the new site or none, and a reason (required). Permission
`stock.adjust` or `gate_in.post`. Writes CHANGED or CLEARED.

#### 4.16.7 Reads

- Units and drums carry `earmark_site` and its name; `GET /stock` rows carry `earmarked:
  [{site, name, quantity}]` and `free` (one subquery, not per row).
- `issuable_contents` (boxes, §4.15.4) and the gate-out holdings list say what is earmarked, so the
  screen can warn before submit.

#### 4.16.8 Report — "Material by site" (Q5)

Registered with the report framework in the "Stock" group. Parameters: period, client, project
(its sites), site. One row per site and item: received for (EARMARKED + CHANGED-to in the period),
sent to (DELIVERED, plus released gate-out lines to that site that used free stock), still in the
yard (current earmarks), diverted away (DIVERTED). Quantities in the item's unit; units counted.
Exports through the existing Excel and PDF paths.

#### 4.16.8a Site-first gate-out (Q6)

- `GET /stock/earmarked?site=<id>&from_location=<id>` returns what is earmarked for the site at
  that location, in the proposal shape `issuable_contents` uses (§4.15.4): one line per item and lot,
  units and drums named, bulk with its earmarked quantity; plus the site's open jobs with their
  projects. Permission `gate_out.request`.
- The request screen drops "A project" from "Where it is going". On choosing a site it calls the
  endpoint and shows the list ticked; Confirm turns ticked rows into lines through the same builder
  the box proposal uses. The job picker appears when the open jobs span more than one project.
- The backend still accepts a project destination, so passes raised before keep their meaning.

#### 4.16.9 Errors

`EARMARK_DIVERSION_NEEDS_REASON` (409) — names the site, item and quantity, and says to give a reason
or take free stock. `EARMARK_CHANGE_INVALID` (400) — the subject is not earmarked as stated, or the
quantity exceeds the earmark.

#### 4.16.10 Testing

Backend: the hook rules (deliver, divert with and without a reason, bulk draw order, transfers carry,
corrections reduce), gate-in earmarking, submit's diversion detection and the approval payload,
release consuming earmarks, the change endpoint, the report's four columns on a scenario,
`verify_ledger` (Σ earmarks ≤ balance; earmarked units inside the perimeter), RLS and isolation.
Frontend: unit tests for the split text and diversion detection. E2E (phone): receive two RRUs for
site X; request one to site X (no reason asked) and one to site Y (reason asked, then sent); the
report shows site X received 2, sent 1, diverted 1.

---

### 4.17 Finance — stage 1, money out (Epic R)

> **Status: approved 2026-10-09.**

#### 4.17.1 The decision: extend the expense, add the request, reuse the approval tables

- **`ProjectExpense` is extended, not replaced** (R1). It gains a site, a scope of work, fuel fields,
  casual lines, a float link and a payment record, and its status set widens. It remains the only
  source of expense cost (`commercials/costing.py::expense_cost`).
- **`AllowanceRequest` is the one new entry kind** (R2): money asked for before it is spent. A float
  is an `AllowanceRequest` of type FLOAT, and expenses point at it through
  `ProjectExpense.float_request`.
- **Approval moves into `approvals/`** (R4). Today O16 bypasses the engine
  (`commercials.services.decide_expense` checks `project.manager_id` and flips a status). Two levels
  would otherwise be a second approval implementation, so both entry kinds route through
  `approvals.engine` (§5), reusing `ApprovalRequest` and the append-only `ApprovalAction`. The
  entry's `status` is a projection of its requests, as a gate-out's is.

D29 (PM only) is amended for these entries; D28 holds: there is no fallback approver.

#### 4.17.2 Data model

New tables are tenant tables: `enable_rls` in a `commercials` migration and fixtures in
`commercials/isolation.py` (§2, A3).

| Model / field | Shape | Notes |
|---|---|---|
| `ExpenseStatus` (changed) | `PENDING_PM`, `PENDING_FINANCE`, `APPROVED`, `PAID`, `REJECTED` | Replaces `SUBMITTED`; shared by `AllowanceRequest`. Reaches cost on `APPROVED`, stays on `PAID`. |
| `ExpenseCategory.kind` (new) | `GENERAL` / `FUEL` / `CASUAL_LABOUR` | What a category demands, so renaming keeps behaviour. Seeds add Fuel, Team allowance, Transport, Casual labour. |
| `ProjectExpense.site` | → Site, null | R1. Optional only when `project` is given directly (a permit for the PO). |
| `ProjectExpense.scope_of_work` | text | R1. |
| `ProjectExpense.vehicle_reg`, `litres` | char; decimal(10,2) null | Reg required when `kind=FUEL`; litres optional. |
| `ProjectExpense.float_request` | → AllowanceRequest, null | Same person, FLOAT, PAID, open. |
| `ProjectExpense.photos_expected` | small int | Photos the phone will send; "arriving" vs "no evidence" (4.17.8). |
| `ProjectExpense.client_uuid` | uuid, null, unique per org | Offline idempotency, as `GateIn.client_uuid`. |
| `ProjectExpense.paid_at`, `paid_by`, `payment_reference` | | Not set on a float-backed expense (already paid from the float). |
| `ExpenseCasualLine` | `expense`, `casual`, `days` (> 0), `amount` (null) | Required for `CASUAL_LABOUR`. The expense total is the authority; a per-line amount is optional. |
| `Casual` | `name`, `id_number`, `id_number_key`, `phone`, `registered_by`, `client_uuid` | R3. Unique `(organization, id_number_key)`; key = upper-cased, spaces and dashes stripped. ID photo is an `Attachment`. Not a `User`. |
| `AllowanceRequest` | `number` (series `AR`), `type`, `transport_scope`, `amount`, `from_date`, `to_date`, `site`, `project`, `reason`, `recorded_by`, `status`, decision, paid, `closed_at`, `closed_by`, `returned_amount`, `client_uuid` | Types FLOAT, TRANSPORT, NIGHT_OUT, TEAM_ALLOWANCE, OTHER. `transport_scope` = WITHIN_NAIROBI / OUTSIDE_NAIROBI, TRANSPORT only. `days = to − from + 1`. Number via `core.numbering.allocate_number`, new `DocumentType.ALLOWANCE` (D37). |
| `ApprovalRequest.required_permission` (new) | char, blank | A third way to address a level, beside role and user; the CHECK becomes "at most one of three". Used for `finance.approve`. |
| `OrganizationSettings.finance_director_role` | → Role, null | R4. Null: nobody skips the PM level. |
| `OrganizationSettings.allowance_limits` | JSON | Defaults: TRANSPORT_WITHIN_NAIROBI max 500; TRANSPORT_OUTSIDE_NAIROBI none; NIGHT_OUT and TEAM_ALLOWANCE 1,500–10,000. Null bound = no bound. FLOAT and OTHER unlimited. |
| `Attachment.caption`, `Attachment.client_uuid` (new) | char(60); uuid null unique per org | Caption chosen on the phone from Receipt / Fuel pump / Work done / ID / Other. The uuid makes an offline upload idempotent. |

**Float balance** is derived: `amount − Σ expenses on it (not REJECTED) − returned_amount`. Pending
expenses count. It may go negative and then reads "owed to you"; settling that is a new request.
Closing records `returned_amount ≥ 0` (R2).

**Guards.** `ProjectExpense.save` keeps its `_loaded_status` guard, now allowing only the transitions
in 4.17.3 and, once `APPROVED`, only the `paid_*` columns; `AllowanceRequest` and
`ExpenseCasualLine` get the same. The `a_decided_expense_records_when` CHECK is rewritten for the
new statuses. Reversal (`reverse_expense`) is unchanged, allowed to the PM or `finance.approve`, and
the reversing row is created `APPROVED`. A reversal of a PAID expense removes it from cost; recording
money coming back is out of scope.

#### 4.17.3 Status flow and routing (R2, R4)

```
record ─► PENDING_PM ─PM approves─► PENDING_FINANCE ─Finance approves─► APPROVED ─mark paid─► PAID
              │   ▲                       │
          reject   resubmit            reject        (a float-backed expense stops at APPROVED)
              ▼   │                       ▼
           REJECTED ◄─────────────────────┘
```

There is no server-side draft: a draft is a queued entry on the phone (4.17.8).

`approvals.engine.required_levels` gains a finance branch beside the O6 branch:
1. **PM level** (`required_user = project.manager`) — **skipped** when the recorder is the project's
   PM, or holds `settings.finance_director_role`. A skipped entry starts at `PENDING_FINANCE`.
2. **Finance level** (`required_permission = "finance.approve"`).

No `due_at` and no escalation on either level; no delegation. `can_approve` never allows the
recorder on a finance entry, whatever `allow_self_approval` says, and the O6 self-approved-PM
exception does not apply. Both models expose `requested_by_id` as an alias of `recorded_by_id`.

**No or inactive PM** (D28): recording is refused when the PM level is needed and the project has no
active manager (`PROJECT_HAS_NO_ACTIVE_MANAGER`). If the PM goes inactive later, the level waits;
reassigning the project's PM re-addresses its open level-1 requests (a hook in the project update
path). Recording is also refused when no active user other than the recorder holds
`finance.approve` (`FINANCE_NO_OTHER_APPROVER`).

**Services** (`commercials/finance.py`): `record_expense`, `request_allowance`, `decide`,
`resubmit` (REJECTED → first open level, new requests, old rows kept), `mark_paid`, `close_float`,
`register_casual`, `resolve_project`. Each writes an audit row. `decide_expense` is retired and its
callers moved; `NotTheProjectManager` gives way to the engine's `NotAnApprover`.

#### 4.17.4 Site to project (R1)

`resolve_project(site, project=None)`: the site's OPEN projects; one is chosen automatically; two or
more need `project` (`PROJECT_AMBIGUOUS`, listing them); none is `SITE_HAS_NO_OPEN_PROJECT`; a
`project` not on the site is `SITE_NOT_ON_PROJECT`. `ProjectViewSet` gains `site` and `status`
filters, and the offline bundle carries each site's open projects.

#### 4.17.5 Rules (R5)

`commercials/finance_rules.py`, pure, called by `request_allowance` (so replay enforces them too):
- **Overlap** for TRANSPORT, NIGHT_OUT and TEAM_ALLOWANCE: same `recorded_by` and `type`, dates
  intersecting, status PENDING_PM, PENDING_FINANCE, APPROVED or PAID → `ALLOWANCE_OVERLAP` naming
  the earlier number. FLOAT and OTHER are exempt (R2 allows a second float). The check runs under
  `select_for_update` on the recorder's user row, so two simultaneous sends cannot both pass.
- **Limits**: key = type, or TRANSPORT_WITHIN/OUTSIDE_NAIROBI. `amount` is compared with
  `min × days` and `max × days` (no rounding) → `ALLOWANCE_LIMIT`, stating the daily figure and the
  limit. TRANSPORT without a scope → `TRANSPORT_SCOPE_REQUIRED`. Expenses are not limit-checked.

#### 4.17.6 Endpoints

| Endpoint | Purpose | Permission |
|---|---|---|
| `GET/POST /project-expenses`, `PATCH /{id}` | Extended payload. PATCH only while PENDING_PM, by the recorder. | member |
| `POST /project-expenses/{id}/decide` | `{approved, reason}` on the caller's current level. | engine |
| `POST /project-expenses/{id}/resubmit` · `/reverse` | | recorder · PM or `finance.approve` |
| `POST /project-expenses/{id}/mark-paid` | `{payment_reference, paid_at?}`; refused for float-backed. | `finance.approve` |
| `GET /project-expenses?mine=true` · `?payable=true` | payable = APPROVED, no float, unpaid | member · `finance.approve` |
| `/allowance-requests` (CRUD, `decide`, `resubmit`, `mark-paid`) | Detail adds `days`, `daily_amount`, `open_float_warning`; floats add `spent`, `balance`. | as above |
| `POST /allowance-requests/{id}/close-float` | `{returned_amount}`; PAID and open only. | `finance.approve` |
| `GET/POST/PATCH /casuals` (`?search=`) | | member |
| `GET/PATCH /finance/settings` | limits, director role | read member; write `finance.approve` or `settings.manage` |
| `POST /attachments` | adds optional `caption`, `client_uuid` | see 4.17.7 |

**`approvals/pending` is fixed.** It currently passes every `required_role IS NULL` row, so anyone
sees every person-addressed (PM) request. It becomes `Q(required_role in my roles) |
Q(required_user=me) | Q(required_permission in my permissions)`, and `get_document` summarises the
two new document types. Gate-out PM approvals keep working because they are addressed to the PM.

The open-float warning is computed on read: if the recorder has another PAID, unclosed float, the
detail carries `open_float_warning: {number, balance}`. It never blocks (R2).

#### 4.17.7 Permissions and roles

- New `finance.approve` (group "Finance") in `accounts/permissions_registry.py` and
  `frontend/src/auth/permissions.ts`; Owner holds it automatically. It covers approving at the
  Finance level, marking paid, closing floats and setting limits. A separate pay permission can be
  split out later.
- New seeded role **Finance**: `finance.approve`, `project.view_cost`, `report.view_all`, added for
  existing tenants by a data migration through `accounts/role_sync`.
- Recording stays open to every member, as O16.
- Attachments: the recorder (or registrar) may attach to their own entry while PENDING_* or REJECTED;
  `commercials.Casual` becomes a target. After final approval photos are fixed.
- Casuals: any member reads name and phone and the ID number masked to its last three characters;
  the full number and the ID photo need `finance.approve`.

#### 4.17.8 Offline (R6, §8)

D17 widens to three `SyncOperation`s: `EXPENSE`, `ALLOWANCE_REQUEST`, `CASUAL`. The
`sync/services._HANDLERS` entries validate with the online serializers and call the same services,
so rules, `resolve_project` and the approver check all run on replay. Approving, paying and closing
floats are never offline (§8.3 unchanged).

- **Idempotency:** `SyncSubmission (organization, client_uuid)` as now, plus each entity's own
  unique `client_uuid`.
- **References within a queue:** an expense naming a casual registered offline in the same queue
  sends `casual_client_uuid`; the queue replays in capture order. A float must be PAID, so it is
  always named by server id.
- **Photos:** Dexie `version(2)` adds a `photos` table (`queue_client_uuid`, caption, blob,
  filename, `client_uuid`, status). After `drainQueue` marks an entry applied (it receives
  `document_id`), `drainPhotos` uploads each blob to `/attachments` with its `client_uuid`, so a lost
  response replays to the same attachment. Until they land, `evidence_state` is `"arriving"`, and the
  approver sees "photos on the way", not "no evidence".
- **Refusal:** a refused entry becomes a `SyncException` with the code and stays on the phone with
  its reason (R6). "Fix and resend" sends a new `client_uuid` with `supersedes_client_uuid`, which
  resolves the old exception.
- **Bundle:** `OfflineBundleView` adds `expense_categories` (with `kind`), `casuals` (masked),
  `my_floats` (with balances), `finance_limits` and `sites[].open_projects`. The phone runs the same
  rules (`features/money/rules.ts`) for early warnings; the server decides.

#### 4.17.9 Notifications (R4)

New events in `notifications/matrix.py`: `finance.awaiting_approval` (new recipient
`LEVEL_APPROVERS`, in-app and email), `finance.approved` (requester, in-app), `finance.rejected`
(requester, in-app and email, with the reason), `finance.paid` (requester, in-app, with the
reference). SMS is off by default (D30). `LEVEL_APPROVERS` resolves from the open
`ApprovalRequest` — the `required_user`, or the active holders of `required_permission` — never the
recorder. The payload carries amount, type, site, `evidence_state` and the float warning.

#### 4.17.10 Frontend

- **Money** (`features/money/`, a nav entry for every member): My expenses, My requests and floats
  (balance; Close float for Finance), Casuals. Forms: Record expense (the existing
  `RecordExpensePage` moved here, old route redirected), Request allowance, Add casual. Site first;
  project filled in, or chosen when the site has 2+ open projects. Fuel asks reg and litres; casual
  labour asks casuals and days; photos via `PhotoCapture` with a caption. Offline entries show
  "Waiting to send".
- **Approvals:** the Expenses tab becomes level-aware and a Requests tab is added; the sheet shows
  the level, photos, evidence state and the float warning.
- **To pay** (`/money/to-pay`, `finance.approve`): payable entries and a Mark paid sheet (reference
  required).
- **Settings → Finance:** limits, the Director role, expense categories with `kind`.
- **Pure helpers** in `features/money/rules.ts`: `daysBetween`, `checkLimit`, `findOverlap`,
  `floatBalance`, `candidateProjects`, `normaliseIdNumber`, with Vitest tests.

#### 4.17.11 What changes for existing O16 data

- A data migration moves in-flight `SUBMITTED` expenses to `PENDING_PM` and creates their two
  approval requests. Existing APPROVED and REJECTED rows are untouched and still counted.
- `costing.expense_cost`, `reports_finance.py` and the project figures change from
  `status=APPROVED` to `status__in=(APPROVED, PAID)` — the easiest thing to miss.
- A PM's approval alone no longer reaches cost: Finance must approve too, except where the PM level
  is skipped.
- Existing categories get `kind=GENERAL`; "Transport and fuel" stays; the four new categories are
  added where missing.

#### 4.17.12 Errors

| Code | HTTP | When |
|---|---|---|
| `PROJECT_AMBIGUOUS` | 400 | The site is on 2+ open projects and none was chosen. |
| `SITE_HAS_NO_OPEN_PROJECT`, `SITE_NOT_ON_PROJECT` | 400 | R1. |
| `ALLOWANCE_OVERLAP` | 409 | Names the earlier request. |
| `ALLOWANCE_LIMIT` | 400 | States the daily figure and the limit. |
| `TRANSPORT_SCOPE_REQUIRED` | 400 | R5. |
| `CASUAL_ID_DUPLICATE` | 409 | Names the existing casual. |
| `FINANCE_NO_OTHER_APPROVER` | 409 | Nobody but the recorder holds `finance.approve`. |
| `PROJECT_HAS_NO_ACTIVE_MANAGER` | 409 | Existing (D28). |
| `FINANCE_SELF_APPROVAL` | 403 | The recorder tried to decide. |
| `FINANCE_NOT_DECIDABLE` | 409 | Wrong status. |
| `FLOAT_NOT_OPEN` | 409 | Not PAID, closed, or not the recorder's. |
| `PAYMENT_REFERENCE_REQUIRED` | 400 | Mark paid without a reference. |

#### 4.17.13 Testing

- **Backend:**
  - Routing: ordinary entries, PM-recorded entries, and Director-recorded entries with the setting
    both set and unset.
  - Self-approval refused at both levels, even with `allow_self_approval` on.
  - No PM, an inactive PM, and PM reassignment.
  - Reject, then resubmit.
  - Overlap, including two concurrent sends, and the exempt types.
  - All three limit cases.
  - Float balance, closing a float, and the open-float warning.
  - Casual duplicate and normalisation.
  - Mark paid refused for a float-backed expense.
  - Cost counts APPROVED and PAID only.
  - The data migration.
  - The fixed `approvals/pending`.
  - `LEVEL_APPROVERS` and emails.
  - Sync: replay, refusal, supersede, a casual and an expense in one batch, and approval refused
    offline.
  - Attachment `client_uuid` replay.
  - RLS and isolation.
- **Frontend:** Vitest on `rules.ts` and on the queue's photo step.
- **E2E (phone):**
  1. Offline, register a casual, record a casual-labour expense with two photos, and request
     transport twice with overlapping dates.
  2. Online again, the first lands with its photos and the second is refused for overlap and stays
     on the phone.
  3. Fixing the dates sends it.
  4. The PM approves, Finance approves and marks it paid, and project cost rises once.

#### 4.17.14 Assumptions taken (each can be changed later without redesign)

1. FLOAT and OTHER are exempt from the overlap rule (R2 allows a second float).
2. One permission, `finance.approve`, approves, pays, closes floats and sets limits.
3. Site is optional when a project is given directly.
4. A float may be overspent. It then reads "owed to you" and is settled by a new request.
5. Full casual ID numbers and ID photos are visible only to `finance.approve`.
6. A payment reference is not unique, since one M-Pesa batch can cover several entries.
7. Casual lines carry days, and optionally an amount.
8. No refund flow for a reversed PAID expense.
9. "Transport and fuel" is kept beside the new categories.
10. The `approvals/pending` leak is fixed for every document type.
11. Changing a project's PM re-addresses its open PM-level requests.
12. Photo captions come from a fixed list on the phone.

### 4.18 Clock-in (Epic R, R13)

> **Status: approved 2026-10-09.**

#### 4.18.1 The decision: extend the places, add one small app, reuse the approval tables

- **Places are extended, not copied** (R13). `network.Site` already has nullable `latitude` and
  `longitude`, so it gains only `radius_m`. `locations.Location` gains all three, and a new
  `LocationType.OFFICE` beside `YARD`. Nothing else about either model changes.
- **`attendance/` is a new tenant app** with two records: `WorkSession` (one clock-in to one
  clock-out) and `WorkDay` (one person, one local date, the thing that is approved). Hours are never
  stored; they are summed on read, so a correction cannot leave a stale total.
- **Approval reuses `approvals.engine`** (§5). A work day is a third document kind beside gate-outs
  and finance entries, with `ApprovalRequest` and the append-only `ApprovalAction` unchanged. The
  engine gains four small changes (4.18.5), because a day can need two approvers at once and the
  engine today answers levels strictly in order.
- **One area check, written twice and tested against one fixture** (4.18.4), because the phone must
  make the same decision the server will (R13 offline).

D28 holds: there is no fallback approver for a PM's day. D29 (PM only) does not apply to days.

#### 4.18.2 Data model

New tables are tenant tables: `enable_rls` in an `attendance` migration and fixtures in
`attendance/isolation.py` (§2, A3).

| Model / field | Shape | Notes |
|---|---|---|
| `Site.radius_m` (new) | int, default 200 | CHECK 20-2000. `latitude`/`longitude` already exist. |
| `Location.latitude`, `longitude`, `radius_m` | decimal(9,6) null; same; int default 200 | CHECKs: ranges, and lat/lng both set or both null. Not enforced in `Location.save` (it runs `full_clean`, so the rule would break seeds and system rows); enforced in serializers and admin (4.18.8). |
| `LocationType.OFFICE` (new) | choice | Clockable. Never gets a `StockNode` (nodes are created lazily by `locations/nodes.py`, so none exists unless something asks) and is excluded from stock pickers. |
| `Site.area_history`, `Location.area_history` | JSON list | Newest first, max 10 entries `{lat, lng, radius_m, valid_until}`, pushed whenever the area changes. Lets the server recognise the area an offline phone legitimately held (4.18.4). |
| `OrganizationSettings.clock_auto_close_hour` | int 0-23, default 18 | R13. |
| `OrganizationSettings.clock_accuracy_cap_m` | int, default 100 | R13: a 2 km "fix" cannot pass. |
| `WorkSession` | `person`, exactly one of `site` / `location` (CHECK), `project` (null), `work_day`, `local_date` | Project resolved at clock-in (4.18.5). `local_date` is in the organization's `timezone`. |
| | `clock_in_at` (phone time), `clock_in_received_at`, `in_lat`, `in_lng`, `in_accuracy_m`, `in_distance_m` | R13: the phone's time is the record; arrival time sits beside it. |
| | `clock_out_at` (null), `clock_out_received_at`, `out_lat`, `out_lng`, `out_accuracy_m`, `out_distance_m` | All null while open; out position null when none was available. |
| | `closed_by` | `PERSON` / `NEXT_CLOCK_IN` / `AUTO`. |
| | `in_client_uuid`, `out_client_uuid` | Unique per org: offline idempotency, as `GateIn.client_uuid`. |
| | `in_checked_area` (JSON null), `area_changed` (bool) | What the phone checked against; set only on offline replay (4.18.4). |
| | `approval_request` → ApprovalRequest, null | The slice this session belongs to (4.18.5). Null = unrouted. |
| | constraints | Partial UNIQUE `(organization, person)` where `clock_out_at IS NULL` (one open session); CHECK out ≥ in. |
| `WorkDay` | `person`, `date`, `status`, `formed_at` | UNIQUE `(organization, person, date)`. `status`: `OPEN`, `PENDING`, `APPROVED`, `REJECTED`, a projection of its slices' requests, as a gate-out's is. Exposes `requested_by_id` and `recorded_by_id` as aliases of `person_id`. |
| `WorkSessionCorrection` | `session` (null for an added session), `work_day`, `kind` (`EDIT` / `ADD`), `place` (site or location, ADD only), `original_in_at`, `original_out_at`, `corrected_in_at`, `corrected_out_at`, `reason`, `made_by`, `made_at`, `rejected_request`, `reopened_request` | Append-only. The session keeps its original times untouched; the corrected times apply from the latest correction (4.18.6). |
| `OrganizationSettings.finance_director_role` | existing | Reused as the Director for days. Null: a day with nobody to approve it stays unrouted and the owner is told. |

**Derived flags** (computed on read, never stored): *outside at clock-out* (`out_distance_m` beyond
the place's radius), *no position at clock-out*, *closed automatically* (`closed_by=AUTO`), *sent
late* (`received_at - at > 1 h`, constant `LATE_AFTER`), *area changed* (`area_changed`),
*corrected* (has a correction).

**Guards.** A session is immutable once its slice is `APPROVED`. While open or pending, only the
service functions in 4.18.5 and 4.18.6 change it. Audit rows are written for clock-in, clock-out,
auto-close, routing and correction.

#### 4.18.3 Places and who may clock in where (R13)

A place is clockable when it is **active, has coordinates**, and is a Site of any status other than
decommissioned, or a Location of type `YARD` or `OFFICE`. Stores, vehicles and quarantine are not
clockable. `Location.is_system` rows are exempt from the coordinates rule, and are not clockable.
Casuals are not users and do not clock in (R3).

#### 4.18.4 The area check (R13)

`core/geo.py`: `haversine_m(a, b)` and `check_area(fix, place, cap)`, returning `NO_FIX`,
`TOO_VAGUE` (accuracy above the cap), or the distance and `inside = distance <= radius + accuracy`.
The accuracy allowance is what lets a real phone indoors pass; the cap is what stops a bad fix doing
the same. The TypeScript twin is `features/attendance/area.ts`. A shared
`shared/area-cases.json` (inside, outside, on the edge, bad accuracy, near the poles and the date
line) is read by pytest and by Vitest, so the two cannot drift.

**Offline replay, the phone's check stands** (R13, decided 2026-10-09). A `CLOCK_IN` payload carries
`place_area: {lat, lng, radius_m}`, the area the phone checked against. On replay the server:
1. checks the fix against the place's **current** area. Inside: accepted as normal;
2. else checks it against `place_area`, but only when `place_area` equals the current area or an
   `area_history` entry whose `valid_until` is not before `captured_at`. Inside that: **accepted,
   `area_changed=True`**, `in_checked_area` stored, flag shown to the approver;
3. else refused `CLOCK_OUTSIDE_AREA`. A phone cannot name an area the place never had.

An online clock-in is checked against the current area only. A clock-out is never refused on
position (R13); it stores the distance and flags if outside.

#### 4.18.5 Flows and routing

**Clock-in** (`attendance/services.py::clock_in`), in one transaction:
1. Lock the person's user row; return the existing session for a known `in_client_uuid`.
2. The place must be clockable (4.18.3): `PLACE_NOT_AVAILABLE`, `PLACE_HAS_NO_COORDINATES`.
3. Time bounds on `clock_in_at`: at most 5 minutes ahead of the server, at most 72 hours old, and
   not before the person's previous session ended: `CLOCK_TIME_INVALID`, `CLOCK_OVERLAP`.
4. Area check (4.18.4): `CLOCK_LOCATION_REQUIRED`, `CLOCK_LOCATION_TOO_VAGUE`, `CLOCK_OUTSIDE_AREA`
   (the message states the distance).
5. Resolve the project. `commercials/finance.py::resolve_project` raises on none; it is split so a
   non-raising `open_projects_of(site)` exists and `resolve_project` calls it. One open project is
   taken; none gives `project=None` (the Director approves); two or more need `project`
   (`PROJECT_AMBIGUOUS`, as R1). A YARD or OFFICE has no project.
6. Close any open session at this `clock_in_at` (`closed_by=NEXT_CLOCK_IN`, no out position).
7. Get-or-create the `WorkDay` for `local_date` (status `OPEN`); create the session; audit.

**Clock-out** (`clock_out`): the session is found by `session_client_uuid`, else the person's open
one; none gives `CLOCK_NOT_CLOCKED_IN`. Never refused on position. A replayed clock-out whose time
is earlier than an `AUTO` or `NEXT_CLOCK_IN` close replaces it while the slice is undecided, so
being offline does not cost the hours; once decided, `CLOCK_SESSION_LOCKED`.

**Auto-close and day formation** (`attendance/sweeps.py`, beat entry `attendance-sweep`, hourly at
:05; the existing `core.sweeps.dispatch_sweeps` is a daily 05:30 job, too coarse for an hour that
the tenant sets). Following its shape, `dispatch_attendance_sweep` fans out per active
organization to `attendance_sweep_tenant`, each step guarded separately as in `sweep_tenant`.
- `close_stale_sessions`: an open session is closed **at the cutoff itself** (not at the time the
  sweep ran): `clock_auto_close_hour` on the session's local date, or midnight for a session opened
  after that hour. `closed_by=AUTO`.
- `form_days`: each past `OPEN` day with no open session is routed (`route_day`) and becomes
  `PENDING`. Day formation uses the organization's `timezone`, tested in Nairobi and in a second
  zone.

**`route_day`** groups the day's unrouted sessions by addressee and creates one request per group.
Idempotent: a session already on a request is skipped, and a late-arriving session reopens only its
own slice.
- Addressee: the session's project manager; with no project, the Director (the people holding
  `finance_director_role`).
- A PM's own sessions go to the Director; a Director's own go to another Director. Nobody approves
  their own day (R13).
- With no addressee the session stays unrouted and the owner gets `attendance.unrouted`. Clock-in
  is never refused for this.
- A day spanning two PMs has two requests, answered **in parallel** (R13: each for their own
  sessions). The day is `APPROVED` only when every slice is.

**Engine changes** (`approvals/engine.py`, `approvals/addressing.py`, `notifications/events.py`):
1. `WORK_DAY_DOCUMENT_TYPES = {"attendance.WorkDay"}` and `_work_day_levels`, which return the
   slice's single level-1 request (addressed with `required_user`, or by Director role), no
   `due_at`, no delegation. `required_levels` and `create_requests` take the slice.
2. `can_approve`: the self-approval refusal for work days runs first, before the `required_user`
   branch, so a PM can never approve their own day through the "PM may self-approve" exception.
3. `next_pending_request` and `record_decision` take an optional `approval_request`. Today they
   assume one chain per document; with parallel slices a rejection supersedes **only that slice**,
   not the other PM's.
4. `addressing.open_requests_addressed_to`: the gate-out blanket (`PERM.GATE_OUT_APPROVE` sees every
   role level) excludes work-day requests, and the "earlier level open" exclusion is per slice.
5. `_level_approvers` (notifications) returns the addressees of **every** open request at the lowest
   level, not just the first; for a day that is both PMs.
6. `readdress_project_requests` gains a `WorkDay` branch: reassigning a project's PM moves that
   project's open slices to the new PM (and `WorkSession.project` is unchanged).

#### 4.18.6 Correcting a rejected day (R13, decided 2026-10-09)

Rejection needs a reason. A rejected slice does not end the day: the person may correct it.

`attendance/services.py::correct_session`, allowed to the day's person only, only while the
**slice** of that session is `REJECTED`, for up to 30 days after the rejection:
- **EDIT** an existing session: new `corrected_in_at` and/or `corrected_out_at`, or a clock-out for
  an auto-closed session. **ADD** a missing session at a clockable place (kind `ADD`, `place`
  required; it takes the session's project rules from 4.18.5 step 5).
- `reason` is required (`CORRECTION_REASON_REQUIRED`). Times must stay on the day's `local_date`,
  `out ≥ in`, and not overlap another session (`CLOCK_TIME_INVALID`, `CLOCK_OVERLAP`). No position
  is taken: a correction is the person's statement, not a measurement, and it is flagged
  *corrected*.
- The session's recorded times and position are **never overwritten**. A `WorkSessionCorrection`
  row stores original and corrected; effective times are those of the latest correction.
- It reopens **only the rejected slice**: a new `ApprovalRequest` to the **same addressee** (the
  rejected request's `required_user` / role), level 1, PENDING, with the correction's
  `rejected_request` and `reopened_request` linking old to new. The slice's sessions are repointed
  to the new request; the old request and its `ApprovalAction` rows stay, so history shows
  reject, correct, decide. The other PM's slice, if already approved, is not touched. The day goes
  back to `PENDING`.
- A second rejection allows another correction; each is a row.
- The approver sees, per session: the place, times and hours as corrected, the **original** times
  beside them, the reason, who and when, plus the earlier rejection reason.

#### 4.18.7 Endpoints

| Endpoint | Purpose | Permission |
|---|---|---|
| `GET /work-sessions/open` | The caller's open session, if any. | member |
| `POST /work-sessions/clock-in` | `{site \| location, project?, at?, fix, place_area?, client_uuid}` | member |
| `POST /work-sessions/clock-out` | `{at?, fix?, session_client_uuid?, client_uuid}` | member |
| `GET /work-days?scope=mine\|team\|all` | Filters: person, project, status, date range, `awaiting_me`. | scoped, below |
| `GET /work-days/{id}` | Sessions with flags, distances, corrections, slices and their status. | scoped |
| `POST /work-days/{id}/decide` | `{approved, reason}` on the caller's slice; `reason` required to reject. | engine |
| `POST /work-sessions/{id}/correct` | `{kind, corrected_in_at?, corrected_out_at?, place?, reason}` | the person |
| `GET/PATCH /attendance/settings` | auto-close hour, accuracy cap. | read member; write `settings.manage` |
| `PATCH` sites, locations | Coordinates and radius; `has_coordinates` on read; `?missing_coordinates=true`. | existing |

`approvals/pending` and `get_document` summarise work days (the finance fix in 4.17.6 already
narrows the list to what is addressed to the caller).

#### 4.18.8 Permissions and visibility

- You see your own days. A PM sees days with a session on their projects, **their slice only**
  being decidable. Holders of the new `attendance.view_all` (Owner automatically; added to the seeded
  Finance role through `accounts/role_sync`) and of the Director role see everyone. Recording is
  open to every member.
- `attendance.view_all` is registered in `accounts/permissions_registry.py` and
  `frontend/src/auth/permissions.ts` (group "Attendance").
- **Coordinates are required** (R13): a `CoordinatesMixin` shared by the site and location
  serializers (and admin) refuses a save without latitude and longitude, `COORDINATES_REQUIRED`,
  for every Site and for `YARD` and `OFFICE` locations, except `is_system` rows. Existing sites
  without them keep working everywhere else; they simply cannot be clocked in at. Seeding
  (`locations/nodes.py::seed_locations_and_nodes`, which creates "Main yard") and factories get
  coordinates; for a **new tenant** "Main yard" starts blank and Settings shows a "Set coordinates"
  prompt until it has them.

#### 4.18.9 Offline (R6, R13, §8)

D17 widens by two `SyncOperation`s, `CLOCK_IN` and `CLOCK_OUT`. The
`sync/services._HANDLERS` entries validate with the online serializers and call `clock_in` /
`clock_out`, so every rule runs on replay. Approving and correcting are not offline (§8.3 unchanged).
- **Bundle:** `OfflineBundleView` adds `latitude`, `longitude`, `radius_m`, `has_coordinates` to
  `sites` and to YARD and OFFICE locations, and `attendance: {accuracy_cap_m, auto_close_hour}`.
- **On the phone:** `area.ts` checks the area from the bundle and refuses early with the same words.
  The open session is held as a local reference row (so Clock out works with no network), its
  `client_uuid` is the `session_client_uuid` the clock-out names. `ATTENDANCE_OPERATIONS` in
  `offline/db.ts`; the capture time is the phone's, and `captured_at` is what the server compares
  with `area_history`.
- **Idempotency:** `SyncSubmission (organization, client_uuid)` plus the session's own unique
  `in_client_uuid` / `out_client_uuid`.
- **Refusal:** a refused entry becomes a `SyncException` with its code and stays on the phone (R6).
  A clock-in refused for being outside the area is never turned into hours by resending. If the
  person really was there, the Director adds the day (4.18.6a).

#### 4.18.10 Notifications

New events in `notifications/matrix.py`: `attendance.awaiting_approval` (`LEVEL_APPROVERS`, in-app
and email; one per slice, with person, date, hours and flags), `attendance.rejected` (to the person,
in-app and email, with the reason and a link to correct), `attendance.unrouted` (to the owner,
in-app). A corrected slice sends `attendance.awaiting_approval` again, tagged "corrected". SMS off
(D30).

#### 4.18.11 Frontend

- **Clock-in card on Home** (`features/attendance/`): reads the position once, lists clockable places
  nearest first with their distance, shows the open session and an elapsed time. Refusals state the
  distance or "turn location on". Offline entries show "Waiting to send".
- **My time** (`/time`): own days, hours and status; Team and Everyone toggles by permission. A
  rejected day shows the reason and a **Correct** sheet (edit a time, or add a missing session, with
  a required reason).
- **Approvals, Days tab** (`DayApprovals.tsx`): the day with sessions, distances, flags, original
  against corrected, and Approve / Reject (reason required).
- **Settings, Clock-in** (`AttendancePage.tsx`): hour and accuracy cap; sites and locations missing
  coordinates.
- **Sheets:** `SiteSheet` and `LocationSheet` in `features/settings/NetworkPage.tsx` are create-only
  today, so each gains an **edit mode** with latitude, longitude, radius and a "Use my location"
  button (`components/ui/UseMyLocation.tsx`). `features/quickCreate.tsx` reuses the same sheets
  (`QUICK_CREATE.sites` / `.locations`), so the quick "Add new site" gets the same required
  coordinates. The site list gets a "No coordinates" badge and filter.
- **Helpers:** `area.ts`, `position.ts`, `nearby.ts`, `offline.ts`, `queued.ts`, with Vitest.

#### 4.18.12 Errors

| Code | HTTP | When |
|---|---|---|
| `CLOCK_LOCATION_REQUIRED` | 400 | No fix. The screen says to turn location on. |
| `CLOCK_LOCATION_TOO_VAGUE` | 400 | Accuracy above the cap. |
| `CLOCK_OUTSIDE_AREA` | 409 | States the distance. |
| `CLOCK_PLACE_REQUIRED` | 400 | Neither or both of site and location. |
| `PLACE_HAS_NO_COORDINATES` | 409 | Names the place so someone can fix it. |
| `PLACE_NOT_AVAILABLE` | 409 | Inactive, decommissioned, or not clockable. |
| `CLOCK_TIME_INVALID` | 400 | Future, over 72 h old, or off the day. |
| `CLOCK_OVERLAP` | 409 | Overlaps another session. |
| `CLOCK_NOT_CLOCKED_IN` | 409 | Clock-out with no open session. |
| `CLOCK_SESSION_LOCKED` | 409 | The slice is decided. |
| `CORRECTION_NOT_ALLOWED` | 409 | The slice is not rejected, or the 30 days are over. |
| `CORRECTION_REASON_REQUIRED` | 400 | R13. |
| `WORK_DAY_SELF_APPROVAL` | 403 | The person tried to decide their own day. |
| `WORK_DAY_NOT_DECIDABLE` | 409 | Wrong status. |
| `COORDINATES_REQUIRED` | 400 | Site, YARD or OFFICE saved without them. |
| `PROJECT_AMBIGUOUS`, `REJECTION_REASON_REQUIRED` | 400 | Existing. |

#### 4.18.13 Testing

- **Backend:**
  - Geo fixture; clock in and out; the two-sessions day; auto-close in Nairobi and a second timezone.
  - Concurrency: two simultaneous clock-ins leave one open session.
  - `route_day`: PM, Director, no project, a PM's own, a Director's own, two PMs, no approver,
    idempotency, a late session reopening one slice.
  - Each engine change; visibility; coordinates required; RLS and isolation.
  - Offline replay: inside the new area, inside only the old area (accepted and flagged), outside
    both, and a forged `place_area`.
  - Correction: EDIT, ADD, only on a rejected slice, same approver, other slice untouched, original
    kept, window.
  - Sync: replay, refusal, clock-out by `session_client_uuid`, replay overriding an auto-close.
- **Frontend:** Vitest on `area.ts` with the shared fixture, and on `queued.ts`.
- **E2E (phone, `context.setGeolocation`):**
  1. Outside the area, clock-in is refused with the distance; inside it succeeds.
  2. Offline, clock in; the area is edited by an owner; online again, the clock-in lands flagged.
  3. Clock out; the PM rejects the day with a reason; the person adds a correction; the same PM sees
     original and correction and approves.

#### 4.18.14 Assumptions taken (each can be changed later without redesign)

1. The Director for days is `finance_director_role`; no separate setting.
2. `OFFICE` is a `LocationType` that never gets a `StockNode` and is excluded from stock pickers.
3. `attendance.view_all` is a new permission rather than reusing `report.view_all`.
4. A new tenant's "Main yard" starts without coordinates, with a prompt in Settings.
5. The Days tab shows requests addressed to the caller, plus days on projects they manage.
6. Corrections are allowed for 30 days after a rejection; there is no limit on rounds.
7. A correction takes no position; it is flagged, not area-checked.
8. `area_history` keeps ten changes; a phone older than that is checked against the current area.
9. A refused clock-in does not become hours by resending. If the person really was there, the
   Director adds the day with a reason (decided 2026-10-09; 4.18.6a), and the PM may also move the
   place's pin for next time.
10. Parallel slices and the days' hourly sweep are the only engine and scheduler additions.

#### 4.18.6a A day added by the Director (decided 2026-10-09)

For someone who was on site but whose phone placed them outside, or could not clock in at all:
`POST /work-days/add` (`{person, date, place, project?, start, end, reason}`), allowed only to a
holder of the Director role (`finance_director_role`), never for their own day. It creates a
`WorkSession` with no position, `closed_by = PERSON`, and a stored `added_by` and `added_reason`; the
session is flagged **"added by the Director"** wherever it shows, and the action is audited. The
day is then routed as usual (4.18.4); the Director's own slice is refused self-approval, so a day
the Director added is approved by the site's PM, or by another Director-role holder when there is no
project. Error `WORK_DAY_ADD_NOT_ALLOWED` (403) for anyone else.

### 4.19 Finance stage 2 — POs, budgets and sites (Epic R, R7–R12)

> **Status: approved 2026-10-09.**

#### 4.19.1 The decision: what is extended, what is new, and why

Stage 2 adds money *in* (milestones), money to subcontractors, and one more thing to buy. Almost all
of it hangs off structures that already exist, so the rule is: **extend the row that exists; add a
table only where nothing fits, and say why.**

| Need | Built on | New, and why nothing fits |
|---|---|---|
| R7 purchase | The stage 1 lifecycle, not the row. `StatusGuardMixin`, `ExpenseStatus`, `COSTED_STATUSES`, `finance._route/decide/mark_paid`, `engine._finance_levels`, `LEVEL_APPROVERS`, `Attachment` | **`SitePurchase` + `SitePurchaseLine`.** See the decision below. |
| R8 contract | `network.Subcontractor` (O4), `Job.subcontractor/agreed_price` (O3), `SubcontractorSpendReport` | **`Subcontract`** (the contract has a value, sites, terms, document; a job cannot hold those) and **`SubcontractPayment`** (cash against a contract; not a cost line — cost stays the closed job's agreed price, O11). |
| R9 budget | `Project.cost_budget`, `current_cost_budget`, `costing.cost_for`, `ProjectViewSet.performance`, `may_see_project_cost` | Nothing stored: **`commercials/budget.py`** computes committed and spent. Three columns for the reason (4.19.5). |
| R10 site dates | `Project.sites` (an auto M2M — no through model today, so nothing to extend) | **`ProjectSite`** becomes its through model; the existing table and rows are kept (4.19.6). |
| R11 PO payments | `Project` (+3 columns), `Attachment`, the report framework, `core.sweeps.dispatch_sweeps` | **`ProjectMilestone`, `MilestoneInvoice`, `MilestoneReceipt`.** Nothing in the system records money coming in. |
| R12 late PO | `Project.po_number` and `a_po_project_is_fully_specified` | One action and one timestamp. No new table. |

**Why `SitePurchase` and not a `ProjectExpense` with a kind.** I weighed it seriously, because the
lifecycle is identical. Against: an expense is one amount against one category; a purchase is a
supplier, a destination, a receiving location, N priced lines and, for yard goods, a link to a
`GateIn`. Folding that in would add six nullable columns and a child table to a row that already
carries fuel, casual-labour and float columns, and every `ProjectExpense` consumer
(`reports_finance.ExpensesLedgerReport`, `expense_cost`, the Money screens) would need an `if kind`
to stay right — and an INTO_YARD "expense" must be **kept out** of `expense_cost`, which is the
opposite of what that function is for. For: no new approval wiring. That saving is kept anyway,
because the wiring is generic: `AllowanceRequest` already proved a second entry kind rides the same
mixin, statuses, engine branch, services and notifications. So `SitePurchase` is the **third entry
kind of the same family**, not a second implementation. Its cost is counted by a sibling of
`expense_cost`, not inside it.

`SubcontractPayment` is the fourth member but with one level (R8) and no PAID stage, because it
*records* a payment Finance already made; the PM's approval confirms it.

Where it lives: all new finance tables in `commercials` (RLS in a new migration, fixtures in
`commercials/isolation.py`, §2, A3); `ProjectSite` and the PO columns in `network`; `Job.subcontract`
in `jobs`.

**From §4.20:** `network.Supplier` (beside Client and Subcontractor) and its `assert_payable(supplier)`
hook. The only couplings: `SitePurchase.supplier` FK, "not inactive to record", and `assert_payable`
at approve-for-payment and mark-paid (`SUPPLIER_NOT_APPROVED`). §4.20 adds `GateIn.supplier`, so the
draft delivery an INTO_YARD purchase creates sets both `supplier` and `supplier_name`. §4.20's
migration must run before this section's `SitePurchase.supplier` FK.

#### 4.19.2 Data model

| Model / field | Shape | Notes |
|---|---|---|
| `SitePurchase` (new) | `number` (series `SP`), `project`, `site` (required), `supplier` → Supplier PROTECT, `purchase_date`, `destination` `USED_AT_SITE`/`INTO_YARD`, `receive_into` → Location null, `amount`, `recorded_by`, `status`, decided/paid columns exactly as `ProjectExpense`, `reverses`, `gate_in` OneToOne → `receiving.GateIn` null PROTECT, `over_budget_by`, `over_budget_reason`, `photos_expected`, `client_uuid` | `StatusGuardMixin`, `ExpenseStatus`, the same `a_decided_…_records_when` CHECK, unique `(organization, number)` and `(organization, client_uuid)`. `receive_into` is required when INTO_YARD, forbidden otherwise (CHECK). `amount` is stored (the sum of the lines, set by the service) so `_visible_to`, reports and the budget need no join. `requested_by_id` alias as the others. Never deleted. New `DocumentType.SITE_PURCHASE` (D37). |
| `SitePurchaseLine` (new) | `purchase`, `item_type` → ItemType null, `description` char(200), `quantity` dec(14,3) > 0, `uom`, `unit_price` dec(14,2) ≥ 0 | CHECK: `item_type` or `description` present. Frozen with the purchase from APPROVED (the `ExpenseCasualLine._guard` pattern). Line total = `quantity × unit_price`, rounded to 2 places. |
| `Subcontract` (new) | `number` (series `SC`), `project`, `subcontractor` → network.Subcontractor, `sites` M2M Site, `contract_value`, `payment_terms` text, `status` `ACTIVE`/`CLOSED`, `created_by` | Signed contract is an `Attachment`. Sites must be a subset of `project.sites`. Several per (project, subcontractor) are allowed (different scopes). Never deleted; closed. |
| `SubcontractPayment` (new) | `subcontract`, `amount`, `paid_on`, `reference`, `recorded_by`, `status` (PENDING_PM / APPROVED / REJECTED only), decided columns, `reverses` self-FK, `over_budget_*` absent | `StatusGuardMixin`. Reference required. Invoice is an `Attachment`. A wrong approved payment is reversed, not edited (O16 discipline). Exposes `project` (via the subcontract) for the engine. |
| `Job.subcontract` (new) | → `Subcontract`, null PROTECT; `over_contract_reason` char(500) | CHECK: set only when `delivery_mode = SUBCONTRACTED`. Service check: same project, same `subcontractor`. Joins `_DELIVERY_FIELDS`, so frozen once the job is CLOSED, like price. |
| `ProjectSite` (new, through) | `project`, `site`, `mobilised_on`, `accepted_on`, tenant columns | `Project.sites` gets `through="ProjectSite"`. Unique `(project, site)`. Certificate is an `Attachment` captioned "Acceptance certificate". |
| `Project` (+) | `po_issue_date` date null, `payment_terms` char(500), `payment_terms_days` small int null, `po_recorded_at` datetime null | PO PDF is an `Attachment` captioned "PO". Existing `a_po_project_is_fully_specified` stays; the issue date is required by the service, not the CHECK, so existing POs remain valid. |
| `ProjectMilestone` (new) | `project`, `sequence`, `name`, `share_type` `PERCENT`/`AMOUNT`, `share_value`, `condition` `NONE`/`ALL_SITES_ACCEPTED`/`DATE`, `condition_date`, `due_notified_on`, `overdue_notified_on` | Unique `(project, sequence)`. CHECK: `condition_date` iff DATE. Amount = percent × `current_contract_value`, or the amount. |
| `MilestoneInvoice` (new) | `milestone`, `invoice_number`, `invoice_date`, `amount`, `recorded_by`, `voided_at`, `voided_by`, `void_reason` | Append-only; a mistake is voided, never edited or deleted. Document is an `Attachment`. |
| `MilestoneReceipt` (new) | `milestone`, `received_on`, `amount`, `reference`, `recorded_by`, void columns | Many per milestone: partial receipts (decided 2026-10-09). |
| `over_budget_by`, `over_budget_reason` (new) | dec(14,2) null; char(500) | On `ProjectExpense`, `AllowanceRequest`, `SitePurchase`. Written once, at recording; frozen with the row. |
| `Attachment` | no schema change | New targets and caption values in 4.19.9. |

**Not stored:** committed, spent, remaining, work done, paid, owed, milestone due and overdue,
site accepted, collection and dispatch dates. All are queries (D23).

**Existing-constraint note.** `ProjectSerializer.sites` is writable today. DRF makes a through-model
M2M read-only, so the serializer declares `sites` explicitly and calls `network.services.set_project_sites`.
A bulk `project.sites.add()` no longer fills `organization`; the one grep hit outside tests is none,
but fixtures and factories that use it must move to the service (4.19.16 risk).

#### 4.19.3 R7: site purchases, and how each destination is costed

**Recording** (`finance.record_site_purchase`, same shape as `record_expense`): `resolve_project(site,
project)`; lines validated (at least one, quantity > 0, price ≥ 0); supplier not INACTIVE; INTO_YARD
requires `receive_into` (a YARD or STORE location) **and** a catalogue `item_type` on every line (free
text goods cannot be received into stock: `SITE_PURCHASE_YARD_NEEDS_CATALOGUE`); `amount` = Σ lines;
`_require_other_approver`; budget check (4.19.5); `_route` (PM, then Finance — PM skipped when the
recorder is the PM or the Director, as `_finance_levels`). Idempotent on `client_uuid`.
`SitePurchase` joins `engine.FINANCE_DOCUMENT_TYPES`; nothing else in the engine changes for it.

**Cost accounting.** The one rule: *money reaches project cost exactly once, by one route.*

| | USED_AT_SITE | INTO_YARD |
|---|---|---|
| Reaches cost | On APPROVED, as a new `costing.purchase_cost(project)` (signed, `COSTED_STATUSES`, reversals negative), a new `ProjectCost.purchases` line included in `total`. | **Not by the purchase.** `purchase_cost` filters `destination = USED_AT_SITE`. The goods become cost when issued to the project, through the ledger (`material_cost`, §4.14). |
| Gate-in | none | On APPROVED the same transaction creates a **draft** `GateIn` (below). |
| In the budget (4.19.5) | Spent, from approval (it is cost). | Committed from approval until its gate-in is POSTED; then it leaves the budget and the ledger takes over at issue. |

Honest limit, stated rather than hidden: yard stock is valued at `ItemType.unit_cost` when it moves
(§3.2, D27), not at the price on the purchase. The purchase price drives the money screens and the
budget until receipt; the P&L sees catalogue value at issue. Passing the paid price into the
valuation is a ledger change and out of scope.

**Draft delivery** (`receiving.services.draft_gate_in_for_purchase(purchase, actor)`, called from
`finance.decide` when the entry becomes APPROVED and `destination = INTO_YARD`, inside its
transaction, under `select_for_update` on the purchase so a retried approval cannot make two):
`GateIn(source_type=PURCHASE, status=DRAFT, supplier_name=supplier.name, to_location=receive_into,
received_at=purchase_date, for_site=site, notes="From site purchase SP-…")`; one `GateInLine` per line
(`item_type`, `tracking_mode=item_type.default_tracking_mode`, `uom=item_type.uom`, `quantity`,
condition NEW, owner OWN, `for_site=site`). `SitePurchase.gate_in` is set. The storekeeper opens it as
any draft (D8): adds serials, reels or boxes, corrects quantities, posts. `for_site` means posting
earmarks the goods to that site (Q1), which is what the buyer intended. Data errors cannot surface at
approval, because record time validated the same fields; if one does (an item deactivated since), approval
fails whole with `YARD_DELIVERY_FAILED` and nothing is half-done. `PROTECT` stops a linked draft being
deleted; abandoning the purchase is a reversal.

**Reversal** (`reverse_purchase`, PM or `finance.approve`, as `reverse_expense`): a reversing row born
APPROVED. For INTO_YARD it is refused while the gate-in is POSTED (`SITE_PURCHASE_RECEIVED`: void the
gate-in first, through the existing `void_gate_in`); while still DRAFT the draft is removed with it.

**Paying** is `mark_paid`, with one extra refusal: supplier not APPROVED → `SUPPLIER_NOT_APPROVED`.
**Offline** is 4.19.11.

#### 4.19.4 R8: subcontracts, payments, and what is owed

**Subcontract** (`commercials/contracts.py`): created by the project's PM, or `finance.approve`. Sites
must belong to the project. `contract_value` may be changed by the PM or Finance; each change is an
`AuditAction` row with old and new value (history without a variation table; see assumptions).

**Jobs link.** `JobSerializer` gains `subcontract` and `over_contract_reason`. When a job is created or
re-priced as SUBCONTRACTED: if the project has exactly one ACTIVE subcontract with that subcontractor
it is chosen automatically, if several the user chooses (`SUBCONTRACT_AMBIGUOUS`), if none the job is
**allowed unlinked** and the sheet says "not under a contract" (jobs predating this stage, and POs that
never sign one, keep working). Then `committed_value = Σ agreed_price` over the subcontract's non-cancelled jobs
(open or closed, so the check sees what is awarded, not only what is done). If that plus this job exceeds
`contract_value`, `over_contract_reason` is required (`SUBCONTRACT_OVER_VALUE`); it warns and records, it does
not block. It is shown on the subcontract and to the PM.

**Payment** (`contracts.record_subcontract_payment`, `finance.approve`): `amount > 0`, `paid_on` not in the
future, `reference` required. Routing is a **second engine branch**: `required_levels` returns one level,
the project's PM (`_project_level`, so an inactive or missing PM is the same `PROJECT_HAS_NO_ACTIVE_MANAGER`),
and `can_approve` refuses the recorder as it does for finance entries. If the recorder *is* the PM there is
nobody else to give the signature, so recording is refused (`PAYMENT_NEEDS_OTHER_APPROVER`). `decide`
generalises over the entry types; the approved branch of a one-level entry moves PENDING_PM → APPROVED
directly (the guard already allows the two single steps, so the service passes through none it should not:
a payment has its own, smaller transition table). A rejection needs a reason and returns it to Finance,
who may resubmit.

**Figures** (`contracts.position(subcontract)`, a query):

| Figure | Definition |
|---|---|
| Contract value | `Subcontract.contract_value` |
| Work done | Σ `agreed_price` of its CLOSED jobs (the O11 rule: closing, not award) |
| Paid | Σ signed APPROVED payments (reversals negative); pending ones are shown separately as "awaiting approval" |
| Owed | work done − paid. Negative reads "advance paid" |
| Flags | paid > work done (advance); paid > contract value (shown to the PM when deciding) |

Neither refuses; both appear on the payment's approval sheet.

**Report.** `SubcontractorSpendReport` (`reports_finance.py`) gains `paid` and `owed` columns and a
`subcontract` filter; `delivered` and `committed` are unchanged. `paid` is one more grouped
`Sum` over APPROVED `SubcontractPayment` rows (project-filtered like the others), `owed = delivered − paid`.
Delivered work with no subcontract still counts in `delivered` (legacy), so it correctly reads as owed.
A per-subcontract report is not added; the subcontract tab (4.19.13) is that view.

#### 4.19.5 R9: committed and spent, and the over-budget reason

`commercials/budget.py::position(project) -> BudgetPosition`, pure queries, no stored numbers (D23).
**Budget** is `project.current_cost_budget` (so approved variations count, O2). **Spent** is project cost
(O11, now including `purchases`) plus the paid money cost does not see; **committed** is what will
become spend and has not yet:

| Component | Spent | Committed |
|---|---|---|
| Cost (`cost_for(project).total`: material, loss, subcontractor, labour, expenses, purchases) | all of it | |
| Allowance requests that are not floats | PAID: amount | APPROVED, unpaid: amount |
| Floats | PAID: `amount − Σ costed expenses on it − returned_amount`, at least 0 (the part not yet cost; expenses on it are already in cost) | APPROVED, unpaid: amount |
| INTO_YARD purchases | | APPROVED and gate-in not POSTED: amount (paid or not) |
| Subcontracts | advance: `max(paid − work done, 0)` | `max(contract_value − max(work done, paid), 0)` |

This is **a deliberate departure from the wording of R9** ("spent = project cost plus paid entries"),
which would count a paid expense twice, once in cost and once as paid; the table counts each shilling in
exactly one cell. Likewise an approved-unpaid USED_AT_SITE purchase or expense is *spent* (it is cost),
not committed. Committed plus spent, the figure that matters for "will we overspend", is the same under
either reading; only the split differs. Awaiting-approval entries are shown as a third line
(`pending`), outside both. `remaining = budget − committed − spent`. A project with no PO has no budget:
`position` returns `budget = None` and no remaining; a CLOSED project reads cost from its snapshot
and commits nothing (entries on it are refused already, `ProjectNotOpen`).

**Over-budget reason.** `budget.check(project, amount)` returns `over_by` when `committed + spent +
pending + amount > budget` (pending included so two simultaneous entries cannot each slip under).
Called by `record_expense`, `request_allowance`, `record_site_purchase`, **not** for: float-backed expenses
(the float was checked when requested), reversals, projects without a budget (R12), or subcontract
payments (the contract is the control, R8). Online: `over_by > 0` with no `over_budget_reason` raises
`OVER_BUDGET_REASON_REQUIRED` (400, `details` carry `over_by` only for holders of `project.view_cost`,
else just `over: true`); with a reason it records `over_budget_by` and the reason. The forms ask
*before* sending where they can (4.19.13). **On replay (offline) it never refuses**: the phone's figures
may be stale, so the server records `over_budget_by` and an empty reason, and the approver sees "over
budget, no reason given". That is R9's "warns, does not block" applied to a phone that could not know.

**Approvers see it.** The serializers of the three entry kinds expose `is_over_budget`,
`over_budget_reason` and, where `may_see_project_cost`, `over_budget_by`. The PM and Finance sheets show a
banner with the overrun and the reason; the notification payload carries it (4.19.12). The recorder sees
their own reason, not the figures.

`ProjectViewSet.performance` and the project serializer gain `budget_position` (gated by
`may_see_project_cost`, which already lets the PM see their own project's cost, O14).

#### 4.19.6 R10: site dates

`ProjectSite` is the through model: `Project.sites` is unchanged to callers (`project.sites.all()`,
`filter(sites=…)`, `ProjectFilter.site`, `reconciliation.py`'s `project__sites`), because the join table
is the same table. The migration is state-only for the table (`SeparateDatabaseAndState` with
`db_table = "network_project_sites"`), then `AddField organization` (backfilled from `project`,
then NOT NULL), `mobilised_on`, `accepted_on`, timestamps, `enable_rls`, and the isolation fixture.

| Field | Source |
|---|---|
| Mobilisation, acceptance dates | Typed by the project's PM (or `catalogue.manage`): `PATCH /project-sites/{id}` |
| Acceptance certificate | `Attachment` on `network.ProjectSite`, caption "Acceptance certificate" |
| `is_accepted` | `accepted_on` set **and** a certificate attached (R10) |
| Material collection | Earliest `GateOut.released_at` among gate-outs with `site = site` whose project is this one |
| Dispatch | Latest `GateOut.released_at`, same set |
| Waiting in the yard | The earmarks report (Q5, `material-by-site`) filtered to the site; linked, not copied |

"Whose project is this one" must use the same resolution as `engine.project_of` (a gate pass reaches its
project through its job), so routing, costing and this view cannot disagree. Collection and dispatch are
read-only fields of the site row, computed in one grouped query for the whole project (never one per
site). `GateOut.released_at` is overwritten by each release (`dispatch/services.py`), so a pass released
in two goes shows its *last* release; the "first" date is therefore the earliest *final-or-only* release
per pass. Acceptable for a date shown to a PM; recorded as an assumption, and fixable with a first-release
stamp later.

`set_project_sites` refuses removing a site that has a date, a certificate, or an entry against it
(`SITE_HAS_PROJECT_DATA`).

#### 4.19.7 R11: the PO's payments

**On the project.** `po_issue_date`, `payment_terms` (text, as typed on the PO) and `payment_terms_days`
(the number the clock uses), set with the PO (R12). The PO PDF is an attachment captioned "PO".

**Milestones** (`commercials/milestones.py`, pure, shared by the API, the report and the sweep):
`milestone_state(milestone, today, accepted_dates) -> State`.

| State | Rule |
|---|---|
| Condition met | NONE: the project has a PO. ALL_SITES_ACCEPTED: the project has at least one site and every `ProjectSite.is_accepted`. DATE: `today ≥ condition_date`. |
| `met_on` | NONE: `po_issue_date`. ALL_SITES_ACCEPTED: the latest `accepted_on`. DATE: that date. |
| Invoiced / received | Σ non-void invoices / Σ non-void receipts |
| **DUE** | condition met and nothing invoiced |
| **OVERDUE** | `invoiced − received > 0` and `today > latest invoice_date + payment_terms_days`. No terms days, never overdue (the screen says "set payment terms"). |
| Otherwise | NOT_DUE, INVOICED, PART_PAID, PAID |

The latest invoice date, not the first, so a second invoice for the same milestone restarts the clock
rather than the client being chased for something just sent. A milestone's amount is its share of
`current_contract_value`.

**Defaults.** Attaching a PO (4.19.8) seeds M1 *Deposit* (NONE), M2 *Conditional acceptance* and M3
*Final acceptance* (both ALL_SITES_ACCEPTED), **with empty shares** that Finance fills in; the project
screen warns until shares total 100% (a warning, not a block: contracts differ). Existing PO projects
get nothing automatically; Finance presses "Add default milestones". Milestones may be added, renamed,
re-shared or removed while nothing is invoiced against them; after that, only the condition date.

**Recording.** Finance (`finance.approve`) adds an invoice (date, number, amount, document) and receipts
(date, amount, reference). Receipts beyond the invoiced amount are refused (`RECEIPT_EXCEEDS_INVOICED`);
over-invoicing a milestone beyond its amount warns. Voiding needs a reason.

**Notifications.** `core.sweeps.dispatch_sweeps` (05:30 daily, §12) gains `_sweep_milestones(organization_id)`
beside `_sweep_custody_overdue`: for each OPEN PO project, DUE not yet notified → `po.milestone_due`
(stamps `due_notified_on`); OVERDUE and not notified in 7 days → `po.milestone_overdue`. Recipient: the
holders of `finance.approve` (new `Recipient.FINANCE`), in-app and email, with the project's PM copied in
app. No new beat entry.

**PO payments report** (`commercials/reports_finance.py`, `@register`, slug `po-payments`, category
Finance, `required_permission = finance.approve`): one row per PO project with value, invoiced, received,
outstanding (`value − received`), invoiced-unpaid, next milestone and its state, and a filter for status,
client, manager and "overdue only". Built on `reporting.framework` exactly as `SubcontractorSpendReport`
is (`columns`, `filters`, `rows`, `totals`), so export to CSV and the reports page need no work. The
project screen shows the same figures per milestone (amounts hidden from a viewer without
`project.view_margin` or `finance.approve`; they see states only, matching O14).

#### 4.19.8 R12: the PO that arrives late

A project without a PO is the existing work-order project (O1); it already has no budget, so entries
need no reason and `position` returns no budget (4.19.5). Adding the PO is one action,
`POST /projects/{id}/attach-po` (`po_number`, `po_issue_date`, `contract_value`, `cost_budget`,
`payment_terms`, `payment_terms_days`, optional `manager`), by the project's PM or `catalogue.manage`.
The service sets the existing fields on the **same row**, so every job, expense, gate-out and
attachment already on it stays attached; stamps `po_recorded_at`; seeds milestones (4.19.7); writes an
audit row; and tells Finance in app. It refuses when the project already has a PO
(`PROJECT_ALREADY_HAS_PO`; a changed value is a variation, O2), is not OPEN, has no manager (the existing
CHECK needs one), or the PO number is taken (D21, the existing partial unique index, reported as a field
error as `ProjectSerializer.validate` does).

Nothing is back-filled. Entries recorded before the PO have no reason and are not re-checked; the
budget position simply counts them, so a project that spent before its PO can open already over budget.
That is shown, not hidden. "PO arrived after N days" is `po_recorded_at − opened_at`.

**Dashboard list** (`GET /projects?po=none&status=OPEN`, a new `has_po` filter on `ProjectFilter`):
projects working without a PO, each with `days_without_po = today − opened_at`, longest first, for those
who hold `project.view_margin` (the owner). The dashboard card links to the project, where "Add PO"
lives. Projects created with a PO get `po_recorded_at = opened_at`.

#### 4.19.9 Documents and permissions

Documents are `Attachment`s: `ATTACHABLE_TARGETS` gains the targets below, captions are the existing
free `caption` (60 chars) chosen from a fixed list in the app (4.17.8), and PDFs are already in
`ALLOWED_CONTENT_TYPES`.

| Target | Caption | Who may attach |
|---|---|---|
| `network.Project` | PO | the project's PM, `catalogue.manage`, `finance.approve` |
| `network.ProjectSite` | Acceptance certificate | the project's PM, `catalogue.manage` |
| `commercials.Subcontract` | Contract | the project's PM, `finance.approve` |
| `commercials.SubcontractPayment` | Invoice | its recorder (Finance), while PENDING_PM or REJECTED |
| `commercials.MilestoneInvoice` | Invoice | `finance.approve` |
| `commercials.SitePurchase` | Receipt | its recorder, while PENDING_* or REJECTED (an `OWNER_RULED_TARGET`; `_require_evidence_open` extends to it) |

A new rule class, **`PROJECT_RULED_TARGETS`**, sits beside `OWNER_RULED_TARGETS` for the first three
rows, because "this project's PM" is a person, not a permission (the same reason `OWNER_RULED_TARGETS`
exists). Documents on approved or invoiced records are fixed (`ATTACHMENT_LOCKED`), except the PO,
acceptance certificate and contract, which stay replaceable by their owners: they are reference
documents, not evidence for an approval.

**Permissions.** No new codename; `finance.approve` covers Finance's recording of invoices, receipts and
subcontract payments, and marking purchases paid (stage 1 assumption 2, still true). The Finance role
(seeded in stage 1) needs nothing added. `project.view_margin` reads contract value and milestone amounts.
`_visible_to` is extended so a purchase is seen by its recorder, the project's PM, and holders of
`finance.approve` or `project.view_cost`, as expenses are.

#### 4.19.10 Endpoints

| Endpoint | Purpose | Permission |
|---|---|---|
| `GET/POST /site-purchases`, `PATCH /{id}` | Lines nested; PATCH only while PENDING_PM, by the recorder. | member |
| `POST /site-purchases/{id}/decide` · `/resubmit` · `/mark-paid` · `/reverse` | As expenses; `mark-paid` adds the supplier check. `?mine=true`, `?payable=true`. | engine · recorder · `finance.approve` · PM or `finance.approve` |
| `GET/POST/PATCH /subcontracts` | `?project=`. Detail carries the position (4.19.4), its jobs and payments. | PM of the project or `finance.approve`; read: PM, `project.view_cost` |
| `GET/POST /subcontract-payments`, `POST /{id}/decide` · `/resubmit` · `/reverse` | `?subcontract=`, `?pending=true` for the PM. | `finance.approve` to record; PM decides |
| `POST /projects/{id}/attach-po` | 4.19.8. | PM or `catalogue.manage` |
| `GET /projects/{id}/budget` · `POST /projects/{id}/budget-check` | Position; `{amount}` → `{over, over_by?}` for the form's early warning. | `project.view_cost` scoped by `may_see_project_cost`; check: member (boolean only) |
| `GET/PATCH /project-sites`, `?project=` | Dates plus the derived collection and dispatch. | PM or `catalogue.manage`; read member |
| `GET/POST/PATCH/DELETE /projects/{id}/milestones` · `POST /projects/{id}/milestones/defaults` | 4.19.7 rules for edit and delete. | `finance.approve`; read: states to PM |
| `POST /milestones/{id}/invoices` · `/receipts`, `POST /milestone-invoices/{id}/void` (and receipts) | Reason on void. | `finance.approve` |
| `GET /reports/po-payments` | Through the existing report framework. | `finance.approve` |
| `GET /projects?po=none` | Dashboard list. | `project.view_margin` |
| `POST /sync/submit` | New operation `SITE_PURCHASE`. | member |

**`approvals/pending`** summarises the two new document types in `get_document`; the engine's
`FINANCE_DOCUMENT_TYPES` gains `commercials.SitePurchase` and a sibling set holds
`commercials.SubcontractPayment` (one level, same self-approval rule).

#### 4.19.11 Offline (R6, §8)

Only R7 is captured offline; contracts, payments, milestones and dates are desk work and need a
connection (§8.3 unchanged). `SyncOperation.SITE_PURCHASE` and `sync/services._apply_site_purchase`,
the same shape as `_apply_expense`: validate through the online serializer, call `record_site_purchase`,
so `resolve_project`, supplier, destination and approver checks all run on replay. Lines travel inline;
`supplier` by server id; an unapproved supplier is nameable but unpayable. "Add new supplier" is
online-only (it is §4.20's flow), so a phone that lacks a supplier queues nothing for it. Photos reuse the
stage 1 queue (`photos` table, `client_uuid`, "photos on the way"). Refusals become `SyncException`s the
person can fix and resend (`supersedes_client_uuid`). The over-budget flag never refuses a replay (4.19.5).

The **bundle** adds `suppliers` (non-INACTIVE: id, name, status) from §4.20's list, and per OPEN PO project
`budget_headroom`, **only for a user who may see that project's cost**, so the form can ask for a reason
offline. Locations and item types are already in it. The phone runs `features/money/rules.ts` helpers
for line totals and the headroom comparison; the server decides.

#### 4.19.12 Notifications

Reused unchanged, by adding the new documents to `notifications/events._FINANCE_TARGETS`:
`finance.awaiting_approval`, `.approved`, `.rejected`, `.paid`. `_finance_payload` gains
`over_budget_by` and `over_budget_reason` (all three entry kinds), `supplier` and `destination` for
purchases, and for subcontract payments `work_done`, `paid` and `contract_value`. New events in
`matrix.py`:

| Event | Recipients | Channels |
|---|---|---|
| `po.milestone_due` | new `Recipient.FINANCE` (holders of `finance.approve`) | in-app, email |
| `po.milestone_overdue` | `Recipient.FINANCE`, PM in app | in-app, email |
| `po.attached` | `Recipient.FINANCE` | in-app |
| `purchase.yard_delivery_expected` | `Recipient.STOREKEEPERS` | in-app |

SMS stays off (D30).

#### 4.19.13 Frontend

- **Money** (`features/money/`): `RecordPurchasePage.tsx` (site first, project resolved as
  `SiteProject.tsx` does; supplier picker; destination toggle; lines editor with catalogue search or
  free text, quantity, price, live total; `receive_into` shown for yard goods; `PhotoCapture` for the
  receipt) and a Purchases list in My expenses, with "Waiting to send" for queued ones. `api.ts`,
  `types.ts`, `queued.ts`, `bundle.ts`, `offline.ts` extend; `rules.ts` gains `lineTotal`,
  `purchaseTotal`, `overBudget(headroom, amount)` with Vitest tests. When the form's amount (or the cached
  headroom) shows an overrun it asks for the reason before sending.
- **Approvals** (`features/dispatch/FinanceApprovals.tsx`): a Purchases tab and, for PMs, a Subcontract
  payments tab, level-aware like the others; every sheet shows the over-budget banner, lines, supplier
  status, and for a yard purchase "will create a delivery". **To pay** (`ToPayPage.tsx`) lists payable
  purchases and blocks Mark paid with the supplier's status.
- **Project screen** (`features/projects/ProjectsPage.tsx` is 670 lines; new panels are separate files,
  loaded as tabs): `BudgetPanel` (budget, spent, committed, pending, remaining, with the components
  expandable), `SitesPanel` (dates, derived collection and dispatch, certificate, accepted badge, link to
  the earmarks report), `SubcontractsPanel` (contract, value, work done, paid, owed, payments, document),
  `MilestonesPanel` (per milestone: share, condition, state, invoice and receipt forms), and
  `AttachPoSheet` for a project without a PO.
- **JobSheet.tsx** gains the subcontract picker and the over-contract reason, under the delivery mode it
  already has.
- **Dashboard** (`DashboardPage.tsx`): the "working without a PO" card (R12).
- **Receiving**: the draft list badges "from purchase SP-…".
- **Reports**: nothing; `po-payments` appears through the registry, and `subcontractor-spend` shows its two
  new columns.

#### 4.19.14 What changes for existing data

- `Project.sites`: state-only through-model conversion; every existing link survives with null dates and
  `organization` backfilled from its project.
- Existing PO projects: `po_recorded_at = opened_at`, no issue date, no milestones (Finance adds them).
- Existing subcontracted jobs: `subcontract = NULL`; the report treats them as before.
- `ProjectCost` gains `purchases` (0 for every old row); `from_snapshot` reads it with a default, so
  closed-project snapshots stay exactly as frozen (O13).
- `ProjectExpense` and `AllowanceRequest` gain the two nullable over-budget columns; old rows have none.

#### 4.19.15 Errors

| Code | HTTP | When |
|---|---|---|
| `OVER_BUDGET_REASON_REQUIRED` | 400 | The entry would pass the budget and no reason was given (online only). |
| `SITE_PURCHASE_YARD_NEEDS_CATALOGUE` | 400 | An INTO_YARD line has no catalogue item, or no `receive_into`. |
| `SITE_PURCHASE_RECEIVED` | 409 | Reversing a purchase whose gate-in is posted. |
| `YARD_DELIVERY_FAILED` | 409 | The draft gate-in could not be built at approval; nothing was approved. |
| `SUPPLIER_NOT_APPROVED` | 409 | Mark paid on a purchase whose supplier is not APPROVED (R7, R15). |
| `SUPPLIER_INACTIVE` | 400 | Recording against an INACTIVE supplier. |
| `SUBCONTRACT_AMBIGUOUS` | 400 | Two or more active contracts with that subcontractor and none chosen. |
| `SUBCONTRACT_OVER_VALUE` | 400 | Awarded jobs would pass the contract value and no reason was given. |
| `SUBCONTRACT_MISMATCH` | 400 | Job and subcontract differ in project or subcontractor. |
| `PAYMENT_NEEDS_OTHER_APPROVER` | 409 | The recorder is the project's PM. |
| `PROJECT_ALREADY_HAS_PO` | 409 | Attaching a second PO. |
| `RECEIPT_EXCEEDS_INVOICED` | 400 | More received than invoiced. |
| `MILESTONE_LOCKED` | 409 | Changing the share of a milestone that has an invoice. |
| `SITE_HAS_PROJECT_DATA` | 409 | Removing a site that has dates, a certificate or entries. |

Existing codes reused: `PROJECT_AMBIGUOUS`, `SITE_HAS_NO_OPEN_PROJECT`, `SITE_NOT_ON_PROJECT`,
`PROJECT_HAS_NO_ACTIVE_MANAGER`, `FINANCE_NO_OTHER_APPROVER`, `FINANCE_SELF_APPROVAL`,
`FINANCE_NOT_DECIDABLE`, `PAYMENT_REFERENCE_REQUIRED`, `ATTACHMENT_LOCKED`.

#### 4.19.16 Testing

- **Backend, purchases:** routing as expenses (PM-recorded, Director-recorded); self-approval refused;
  USED_AT_SITE raises `expense`-style cost on APPROVED and PAID once, a reversal lowers it; **INTO_YARD
  adds nothing to cost on approval or payment, creates exactly one draft gate-in with the right lines,
  location, supplier and `for_site` (also under a repeated or concurrent approval), and cost rises only
  when the stock is issued**; free-text lines refused for yard goods; reversal blocked once the gate-in is
  posted and removes a draft; unapproved supplier recordable but not payable; idempotent `client_uuid`.
- **Subcontracts:** auto-link of a single contract, ambiguity, mismatch, over-value reason, jobs frozen on
  close; payment routing (PM only, recorder-PM refused, inactive PM), reject and resubmit, reversal;
  work done, paid, owed on a worked example including advance and legacy unlinked jobs; the report's new
  columns against hand-computed figures.
- **Budget:** a table-driven test of every component in 4.19.5, including a paid float with some expenses
  (no double count), a paid and an approved-unpaid request, a yard purchase before and after posting, an
  advance; reason required online, never refused on replay; float-backed and reversal skipped; no-PO project;
  concurrent pending entries; `may_see_project_cost` gating; closed project from snapshot.
- **Sites:** the through-model migration keeps links and org; `set_project_sites`; `is_accepted` needs both
  parts; collection and dispatch from gate-outs via the `project_of` resolution, in one query count.
- **Milestones:** every state in the table with date edges (due the day the last site is accepted; overdue
  the day after `latest invoice + terms`), partial receipts, void, no terms days; the sweep's idempotence
  and 7-day repeat; the report's totals.
- **PO later:** attach keeps every row and history, rejects a second PO, a taken PO number and a missing
  manager; seeds milestones; dashboard list and days.
- **Cross-cutting:** RLS and isolation fixtures for every new tenant table (N-3); attachment rules for
  each new target including `PROJECT_RULED_TARGETS`; `approvals/pending` for the new types;
  `readdress_project_requests` re-addresses open purchase and payment requests when the PM changes; the
  sync replay of `SITE_PURCHASE` (refusal, supersede, photos).
- **Frontend:** Vitest on the new `rules.ts` helpers and the queued purchase.
- **E2E (phone, then desk):**
  1. Offline, a supervisor records a yard purchase with three lines and a receipt photo.
  2. Online again it lands over budget with its reason; the PM approves, Finance approves; a draft delivery
     appears for the storekeeper; project cost has not moved.
  3. The storekeeper posts it, issues part to the project, and cost rises by the issued part only.
  4. Finance marks it paid (supplier approved), records a subcontract payment which the PM approves, and
     the subcontractor report shows paid and owed.
  5. A PO-less project gets its PO: history intact, milestones seeded, a milestone shows DUE when its
     site is accepted, and OVERDUE after the terms.

#### 4.19.17 Assumptions taken (each can change without redesign) and risks

1. A purchase of **any** non-INACTIVE supplier can be recorded; payment needs APPROVED. R7 says "cannot
   be paid"; R15 says "or used on a purchase". Chosen so a supervisor in the field is not stopped.
2. Budget split follows 4.19.5, not R9's literal wording, to avoid counting paid expenses twice. The total
   (committed plus spent) is unaffected.
3. An INTO_YARD purchase leaves the budget when its gate-in posts, because the ledger takes over at
   issue. Between receipt and issue, the money sits in stock and shows in neither (as all yard stock does).
4. Yard stock is valued at catalogue cost, not at the price on the purchase.
5. Contract value edits are audited, not versioned, and subcontracts have no variations.
6. The over-contract reason is recorded by whoever creates the job, not only the PM.
7. Default milestone shares are blank, conditions M1 none and M2, M3 all sites accepted.
8. Overdue counts from the latest invoice on a milestone.
9. "Collection" is the earliest release date among passes, "dispatch" the latest (4.19.6).
10. Replay never refuses for over-budget; it flags.
11. One permission, `finance.approve`, covers every Finance action here.
12. **Risk:** converting `Project.sites` to a through model breaks any code that bulk-adds sites without
    `organization`; mitigated by one service and a grep of fixtures and factories before merge. **Risk:**
    stage 4's `Supplier` model and stage 2's FK must land in an order that migrates (stage 4's `suppliers`
    first, or stage 2 behind a nullable FK added after).

### 4.20 Finance stage 4 — assets and suppliers (Epic R, R14–R15)

> **Status: approved 2026-10-09.**

#### 4.20.1 The decision: suppliers join the network registers, assets get one small app, nothing else is new

- **`Supplier` lives in `network`** (R15), beside `Client` and `Subcontractor` (O4). Those are the
  tenant's registers of outside parties; they share the Settings → Network screen
  (`NetworkPage.tsx`), the `TenantScopedViewSet` pattern (`network/views.py`), `network/isolation.py`
  and the quick-create registry (`features/quickCreate.tsx` → `ReferenceSelect`). A new `suppliers`
  app would duplicate all of that for one table. The `Subcontractor` docstring and requirements §7
  ("suppliers stay free text") are updated: the distinction it draws (a supplier sells goods, a
  subcontractor does work) still holds, only "free text" goes.
- **`Asset` and `AssetHandover` live in a new `assets` app** (R14). Nothing fits: `catalogue`/`stock`
  are item-type quantity ledgers (R14 says an asset is *not* stock), and `network` holds parties,
  not things owned. The app is deliberately thin: two models, one service module, one sweep.
- **Handover history is a new append-only table, not `CustodyTransfer`.** Custody transfers
  (`custody/models.py`) are built around `CustodyTransferLine` (item type, serial unit, quantity),
  post stock movements on acknowledgement, and need the receiver's acknowledgement (I5). R14 wants a
  dated record of who handed what to whom, with no acknowledgement and no ledger. Forcing an asset
  through a transfer would need a fake item type per asset and a ledger node per person. What *is*
  reused: the "both ends are users, they must differ" CHECK, the append-only trigger from
  `core/immutability.py`, and `custody.services.assert_can_deactivate` (extended, 4.20.4).
- **`Supplier` approval reuses `approvals/`** (one level, addressed by `required_permission =
  "finance.approve"`, added in 4.17), so there is no second approval implementation.
- **Documents and photos reuse `Attachment`** (`core/attachment_api.py`): two new targets, with the
  existing `caption` (4.17) naming the document kind. No document model.
- **`GateIn` and `ProjectExpense` are extended**, each by one nullable FK; their old text columns
  stay as history.

#### 4.20.2 Data model

New tables are tenant tables: `enable_rls` in migrations of their app and fixtures in
`network/isolation.py` and `assets/isolation.py` (§2, A3).

| Model / field | Shape | Notes |
|---|---|---|
| `Supplier` (`network`) | `name`, `name_key`, `kra_pin`, `kra_pin_key`, `contact_name`, `phone`, `email`, `address`, `bank_name`, `account_number`, `mpesa_type` (`PAYBILL`/`TILL`/blank), `mpesa_number`, `mpesa_account`, `status`, `is_active`, `registered_by`, `decided_by`, `decided_at`, `decision_reason`, `client_uuid` | R15. Unique `(organization, name_key)` (casefolded, whitespace collapsed) and, where not blank, `(organization, kra_pin_key)` (upper-cased, spaces stripped); both are partial/ordinary unique constraints like `uniq_subcontractor_name_per_org`. `client_uuid` unique per org, null, for offline add (4.20.6). |
| `SupplierStatus` | `PENDING`, `APPROVED`, `REJECTED` | A projection of the supplier's `ApprovalRequest`, as an expense's status is (4.17.1). |
| `GateIn.supplier` (new) | → Supplier, null, PROTECT | R15. `supplier_name` stays, filled with the supplier's name at save, so search (`search_fields`), exports and old documents need no change. |
| `Asset` (`assets`) | `type` (`VEHICLE`/`GENERATOR`/`TOOL`/`EQUIPMENT`/`OTHER`), `name`, `tag`, `tag_key`, `purchase_date`, `supplier` → Supplier null, `cost` (decimal null), `purchase_terms` (text), `make`, `model`, `insurance_expires_on`, `inspection_expires_on`, `holder` → User null, `status` (`ACTIVE`/`CLOSED`), `closed_on`, `closed_reason` (`SOLD`/`WRITTEN_OFF`), `closed_note`, `closed_by`, `insurance_alerted_for`, `inspection_alerted_for` | R14. `tag` is the registration for a vehicle. Unique `(organization, tag_key)` where not blank (upper-cased, spaces and dashes stripped, as `Casual.id_number_key`). `holder` null means **held in the yard**; it is a projection of the last handover, not a second truth. `make`, `model` and the expiry dates are for `VEHICLE` only; `clean` refuses them on other types. Never deleted: `delete` raises, as `ProjectVariation` does. |
| `AssetHandover` | `asset`, `from_holder` null, `to_holder` null, `handed_over_by`, `handed_over_on` (date), `note` | Append-only (trigger). Null on either side means the yard. CHECK: not both null and `from_holder` ≠ `to_holder`. The first row (from nobody to the first holder or the yard) is written when the asset is created. |
| `ProjectExpense.vehicle` (new) | → Asset, null, PROTECT | R14. `vehicle_reg` stays: it holds the typed registration on old rows, and on new rows is filled from the asset's tag so existing fuel reports read as they do. |
| `Attachment` targets | `assets.Asset`, `network.Supplier` | `caption` (char 60, from 4.17) carries the kind: asset Photo / Contract / Logbook / Insurance / Warranty / Other; supplier KRA certificate / Certificate of incorporation / Other. |

**Supplier states.** `usable` means `status = APPROVED` and `is_active`. A supplier is deactivated,
never deleted; once any `GateIn`, `Asset` or purchase names it, PROTECT keeps it, and `delete` raises
regardless, as `Subcontractor` never offers one. A deactivated supplier stays on the documents that
name it and drops out of pickers.

**Sensitive edits go back to Finance.** Changing `kra_pin` or any payment detail of an APPROVED
supplier sets it back to PENDING with a new request: the bank account a payment goes to is the most
valuable thing on the record, and "approved" must mean approved *as it now reads*. A PENDING supplier
is still named on gate-ins; it cannot be paid (4.20.3).

#### 4.20.3 Supplier flow (R15)

```
add ─► PENDING ─Finance approves─► APPROVED ──edit PIN or payment──► PENDING
          │  ▲                        │
      reject  resubmit            deactivate / reactivate
          ▼  │
       REJECTED
```

- **Adding.** Any member (`POST /suppliers`). Only `name` is required, so the gate-in clerk can add
  "Kenya Cable Ltd" with a phone in ten seconds and move on. `kra_pin` is required to **approve**,
  not to add (`SUPPLIER_PIN_REQUIRED`), and so is at least one payment route, because "checked
  businesses" (R15) is the point of approval.
- **Approval** goes through `approvals.engine`: `required_levels` gains a branch beside
  `_finance_levels` for `network.Supplier`, returning one level addressed to
  `required_permission="finance.approve"`, no `due_at`, no PM level (a supplier has no project).
  `SUPPLIER_DOCUMENT_TYPES` joins `FINANCE_DOCUMENT_TYPES` in `can_approve`, so the registrar can
  never approve their own entry (`FINANCE_SELF_APPROVAL`); `requested_by_id` aliases
  `registered_by_id`. `approvals/pending` surfaces it to holders of the permission through the
  fixed query of 4.17.6.
- **Services** (`network/suppliers.py`): `add_supplier`, `update_supplier` (detects a sensitive
  change and records it), `decide_supplier`, `resubmit`, `set_active`, `link_history`,
  `assert_payable(supplier)`. Each writes an audit row.
- **Duplicates.** A repeated `kra_pin_key` is refused with `SUPPLIER_PIN_DUPLICATE` naming the
  existing supplier (its id, name and status); a repeated `name_key` with `SUPPLIER_NAME_DUPLICATE`.
  The UI turns either into "Use <existing> instead", which is the quick-create's resolution too.
- **The hook §4.19 uses.** `assert_payable(supplier)` raises `SUPPLIER_NOT_APPROVED` (409) unless
  `usable`. R7 calls it when a site purchase is **approved for payment** and again at `mark_paid`;
  it is the only definition of "may be paid or used on a purchase", so §4.19 holds no copy. A
  supplier can be named on a draft purchase while PENDING; naming is not paying.

#### 4.20.4 Asset flow (R14)

- **Create** (`asset.manage`): fields as the table. A vehicle needs `tag`; others may leave it
  blank. `holder` is chosen at creation (a person or the yard), which writes the first handover.
- **Hand over** (`POST /assets/{id}/handover`, `{to_holder|null, note?, handed_over_on?}`): allowed to
  `asset.manage` or the **current holder** (giving it on is natural; taking it from someone is not).
  `handed_over_by` is the caller. Under `select_for_update` on the asset, it checks the target
  differs from `holder` (`ASSET_ALREADY_WITH`), appends the row and updates `Asset.holder`, so two
  simultaneous handovers cannot both start from the same holder. The date defaults to today and may
  be back-dated, never future (`ASSET_DATE_IN_FUTURE`). Handing to an inactive user is refused.
- **History** is the handover rows newest first: date, from, to, by, note. R14's "every holder" is
  this list.
- **Close** (`POST /assets/{id}/close`, `asset.manage`): `{closed_on, closed_reason, closed_note}`;
  the reason is required, the date may not precede `purchase_date`. A CHECK mirrors
  `a_decided_expense_records_when`: `CLOSED` needs date and reason. Closing writes a final handover
  to the yard when someone holds it, so history ends clean. A closed asset is frozen apart from
  attachments; it cannot take fuel (`ASSET_CLOSED`) or be handed over. No reopen in v1.
- **Leaving staff.** `custody.services.assert_can_deactivate` also counts open assets whose `holder`
  is the user and raises the existing `HolderStillHasMaterial` with an `assets` list in `details`,
  so B3's "cannot deactivate while holding" covers a company vehicle, with no new rule.
- **Fuel by vehicle.** `GET /assets/{id}/fuel?from=&to=` returns litres, spend, fill count, and
  spend per litre where litres are known, over `ProjectExpense` rows whose `vehicle` is the asset.
  Spend counts `status in (APPROVED, PAID)`, the same set as `costing.expense_cost`; pending is shown
  separately as "awaiting approval". The asset detail shows the current month and last 90 days, and
  `GET /assets/fuel-summary` ranks vehicles for the owner. No new cost source: it is a filtered
  read of the one that exists.
- **Recording fuel** (`commercials/finance.record_expense`): for a `FUEL` category the expense needs
  `vehicle` (an open `VEHICLE` or `GENERATOR` asset), replacing "the registration is required". The
  service fills `vehicle_reg` from `asset.tag`. Old payloads sending only `vehicle_reg`
  are still accepted as **"not on the register"** (4.20.9), so replay of expenses queued before the
  upgrade does not break. `vehicle` + a different `vehicle_reg` is `FUEL_VEHICLE_MISMATCH`.
- **Expiry alerts** (R14): a new `_sweep_asset_expiries` step in `core/sweeps.sweep_tenant`, guarded
  separately like its neighbours. For each ACTIVE vehicle, if `insurance_expires_on <= today + 30` and
  `insurance_alerted_for != insurance_expires_on`, it emits `asset.expiry_due` and stores the
  expiry it alerted on; likewise inspection. Comparing with the stored date rather than "exactly 30
  days" means a missed beat run catches up, a renewal (new date) re-arms by itself, and a daily
  sweep cannot nag. An already-lapsed date alerts once, worded "expired on".

#### 4.20.5 Gate-in supplier (R15; section 7 free text)

- **Capture.** `GateInCapturePage.tsx` replaces the Supplier text input (`gi-supplier`) with
  `ReferenceSelect` over suppliers (usable and PENDING, active), with "Add new supplier" opening
  `SupplierSheet` through `quickCreate.tsx`, as clients and subcontractors do. The header carries
  `supplier` (id) in place of `supplier_name`. Source `PURCHASE` shows the picker; `CLIENT_ISSUE`
  keeps `client`. Supplier stays optional, as the text was.
- **API.** `GateInSerializer` (`receiving/views.py`) accepts `supplier`; on save it sets
  `supplier_name = supplier.name`. Sending `supplier_name` alone (old queued drafts, old clients) is
  still accepted and stored as text with `supplier` null, so nothing in the offline queue breaks.
  A deactivated or REJECTED supplier is refused on a **new** gate-in (`SUPPLIER_NOT_USABLE_ON_GATE_IN`
  — PENDING is fine, per R15); a document being edited keeps one it already names. The gate-in
  list gains a `supplier` filter, and the detail shows the supplier with a status chip.
- **Reports.** Anything grouping or showing `supplier_name` keeps working unchanged; grouping by
  supplier uses `supplier_id` where present, falling back to the text.

**Matching old names (decision).** A data migration in `receiving` links `GateIn.supplier` where
`name_key(supplier_name)` equals a register `name_key` — exactly the R15 rule ("same" = casefolded,
whitespace-collapsed). It **does not create suppliers from the distinct old names.** Reasons: the
register must hold only businesses a person chose to add; old free text is full of typos and
one-offs ("kenya cable", "KCL", "Kenya Cables Ltd") and each would become a PENDING row Finance has to
approve or reject, burying the real queue; and creating them would invent registrar and PIN data
nobody supplied. Because the register is empty on the day this ships, the migration is a no-op
there; the matching that matters is `link_history(supplier)`, run when a supplier is **added or
approved** and linking every unlinked gate-in of that tenant whose key matches. History therefore
joins up as the register fills, in a single indexed update, and the rest stays text, exactly as R15
says. The same function backs a "Link past deliveries" button on the supplier detail. Both are
idempotent and never overwrite an existing `supplier`.

#### 4.20.6 Endpoints

| Endpoint | Purpose | Permission |
|---|---|---|
| `GET/POST /suppliers` (`?status=&is_active=&search=`), `PATCH /{id}` | List, add, edit. Edit by the registrar while PENDING or REJECTED; otherwise by `finance.approve`. | read and add: member |
| `POST /suppliers/{id}/decide` · `/resubmit` | `{approved, reason}` through the engine · REJECTED → PENDING. | engine · registrar |
| `POST /suppliers/{id}/deactivate` · `/reactivate` | Reactivating an APPROVED supplier needs no new approval. | `finance.approve` |
| `POST /suppliers/{id}/link-history` | `{linked: n}`. | `finance.approve` |
| `GET /suppliers?payable=true` | `usable` only; used by §4.19 pickers. | member |
| `GET/POST /assets` (`?type=&status=&holder=&search=`), `PATCH /{id}` | Register. | read: member; write: `asset.manage` |
| `POST /assets/{id}/handover` · `/close` | 4.20.4. | holder or `asset.manage` · `asset.manage` |
| `GET /assets/{id}/handovers` · `/fuel` · `/assets/fuel-summary` | History, fuel, ranking. | read: member (fuel-summary `asset.manage` or `report.view_all`) |
| `POST /attachments` | New targets; `caption` is the kind. | 4.20.7 |

`GET /approvals/pending` gains `network.Supplier` in `get_document` (name, PIN, phone, who added,
document count). The OpenAPI schema and `frontend/src/api/schema.d.ts` are regenerated.

#### 4.20.7 Permissions and visibility

- New `asset.manage` (group "Assets") in `accounts/permissions_registry.py` and
  `frontend/src/auth/permissions.ts`; Owner holds it automatically. Every member **reads** the
  register (so a fuel entry can pick a vehicle).
- `Asset.cost` and `purchase_terms` are gated through `core/field_permissions.py` to `asset.manage`,
  `finance.approve` and `project.view_cost`; the list omits them for others.
- Supplier **payment details** and PIN: shown to `finance.approve` and the registrar; everyone else
  gets name, contact person, phone and status. The picker needs nothing more.
- No new `supplier.*` permission: adding is open (R15), approving is `finance.approve`.
- Attachments: assets by `asset.manage` (documents are an office act); suppliers by the registrar
  while PENDING or REJECTED, and always by `finance.approve`. Both keep the existing allow-list,
  size, content-type and owner checks; documents are viewed through pre-signed URLs (N-7).
  A supplier's documents are fixed once APPROVED, like an expense's photos, except by
  `finance.approve`.

#### 4.20.8 Offline (§8)

- **Bundle.** `OfflineBundleView` adds `suppliers` (id, name, status; active only, no PIN or payment
  data) and `vehicles` (id, tag, name, type; ACTIVE `VEHICLE` and `GENERATOR` only). Both are small,
  and the two forms that need them, gate-in and the expense, are the two that work offline.
- **Supplier added offline.** D17 widens by one `SyncOperation`, `SUPPLIER`, handled in
  `sync/services._HANDLERS` through `add_supplier`, so duplicate checks run on replay. A gate-in
  queued after it names `supplier_client_uuid`; the queue replays in capture order (4.17.8's casual
  rule). A refused supplier (duplicate PIN or name) becomes a `SyncException`; "fix and resend" with
  `supersedes_client_uuid` works as for other entries, and the dependent gate-in waits behind it.
- **Never offline:** approving, deactivating, asset create, handover, close. Offline asset handover
  would let a stale view reassign a vehicle; a handover happens in person and online is acceptable.
- A vehicle added or closed after the last bundle shows its status at replay: a closed one is refused
  with `ASSET_CLOSED` and the entry stays on the phone with the reason (R6).

#### 4.20.9 Notifications

One new event in `notifications/matrix.py`, `asset.expiry_due`, recipient `Recipient.OWNER`, in-app
and email, SMS off (D30). Payload: asset name and tag, which document, the date, days left or
"expired". Supplier approval reuses the 4.17 events with a new subject: `finance.awaiting_approval`
(to `LEVEL_APPROVERS`, so holders of `finance.approve` other than the registrar),
`finance.approved` and `finance.rejected` (to the registrar, with the reason); `payload.kind`
distinguishes "supplier" from an expense for the template. No event for a handover.

#### 4.20.10 Frontend

- **Settings → Network** gains a Suppliers tab: list with status chips (Pending, Approved, Rejected,
  Inactive) and search, `SupplierSheet` for add and edit (details, payment, documents through
  `PhotoCapture`/file attach with a kind chooser), Deactivate, "Link past deliveries". Registered in
  `quickCreate.tsx` for the gate-in and §4.19 pickers; a duplicate PIN offers "Use existing".
- **Approvals** gets a Suppliers tab for `finance.approve`: the sheet shows PIN, payment route,
  documents and who added it, with Approve/Reject.
- **Assets** (`features/assets/`, nav entry for every member, write actions for `asset.manage`):
  register list (type, tag, holder, status), `AssetDetailPage` with details, documents, handover
  history, Hand over, Close, and a Fuel panel (litres, spend, per-litre, a month selector).
  Expiries within 30 days or lapsed show an amber or red chip.
- **Record expense** (`features/money/RecordExpensePage.tsx`): for a FUEL category the "Vehicle
  registration" input becomes a vehicle picker from the bundle, plus "Not on the register" which
  reveals the typed registration (kept as `vehicle_reg`). Local validation in `rules.ts`.
- **Gate-in**: 4.20.5. `types.ts` and the sync payload gain `supplier` and `supplier_client_uuid`.

#### 4.20.11 Errors

| Code | HTTP | When |
|---|---|---|
| `SUPPLIER_PIN_DUPLICATE` | 409 | KRA PIN already on the register; names it. |
| `SUPPLIER_NAME_DUPLICATE` | 409 | Same name key; names it. |
| `SUPPLIER_PIN_REQUIRED` | 400 | Approving with no PIN or no payment route. |
| `SUPPLIER_NOT_APPROVED` | 409 | `assert_payable` on a PENDING, REJECTED or inactive supplier. |
| `SUPPLIER_NOT_USABLE_ON_GATE_IN` | 400 | A new gate-in names an inactive or REJECTED supplier. |
| `FINANCE_SELF_APPROVAL`, `FINANCE_NOT_DECIDABLE` | 403, 409 | Existing, now also for suppliers. |
| `ASSET_TAG_DUPLICATE` | 409 | Tag or registration already on the register; names it. |
| `ASSET_ALREADY_WITH` | 409 | Handover to the current holder. |
| `ASSET_CLOSED` | 409 | Handover, edit or fuel on a closed asset. |
| `ASSET_DATE_IN_FUTURE` | 400 | A handover or closing date after today. |
| `FUEL_VEHICLE_REQUIRED`, `FUEL_VEHICLE_MISMATCH` | 400 | R1 with a register in play. |
| `HolderStillHasMaterial` (existing) | 409 | Deactivating a user who holds an asset. |

#### 4.20.12 Testing

- **Backend:**
  - Supplier add by any member; approve by `finance.approve`; the registrar refused on their own;
    reject and resubmit; PIN and name duplicates, including case and spacing; the PIN-required rule.
  - Editing a PIN or payment detail of an APPROVED supplier does **not** reopen approval (decided
    2026-10-09); the change is audited with before and after, and Finance is notified.
  - `assert_payable` for each status; deactivate and reactivate; delete refused.
  - Gate-in with a supplier fills `supplier_name`; text-only still works; inactive refused on new,
    kept on edit; search still finds by name.
  - `link_history`: exact and case/space match, idempotent, never overwrites, tenant-bound; the
    migration with and without matches.
  - Asset create writes the first handover; handover by holder, by manager, by a stranger (refused);
    concurrent handovers; history order; yard as null; close, frozen afterwards.
  - `assert_can_deactivate` with an asset; expiry sweep (30-day edge, catch-up after a missed day,
    renewal re-arms, alerts once, closed assets skipped, a failure in it does not stop the other
    sweeps).
  - Fuel by vehicle matches `expense_cost`, counts APPROVED and PAID only, and splits pending;
    legacy `vehicle_reg`-only expense accepted; mismatch refused.
  - Field gating of cost and payment details; attachments' targets and locks.
  - Sync: `SUPPLIER` then a gate-in naming it in one batch; duplicate refused and supersede;
    bundle contents (no PIN, no payment).
  - RLS and isolation fixtures for `Supplier`, `Asset`, `AssetHandover`.
- **Frontend:** Vitest on the vehicle picker rules and the supplier duplicate resolution; component
  tests for the gate-in picker's "Add new supplier".
- **E2E (phone):**
  1. Offline, add a supplier at gate-in and post the delivery naming it.
  2. Online, it lands PENDING with the gate-in linked; Finance approves it after adding the PIN.
  3. The owner adds a vehicle with insurance 20 days out; the sweep notifies once.
  4. A driver records a fuel expense choosing that vehicle; once approved the vehicle's fuel panel
     shows the litres and spend.
  5. Hand the vehicle to the driver and back to the yard; history shows both.

#### 4.20.13 Assumptions to confirm

- A supplier added by the **only** holder of `finance.approve` cannot be approved by anyone else.
  4.17 refuses recording for that reason (`FINANCE_NO_OTHER_APPROVER`); here it is not refused, so
  the supplier waits PENDING (usable on gate-ins) until a second approver exists.
- Editing PIN or payment details does not reopen approval (decided 2026-10-09); it is audited and
  Finance is notified.
- Fuel may be recorded against a `GENERATOR` as well as a `VEHICLE`, and "Not on the register"
  remains as a typed fallback, because a hired truck is not an asset.
- `asset.manage` is not added to the Finance role; the owner decides who keeps the register.
- Old free-text suppliers are not turned into register rows (4.20.5).

## 5. Approval engine

Implements `F3`, `F4`, `F5`. Lives in `approvals/engine.py` and is the only place routing is
decided.

### 5.1 Routing

```python
def required_levels(document) -> list[ApprovalLevel]:
    facts = collect_facts(document)      # categories, criticalities, ownership, quantities
    matched = [r for r in ApprovalRule.objects.active()
               if r.criticality in facts.criticalities
               and predicate_matches(r.conditions, facts)]     # conditions == {} in v1
    return dedupe_by_sequence(matched)
```

`collect_facts` gathers every fact any future predicate might need — categories, criticalities,
whether any line is client-owned, total quantity per item, monetary value if enabled, destination
type. In v1 only `criticality` is consulted, but the fact set is already complete, so switching on
a new dimension is a settings change plus a predicate function, not a migration (`F3`).

Rules for a multi-category document resolve to the **highest** applicable level (`F3`).

### 5.2 Guard rails

- **Self-approval** is blocked unless `allow_self_approval` is on. Default off (`F3`).
- Hardcoded, non-configurable escalations: any disposal of client-owned material (`J3`), and any
  stock adjustment touching client-owned stock (`E5`).
- Zero matched rules means auto-approval, recorded as an `ApprovalAction` with
  `decision = AUTO` so the audit trail never has a gap. **Every path that routes must apply this**,
  submission and amendment alike: `amend_gate_out` voids the approval and re-runs routing, and if it
  does not re-auto-approve when no rule matches, the pass sits in `PENDING_APPROVAL` with no request
  to answer and no screen that can move it. That stranded GP-000001 in a live tenant — the requester
  had a notification saying approved and a list saying pending.
- A tenant provisioned with no approval rules therefore auto-approves **everything**. That is the
  documented behaviour, not a defect — but arriving in it by default made approval look like a
  feature that did nothing. Two changes close that: provisioning seeds one starter rule
  (high-criticality material needs the Owner), and **Settings → Approvals** is where a tenant writes
  its own. Where the list is empty the screen says what that means in a sentence, rather than
  leaving somebody to infer it from a pass that approved itself.

### 5.3 Acting on an approval

1. Approver opens the deep link from the notification.
2. Authentication is **always required** (`F4`). WebAuthn where a credential is enrolled for that
   device, password otherwise.
3. Server issues a WebAuthn challenge bound to the `ApprovalRequest` id, so an assertion cannot be
   replayed against a different document.
4. On success, an `ApprovalAction` records actor, `on_behalf_of` if acting under delegation,
   `auth_method`, credential id, IP and user agent.
5. When the final level approves, the document transitions to APPROVED and `expires_at` is set from
   `gate_pass_expiry_hours`.

A beat task escalates requests past `due_at` to the configured fallback (`F5`) and expires approved
gate passes that were never released (`D32`).

### 5.4 Routing project material (`O6`, `D22`)

`ApprovalRequest.required_role` gains a sibling `required_user`, nullable, under a check that **at
most one** of the two is set — neither remains legal, because §5.2's auto-approval row needs it.

```python
def required_levels(document) -> list[RequiredLevel]:
    project = project_of(document)          # gate_out.job.project, else None
    if project is not None:
        return [RequiredLevel(sequence=1, user=project.manager)]
    facts = collect_facts(document)         # unchanged from here down
    ...
```

The criticality path is untouched, and project material never reaches it. This is a **branch, not a
rule row**, deliberately: a rule that routes to "the manager of whichever project this happens to be
for" cannot be expressed in a table keyed on category and criticality without inventing a placeholder
role that nobody actually holds — and that placeholder would then be grantable to anyone.

What changes for project material, and only for it:

- **Self-approval is permitted** (`O6`). The `ApprovalAction` is written with `self_approved = True`
  rather than leaving §10 to compare `requested_by` against `actor` across two tables later.
- **No escalation.** `due_at` is left null on a PM level, so the beat task skips it rather than
  needing a special case (`D22`).
- **No delegation.** `resolve_delegate()` is not consulted. A delegation that lends the Approver role
  does not lend a named person's signature, and treating it as though it did would forge exactly the
  thing §4.8 exists to evidence.
- **An inactive PM blocks.** Routing raises a domain error naming the project and saying an owner
  must reassign the manager (`D28`). It must **not** fall through to criticality routing: that would
  quietly restore a weaker control at the one moment nobody is watching for it.

`project_of(document)` is the single place attribution is decided, for gate-outs today and disposals
under `O10`. A gate-out carries `job` as an attribution, not a destination — §4.7's
exactly-one-destination constraint is unaffected, and where the destination is a site the job must be
a job at that site (`O5`).

Disposal (`O10`) is the one place the PM level **adds** to the existing rules rather than replacing
them: `required_levels` returns the disposal's own matched levels with the PM prepended. Disposal is
permanent, so nothing already in place is given up for it.

---

## 6. API surface

REST, `/api/v1/`, JSON, OpenAPI generated by drf-spectacular (`N-10`). Cursor pagination on list
endpoints. All list endpoints support `search`, ordering and filtering via django-filter.

**Convention:** collections are nouns; state changes are explicit sub-resource `POST` actions, never
a `PATCH` on `status`.

**No trailing slashes**, on router routes and hand-written paths alike — every URL in this table is
the canonical form. A router registered the other way answers only `/gate-outs/`, and Django's
`APPEND_SLASH` cannot redirect a `POST` without discarding its body: every write from a client
calling the documented form becomes a 500 rather than a redirect. Found from the browser after the
fact, so it is written down here.

**But a slash is forgiven, not refused.** One form being canonical does not mean the other should
fail obscurely. `core.api_urls.ApiUrlCanonicalisationMiddleware` redirects `/api/…/` to `/api/…` with
**308**, which — unlike 301 or 302 — obliges the client to repeat the method and the body, so a
redirected `POST /gate-outs/{id}/release` still releases. It redirects only when dropping the slash
resolves; otherwise the path is simply wrong and a redirect would move the 404 rather than fix it.

This came from a second browser report. A tab left open across a deployment kept an older bundle that
appended the slash, and the log showed the same endpoint answering both ways:

```
"GET /api/v1/gate-ins?page_size=50"   200
"GET /api/v1/gate-ins/?page_size=50"  404
```

The 404 was Django's HTML page, so the frontend had no message to read and printed
`Request failed (404).` beside an empty list — indistinguishable, to a storekeeper, from an empty
yard. Hence the second half of the middleware: **any unmatched path under `/api/` answers in the
envelope** (§6.1), so a caller always has a sentence rather than a status code.

| Area | Endpoints |
|---|---|
| Auth | `POST /auth/login`, `/auth/refresh`, `/auth/logout`, `/auth/password-reset`, `/auth/password-reset/confirm` |
| WebAuthn | `POST /auth/webauthn/register/begin`, `/register/complete`, `/auth/webauthn/assert/begin`, `/assert/complete` |
| Me | `GET /me` (user, roles, resolved permissions, org settings) |
| Users & roles | `/users`, `/roles`, `/permissions`, `/delegations` |
| Catalogue | `/item-categories`, `/item-categories/{id}/custom-fields`, `/item-types` |
| Network | `/clients`, `/sites`, `/sites/{id}/references`, `/projects` (was `/work-orders`) |
| Locations | `/locations`, `/stock-nodes` |
| Stock | `GET /stock/balances`, `/stock/as-of?date=`, `/serial-units`, `GET /serial-units/{serial}/history`, `/reels`, `/stock-counts`, `POST /stock-counts/{id}/post` |
| Receiving | `/gate-ins`, `POST /gate-ins/{id}/post`, `POST /gate-ins/{id}/void` |
| Dispatch | `/gate-outs`, `POST /gate-outs/{id}/submit`, `/{id}/amend`, `/{id}/cancel`, `/{id}/release`, `/{id}/close`, `GET /gate-outs/{id}/pdf` |
| Approvals | `/approval-rules`, `GET /approvals/pending`, `POST /approvals/{id}/approve`, `/{id}/reject` |
| Jobs | `/jobs`, `POST /jobs/{id}/closeout`, `POST /jobs/{id}/close`, `GET /jobs/{id}/reconciliation`, `POST /jobs/{id}/closeout/{cid}/confirm-cost` (`O8`) |
| Projects | `/projects`, `/projects/{id}/variations`, `POST /projects/{id}/close`, `POST /projects/{id}/reopen`, `GET /projects/{id}/performance` (`O12`) |
| Subcontractors | `/subcontractors` (`O4`) |
| Expenses | `/project-expenses`, `POST /project-expenses/{id}/approve`, `/{id}/reject`, `/{id}/reverse`, `/expense-categories` (`O16`) |
| Day rates | `/day-rates` on users and roles — gated by `project.view_rates` (`O14`, `O15`) |
| Custody | `GET /custody/holders`, `GET /custody/overdue`, `/custody-transfers`, `POST /custody-transfers/{id}/acknowledge` |
| Disposition | `GET /quarantine`, `/dispositions`, `POST /dispositions/{id}/submit`, `/{id}/approve`, `/{id}/reject`, `/{id}/post`, `/disposals` and the same four actions, `GET /disposals/{id}/certificate` |
| Client returns | `/client-return-acks`, `GET /client-position`, `GET /gate-outs/{id}/waybill` |
| Attachments | `POST /attachments`, `GET /attachments?target_type=&target_id=`, `GET /attachments/{id}/url`, `DELETE /attachments/{id}`, `GET /attachment-targets` |
| Notifications | `GET /notifications`, `POST /notifications/{id}/read`, `/notification-settings` |
| Reports | `GET /reports/{slug}`, `POST /reports/{slug}/export` |
| Sync | `POST /sync/submissions`, `GET /sync/exceptions`, `POST /sync/exceptions/{id}/resolve` |
| Platform admin | `/admin-api/organizations`, `POST /admin-api/organizations/{id}/suspend` |

`POST /attachments/presign` is not built. It exists for direct browser-to-S3 uploads of large
files; every attachment the requirements describe is a phone photo or a delivery note, and routing
those through the API keeps one code path that validates size, type and target. `GET
/attachment-targets` tells a screen what this user may attach to, from the same allow-list the
upload enforces, so a camera button appears exactly when the upload would succeed.

### 6.1 Error envelope

```json
{
  "error": {
    "code": "INSUFFICIENT_STOCK",
    "message": "Only 340 m remaining on drum D-0007.",
    "field_errors": { "lines.2.length": ["Exceeds remaining length."] },
    "details": { "reel": "D-0007", "remaining": "340.000" }
  }
}
```

Domain exceptions subclass `DomainError` with a stable `code`, mapped to 400/409 by a DRF exception
handler. The frontend switches on `code`, never on message text. `field_errors` maps directly onto
react-hook-form paths.

---

## 7. Frontend architecture

### 7.1 Structure

```
src/
  api/            generated OpenAPI client, query hooks
  auth/           session, permission gates, WebAuthn helpers
  offline/        Dexie schema, mutation queue, sync engine
  components/     shared UI (shadcn wrappers, scanner, signature pad)
  features/
    gate-in/  gate-out/  approvals/  stock/  jobs/  custody/
    catalogue/  sites/  reports/  settings/  users/
  routes/         route definitions, code-split per feature
```

**Code splitting has a cost that has to be paid for.** Because each screen is a separate chunk, the
page holds filenames belonging to the build it loaded with. Deploy again and the tab somebody left
open asks for a chunk that no longer exists: a 404, a rejected dynamic import, and — with no error
boundary above it — an unmounted tree and a blank white screen. It struck the first tap after a
release, on the three screens people use most, and on a phone in a yard a blank screen is
indistinguishable from a dead app.

Two pieces answer it. `routes/lazyRoute.ts` wraps every lazy import and, on a chunk failure, reloads
once — a reload fetches the current `index.html` and with it the current chunk names. The reload is
guarded twice: once per session, so a chunk that is genuinely missing cannot loop, and only when
online, because offline the chunk is absent from the service-worker cache and no reload will produce
it. `components/ErrorBoundary.tsx` sits above the router as the floor under all of it, telling the
three cases apart — a new version (reload), offline (go somewhere cached), or a genuine bug — and
always leaving something to tap. `e2e/stale-bundle.spec.ts` pins both.

### 7.2 Role-driven navigation

`GET /me` returns resolved permissions. A `<Can permission="gate_out.approve">` component and a
`usePermission()` hook drive both navigation and control visibility, so the same build serves every
role (`B4`). Permissions are re-checked server-side on every call — the frontend gate is UX, not
security.

### 7.3 Mobile-first specifics

- Primary actions are bottom-anchored and thumb-reachable; targets at least 44px.
- The bottom bar carries **four tabs and a More button**, not the first five nav items. A bar fits
  about five targets; an owner has twelve destinations. Taking the first five silently made the rest
  unreachable on a phone — no Reports, no Settings, no Exceptions, no Quarantine — while the sidebar
  showed them all, so the gap was invisible to anyone testing on a laptop and total for everyone
  working in a yard. More opens a sheet above the bar (so the way out stays put), closes on
  navigation, on the backdrop and on Escape, and is highlighted while the current screen lives inside
  it. Roles with five or fewer destinations get no More button at all. `e2e/navigation.spec.ts`
  asserts the rule that matters: whatever the sidebar offers a role, a phone can reach.
- Lists are cards on narrow screens, tables at `md` and above. No horizontal page scroll.
- Tabbed screens (Approvals, Network, People, Settings) change tab on a horizontal swipe across the
  content, and it has to feel like one: the pane follows the finger (`useSwipeTabs` + `SwipePane`),
  springs back from a short drag, and the next pane slides in from the side the finger was heading,
  also on a tap. The strip (`TabStrip`) scrolls sideways, never the page, to keep the selected tab in
  view — nine settings panes do not fit a phone, and a highlighted tab off the edge looks like no
  selection at all. The pane moves by direct style writes, not state, and drops its transform the
  moment the gesture ends: sheets are `position: fixed` and a transformed ancestor would trap them.
  Reduced motion keeps the change and loses the movement.
- Barcode scanning uses `BarcodeDetector` where available, `@zxing/browser` otherwise, and manual
  entry is always available (`D7` — many recoveries have no barcode at all).
- Signature capture is a canvas pad, uploaded as PNG (`G3`).
- Line entry uses a scan-or-search-then-quantity flow, optimised so a storekeeper can enter a
  multi-line delivery one-handed.

### 7.3b Finding an item (C9)

> **Status: approved 2026-10-03.**

One component, `ItemPicker` (`src/components/ItemPicker.tsx`), replaces every item `<select>`. It is
a combobox: a text input with a listbox under it (`role="combobox"` / `role="listbox"`, arrow keys
and Enter on a keyboard, 44px rows on a phone).

- **Online:** `GET /item-types?search=<text>&page_size=20&is_archived=false`, after a 250 ms pause,
  latest request wins. The API orders a search so that names starting with the text come first,
  then names containing it, then code and description matches, each alphabetically. The response's
  `count` gives "20 of N shown".
- **Offline:** the item list in the sync bundle (§8), read from IndexedDB with `readReference`, is
  searched on the device by `rankItems(items, text)` — the same order as the server. The bundle
  gains each item's `description` and `category_name` so both sides match on the same fields.
- **Empty input:** the last 8 items picked on this phone, kept in `localStorage` per tenant; reads
  and writes are wrapped so a blocked storage only loses the convenience.
- **A value not in the matches** (an edited draft) is shown by name from `GET /item-types/{id}`,
  or from the bundle offline.
- **Add new item:** the quick-create entry for `item-types` (§7.3) as the last row, for people with
  `catalogue.manage`; the created item is selected.
- The pure parts — `rankItems`, the recent-items list, the "N of M" text — live in
  `src/components/itemPicker.ts` with unit tests.

### 7.3c Cable not on a drum (D10)

> **Status: approved 2026-10-03.**

A reel-tracked item may also move as BULK: the ledger already allows a BULK movement of any item, and
gate-in already accepts a BULK line for a reel item. What D10 adds is the rule that keeps the two
honest, in `post_movement` beside the box rules (§4.15.3):

- **Loose length** at a node, for a lot, is `balance − Σ remaining_length of OPEN drums there for
  that lot`. A BULK movement **out** of an internal node for an item whose `default_tracking_mode`
  is REEL may take at most the loose length; beyond it, `ON_DRUMS_ONLY` (409) names the drums.
  ADJUST and REVERSAL are exempt, as corrections are for boxes.
- The stock API gives each balance row of a reel item `on_drums` and `loose` beside `quantity`.
- Gate-in: "Not on a drum" sets the line's tracking to BULK. Gate-out: "Loose length" does the
  same, and the existing lot choice applies.

### 7.3d Stock within reach, vendor labels and aiming the camera (E7–E10)

> **Status: proposed 2026-10-09, awaiting approval.**

**Phone bar (E7).** `NAV_ITEMS` puts Stock before Approvals, so `PhoneTabBar` shows Home, Gate-in,
Gate-out and Stock, with Approvals under More. The sidebar uses the same list and gets the same
order. More, and the Approvals row inside it, show a count badge from `approvals/pending` (its
`count`, loaded with `page_size=1`, refetched on focus) for users who can approve. A zero shows no
badge.

**Find stock on Home (E8).** This is a new `FindStock` card at the top of `DashboardPage`, above the
role panels, shown to anyone with a role.
- One text field and the existing `BarcodeScanner` (whose button reads "Scan QR code").
- **Typing** (debounced 250 ms, from 2 characters) calls `GET /stock/find?q=` and lists up to 8
  items. Tapping one goes to `/stock?item={id}`.
- **A scan, or Enter,** first calls `/stock/lookup` (the value has already been through `readLabel`).
  A hit navigates to its `resource`. On a 404, Enter keeps the item list showing, and a scan says
  "Nothing here matches …".
- Offline (`navigator.onLine` false), the field shows "Searching needs a connection."

The Stock page reads `?item=` into its item filter on load, so the link above lands filtered.

**`GET /stock/find?q=`** (new, in `stock/views.py`):
- Item types ranked with the C9 search (name, code, description), each with `on_hand`, the sum of
  balances at nodes inside the perimeter, in the item's unit.
- Items with nothing in stock are listed after those with stock, so a search for something we hold
  none of still answers "no".
- Limit 8, and one query: the balance sum is a subquery, not a per-row query.

**`GET /stock/summary`** (new) returns:

| Field | Meaning |
|---|---|
| `items_in_stock` | Distinct item types with a positive balance inside the perimeter. |
| `deliveries_7d` | Gate-ins posted in the last 7 days (not voided). |
| `earmarks` | `[{site, name, items}]`: per site, the number of distinct items earmarked (units, drums and bulk earmarks together), the 5 largest first. |
| `earmark_sites_more` | How many other sites have earmarks. |

On Home, under Find stock, an **In the yard** panel shows three tiles: items in stock (→ `/stock`),
deliveries this week (→ `/gate-in`), and earmarks by site (each → `/stock?earmarked_for={site}`,
which the Stock page passes through as a filter). An empty yard shows "Nothing in stock yet" with a
link to `/gate-in/new`. The panel uses `useResource`, so the last figures loaded remain on screen
while offline. Both endpoints need only authentication plus a role, as `GET /stock` does.

**ISO 15434 labels (E9).** This is a new rule in `read_label` and `readLabel`, run after the
gate-pass token and before JSON:
- It matches text starting with `[)>`, then RS (``), then `06`, then GS (``).
- Fields are split on GS. RS, and the trailing EOT (``), end the envelope.
- Each field is an ANSI MH10.8.2 data identifier (`\d{0,3}[A-Z]`) followed by data. A field whose
  identifier is exactly `S` gives a serial. All other identifiers (`1P`, `P`, `Q`, `1T`, `10D`,
  `V`, …) are ignored.
- If no `S` field is found, the rule falls through, as other rules do.
- A body with no separators, run together, does not match, so the existing fallback keeps it whole
  (E9 edge case).
- New vectors cover: one serial, two serials, no `S` field, run together, and a trailing EOT.

**Correcting saved serials (E9).** This is a management command,
`correct_label_serials [--org slug] [--apply]`:
- For every `SerialUnit` whose `serial_number` starts with `[)>`, it runs `read_label`.
- A reading with exactly one serial renames the unit, and any `GateInSerial` rows with the same old
  text, in one transaction per unit. Each rename writes an `AuditLog` row (new action
  `SERIAL_CORRECTED`, before and after).
- Without `--apply` it prints old → new and changes nothing.
- It skips and reports: a collision with an existing serial in the organization (`uniq_serial_per_organization`), no serial found, or more than one serial.
- The ledger is untouched, because movements point at the unit, not at its text.

**Aiming the camera (E10).** All of this is in `BarcodeScanner.tsx`:
- **One decode path for both decoders.** Every ~120 ms, the centre square of the video (40% of the
  shorter side, matching what `object-cover` shows) is drawn onto an offscreen canvas at 2×, and
  only that canvas is decoded: `BarcodeDetector.detect(canvas)` on Android, and ZXing's
  `decodeFromCanvas` (replacing `decodeFromVideoElement`) elsewhere. Codes outside the square are
  never seen by a decoder.
- When the native detector finds several codes, the one whose `boundingBox` centre is nearest the
  canvas centre wins.
- **Overlay:** the drawn box shrinks from 72% to the same 40%, with a centre cross. The rest of the
  picture is dimmed, so the user aims with the box.
- **Zoom:** if `track.getCapabilities().zoom` exists, a button cycles 1×/2×/3× (clamped to the
  range) through `applyConstraints({advanced:[{zoom}]})`. Otherwise no button is shown.
- The crop maths (video size → source rectangle) is a pure helper, `aimRegion(videoW, videoH,
  fraction)`, tested with Vitest. `continuous` mode and repeat suppression are unchanged.

**Testing.**
- Backend: `stock/find` ranking, `on_hand` inside the perimeter only, and tenant isolation;
  `stock/summary` figures on a scenario with earmarks for 6 sites; the shared label vectors; the
  command's dry run, apply, collision and audit row.
- Frontend: Vitest on the vectors (existing harness), and a small pure helper that decides what
  Enter versus a scan does.
- E2E (phone): the bar shows Stock; Home → type a seeded item → Stock filtered; Home → type a seeded
  serial and press Enter → the unit's page; More shows Approvals.

### 7.3a The product mark

The name is set as a wordmark — Archivo Semi-Condensed Bold, converted to outlines — and lives in
`components/Logo.tsx` as two inline SVGs. Outlines rather than a webfont, so there is no font to load
and nothing to fall back to on a phone with one bar; inline rather than an `<img>`, so it inherits
`currentColor` and one asset serves both the light login screen and dark surfaces.

Two forms, because one shape cannot do both jobs. `Wordmark` is the full name at roughly 4.4∶1 — the
sidebar, the login screen, the footer of every printed document. `LogoMark` is the bare `Y`, cut from
the same outlines, for anywhere too small or too square for the word: the phone header (where the
sidebar is hidden), the browser tab, the Android home screen.

The printed documents get the mark through `templates/documents/_wordmark.html`, sized in millimetres
and filled with a literal black rather than `currentColor` — WeasyPrint renders these, and inheriting
a colour through an inline SVG is the kind of thing a print engine gets wrong. The include carries no
surrounding whitespace, because it sits inside a sentence and a stray newline renders as a gap before
the full stop.

Note the division of ownership on paper: the **header** carries the *tenant's* logo, because a gate
pass is Silvertech's document, not ours. The product mark appears only in the footer, in "Produced
by". Sources, alternatives and the generator are in `docs/brand/`.

**Waiting is the mark, not a ring.** `LogoActivity` draws the letterform with a band of light sweeping
through it — across the word for a whole-screen wait, since that is the direction it is read and it
looks like the name filling in; up the `Y` everywhere else, down to 16px inside a button. The outline
stays faintly visible behind the band so the loader never reads as an empty box, and the travel stops
a little short of clearing the shape, because a longer sweep left a beat where it looked like it had
stopped. It keeps the name `Spinner` in `components/ui`, which is how one edit reached all thirty
call sites instead of twenty-six of them. `prefers-reduced-motion` replaces the sweep with a slow fade
rather than removing it — somebody who asked for less motion still needs to know it is working — and
`role="status"` with an accessible name survives either way.

**A list is in exactly one of three states.** Loading, failed, or genuinely empty — and they must
not be confusable. The screens used to render the error banner *alongside* the list, so a failed
request drew "Nothing received yet." underneath the error and the commonest reading of a broken
screen was that the yard was empty. `ListState` makes the three exclusive and gives the failed state
the one useful action: try again. The empty state stays where it was, inside `DataList`, now reached
only when the load actually succeeded.

### 7.4 Screens

| Role | Primary screens |
|---|---|
| Storekeeper | Gate-in capture, gate-out request, **gate release**, stock lookup, counts, quarantine |
| Approver / Owner | Pending approvals, approval detail with full line list, dashboards, reports |
| Technician | My requests, my custody, job list, job closeout (**days worked**, `O15`), custody transfer, **record an expense** (`O16`) |
| Admin | Users, roles, catalogue, categories, sites, clients, locations, settings, notification matrix, **subcontractors**, **day rates** |
| Project manager | My projects, project detail with cost against budget, **pending: material, closeouts, expenses**, variations, close project |
| Platform admin | Organizations list, create tenant, suspend |

**A PM approves against a number, not a feeling.** The approval screen for project material shows
the project, its budget, cost to date and what this release would add (`O6`). Nothing on it blocks:
a project over budget is stated plainly and the approve button still works (`O12`), because the
decision to spend past a budget should be made consciously rather than routed around at six in the
morning.

**When a project's PM is inactive, the gate-out screen says so in those words** and names
reassignment as the remedy (`D28`). The failure mode to avoid is a request that merely looks slow.

**A gate-out line names a lot, not just an item.** Balances are keyed by
`(node, item, owner_client, condition)`, so "five vests at Main yard" may be five of a client's and
none of the company's. The line sheet reads the balances at the chosen location and either states
the single lot it will take, offers the choice when there is more than one, or says the location
holds none — before the line is added. The check at submission stayed, because stock moves in
between, but its message now names the lot that is there and whose it is; the requester's screen is
where a mismatch should surface, not the approver's.

**A draft is not finished work.** `/gate-in/:id` offers Edit, Post and Discard while the document
is a draft, and `/gate-out/:id` offers Edit on a draft request, and the Edit link opens the capture screen itself (`/gate-in/:id/edit`) rather than a
second form that would drift from it. Editing keeps the received time the document already has — a
correction made on Friday must not record Tuesday's delivery as arriving on Friday — and does not
touch the browser's own stored draft, which belongs to a different, unsaved delivery. Discard is
refused server-side on anything posted (`D8`, `M6`).

The same screen carries the attachment control (`D6`): where an organization requires a photo or
the delivery note before posting, the refusal and the remedy have to be in the same place.

---

## 8. Offline capture and sync

Implements `N1`–`N3`, `D17`. Deliberately narrow: **gate-in and gate-out capture only.**

### 8.1 Client side

- Workbox precaches the app shell and the feature bundles for the two offline flows.
- Dexie holds: reference data (item types, locations, clients, sites, users), a **mutation queue**,
  and downloaded approved gate passes eligible for release.
- Every queued mutation carries a client-generated `client_uuid`.
- The UI shows an unmistakable offline banner and a pending-sync count. Records created offline are
  badged until confirmed.

### 8.2 Server side — idempotency

`SyncSubmission` stores `(organization, client_uuid) -> resulting document`, unique together. A
replayed `client_uuid` returns the original document with 200 rather than creating a second one, so
retries after a flaky connection cannot double-post (`N2`).

**The online path uses the same identity.** This was a gap found in use: a storekeeper pressed
"Save as draft" three times on a slow connection and got three drafts. The capture screens generate
a `client_uuid` per draft — once, kept with the draft so a reload or a retry is still the same
delivery — and send it whether the connection is good or not. `core.idempotency.already_created`
returns the existing document instead of creating a second, and the unique constraint per
organization is what makes that reliable rather than hopeful.

Disabling the button while a request is in flight is done too, but it cannot be the guarantee: the
second press leaves before the first reply arrives, and no client-side care closes that window.

The queue drains as a **batch** to `POST /sync/submissions`, and each item is applied
independently: a phone that has been offline for a shift has ten things to send, and one stale
gate-in must not strand the other nine. Every handler calls the same service a request would
(`post_gate_in`, `submit_gate_out`, `release_gate_out`) — a second posting path would be a second
set of rules, and the offline one would be the one nobody tests against.

### 8.3 The approval hole, closed

**Release of an unapproved gate-out is impossible offline** (`N3`). Approval requires connectivity,
because it requires server-side authentication and a WebAuthn challenge. Only gate passes that were
already approved *and* downloaded to the device can be released offline. Anything else queues as a
request.

This is the single most important constraint in the offline design: without it, offline mode is a
bypass around the entire approval control the system exists to provide.

Enforced in three places, deliberately: the device only ever holds passes the server said were
approved (`GET /sync/bundle` sends nothing else, and drops expired ones per `D32`); the release
screen can only act on that list; and the sync handler re-checks the status on arrival and refuses
with `OFFLINE_APPROVAL_NOT_ALLOWED`. The first two are courtesy — telling a storekeeper at the gate
rather than tomorrow — and the third is the control.

### 8.4 Conflicts

On sync, the server revalidates against current stock. If the document is no longer valid — stock
moved, a serial was issued elsewhere, a drum ran out — it is **not** force-posted and **not**
silently dropped. It lands in `SyncException` with the payload and reason, and appears in the
storekeeper's exception queue for resolution (`N3`).

---

## 9. Notifications

Implements Epic L. Provider-agnostic by design (`D19`, `N-10`).

```python
class NotificationChannel(Protocol):
    def send(self, recipient: Recipient, message: RenderedMessage) -> DeliveryResult: ...
```

Adapters: `EmailChannel` (SES), `SmsChannel` (Ujumbe SMS), `WhatsAppChannel` (Meta Cloud
API), `InAppChannel` (database). Selection is per tenant per event from
`OrganizationSettings.notification_matrix`, seeded with the default matrix in `L2`.

**Both are editable, behind `settings.manage`.** The first build shipped them read-only, reasoning
that "who is told what is a control, not a preference". That was wrong, and worth recording
because the mistake is a tempting one: it locked a decision away from the Owner and Admin roles —
the people accountable for it — in a system that already has a permission for exactly this and an
audit trail to show what changed. A control is enforced by *permitting* the change to the right
people and *recording* it, not by removing it.

Two things make editing safe rather than merely possible:

* **Nothing here is only a notification.** Every event the matrix covers is also visible in the
  app — a request with no notification still sits on the Approvals screen, an overdue item still
  appears on Custody. Muting a message never hides the work, which is what lets an owner decide.
* **The events that carry a control say so at the point of change**, in the interface, in plain
  words: turning off *gate-out awaiting approval* means approvers are not told, only that.

A channel switched off tenant-wide still beats the matrix (`channels_for`): a company with no SMS
budget must not have SMS reintroduced by a per-event setting.

### 9.1 Dispatch flow

1. Domain code emits an event inside the transaction: `emit(GateOutAwaitingApproval, gate_out)`.
2. `transaction.on_commit` enqueues a Celery task — so **a notification never blocks or rolls back
   the underlying business transaction** (`L3`).
3. The task resolves recipients from roles, renders per channel, and writes a
   `NotificationDelivery` row per recipient-channel.
4. Failures retry with exponential backoff, capped. Terminal failures are visible to admins (`L3`).

### 9.1a SMS credits (`L4`)

SMS is the only channel with a per-message cost, so it is the only one that is metered. One SMS is
one credit; one credit is KES 1.

```
SmsCreditEntry  (append-only, per tenant)
  id, organization, created_at, created_by
  kind        PURCHASE | CONSUMPTION | ADJUSTMENT
  quantity    signed integer — positive buys, negative spends
  delivery    the NotificationDelivery this paid for, when it was a send
  note        why, for a purchase or an adjustment
```

The balance is `SUM(quantity)`, cached on `OrganizationSettings.sms_credit_balance` and updated
under a row lock in the same transaction as the entry. That is the pattern the stock ledger already
uses: **the ledger is the truth and the balance is a convenience**, so the two can be reconciled and
the cached figure is never the only record.

Charging happens in `send_delivery`, the single funnel every real send passes through:

1. Before calling the provider, reserve one credit under `SELECT … FOR UPDATE`. Two events
   dispatching at once cannot both spend the last credit.
2. If there is none, the delivery is recorded `FAILED` with "No SMS credit", **not retried**, and
   the provider is never called. Retrying a message there is no credit for burns attempts and hides
   the cause.
3. If the provider refuses the message, the reservation is released — a failed send costs nothing.

Top-ups are made by the platform, from the Django admin, and recorded with the actor who made them.
There is no payment integration in v1: money changes hands outside the system and the credit entry
records that it did.

Note a deliberate simplification: a message longer than one SMS segment costs the provider more than
one message but the tenant one credit. Pricing per *message* is what an owner can reason about, and
the notification bodies are short by design. If long messages become common this becomes a per-
segment count, which is a change to one function.

### 9.2 WhatsApp — the position taken in `D30`

The WhatsApp Business API requires an approved sender and pre-registered message templates, which
takes weeks and carries per-message cost. **Ship v1 with SMS, email and in-app; leave the WhatsApp
adapter written but disabled behind the channel setting.** The approval loop is the system's core
value and must not be blocked waiting on Meta's approval queue. Enabling WhatsApp later is a
settings change, not a code change.

### 9.3 Provider credentials

Both external channels take their credentials from the environment and nothing else (`N-4`).

**UjumbeSMS** authenticates on the API key *and* the account's login email, sent as
`X-Authorization` and `Email` headers to `POST {host}/api/messaging`; the body is
`{"data":[{"message_bag":{"numbers","message","sender"}}]}` and the reply is
`{"status":{"code","type","description"},"meta":{…}}`.

**Success is `status.type`, not the code.** Two things about that envelope are easy to get wrong, and
both have been got wrong here. The first is that the status is *nested* inside `status` rather than
sitting at the top level. The second is that the code is not HTTP-shaped: a live account answers a
balance query with `{"code":"1008","type":"success"}` — a 1xxx code that means yes. Judging by
"starts with 2" reads that as a failure, and for a send that means recording a delivered message as
failed and retrying it, which is worse than failing outright: the retry queue turns the mistake into
cost and the delivery record into a lie. So the adapter reads `type`, which is the provider's own
classification and survives codes we have never seen, and falls back to 2xx only for a response
carrying no `type` at all.

A refusal is never retried — no credits, an unapproved sender ID and a rejected number are none of
them fixed by asking again. Nor is an envelope the adapter cannot classify: it fails once, loudly,
and logs the raw payload, because a message that may already have gone out must not be sent twice on
the strength of a guess.

The host is a setting: UjumbeSMS publishes two, and an account is issued against
one of them. Numbers are normalised to `254…` before sending, because `B1` lets a person be
identified by phone alone and the same technician may be stored three different ways.

`manage.py sms_selftest` checks credentials against the provider's balance endpoint — the same key,
headers and host as a send — so "does this work?" is answerable without texting a colleague, and
"are there credits?" comes back with it. An account out of credits is the commonest silent failure.

A **system check** (`notifications.W001`) warns at startup when `SMS_BACKEND` names the provider but
a credential is empty, and `W002` when the dotted path cannot be imported at all. Both exist because
there is one way to misconfigure SMS that gives no sign of being wrong: everything works, nothing
sends, and the first anybody hears is a technician who was never told about an approval. The
particular trap is the dotenv file — django-environ matches `KEY=value` and silently skips
`KEY = value`, so a credential can be present in `.env` and absent from the settings.

**WhatsApp** needs `WHATSAPP_ACCESS_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID`, and stays off until
they are set *and* the channel is enabled in the tenant's settings.

Approval deep links are short-lived signed URLs that land on the approval screen. They authenticate
nothing by themselves — the user still logs in or presents a fingerprint (`F4`).

---

## 10. Reporting

Each report in `M1` is a class with a `query(params) -> QuerySet` and a column spec, so the API
view, the Excel writer and the PDF renderer all read one definition. That is what keeps the export
consistent with what the screen showed (`M2`).

| Report | Core query shape |
|---|---|
| Stock on hand | `StockBalance` filtered to available node types |
| Stock as at date | Ledger aggregation, section 3.4 |
| Movement history | `StockMovement` filtered by item / serial / reel |
| Serial history | `StockMovement` for one `serial_unit`, chronological (`E2`) |
| Outstanding gate-outs | `GateOut` in APPROVED / PARTIALLY_RELEASED |
| Overdue returns & custody | `CustodyExpectation` OPEN past due, plus PERSON-node balances |
| Consumption per site / project | Movements to SITE and CONSUMED nodes, grouped |
| Client-owned position | `StockBalance` grouped by `owner_client` |
| Variance & exceptions | `Variance` + `ReleaseVariance` + `SyncException`, status OPEN |
| Recoveries by origin site | `GateIn` where `source_type = RECOVERY`, grouped by `origin_site` |
| Disposals & write-offs | `Disposal` with lines |
| **Project performance** (`O12`) | Per project: current contract value, budget, cost split four ways, exposure, loss, variance, margin, jobs closed over jobs opened |
| **Projects ranked** (`O12`) | The same, across projects, orderable by margin, overrun or exposure |
| **Self-approved releases** (`O6`) | `ApprovalAction` where `self_approved`, joined to project and value |
| **Uncosted and overlapping labour** (`O15`) | Closed jobs with no `JobLabour`, rows with `rate_source = NONE`, rows with `overlaps_day` |

Exports over a threshold run in Celery, write to S3, and notify with a pre-signed link.

**Financial columns are withheld at the serializer, not the template** (`O14`). Three permissions
gate them — `project.view_cost`, `project.view_margin`, `project.view_rates` — and a caller without
one gets a response with the field **absent**, not null and not zero. Hiding a number in the
interface while the API still returns it is not a restriction; it is a restriction-shaped thing that
a browser dev-tools tab defeats.

Labour reaches a PM as a single total. A PM who could see both a person's days and that person's
labour cost could divide one by the other and read their rate, so the per-person split is withheld
from the query rather than dropped from the display (`O14`).

A project's margin is reported **before** overheads, and the report says so on its face. The four
cost lines in §4.14 are the whole of it (`O11`).

---

## 11. Documents

WeasyPrint templates for the gate pass (`G4`), GRN, client return waybill (`K2`) and disposal
certificate. Each carries the tenant logo (`A4`), the document number, and a QR code encoding the
document id for gate scanning (`G5`). QR item labels are generated on demand when
`qr_labels_enabled` is on (`C8`).

**The fallback is deliberate and was invisible for too long.** Where WeasyPrint cannot be imported
the document is served as HTML, so a missing system library never stops a driver leaving with
something in their hand. But nothing said which of the two you were getting, and the dependency was
pinned to `WeasyPrint==69.1` — a version that does not exist — so it was never installed, the import
always failed, and every document in development was a web page. The tests accepted "a PDF *or*
HTML", so the suite stayed green.

Three things close that: the pin is a real version, CI installs the Pango libraries so the PDF path
is actually exercised (and a test asserts the bytes start with `%PDF`, skipped only where the host
genuinely cannot render), and `dispatch.W001` warns at startup which format the server will produce.
The response carries `X-Document-Fallback: html` when a PDF was asked for and a page came back.

On Windows, WeasyPrint needs the GTK3 runtime installed separately; without it local development
serves HTML while the deployed Linux image serves PDFs.

---

## 12. Runtime and deployment

### 12.0 Local first

**The build runs entirely locally until the customer is ready to deploy.** No AWS account, no cloud
credentials, no managed services are required to complete phases 1–8.

| Concern | Local | Production (deferred) |
|---|---|---|
| Database | Postgres 16 in Docker | RDS |
| Broker / cache | Redis in Docker | ElastiCache |
| File storage | `FileSystemStorage` under `media/` | S3 + pre-signed URLs |
| Email | `console.EmailBackend` | SES |
| SMS | Logging adapter that prints the message | Ujumbe SMS |
| Frontend | Vite dev server, proxying `/api` | S3 + CloudFront |
| Subdomains | `silvertech.localhost:5173` (resolves without hosts-file edits in modern browsers) | Wildcard DNS + ACM |

Everything above sits behind an interface — `django-storages` for files, the
`NotificationChannel` protocol for messaging (§9) — so moving to AWS is configuration, not a
rewrite. The deployment tasks are grouped in **Phase 9** of `03-tasks.md` and are not blockers for
anything else.

**The clock-driven half.** Several requirements are only met by something running on a schedule: an
overdue tool nobody is reminded about is a lost tool (`I3`), an approval nobody chases blocks a job
(`F5`), an approved gate pass that never expires is a stale authorisation (`D32`), and a return the
client never acknowledged is exposure nobody is watching (`K3`). `CELERY_BEAT_SCHEDULE` in
`config/settings/base.py` runs three entries — the sweeps at 05:30 before the yard opens, a
notification retry every fifteen minutes, and the ledger verification at 02:00. Each fans out one
child task per tenant so `TenantTask` establishes the tenant context (§2.2), and each sweep is
guarded separately: a failing reminder must not stop a control from running.

### 12.1 Production target (Phase 9, when the customer is ready)

Sized for `N-6` — modest cost, dozens of tenants before any scaling work.

| Component | Choice | Notes |
|---|---|---|
| Compute | ECS Fargate, 2 web tasks + 1 worker + 1 beat | 0.5 vCPU / 1 GB each to start |
| Load balancer | ALB, ACM certificate | Wildcard cert for `*.yardflow.co.ke` to serve tenant subdomains |
| Database | RDS Postgres 16, `db.t4g.micro`, Multi-AZ off initially | Automated backups, 7-day PITR (`N-5`) |
| Cache / broker | ElastiCache Redis `cache.t4g.micro` | Celery broker and cache |
| Frontend | S3 + CloudFront | SPA, long-cache hashed assets |
| Files | S3, versioning on, no public access | Pre-signed URLs only (`N-7`) |
| Secrets | AWS Secrets Manager | `N-4` |
| Logs | CloudWatch, structured JSON | `N-11` |
| CI/CD | GitHub Actions: test, build image to ECR, deploy service | Migrations as a one-off ECS task before rollout |

Health check endpoint returns 200 without touching the database, and its host is added to
`ALLOWED_HOSTS` so ALB probes do not generate `DisallowedHost` noise.

A documented restore drill is part of the definition of done for the infrastructure task (`N-5`).

### 12.2 The small production (what is actually deployed first)

§12.1 is the target. It is not what a first publish needs, and its cost is roughly twenty times
this one's, so the system goes live on a **single Lightsail instance** running the whole stack
under Docker Compose: Caddy, Django, Celery worker and beat, Postgres and Redis.

The runbook is [`docs/04-deployment.md`](04-deployment.md). Three decisions are worth recording
here, because each one is a trade somebody will otherwise have to re-derive.

**One box, and the backup is what makes that acceptable.** Postgres runs beside the app rather than
on RDS. That removes failover and point-in-time recovery, and it means a deploy has a few seconds
of downtime. What it must not remove is durability, so the database is dumped to S3 nightly and
`restore.sh` exists to be *run* — `N-5`'s drill is part of the definition of done, not a document.

**Certificates are issued on demand, not from a wildcard.** A wildcard certificate needs a DNS-01
challenge, which needs Route 53 credentials from the account that owns the parent domain sitting on
a box in a different account. Per-hostname issuance over HTTP-01 needs nothing but the DNS records.
But the DNS record *is* a wildcard — a tenant is a subdomain (§2.2) and the next tenant's name does
not exist yet — so every name under the domain reaches the box, and ungated issuance would let a
few thousand requests to invented subdomains exhaust the certificate authority's rate limit for the
whole domain. `GET /internal/tls-allowed` is the gate: it answers yes only for a slug that belongs
to an organization. It is unauthenticated because it is asked *during* the handshake, and it
discloses only whether a slug is in use, which the login page at that address discloses anyway. A
suspended tenant still passes, because `A2` leaves them able to log in and read.

**The SPA and the API are one origin.** Caddy serves the built frontend and proxies `/api` to
Django on the same hostname, so there is no CORS to configure, no preflight on every request, and
the tenant's subdomain reaches Django in the Host header unchanged — which is the only reason the
tenant resolves at all.

**The images are built in CI, not on the box.** A 2 GB instance already running Postgres cannot also
run `npm ci` and a Docker build without the kernel killing something, and the largest process is
usually the database. So `main` builds both images, pushes them to GHCR, and the box pulls — which
also means the artifact that was tested is the artifact that runs, rather than one built again on a
different machine. Every build carries its commit as a tag as well as `latest`, so a rollback names
a specific build. A rollback runs old code against a new schema, which is safe for an additive
migration and not for a destructive one; the way back from the latter is a restore.

Deployment is continuous: a push to `main` that passes the tests is on the box about two minutes
later. Tests gate the images and the images gate the deploy, so a red build cannot reach production.
The instance holds no standing registry credential — CI mints a token for the run and logs out
afterwards.

Migrations run in a one-off container **before** the services restart, so a failed migration leaves
the previous version serving rather than a new version addressing a schema it does not understand.

The first tenant is created with `manage.py provision_tenant`, which invites its owner to set a
password and never generates one (`A1`). `seed_demo` is not used in production and refuses to run
with `DEBUG` off: every account it makes shares a password that is written in this repository.

---

## 13. Error handling

| Situation | Handling |
|---|---|
| Insufficient stock, closed drum, duplicate serial | `DomainError` subclass, 400 with `code` and `field_errors` |
| Concurrent release of the same serial | `select_for_update()` on `SerialUnit`; loser gets 409 `SERIAL_ALREADY_ISSUED` |
| Balance race | `select_for_update()` on the `StockBalance` row inside the posting transaction |
| Ledger drift | Nightly `verify_ledger`; alert, never auto-correct |
| Notification failure | Retried, logged, never blocks the transaction (`L3`) |
| Offline sync conflict | `SyncException` queue, human resolution (`N3`) |
| Tenant mismatch | 404, never 403 (`A3`) |
| Suspended tenant write | 403 with `ORGANIZATION_SUSPENDED` (`A2`) |
| Posting an already-posted document | 409 `ALREADY_POSTED`, idempotent for a matching `client_uuid` |

---

## 14. Testing approach

| Layer | What |
|---|---|
| **Tenant isolation** | Parametrised suite over every router endpoint asserting 404 across tenants (`A3`). Non-negotiable, runs on every commit. |
| **Ledger invariants** | Property-style tests: after any sequence of postings, balances equal recomputed movements, and no negative balance exists at any node. |
| **Immutability** | Asserts `UPDATE`/`DELETE` on `StockMovement` and `AuditLog` raise at the database level, not just in Python. |
| **Approval engine** | Unit tests per routing case: single category, mixed criticality, self-approval blocked, delegation attribution, auto-approve, client-owned disposal forced escalation. |
| **Numbering** | Concurrent posting produces gap-free sequences; abandoned drafts consume no numbers. |
| **Offline idempotency** | Same `client_uuid` replayed N times yields exactly one document. |
| **Reel arithmetic** | Partial issues, over-issue rejection, auto-close at zero. |
| **Reconciliation** | Issued vs installed vs returned vs unaccounted sums correctly across a full job lifecycle. |
| **Project costing** (`O11`) | A full PO lifecycle — receive, issue, install, consume, return, lose — produces a cost equal to the four lines summed by hand. Repricing an `ItemType` afterwards leaves the closed project's figures **unchanged** (`D27`). An expectation resolved late reduces the loss without anyone editing anything. |
| **Project routing** (`O6`) | Project material routes to the PM and never to a criticality rule; a PM may self-approve and the action records it; an inactive PM raises rather than falling through to the criticality path. |
| **Financial permissions** (`O14`) | Response bodies for a storekeeper, a PM and an owner asserted **field by field** — a withheld figure must be absent, not null. The PM's labour total must not be accompanied by anything that divides into a rate. |
| **E2E (Playwright)** | Two flows on a mobile viewport: gate-in of a mixed delivery, and request → approve → release → closeout → return. |
| **Factories** | `factory_boy` with an `organization` fixture; the default test client is always tenant-scoped. |

Target: the ledger, approval engine and tenancy layers are the parts where a bug is expensive and
invisible. Coverage effort concentrates there rather than on CRUD serializers.

---

## 15. Build phases

Each phase ends with something demonstrable.

| Phase | Contents | Requirements |
|---|---|---|
| **1. Foundation** | Project scaffold, tenancy (all four layers), User/Role/Permission, audit log, numbering, settings, platform admin console, CI/CD, AWS baseline | A, B1–B4, B6, C8, M3, M6 |
| **2. Master data** | Categories, custom fields, item types + seed catalogue, locations, stock nodes, clients, sites + references, projects (unpriced) | C1–C7 |
| **3. Receiving & stock** | Ledger, balances, serial units, reels, gate-in all source types, quarantine on receipt, stock views, serial history, transfers, counts | D, E, J1 |
| **4. Dispatch & approvals** | Gate-out request, approval engine, rules admin, notifications (in-app + email), release with vehicle/driver, partial release, variances, gate pass PDF | F, G, L (partial) |
| **5. Jobs & custody** | Jobs, closeout, expectations, return matching, variances, custody views, overdue sweeps, transfers, reconciliation | H, I |
| **6. Disposition & client returns** | Quarantine decisions, disposal with approval, client returns, waybills, acknowledgement | J2, J3, K |
| **7. Reporting** | All day-one reports, Excel and PDF export, async exports, dashboards | M1, M2 |
| **8. Offline & biometrics** | Service worker, Dexie queue, sync idempotency, exception queue, WebAuthn enrolment and approval step-up, SMS channel | N, B5, F4 |
| **9. Production deployment** | The AWS move (§12.1), deferred until the customer is ready | Non-functional |
| **10. Projects & commercials** | WorkOrder→Project migration, PO fields and variations, subcontractor register, job delivery mode, PM routing, movement valuation, labour, expenses, project performance reporting, financial permissions | O |
| **11. Boxes** | Box model and ledger hooks, label reader, gate-in boxes and pallets, gate-out by box, scan-to-release and pass scanning, box screens, release by named serials | P, G1, G5 |

Phases 1–4 deliver the system's core value: controlled, approved, auditable gate movements. If the
schedule compresses, phases 5–8 are where scope can be traded, not earlier.

**Phase 10 is last by dependency, not by importance.** Project cost is a query over the ledger, the
closeouts and the custody expectations — so it cannot be built before those exist and be worth
anything. The two pieces that must land **earlier than phase 9** are the `unit_cost` columns on
`StockMovement` (§3.2) and the `required_user` column on `ApprovalRequest` (§5.4). Both belonged in
phases 3 and 4 respectively, and both phases are already built — so they arrive now as migrations
against live structures instead. Valuation cannot be backfilled onto historical movements without
guessing, so movements posted before phase 10 carry `unit_cost_source = NONE` and §10 reports the
projects that include them as partly unvalued, rather than quietly understating them.

---

## 16. Requirement traceability

| Epic | Design sections |
|---|---|
| A — Tenancy | 2, 4.1, 12 |
| B — Identity | 4.2, 5.3, 7.2 |
| C — Master data | 4.3, 4.4, 4.5 |
| D — Gate-in | 3.5, 4.6, 8 |
| E — Stock | 3, 4.13 |
| F — Gate-out & approval | 4.7, 5 |
| G — Gate release | 4.7, 7.3, 11 |
| H — Jobs & reconciliation | 4.9, 10 |
| I — Custody | 4.10 |
| J — Quarantine & disposal | 4.6, 4.11 |
| K — Client returns | 4.12 |
| L — Notifications | 9 |
| M — Reporting & audit | 3.2, 4.2, 4.13, 10 |
| N — Offline | 8 |
| O — Projects & PO performance | 3.2, 4.4, 4.9, 4.14, 5.4, 6, 10 |
| P — Boxes | 3, 4.15, 8 |
| Q — Site earmarks | 4.16, 10 |
| Non-functional | 1.1, 12, 13, 14 |

---

## 17. Positions on the settled questions

Every question the requirements carried is now decided (`D30`–`D36`). The design positions that
answer them, for anyone reading this section expecting them to still be open:

| Ref | Decided | Design position |
|---|---|---|
| `D30` | SMS + email at launch | WhatsApp adapter written and disabled by setting (§9.2); enabling it is configuration, not code |
| `D31` | Technician closes out, storekeeper may act for them | One endpoint, `submitted_by` and `on_behalf_of` distinguish them (§4.9) |
| `D32` | Gate passes expire | Configurable, 24 h default (§4.1), swept by the beat task (§5.3) |
| `D33` | One approver per level | `ApprovalRequest.level` already carries it; a `quorum` field would be the additive change if this ever reverses |
| `D34` | Metres consumed on site | Closeout lines carry `length` (§4.9) |
| `D35` | holder → storekeeper → owner | §4.10, no supervisor role introduced |
| `D36` | Storekeeper is the gate guard | `gate_out.release` stays a distinct permission (§4.2), so another tenant separates them without code |

### Risks this design carries rather than solves

| Ref | Risk | What the design does about it |
|---|---|---|
| `R1` | Labour cost rests on self-reported days, and `D31` lets a storekeeper report them secondhand | Cannot be fixed in code. §10 names closed jobs with no labour and rows with `rate_source = NONE` instead of letting them cost zero, and flags closeouts where days were claimed under `on_behalf_of`. The figure is still hearsay; the report at least says which figures are |
| `R2` | PM self-approval, no second signature on project material | §5.4 records `self_approved` on the action and §10 lists them for the owner. This is visibility, not control — the control was traded away deliberately in `D22`, and if it proves wrong the fix is a ceiling rule, which `ApprovalRule.conditions` can already express |

---

## 18. Approval

This is step 2 of 4. On approval, `03-tasks.md` breaks phases 1–8 into sequenced tasks, each
referencing a design section and requirement ID.
