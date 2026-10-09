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
