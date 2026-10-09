# Yard Inventory & Gate Control System — Implementation Tasks

**Status:** Draft v1 — awaiting approval
**Requirements:** `01-requirements.md` · **Design:** `02-design.md`

---

## How to use this document

Each task is small enough to complete and verify in one sitting. Every task carries:

- **Refs** — the design section (`§`) and requirement IDs it implements
- **Done when** — the observable condition that closes it, including its test

**Working rule:** build one task, verify it against its requirement, tick it, then pause before
starting the next. Tests are part of a task's definition of done, never a later cleanup task.

Phases are strictly ordered. Within a phase, tasks are ordered by dependency; where two tasks are
independent, that is noted.

**Legend:** `[B]` backend · `[F]` frontend · `[I]` infrastructure · `[T]` test-only

---

## Phase 1 — Foundation

Nothing in this phase is user-visible except login, but every later phase depends on it. Tenancy
and the audit trail are cheap now and near-impossible to retrofit.

- [x] **T1.1 `[I]` Repository scaffold**
  Refs: §1.2
  Two top-level directories, `backend/` and `frontend/`. Django project `config`, the app skeleton
  from §1.2, `pyproject.toml` with pinned dependencies, `docker-compose.yml` running Postgres 16
  and Redis for local development.
  *Done when:* `docker compose up` starts, `manage.py check` passes, `README` documents local setup.
  **Runs entirely locally — no cloud account needed (§12.0).**

- [x] **T1.2 `[B]` Settings and environment**
  Refs: §1.1, N-4
  Split settings (`base`/`dev`/`prod`), configuration via environment variables,
  `ATOMIC_REQUESTS = True`, `USE_TZ = True`, timezone `Africa/Nairobi`.
  *Done when:* no secret appears in the repository, and `prod` refuses to boot without required
  variables.

- [x] **T1.3 `[B]` Organization and OrganizationSettings**
  Refs: §4.1 · A1, A2, C8
  Both models with every setting field listed in §4.1 and its documented default. One-to-one,
  created together.
  *Done when:* migrations apply and a factory creates an organization with correct defaults —
  `money_tracking_enabled` false, `allow_self_approval` false, `allow_document_amendment` false.

- [x] **T1.4 `[B]` TenantModel, TenantManager, context**
  Refs: §2.1 · A3
  `ContextVar` holding the active organization, `TenantManager` that raises `TenantContextMissing`
  when unset, `all_objects` escape hatch, `save()` guard rejecting cross-tenant writes.
  *Done when:* a test proves an unscoped query raises rather than returning rows, and that saving a
  row into a foreign organization raises.

- [x] **T1.5 `[B]` TenantMiddleware**
  Refs: §2.2 · A1, A3
  Resolve organization from subdomain, then from the user; 404 on disagreement; set the
  `ContextVar` and issue `SET LOCAL app.current_org`. Celery task base class doing the same from an
  explicit `organization_id`.
  *Done when:* tests cover subdomain resolution, user fallback, mismatch → 404, and that a Celery
  task without an organization refuses to run.

- [x] **T1.6 `[B]` Row-level security**
  Refs: §2.3 · A3
  `enable_rls('app.Model')` migration helper, policies on every tenant table, and a Django system
  check that fails startup when a `TenantModel` subclass has no policy.
  *Done when:* the system check catches a deliberately unpolicied model, and a raw SQL query under
  a set `app.current_org` returns only that tenant's rows.

- [x] **T1.7 `[I]` Least-privilege database role**
  Refs: §2.3
  The application connects as a non-superuser, non-`BYPASSRLS` role. Migrations may use a separate
  privileged role.
  *Done when:* documented in `README`, and RLS is provably enforced for the app role.

- [x] **T1.8 `[B]` API base classes and error envelope**
  Refs: §2.4, §6.1 · A3
  `TenantScopedViewSet` returning 404 cross-tenant, `DomainError` hierarchy with stable `code`, DRF
  exception handler producing the §6.1 envelope with `field_errors`.
  *Done when:* a raised `DomainError` renders the documented shape, and cross-tenant access returns
  404 not 403.

- [x] **T1.9 `[B]` Custom User model**
  Refs: §4.2 · B1
  `email` and `phone` both nullable, each unique per organization, at least one required.
  `organization` nullable for platform admins.
  *Done when:* constraint tests pass, including that the same email may exist in two organizations.

- [x] **T1.10 `[B]` Permission registry, Role, assignments**
  Refs: §4.2 · B3, B4
  Static permission codename registry from §4.2, plus `Role`, `RolePermission`, `UserRole`.
  *Done when:* codenames are enumerable from one module, and roles can be created and assigned.

- [x] **T1.11 `[B]` Permission resolution and guards**
  Refs: §4.2, §7.2 · B3, B4
  Resolution service unioning roles (delegations added in T1.17), DRF permission class taking a
  codename, and the guard preventing removal of the last user holding `users.manage` plus
  `gate_out.approve`.
  *Done when:* the last-owner guard raises, and an endpoint decorated with a codename rejects a
  user lacking it with 403.

- [x] **T1.12 `[B]` Authentication endpoints**
  Refs: §6 · B1
  Login accepting email *or* phone, JWT access and refresh, logout with refresh blacklisting, `/me`
  returning user, roles and resolved permissions.
  *Done when:* login works with either identifier, a blacklisted refresh token is rejected.

- [x] **T1.13 `[B]` Password reset**
  Refs: §6 · B2
  Token-based reset delivered by email; SMS delivery behind the channel interface, stubbed until
  T8.13.
  *Done when:* the email flow completes end to end and tokens are single-use and expiring.

- [x] **T1.14 `[B]` Audit log**
  Refs: §4.2 · M3, B6
  `AuditLog` model, append-only Postgres trigger, and capture of logins, failed logins, permission
  changes and document state transitions with before/after values.
  *Done when:* `UPDATE` and `DELETE` on the table raise at the database level, and a login attempt
  produces a row with actor, IP and user agent.

- [x] **T1.15 `[B]` Document numbering**
  Refs: §4.13 · M6
  `DocumentSequence`, allocation under `select_for_update()`, allocation **at posting only**.
  *Done when:* a concurrency test posting N documents in parallel yields a gap-free sequence, and
  abandoned drafts consume no numbers.

- [x] **T1.16 `[B]` Attachments**
  Refs: §4.13, §12.0 · D6, G3, N-7
  `Attachment` generic model behind a storage interface: `FileSystemStorage` locally, S3 with
  pre-signed POST upload and short-lived pre-signed GET in production. Reads always go through a
  signed, expiring URL — locally a signed view, never a raw media path.
  *Done when:* a file uploads and is readable only through a URL that expires, with the same API
  contract under both backends.

- [x] **T1.17 `[B]` Delegation**
  Refs: §4.2 · F5
  `Delegation` model with a validity window, folded into permission resolution and tagged with its
  source so callers can attribute "on behalf of".
  *Done when:* an active delegation grants the permission, an expired one does not, and resolution
  reports the source.

- [x] **T1.18 `[B]` Tenant provisioning**
  Refs: §4.1, §12 · A1
  Create an organization with subdomain and owner invitation; seed default roles, settings, one
  yard with its quarantine location, the system stock nodes, and the starter catalogue (catalogue
  seed lands in T2.4 and is called from here).
  *Done when:* one API call produces a usable tenant, and its owner can set a password and log in.

- [x] **T1.19 `[B]` Suspension and platform admin console**
  Refs: §4.1 · A2
  Cross-tenant `platform_admin` endpoints to list, create and suspend organizations, plus a
  permission class blocking all writes for a suspended tenant while reads continue.
  *Done when:* a suspended tenant's write returns 403 `ORGANIZATION_SUSPENDED` and its reads still
  succeed.

- [x] **T1.20 `[T]` Tenant isolation suite**
  Refs: §2.4, §14 · A3
  Parametrised test walking the DRF router: create one object per endpoint in tenant A, assert
  every verb returns 404 for tenant B. Wired into CI as a required check.
  Also: CI fails when `all_objects` appears outside `platform_admin/` or migrations.
  *Done when:* the suite covers every registered endpoint and fails if a viewset skips scoping.

- [x] **T1.21 `[B]` OpenAPI schema**
  Refs: §6, N-10
  drf-spectacular, schema committed, CI check failing when the schema drifts from the code.
  *Done when:* the schema generates cleanly with no warnings.

- [x] **T1.22 `[I]` Logging and error tracking**
  Refs: §12, N-11
  Structured JSON logging with request and organization identifiers; error tracking configured for
  production.
  *Done when:* a deliberately raised exception appears in the tracker with tenant context attached.

- [x] **T1.23 `[I]` CI pipeline**
  Refs: §12, §14
  Lint, type check, pytest with coverage, frontend build. No image registry push — that arrives with
  Phase 9.
  *Done when:* a pull request runs the full suite locally and in CI.

- [x] **T1.24 `[B]` Local runtime and seed command**
  Refs: §12.0
  `console` email backend, a logging SMS adapter that prints messages instead of sending, local
  media storage, and a `seed_demo` management command creating a demo tenant with users for each
  role so any screen can be exercised immediately.
  *Done when:* `manage.py seed_demo` produces a working tenant, and `runserver` plus the Vite dev
  server give a usable app with no cloud account of any kind.

> **AWS deployment (former T1.24–T1.26) has moved to Phase 9.** It is deferred until the customer
> is ready to host, and blocks nothing in phases 1–8 (§12.0).

- [x] **T1.27 `[F]` Frontend scaffold**
  Refs: §7.1, N-1
  Vite, TypeScript, Tailwind, shadcn/ui, router with per-feature code splitting, generated API
  client from the OpenAPI schema, TanStack Query provider, mobile-first app shell with
  bottom-anchored primary actions.
  *Done when:* the shell builds, deploys to CloudFront, and renders correctly at 360px and 1280px.

- [x] **T1.28 `[F]` Session, permission gates, login**
  Refs: §7.2 · B1, B4
  Token storage and refresh, `/me` hydration, `usePermission()` hook and `<Can>` component,
  login screen accepting email or phone, protected routes, password reset screens.
  *Done when:* a user with only technician permissions sees no storekeeper navigation, and route
  guards redirect unauthenticated users.

---

## Phase 2 — Master data

Everything here is admin-facing configuration. It must exist before any stock can be recorded.

- [x] **T2.1 `[B]` ItemCategory**
  Refs: §4.3 · C1
  Two-level hierarchy, `criticality` NONE/LOW/MEDIUM/HIGH.
  *Done when:* a category tree can be created and criticality is readable for routing.

- [x] **T2.2 `[B]` CategoryCustomField**
  Refs: §4.3 · C2
  Field types text, number, date, dropdown, boolean; `required_at_gate_in`; ordering.
  *Done when:* fields can be defined per category and a validator rejects a missing required value.

- [x] **T2.3 `[B]` ItemType**
  Refs: §4.3 · C3
  All fields from §4.3 including `default_tracking_mode`, `is_returnable`, `default_return_days`,
  `min_stock_qty`, `unit_cost`, `is_archived`.
  *Done when:* an item type with movements cannot be deleted, only archived, and the archive is
  excluded from pickers.

- [x] **T2.4 `[B]` Starter catalogue seed**
  Refs: §4.3 · C3
  Seed data covering antennas, RRUs, BBUs, feeder cable, jumpers, connectors, batteries,
  rectifiers, tools and PPE, with sensible default tracking modes. Called from T1.18.
  *Done when:* a new tenant starts with a usable catalogue that can be renamed or deleted freely.

- [x] **T2.5 `[B]` Location**
  Refs: §4.5 · C4
  Tree of YARD/STORE/VEHICLE/QUARANTINE, `vehicle_reg`, deactivate-not-delete, and automatic
  creation of one quarantine location per yard.
  *Done when:* a location holding stock cannot be deleted, and every yard has a quarantine child.

- [x] **T2.6 `[B]` StockNode**
  Refs: §3.1, §4.5
  Node types from §3.1, check constraint enforcing exactly one target FK, auto-creation hooks for
  locations, users, sites and clients, plus the per-tenant system nodes (`CONSUMED`, `SCRAP`,
  generic `EXTERNAL`).
  *Done when:* creating a site or user creates its node, and a node with two targets is rejected by
  the database.

- [x] **T2.7 `[B]` Client**
  Refs: §4.4 · C5, C6
  `name`, `code`, contacts, optional `site_code_pattern` applied when validating that client's site
  references.
  *Done when:* a site reference violating a client's pattern is rejected with a clear message, and
  a client with no pattern accepts anything.

- [x] **T2.8 `[B]` Site**
  Refs: §4.4 · C6
  All fields from §4.4 including coordinates, `site_type`, `status`, and free-text `cell_id` and
  `enodeb_id` that are never validated.
  *Done when:* a decommissioned site remains queryable and selectable as a recovery origin.

- [x] **T2.9 `[B]` SiteReference and cross-reference search**
  Refs: §4.4 · C6
  Multiple labelled references per site, unique per `(site, label)`, with indexed search across
  reference values.
  *Done when:* searching an operator site code, a towerco reference or an internal reference all
  resolve to the same site.

- [x] **T2.10 `[B]` WorkOrder**
  Refs: §4.4 · C7
  Client, reference, status, many-to-many sites, and a close action that warns when material
  remains unreconciled (the warning becomes real in T5.7).
  *Done when:* work orders are optional everywhere material is issued.

- [x] **T2.11 `[B]` Master data API**
  Refs: §6
  Tenant-scoped viewsets for T2.1–T2.10 with filtering, search and the `catalogue.manage` /
  `settings.manage` permissions applied.
  *Done when:* T1.20's isolation suite covers the new endpoints and passes.

- [x] **T2.12 `[F]` Catalogue administration screens**
  Refs: §7.4 · C1, C2, C3
  Category tree with criticality, custom field builder, item type list and editor.
  *Done when:* an admin can define a category, add custom fields, and create an item type without
  developer help.

- [x] **T2.13 `[F]` Network and location screens**
  Refs: §7.4 · C4, C5, C6, C7
  Locations tree, clients, sites with a references sub-editor and map-optional coordinates, work
  orders.
  *Done when:* a site can be created with three differently-labelled references and found by any of
  them.

- [x] **T2.14 `[F]` Master settings screen**
  Refs: §4.1 · C8
  Every setting in §4.1 grouped sensibly, with money tracking, minimum stock, QR labels, asset
  tags, attachments and approval behaviour clearly labelled.
  *Done when:* toggling `money_tracking_enabled` changes whether cost fields appear elsewhere in
  the app.

- [x] **T2.15 `[F]` Users, roles and delegation screens**
  Refs: §7.4 · B3, B4, F5
  User list with role assignment and deactivation, role editor with a permission matrix, delegation
  editor.
  *Done when:* deactivating a user holding custody warns and blocks until custody is cleared
  (custody check wired in T5.8; the warning hook is added here).

---

## Phase 3 — Receiving and stock

The ledger lands here. This is the highest-risk phase in the build; test coverage is concentrated
on it deliberately.

- [x] **T3.1 `[B]` StockMovement**
  Refs: §3.2 · M3, M4
  All fields from §3.2, `save()` rejecting a second write, Postgres trigger raising on `UPDATE` and
  `DELETE`, indexes on `(organization, occurred_at)` and `(organization, item_type, occurred_at)`.
  *Done when:* a test proves both the Python and database-level immutability, independently.

- [x] **T3.2 `[B]` StockBalance and the posting service**
  Refs: §3.3 · E1
  Unique-together balance model and a single `post_movement()` service that inserts the movement and
  updates both balance rows under `select_for_update()`, in one transaction. Every later document
  posts exclusively through this service.
  *Done when:* concurrent postings against the same balance serialise correctly, and no code path
  writes `StockBalance` directly.

- [x] **T3.3 `[B]` SerialUnit**
  Refs: §3.5 · D3, E2
  Serial uniqueness per tenant, `source` MANUFACTURER/INTERNAL, denormalised `current_node`,
  `condition`, `owner_client`, `status`, `origin_site`.
  *Done when:* a duplicate serial is rejected with a message naming where the existing unit sits.

- [x] **T3.4 `[B]` Reel**
  Refs: §3.5 · D4, E3
  Drum number unique per tenant, `initial_length`, `remaining_length`, auto-close at zero,
  over-issue rejected with the remaining length in the error.
  *Done when:* partial issues decrement correctly, and issuing more than remains returns 400 with
  the remaining figure.

- [x] **T3.5 `[T]` Ledger invariant suite**
  Refs: §14 · E1
  Property-style tests: after any sequence of postings, every balance equals the recomputed sum of
  movements, no node holds a negative balance, and serial/reel denormalisation matches the ledger.
  *Done when:* the suite runs on every commit and fails if `post_movement` is bypassed.

- [x] **T3.6 `[B]` verify_ledger command**
  Refs: §3.3, §13
  Management command recomputing balances from movements and reporting drift. Nightly beat schedule
  that alerts and never auto-corrects.
  *Done when:* deliberately corrupting a balance row is detected and reported, not silently fixed.

- [x] **T3.7 `[B]` GateIn models**
  Refs: §4.6 · D1, D2
  `GateIn` with all six source types, `GateInLine` with condition and ownership,
  `GateInSerial`, `GateInReel`, draft status, `client_uuid`.
  *Done when:* a mixed delivery with serialized, bulk and reel lines saves as a draft.

- [x] **T3.8 `[B]` Asset tag generation**
  Refs: §4.1, §4.6 · D3
  Generator honouring `asset_tag_prefix_format`, active only when `asset_tag_enabled`. When
  disabled and no serial is available, the line must be received as bulk with `no_serial_reason`
  recorded.
  *Done when:* both paths are covered — tags generated when enabled, forced-bulk with a recorded
  reason when not.

- [x] **T3.9 `[B]` GateIn posting**
  Refs: §4.6 · D1, D2, D3, D4, J1
  Posting allocates the number, creates serial units and reels, posts movements from the
  appropriate `EXTERNAL` node, and routes FAULTY, DAMAGED and SCRAP lines to the quarantine node
  instead of free stock. All in one transaction.
  *Done when:* a posted gate-in produces correct balances, and quarantined lines never appear as
  available stock.

- [x] **T3.10 `[B]` GateIn void and reversal**
  Refs: §3.2, §4.13 · M4, M6
  Void posts `REVERSAL` movements, retains the number, and marks the document void. Amendment is
  possible only when `allow_document_amendment` is on, requires elevated permission and a reason,
  and retains the prior version.
  *Done when:* a voided gate-in leaves balances at their pre-posting values and its number is never
  reused.

- [x] **T3.11 `[B]` Custom field and attachment validation on gate-in**
  Refs: §4.3, §4.13 · C2, D6
  Required custom fields enforced at posting; attachments enforced when
  `attachments_required_gate_in` is on.
  *Done when:* posting without a required custom field or attachment returns 400 with the field
  path.

- [x] **T3.12 `[B]` Stock read API**
  Refs: §3.3, §6 · E1
  Balances endpoint with filtering by item, location, owner, condition; availability excluding
  quarantine, scrap, consumed, site and client nodes; client-owned stock flagged in the payload.
  *Done when:* a client-owned item and an own-stock item of the same type report separately.

- [x] **T3.13 `[B]` As-of-date stock**
  Refs: §3.4 · M1
  Ledger-based aggregation at a given date, using the T3.1 indexes.
  *Done when:* stock as at a past date matches a hand-computed fixture, and the query plan uses the
  index.

- [x] **T3.14 `[B]` Serial and reel history**
  Refs: §3.2, §10 · E2
  Chronological movement history for one serial unit or drum, showing every node transition.
  *Done when:* a serial received, issued, installed and recovered shows all four events in order.
  *This is the query an operator audit will actually ask for.*

- [x] **T3.15 `[B]` Internal transfers**
  Refs: §4.5 · E4
  Transfer between locations inside the yard posts directly. A transfer whose destination is
  outside the yard perimeter must be raised as a gate-out instead, and is rejected here.
  *Done when:* an in-yard transfer posts, and an out-of-perimeter transfer returns a `DomainError`
  directing the user to a gate-out.

- [x] **T3.16 `[B]` Stock counts**
  Refs: §4.13 · E5
  `StockCount` and lines with expected, counted and variance; posting writes `ADJUST` movements
  with a mandatory reason. Counts touching client-owned stock are held pending approval — the
  approval wiring completes in T4.15.
  *Done when:* posting a count with a variance produces adjustment movements and an audit entry,
  and a client-owned variance cannot post without approval.

- [x] **T3.17 `[B]` Minimum stock alerts**
  Refs: §4.3 · E6
  Active only when `min_stock_enabled`. Beat task comparing available balances against
  `min_stock_qty`, emitting a notification event (delivery lands in T4.16).
  *Done when:* crossing the threshold emits exactly one event, not one per run.

- [x] **T3.18 `[F]` Barcode scanner component**
  Refs: §7.3 · D7, G5
  `BarcodeDetector` with `@zxing/browser` fallback, torch toggle where supported, and manual entry
  always available.
  *Done when:* it reads a printed barcode on a mid-range Android phone, and the flow is completable
  with the camera denied.

- [x] **T3.19 `[F]` Gate-in capture flow**
  Refs: §7.3, §7.4 · D1–D8
  Mobile-first: choose source type and ownership, then a scan-or-search line entry loop with
  serial, drum and quantity variants, condition per line, custom fields, photo attachment, save as
  draft, post.
  *Done when:* a storekeeper enters a ten-line mixed delivery on a phone one-handed, and drafts
  survive a page reload.

- [x] **T3.20 `[F]` Stock screens**
  Refs: §7.4 · E1, E2, E3
  Stock lookup with filters, serial lookup landing directly on history, drum list with remaining
  lengths, client-owned stock visually distinct throughout.
  *Done when:* typing a serial number anywhere in search jumps straight to its history.

- [x] **T3.21 `[F]` Transfers and counts screens**
  Refs: §7.4 · E4, E5
  *Done when:* a count can be entered on a phone against a location and posted with reasons.

---

## Phase 4 — Dispatch and approvals

The system's reason for existing. After this phase, Silvertech has something worth using.

- [x] **T4.1 `[B]` GateOut models**
  Refs: §4.7 · F1
  `GateOut` with all purpose types and destination arity constraint, `custody_holder`,
  `GateOutLine`, `GateOutLineSerial`, `GateOutLineReel`, `client_uuid`.
  *Done when:* a gate-out with exactly one destination saves, and one with two destinations is
  rejected by the database.

- [x] **T4.2 `[B]` Status machine**
  Refs: §4.7 · F1, F6, F7, F8
  Single `transition()` method with an explicit allowed-transition map. No view assigns `status`.
  *Done when:* every legal transition in §4.7's diagram is tested, and every illegal one raises.

- [x] **T4.3 `[B]` ApprovalRule and admin API**
  Refs: §4.8, §5.1 · F3
  Rule model with `category`, `criticality`, `required_role`, `sequence` and an unused `conditions`
  JSONB. Seeded with a sensible default set per tenant.
  *Done when:* an admin can map HIGH criticality to the owner role and MEDIUM to a supervisor role.

- [x] **T4.4 `[B]` Routing engine**
  Refs: §5.1 · F3
  `collect_facts()` gathering categories, criticalities, client-ownership, per-item quantities,
  destination type and monetary value; predicate evaluator reading `conditions`; `required_levels()`
  resolving multi-category documents to the highest applicable level.
  *Done when:* unit tests cover single category, mixed criticality taking the highest, and zero
  matched rules yielding auto-approval. **The fact set is complete even though only criticality is
  consulted** — that is what keeps future dimensions migration-free.

- [x] **T4.5 `[B]` ApprovalRequest and ApprovalAction**
  Refs: §4.8 · F4, M3
  Generic document reference, level, required role, due date; action recording actor,
  `on_behalf_of`, decision, reason, `auth_method`, credential, IP and user agent.
  *Done when:* an auto-approval still writes an action with `decision = AUTO`, so the trail has no
  gap.

- [x] **T4.6 `[B]` Submit for approval**
  Refs: §5.1, §5.2 · F1, F2, F3
  Submit endpoint running the engine, creating requests per level, and transitioning to
  PENDING_APPROVAL — or straight to APPROVED on auto-approval.
  *Done when:* a request containing a HIGH-criticality line routes to the owner, and a consumables-
  only request auto-approves.

- [x] **T4.7 `[B]` Self-approval guard**
  Refs: §5.2 · F3
  Blocked unless `allow_self_approval` is on. Default off.
  *Done when:* a requester holding approval rights is refused with a clear `code`, and permitted
  once the setting is enabled.

- [x] **T4.8 `[B]` Approve and reject**
  Refs: §5.3 · F4
  Endpoints requiring authentication, recording the action, advancing or terminating the document.
  Rejection requires a reason. Password path here; WebAuthn added in T8.10.
  *Done when:* approving the final level sets APPROVED and `expires_at` from
  `gate_pass_expiry_hours`.

- [x] **T4.9 `[B]` Delegated approval attribution**
  Refs: §4.2, §5.3 · F5
  Approving under an active delegation records `on_behalf_of`, never the principal as actor.
  *Done when:* the audit trail reads "X on behalf of Y" and never attributes the action to Y alone.

- [x] **T4.10 `[B]` Escalation and expiry**
  Refs: §5.3 · F5, Q3
  Beat tasks escalating requests past `due_at` to the configured fallback, and expiring approved
  gate passes never released within `gate_pass_expiry_hours`.
  *Done when:* both transitions fire once and emit their notification events.

- [x] **T4.11 `[B]` Amend and resubmit**
  Refs: §4.7 · F6
  Amending an approved gate-out voids its approvals, creates a new version via `supersedes`, and
  re-runs routing. Prior versions remain visible.
  *Done when:* the version history is queryable and the voided approval is retained, not deleted.

- [x] **T4.12 `[B]` Cancel**
  Refs: §4.7 · F8
  Cancellation with a mandatory reason, blocked after any release.
  *Done when:* cancelling a partially released gate-out is refused.

- [x] **T4.13 `[B]` Release**
  Refs: §4.7 · G1, G2, F7, I2
  Release endpoint gated on `gate_out.release`, refusing unapproved or expired passes. Records
  vehicle registration and driver, posts movements to the custody holder's PERSON node or the
  destination node, supports partial release, and creates `CustodyExpectation` rows for returnable
  lines.
  *Done when:* releasing an unapproved pass is impossible, a partial release leaves the document
  open, and custody appears immediately at the holder's node.

- [x] **T4.14 `[B]` Release variance**
  Refs: §4.7 · G1
  Variance created when released quantity differs from approved, with an acknowledgement endpoint
  for an approver. Release is not blocked.
  *Done when:* the variance appears on the exceptions register until acknowledged.

- [x] **T4.15 `[B]` Hardcoded escalations**
  Refs: §5.2 · E5, J3
  Wire the two non-configurable rules: any stock adjustment touching client-owned stock (completing
  T3.16), and any disposal of client-owned material (consumed by T6.3).
  *Done when:* neither can be bypassed by editing approval rules.

- [x] **T4.16 `[B]` Notification framework**
  Refs: §9 · L1, L2, L3
  `NotificationChannel` protocol, `InAppChannel` and `EmailChannel` (SES), `NotificationEvent` and
  `NotificationDelivery`, `transaction.on_commit` dispatch into Celery, retries with capped
  exponential backoff, delivery status visible to admins.
  *Done when:* a forced channel failure retries, is recorded, and **does not roll back the approval
  it was notifying about**.

- [x] **T4.17 `[B]` Notification matrix**
  Refs: §9 · L1, L2
  Per-tenant channel and event configuration seeded with the default matrix from requirement `L2`.
  *Done when:* disabling a channel for one event stops only that delivery.

- [x] **T4.18 `[B]` Approval deep links**
  Refs: §5.3, §9.2 · F4
  Short-lived signed URLs landing on the approval screen, authenticating nothing on their own.
  *Done when:* an unauthenticated visit lands on login and returns to the approval afterwards, and
  an expired link is refused.

- [x] **T4.19 `[B]` Gate pass and GRN PDFs**
  Refs: §11 · G4, A4
  WeasyPrint templates carrying the tenant logo, document number, line detail and a QR code
  encoding the document id.
  *Done when:* a gate pass renders on one page for a ten-line load and its QR resolves.

- [x] **T4.20 `[B]` QR document lookup**
  Refs: §11 · G5
  Endpoint resolving a scanned QR payload to its document, permission-checked.
  *Done when:* scanning a gate pass at the gate opens the right release screen.

- [x] **T4.21 `[F]` Gate-out request screens**
  Refs: §7.4 · F1, F2
  Storekeeper full form and a simplified technician request, both mobile-first, with destination
  picker across site, work order, client and location, custody holder selection, and line entry
  reusing the T3.18 scanner.
  *Done when:* a technician raises a request from a phone in under a minute.

- [x] **T4.22 `[F]` Approval screens**
  Refs: §7.4 · F4
  Pending approvals list, approval detail showing every line with criticality and ownership flags,
  approve and reject with reason, delegation indicator.
  *Done when:* an owner can approve from a phone in two taps after authenticating.

- [x] **T4.23 `[F]` Gate release screen**
  Refs: §7.3, §7.4 · G1, G2, G3
  Verify-against-approved checklist, per-line actual quantity, variance reason capture, vehicle
  registration and driver, signature pad, release action.
  *Done when:* releasing a load with one short line records the variance and completes the release.

- [x] **T4.24 `[F]` Notification centre**
  Refs: §7.4 · L1
  In-app list, unread badge, mark-as-read, deep links into documents.
  *Done when:* an approval request appears without a page reload.

---

## Phase 5 — Jobs, reconciliation and custody

- [x] **T5.1 `[B]` Job model**
  Refs: §4.9 · H1
  Client, site, optional work order, assignee, status.
  *Done when:* a job can be assigned to a named technician who is responsible for closing it.

- [x] **T5.2 `[B]` JobCloseout models**
  Refs: §4.9 · H2
  Closeout header and lines with actions INSTALLED, CONSUMED, RETURNING, RECOVERED, supporting
  serial units, reels with lengths, and bulk quantities.
  *Done when:* metres consumed from a drum can be reported without returning the whole drum.

- [x] **T5.3 `[B]` Closeout posting and expectations**
  Refs: §4.9 · H2
  INSTALLED posts to the SITE node, CONSUMED to the CONSUMED node, both immediately. RETURNING and
  RECOVERED create expectations rather than movements.
  *Done when:* installed serials leave stock permanently and appear in the site's installed base.

- [x] **T5.4 `[B]` Return matching and variance**
  Refs: §4.9 · H3
  Gate-in of source type RETURN_FROM_SITE or RECOVERY matches against open expectations; any
  difference creates a `Variance`.
  *Done when:* declaring three returning units and receiving two produces one open variance.

- [x] **T5.5 `[B]` Variance and exceptions register**
  Refs: §4.9, §10 · H3, M1
  `Variance` model plus a unified exceptions endpoint combining variances, release variances and
  (from T8.7) sync exceptions.
  *Done when:* one endpoint answers "what is currently unresolved".

- [x] **T5.6 `[B]` Job close guard**
  Refs: §4.9 · H5
  Closing is blocked while expectations remain open, unless the user holds
  `job.close_with_variance` and supplies a reason.
  *Done when:* both paths are covered and the override is audited.

- [x] **T5.7 `[B]` Reconciliation query**
  Refs: §3.1, §10 · H4
  Per site and per work order: issued versus installed versus returned versus unaccounted, derived
  from movements grouped by destination node type.
  *Done when:* the four figures reconcile exactly across a full job lifecycle fixture, and the
  work-order close warning from T2.10 now reports real numbers.

- [x] **T5.8 `[B]` Custody expectations and overdue sweep**
  Refs: §4.10 · I1, I2, I3
  Expectations created on release; nightly beat flagging overdue and firing escalating
  notifications holder → storekeeper → owner.
  *Done when:* escalation fires once per stage, not once per run, and the T2.15 deactivation warning
  now consults real custody.

- [x] **T5.9 `[B]` Custody transfer**
  Refs: §4.10 · I5
  Transfer requiring acknowledgement by the receiver; movements post only on acknowledgement.
  *Done when:* an unacknowledged transfer leaves custody with the original holder.

- [x] **T5.10 `[F]` Technician screens**
  Refs: §7.4 · H2, I1, I5
  My jobs, job closeout with photo capture, my custody, acknowledge and transfer.
  *Done when:* a technician closes out a job from a site on a phone, including two site photos.

- [x] **T5.11 `[F]` Custody and reconciliation screens**
  Refs: §7.4 · H4, I1, I4
  Custody by holder, overdue report, per-site and per-work-order reconciliation view.
  *Done when:* an owner sees who is holding what and what is overdue on one screen.

- [x] **T5.12 `[F]` Exceptions register screen**
  Refs: §7.4 · H3, M1
  *Done when:* every open variance is actionable from one list.

---

## Phase 6 — Disposition, disposal and client returns

- [x] **T6.1 `[B]` Disposition**
  Refs: §4.11 · J1, J2
  Decisions REPAIR, RESTORE_TO_SERVICEABLE, RETURN_TO_CLIENT, SCRAP, each with a reason, approval
  where required, and the corresponding movements out of quarantine.
  *Done when:* restoring to serviceable returns stock to availability and scrapping does not.

- [x] **T6.2 `[B]` Disposal**
  Refs: §4.11 · J3
  Disposal document, method, approval (always required for client-owned material via T4.15),
  evidence attachments, and a disposal certificate PDF.
  *Done when:* disposing of client-owned material without approval is impossible regardless of
  configured rules.

- [x] **T6.3 `[B]` Client returns**
  Refs: §4.12 · K1
  Gate-out with purpose RETURN_TO_CLIENT moving stock to the CLIENT node, where it reads as in
  transit and remains the tenant's exposure.
  *Done when:* returned material leaves available stock but still reports under the client's
  position as in transit.

- [x] **T6.4 `[B]` Return acknowledgement**
  Refs: §4.12 · K3
  `ClientReturnAck` with reference, date, attachment and recorder; recording it ends liability.
  Beat task notifying on returns unacknowledged after N days.
  *Done when:* the client-owned position report distinguishes in-transit from acknowledged.

- [x] **T6.5 `[B]` Waybill PDF**
  Refs: §11 · K2
  Return waybill template, active only when `client_waybill_enabled`.
  *Done when:* the document prints with tenant branding and line detail.

- [x] **T6.6 `[F]` Disposition and returns screens**
  Refs: §7.4 · J1, J2, J3, K1, K3
  Quarantine list with decision actions, disposal with approval status, client return creation and
  acknowledgement capture.
  *Done when:* a storekeeper moves a faulty unit from quarantine to a client return and records the
  acknowledgement.

---

## Phase 7 — Reporting

- [x] **T7.1 `[B]` Report framework**
  Refs: §10 · M2
  Base class pairing a query with a column specification, consumed by the API view, the Excel
  writer and the PDF renderer from one definition.
  *Done when:* adding a report requires no changes to the export code.

- [x] **T7.2 `[B]` Stock reports**
  Refs: §10 · M1
  Stock on hand, stock as at date, movement history, serial history, client-owned position.
  *Done when:* each matches a hand-computed fixture.

- [x] **T7.3 `[B]` Movement and accountability reports**
  Refs: §10 · M1
  Outstanding gate-outs, overdue returns and custody, consumption per site and per work order,
  variance and exceptions register.
  *Done when:* the consumption report reconciles with T5.7.

- [x] **T7.4 `[B]` Recovery and disposal reports**
  Refs: §10 · M1
  Recoveries grouped by originating site, disposals and write-offs.
  *Done when:* a recovery from a decommissioned site appears against that site.

- [x] **T7.5 `[B]` Exports**
  Refs: §10 · M2
  Excel via openpyxl and PDF via WeasyPrint for every report; exports above a row threshold run in
  Celery, write to S3 and notify with a pre-signed link.
  *Done when:* a large export completes asynchronously and the link expires.

- [x] **T7.6 `[B]` Retention configuration**
  Refs: §4.1 · M5
  Retention period flags records for review at expiry. **Never deletes.**
  *Done when:* expiry produces a review list and no data loss.

- [x] **T7.7 `[F]` Report screens**
  Refs: §7.4 · M1, M2
  Report list, per-report filter panels, results table, export buttons with async progress.
  *Done when:* every day-one report is reachable and exportable from the UI.

- [x] **T7.8 `[F]` Role dashboards**
  Refs: §7.4
  Storekeeper: pending gate-outs, low stock, exceptions. Owner: approvals waiting, overdue custody,
  unaccounted material. Technician: my jobs, my custody.
  *Done when:* each role lands on a dashboard answering its first question of the day.

---

## Phase 8 — Offline capture and biometrics

- [x] **T8.1 `[F]` Service worker and shell caching**
  Refs: §8.1 · N1
  Workbox precaching the app shell and the gate-in and gate-out bundles only.
  *Done when:* the two offline flows load with the network disabled; other routes show a clear
  offline notice.

- [x] **T8.2 `[F]` Offline reference data**
  Refs: §8.1 · N1
  Dexie schema and background sync of item types, locations, clients, sites and users, with
  staleness indication.
  *Done when:* line entry works offline against cached reference data.

- [x] **T8.3 `[F]` Mutation queue**
  Refs: §8.1 · N1, N2
  Queue with a client-generated `client_uuid` per mutation, offline banner, pending count, and
  badging of unsynced records.
  *Done when:* three gate-ins captured offline are visibly pending and survive an app restart.

- [x] **T8.4 `[B]` Sync idempotency**
  Refs: §8.2 · N2
  `SyncSubmission` unique on `(organization, client_uuid)`; a replay returns the original document
  with 200 rather than creating a second.
  *Done when:* the same payload submitted ten times yields exactly one document.

- [x] **T8.5 `[F]` Offline release of approved passes**
  Refs: §8.3 · N3
  Download approved gate passes to the device for offline release. **Release of anything not
  already approved and downloaded is impossible offline.**
  *Done when:* attempting to submit-and-release offline is refused by the client and would be
  refused by the server.

- [x] **T8.6 `[B]` Sync conflict handling**
  Refs: §8.4 · N3
  Server revalidates on sync; invalid documents land in `SyncException` with payload and reason —
  never force-posted, never silently dropped.
  *Done when:* an offline issue of a serial that was issued elsewhere meanwhile produces an
  exception, not a corrupted balance.

- [x] **T8.7 `[F]` Sync exception queue screen**
  Refs: §8.4, §10 · N3, M1
  Storekeeper resolution UI, joined into the T5.5 exceptions register.
  *Done when:* every synced conflict is resolvable without developer intervention.

- [x] **T8.8 `[B]` WebAuthn enrolment**
  Refs: §5.3 · B5
  `py_webauthn` registration begin and complete, multiple credentials per user, device labels,
  admin revocation.
  *Done when:* a user enrols two devices and an admin can revoke one without locking them out.

- [x] **T8.9 `[B]` WebAuthn approval step-up**
  Refs: §5.3 · B5, F4, M3
  Assertion challenge **bound to the `ApprovalRequest` id** so it cannot be replayed against
  another document; `ApprovalAction` records `auth_method`, credential id and AAGUID.
  *Done when:* replaying an assertion against a different approval is rejected, and the action
  records the credential used.

- [x] **T8.10 `[F]` Biometric enrolment and approval**
  Refs: §7.2, §5.3 · B5, F4
  Enrolment in user settings, fingerprint prompt on approval, graceful password fallback where no
  sensor or credential exists.
  *Done when:* an owner approves with a fingerprint on Android Chrome, and the same flow falls back
  cleanly on a desktop without a sensor.

- [x] **T8.11 `[B]` SMS channel**
  Refs: §9 · L1
  Ujumbe SMS adapter behind `NotificationChannel`, wired into password reset and the notification
  matrix. Credentials and endpoint from the Ujumbe SMS account; the local logging adapter from T1.24
  stays the default in development.
  *Done when:* the adapter is unit-tested against a mocked Ujumbe response, delivery status is
  recorded, and switching provider is a settings change.
  *Credentials received and verified* (22 Aug 2026): the account answers, with credits in hand.
  `manage.py sms_selftest` is the check — it asks the provider's balance endpoint rather than texting
  somebody. Two things had to be fixed to get there, both recorded in §9.3: the dotted path in
  `.env` carried a typo, and the three credential lines were written `KEY = value`, which
  django-environ silently skips. A system check now warns about both at startup.
  *Still outstanding:* one real send, to confirm the messaging endpoint's success code. Run
  `manage.py sms_selftest --to 07…` against a phone you own; the adapter is safe either way, since an
  envelope it cannot classify fails without retrying.

- [x] **T8.12 `[B]` WhatsApp channel, disabled**
  Refs: §9.2 · L1, Q1
  Meta Cloud API adapter written and unit-tested against a mock, shipped **disabled** behind the
  channel setting pending sender and template approval.
  *Done when:* enabling it is a settings change with no code change, and it is off by default.
  *Needs from customer:* an approved WhatsApp Business sender and registered message templates,
  then `WHATSAPP_ACCESS_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID` in `backend/.env` plus the channel
  switch in the tenant's notification settings. Nothing else is outstanding.

- [x] **T8.13 `[T]` End-to-end flows**
  Refs: §14
  Playwright on a mobile viewport: (1) gate-in of a mixed serialized, bulk and reel delivery;
  (2) request → approve → release → closeout → return, including a variance.
  *Done when:* both run in CI against a seeded tenant.

---

## Phase 10 — Projects and commercials

Epic O. Phases 1–8 are built, so this phase changes **live structures**: a model rename touching
three apps, and two columns that in a fresh build would have landed back in phases 3 and 4. Order
matters more here than anywhere else in this document — the rename comes first and alone, because a
half-applied one breaks the gate.

Phase 9 remains the AWS move (§12.1). Neither phase blocks the other.

- [x] **T10.1 `[B]` Rename WorkOrder to Project**
  Refs: §4.4, §4.14 · O1, D20
  `RenameModel` WorkOrder → Project, `RenameField` on `Job.work_order` and `GateOut.work_order`,
  then drop and recreate `gate_out_has_exactly_one_destination` because it names the column. Route
  `/work-orders` becomes `/projects`. No new fields in this task.
  *Done when:* the existing suite passes **unchanged**, existing work orders read as projects, and
  the destination constraint still refuses a pass with two destinations.

- [x] **T10.2 `[B]` Project PO fields**
  Refs: §4.14 · O1
  `po_number` (unique per organization when set), `title`, `manager`, `contract_value`,
  `cost_budget`, `starts_on`, `target_completion_on`, `CANCELLED` status, and the check constraint
  `a_po_project_is_fully_specified`.
  *Done when:* a project carrying a `po_number` with no manager is refused **by the database**, and
  a project without one saves exactly as a work order did.

- [x] **T10.3 `[B]` Variations**
  Refs: §4.14 · O2
  `ProjectVariation` with value and budget deltas, append-only, approved by the owner.
  `current_contract_value` reads original plus approved deltas.
  *Done when:* `contract_value` is never written a second time, a negative delta reduces the current
  value, and an approved variation cannot be edited.

- [x] **T10.4 `[B]` Subcontractor register**
  Refs: §4.14 · O4
  Name, code, contacts, `is_active`. Unique per tenant, `PROTECT` once referenced.
  *Done when:* a referenced subcontractor can be deactivated but not deleted.

- [x] **T10.5 `[B]` Job delivery mode**
  Refs: §4.14 · O3
  `delivery_mode`, `subcontractor`, `agreed_price`, paired by check constraint. Both immutable once
  the job is `CLOSED`.
  *Done when:* `SUBCONTRACTED` without a price is refused, and changing the price on a closed job is
  refused — it would rewrite a cost already counted.

- [x] **T10.6 `[B]` Movement valuation columns**
  Refs: §3.2, §4.14 · O11, D27
  `unit_cost` and `unit_cost_source` on `StockMovement`, captured when the movement posts. **This is
  the retrofit named in §15:** movements that already exist cannot be valued without guessing, so
  they carry `NONE`.
  *Done when:* a movement posted today carries the cost that applied today, repricing the item type
  afterwards leaves it unchanged, and historical rows read as `NONE` rather than as zero.

- [x] **T10.7 `[B]` Declared client value on gate-in**
  Refs: §4.6, §4.14 · O11
  Client-owned receipt lines carry `declared_unit_value`, feeding `unit_cost_source =
  CLIENT_DECLARED`. This is the figure the operator debits on a shortfall, not what the item costs
  us.
  *Done when:* a client-owned receipt with no declared value posts as unvalued and is reported that
  way, never as zero.

- [x] **T10.8 `[B]` `required_user` on ApprovalRequest**
  Refs: §5.4 · O6
  Nullable FK plus a check that **at most one** of `required_role` and `required_user` is set —
  neither stays legal, for §5.2's auto-approval row. No routing change in this task.
  *Done when:* every existing approval test passes unchanged and a row with both is rejected.

- [x] **T10.9 `[B]` Project routing in the engine**
  Refs: §5.4 · O6, D22, D28
  `project_of(document)`, branching before `collect_facts`. `self_approved` recorded on the action;
  `due_at` left null so the escalation sweep skips it; `resolve_delegate()` not consulted; an
  inactive PM raises a domain error naming the project and the remedy.
  *Done when:* project material never matches a criticality rule, a self-approving PM is recorded
  rather than blocked, and an inactive PM **raises** instead of falling through to criticality
  routing.

- [x] **T10.10 `[B]` Gate-out job attribution** *(done before T10.9 — routing needs it)*
  Refs: §4.7, §5.4 · O5
  `job` FK on `GateOut` as an attribution, not a destination. Where the destination is a site the
  job must be at that site; the job and its project must be open.
  *Done when:* a pass naming a job at a different site is refused, and a pass with no job behaves
  exactly as it does today.

- [x] **T10.11 `[B]` High-value release notification**
  Refs: §9, §4.14 · O7
  Tenant threshold setting; owner and admin notified after a project release above it.
  *Done when:* a release above the threshold notifies and one below does not, and **neither is
  delayed by the notification**.

- [x] **T10.12 `[B]` Day rates**
  Refs: §4.14 · O15, O14
  `day_rate` on User and Role, behind the `project.view_rates` permission — owner and admin only.
  *Done when:* a PM's API response contains no rate field at all, absent rather than null.

- [x] **T10.13 `[B]` Labour from the closeout**
  Refs: §4.9, §4.14 · O15
  Days per person captured on the closeout; `JobLabour` written on confirmation with the rate
  captured onto the row; `rate_source = NONE` where no rate exists; `overlaps_day` set when that
  person exceeds one day across all jobs on that date.
  *Done when:* a person recorded on three jobs in one day is flagged and **not** refused, and a job
  with no applicable rate reads as uncosted rather than costing zero.

- [x] **T10.14 `[B]` PM cost acceptance of closeouts**
  Refs: §4.9 · O8
  A confirmed closeout on a project job goes to the PM for cost acceptance. Rejection asks for a
  corrected closeout.
  *Done when:* stock moves on the storekeeper's confirmation while the PM's acceptance is still
  pending — the ledger must not wait.

- [x] **T10.15 `[B]` Expenses**
  Refs: §4.14 · O16, D29
  `ExpenseCategory` seeded; `ProjectExpense` recorded by anyone, approved by the PM, append-only
  after approval with correction by reversing entry, unevidenced entries flagged.
  *Done when:* an expense reaches project cost only on approval, and an approved one cannot be
  edited.

- [x] **T10.16 `[B]` PM level on disposals**
  Refs: §5.4 · O10
  The PM is **prepended** to the disposal's own matched levels rather than replacing them.
  *Done when:* disposing project material needs the PM and the existing approver, in that order.

- [x] **T10.17 `[B]` The costing engine**
  Refs: §4.14, §10 · O11
  `commercials/costing.py` — the six figures in §4.14's table, one queryset each, nothing stored.
  *Done when:* a full PO lifecycle produces a cost equal to the four lines summed by hand;
  repricing an item type afterwards leaves a closed project unchanged; and an expectation resolved
  late **reduces the loss with nobody editing anything**.

- [x] **T10.18 `[B]` Financial permissions**
  Refs: §10 · O14
  `project.view_cost`, `project.view_margin`, `project.view_rates`, applied at the serializer.
  *Done when:* storekeeper, PM and owner responses are asserted **field by field**; a withheld
  figure is absent, and the PM's labour total is accompanied by nothing that divides into a rate.

- [x] **T10.19 `[B]` Project performance reports**
  Refs: §10 · O12
  Four report classes: project performance, projects ranked, self-approved releases, uncosted and
  overlapping labour.
  *Done when:* each runs through the existing report machinery and exports to Excel and PDF.

- [x] **T10.20 `[B]` Project close and snapshot**
  Refs: §4.14 · O13
  Warn on open jobs and unreconciled material, reason required, `ProjectSnapshot` written, a closed
  project refuses gate-outs and variations, owner reopen recorded.
  *Done when:* a reversal posted after close does not move the closed project's reported figures.

- [x] **T10.21 `[F]` Project screens**
  Refs: §7.4 · O1, O2, O12, O13
  List, detail with cost against budget, variations, close.
  *Done when:* a PM sees cost and budget, an owner also sees value and margin, and a storekeeper
  cannot reach the screens at all.

- [x] **T10.22 `[F]` PM approval screens**
  Refs: §7.4 · O6, O8, O16
  Queues for material, closeouts and expenses. The material approval shows the budget position and
  what this release adds; a project over budget is stated and **still approvable**.
  *Done when:* an over-budget release can be approved after the overrun is shown, and a gate-out on
  a project with an inactive PM says so in those words and names reassignment as the remedy.

- [x] **T10.23 `[F]` Days and expense capture**
  Refs: §7.4 · O15, O16
  Days per person on the closeout, with the over-a-day warning inline. Expense capture with a
  receipt photo, reusing the attachment control.
  *Done when:* a technician records days and an expense on a phone without leaving the job.

- [x] **T10.24 `[T]` End-to-end project lifecycle**
  Refs: §14
  Playwright on a mobile viewport: create a PO project, subcontract one job, issue material through
  PM approval, close out with days, record and approve an expense, close the project, read the
  performance report.
  *Done when:* it runs in CI against a seeded tenant.

- [x] **T10.25 `[F]` Money reads like money**
  Refs: §7.3
  Tabular numerals on every figure — displays, inputs, stat tiles and numeric report columns — and
  money inputs right-aligned. One `Money` component in place of the formatter copied into three
  screens.
  *Done when:* a column of figures lines up on the decimal point, and a withheld figure renders as
  nothing rather than as a dash or a zero.

- [x] **T10.26 `[F]` Edit a project**
  Refs: §7.4 · O1, D21
  Title, description, manager, budget, dates — and `contract_value`, which is the correction path
  for a value keyed in wrong. The PO number stays fixed: it identifies the project, and changing it
  would re-point every figure already counted against it.
  *Done when:* an owner corrects a project and the figures follow, with the change in the audit
  trail.

- [x] **T10.27 `[B/F]` Raise a job**
  Refs: §4.9, §7.4 · H1, O3
  `job.manage` for admins, storekeepers and project managers, separate from `job.closeout`. A job
  sheet on the project detail with the project fixed, and the same sheet on the jobs list for work
  with no PO behind it.
  *Done when:* a storekeeper can raise a job and a technician cannot — H1's actor could not carry
  out H1 before this, because creating a job required the closeout permission.

- [x] **T10.28 `[B/F]` Number series**
  Refs: §4.13, §7.4 · M6, D37, O1, H1
  `DocumentSequence` gains a prefix, a width and the highest number it has issued; PROJECT and JOB
  join the document types; project and job references are allocated on creation and are read-only.
  Settings → Numbering lists every type with a live example.
  *Done when:* a tenant can set their own prefix and carry on from a sequence they already ran, the
  counter refuses to move back onto numbers already issued, and a project created through the API
  comes back numbered without anybody typing one.

- [x] **T10.29 `[B/F]` Search on every register**
  Refs: §7.3 · O1
  `SearchField` in one place, debounced, with a clear button. Wired into projects, jobs, expenses,
  disposals, quarantine, exceptions and overdue custody. Project search covers `po_number` and
  `title`, not just the reference somebody may not have in front of them. The three bespoke
  registers are assembled in Python rather than queried, so each filters in its own terms:
  quarantine on the queryset, exceptions over the assembled entries, overdue custody over the
  **grouped** rows — grouping first and filtering after would leave totals that did not add up to
  the rows underneath them.
  *Done when:* every list screen can be narrowed by typing, and the counts a register reports match
  the rows it returns.

- [x] **T10.30 `[F]` The subcontractor register**
  Refs: §7.4 · O4
  T10.4 built `/api/v1/subcontractors` and the job sheet read from it, but no screen created one —
  so "delivered by a subcontractor" was a select with nothing in it and the subcontracting half of
  Epic O was unreachable from the UI. A fourth tab under Settings → Network, searchable, alongside
  Clients, Sites and Projects. The job sheet now says where to add one instead of dead-ending.
  *Done when:* a contractor can be added and immediately chosen on a job, and the job comes back
  attributed to them.

- [x] **T10.31 `[F]` Doors onto the commercial screens**
  Refs: §7.4 · O12, O16
  T10.22 and T10.23 built the manager's queue and the expense form, wired their routes, and linked
  to neither — so both were reachable only by typing a URL, and a manager had no way to learn an
  expense was waiting on them. "Waiting on you" joins the sidebar; "Record an expense" joins the
  project's own header and carries the project with it.
  The form now offers the `job` the model has always accepted, optional because a permit belongs to
  the project and a recovery truck to one job — forcing a choice would put the cost somewhere
  untrue rather than leave it unallocated. A refusal that names no field (a closed project) now
  reaches a banner instead of nowhere.
  *Done when:* an expense can be recorded from the project screen against one of its jobs without
  anybody typing a URL.

- [x] **T10.32 `[F]` One queue, not two**
  Refs: §7.4 · O8, O16
  T10.31 gave the project queues a sidebar entry, and that made the duplication visible: approving
  an expense and approving a gate pass are the same act to the person doing them, and two entries
  meant two lists to check and neither to trust. They are tabs on Approvals now — Material,
  Expenses, Closeout costs — each shown only to somebody who can act on it. `/my-projects` redirects
  rather than 404s, because the link had been in the sidebar.
  *Done when:* a project manager sees every decision waiting on them on one screen, and the old
  address still resolves.

- [x] **P1–P6 `[F]` Breadcrumbs**
  Refs: §7.6 · Epic P
  A trail derived from the URL in one place, not passed by twenty-six screens. Parents are declared
  rather than chopped off the end of the path, because `/expenses/new` has no `/expenses` above it
  and `/jobs/custody` is not a job. A detail screen names its own crumb through `useCrumb`, so
  `/projects/42` reads as the reference and never as `42`; until the record arrives the crumb holds
  a placeholder. Top-level screens show nothing, the last crumb is not a link, and a crumb pointing
  somewhere the person cannot go is plain text.
  On a phone it collapses to one link to the level above — a full trail does not fit at 360px, and
  the crumb people use there is the one that goes back.
  *Done when:* a detail screen states where it sits and the crumb gets you there, on both widths.

- [x] **T10.33 `[B/F]` A PDF that cannot be made is not offered, and one that degrades says so**
  Refs: §11, §7.4 · M2
  §11 lets a document fall back to HTML where WeasyPrint's native libraries are missing, so a client
  can still be sent their position. On a machine without them, every "PDF" export was an HTML file and
  nothing said so — it looked like the export was broken. The catalogue now carries `pdf_available`;
  the screen disables the PDF button on a server that cannot make one, and when the fallback fires
  anyway the response says so in a header and the screen shows a warning. The queued export also
  stores its real content type — a `ContentFile` carries none, so it was typed as nothing.
  *Done when:* asking for a PDF never silently yields HTML: either the button is disabled with a
  reason, or the HTML arrives with an explanation. Production renders real PDFs (verified end to end).

- [x] **T10.34 `[F/Ops]` A tab left open across a deploy recovers instead of freezing**
  Refs: §7.1, §8.1, §12.2
  Reported as "the URL changes but the screen stays on the report". Three faults compounded. The SPA
  catch-all answered a missing chunk with `index.html` — 200, `text/html`, and because the path
  matched `/assets/*`, `Cache-Control: immutable` for a year — so the import failed with a
  MIME-type error the stale-bundle detector did not recognise, and React Router's transition kept
  the old screen up under the new URL. And with `autoUpdate` only checking on a full page load, a
  tab open all day never learned a deploy had happened.
  Now: `/assets/*` is a real file or a real 404; the detector recognises the MIME-type wording; the
  service worker checks for an update every fifteen minutes and whenever the tab regains focus.
  *Done when:* a chunk answered with the shell reloads once and opens (E2E), and a missing asset on
  production is a 404 with no cache header.

- [x] **T10.35 `[B/F]` Four finance reports, and a catalogue with groups**
  Refs: §10, §7.4 · O4, O11, O12, O16
  The performance reports said *what* a PO cost; nothing said where the money went, to whom, or
  when. **Expenses ledger** opens the expense figure up one claim at a time — the only place a
  rejection is visible. **Subcontractor spend** rolls cost up by party, which is what the register
  was built for. **Budget variance by month** places each cost line in the month it became a cost
  under the costing rules, so the months add up to cost to date and show *when* a project went over.
  **Stock valuation** prices own stock at catalogue cost, excludes client stock as not ours to value,
  and flags unpriced items rather than dropping them from the total.
  Every report now carries a `category` from a server-fixed vocabulary — Finance, Stock, Movements,
  Custody and control, Exceptions and disposal — and the catalogue is grouped under those headings
  in that order. Registration refuses a category outside the list: a group that exists for one
  report is not a group.
  *Done when:* each finance report agrees with the costing module it sits beside (tested), and the
  twenty reports appear on the catalogue under five headings rather than as one grid.

- [x] **T10.36 `[F]` "Add new …" as the last option of every reference select**
  Refs: §7.3 · C5, O1, O4
  A storekeeper raising a job for a site not yet on the system had to abandon the form, find
  Settings → Network → Sites, create it, come back and start over. Every select over a reference
  list — project, site, client, subcontractor, location, item, person — now ends with "＋ Add new …",
  which opens that entity's own create sheet in place; on save the list refetches and the select
  lands on the new record, with everything else in the form intact. One registry
  (`features/quickCreate`) maps resource → sheet, lazily, so the settings area is not pulled into
  every screen's bundle. Shown only to somebody who may create the thing. Two flavours of select
  (react-hook-form and controlled) share the behaviour; 31 selects converted by a script that kept
  every other attribute in place.
  *Done when:* from the job form, "Add new site…" creates the site and the select shows it, without
  losing the rest of the form (E2E); a storekeeper sees no such option.

- [x] **T10.37 `[F]` Swipe between tabs on a phone**
  Refs: §7.3
  Tapping a tab strip that has scrolled off the right edge means finding it first. A horizontal
  swipe across the content moves to the next or previous tab on every tabbed screen — Approvals,
  Network, People, Settings. A swipe that is mostly vertical is a scroll and is left alone; one that
  starts on something that scrolls sideways itself (the strip, a wide table) is too. The strip
  still works.
  *Done when:* on a phone, a swipe left on Approvals selects the next queue and a mostly-vertical
  drag changes nothing (E2E).
  *Follow-up (shipped):* the first cut switched instantly and left the strip where it was, so it
  read as "static and hard". Now the pane follows the finger, springs back from a short drag, and
  the new pane slides in from the side it came from; a shared `TabStrip` scrolls the selected tab
  into view on every change. E2E: the pane carries the finger's offset mid-drag, and after four
  swipes on Settings the highlighted tab is still within the strip.

---

## Phase 11 — Boxes

Epic P (approved 2026-10-03), design §4.15. A box groups units and bulk quantities under one code,
nests up to three deep, and is received, looked up, sent out and checked at the gate by scan.

**This phase touches `post_movement`, which every stock path in the system goes through.** T11.3
and T11.4 are the risky ones: each lands alone, with the full backend suite green, before anything
builds on it. Everything else is additive — new tables, new optional fields, new screens.

### Backend

- [x] **T11.1 `[B]` Box models**
  Refs: §4.15.2 · P1, P8, P10
  `Box`, `BoxBulkContent`, `BoxEvent` (append-only) and `SerialUnit.box`, with their constraints:
  code unique per org case-insensitively including closed boxes, depth 1–3, claim quantity > 0.
  RLS migration, isolation fixtures, `DocumentType.BOX` with prefix `BX`.
  *Done when:* migrations apply; `core.E001`, the RLS test and the isolation suite pass with the new
  tables; a `BoxEvent` cannot be updated or deleted (test).

- [x] **T11.2 `[B]` Label reader and box lookup**
  Refs: §4.15.6 · P2, P3, P4
  `stock/labels.py:read_label` and the shared vectors file
  `backend/stock/tests/data/label_vectors.json`, covering every rule in §4.15.6. `find_by_identifier`
  tries the raw value, then each candidate. Box codes are resolved in T11.8, once the box exists to
  look up; this task runs alongside T11.1.
  *Done when:* every vector passes; a GS1 code, a URL and an `SN:` label each find the unit their
  serial names; an unreadable label is still looked up whole (tests).

- [x] **T11.3 `[B]` A serialized movement starts where the unit is**
  Refs: §4.15.3
  `post_movement` refuses a serialized movement whose `from_node` is not the unit's `current_node`.
  Lands alone: it is a new rule on the path every movement takes.
  *Done when:* the refusal is tested, and the **whole** backend suite passes unchanged, proving no
  existing caller relied on the gap.

- [x] **T11.4 `[B]` Ledger hooks for boxes**
  Refs: §4.15.3 · P5, P7, P9, P10
  `MovementRequest.from_box` and `moving_box`. A moving unit leaves its box; bulk draws on a named
  box's claim or on loose stock; `BoxedStockOnly` for issue-like movements when loose is short;
  ADJUST and REVERSAL reduce claims in code order; empty boxes close up the tree. Every change
  writes its `BoxEvent` with the movement's document refs.
  *Done when:* each rule has a test, including a pallet emptied by one release closing at every
  level, and the whole backend suite passes.

- [x] **T11.5 `[B]` Box services**
  Refs: §4.15.4 · P1, P7, P9, P10, E4
  `stock/boxes.py`: `create_box` (with generated codes), `put_units`, `put_bulk`, `put_box`,
  `take_out`, `empty_box`, `move_box`, `box_tree`, `issuable_contents`. Locks parent before child;
  refuses cycles, depth over three, contents at another node, claims beyond loose stock.
  *Done when:* each function and each refusal is tested; `move_box` carries a nested box with units
  and bulk to another store without anything leaving its box; `issuable_contents` names every
  exclusion reason.

- [x] **T11.6 `[B]` `verify_ledger` checks boxes**
  Refs: §4.15.12 · P8
  Units in a box sit at its node and the box is open; claims never exceed the balance; trees are
  acyclic, at most three deep, children with their parents. Reports, never corrects.
  *Done when:* each kind of drift, injected directly into the tables, is reported; a clean ledger
  with boxes reports nothing.

- [x] **T11.7 `[B]` Gate-in receives boxes**
  Refs: §4.15.5 · P1, P2, P9, P10
  `GateInBox`, `GateInSerial.box_key`, `GateInLine.box_key`; serializer read and write; validation
  (`BOX_CODE_IN_USE`, `BOX_TOO_DEEP`, `BOX_CYCLE`, `BOX_EMPTY`, `BOX_MIXED_DESTINATIONS`); posting
  creates the boxes top-down and fills them; void empties and closes them first.
  *Done when:* a pallet of two cartons, one mixed and one of bulk, posts and is found by lookup; each
  validation is tested; void leaves every box closed; an offline replay of the same payload through
  `apply_submission` posts once; `api-schema.yml` regenerated.

- [x] **T11.8 `[B]` Box API**
  Refs: §4.15.9 · P4, P6, P7, P8
  `/boxes` list and detail with tree and counts, `/boxes/{code}/history`, `take-out`, `empty`,
  `move`, and `/stock/boxes/{code}/issuable?from_location=`. Permissions as §4.15.9.
  `find_by_identifier` resolves box codes and `/stock/lookup` returns `kind: "box"`.
  *Done when:* API tests cover each endpoint and its permission; another tenant's box is a 404;
  scanning a box code on the lookup returns the box; schema regenerated.

- [x] **T11.9 `[B]` Gate-out by box**
  Refs: §4.15.7 · P5, P6, P9, P10
  `GateOutLine.box`; submit refuses a unit that is elsewhere or on another open pass
  (`UNIT_NOT_AVAILABLE`) and a box line beyond its claim; `_release_line` draws bulk with
  `from_box`; the detail serializer and the releasable bundle carry each unit's serial, asset tag
  and box path, and each line's box path; the gate-pass PDF groups lines under box codes.
  *Done when:* a whole box requested, approved and released leaves the box closed; one unit
  released from a box leaves the rest in it; the new submit refusals are tested; schema regenerated.

- [x] **T11.10 `[B]` Release by named serials**
  Refs: §4.15.8 · P11, G1
  `release_gate_out(released_serials=…)` issues exactly the named units; anything unnamed is short
  with a variance. `OrganizationSettings.release_scan_required` (default off) and
  `SCAN_REQUIRED_FOR_RELEASE`. The offline replay passes `released_serials` through.
  *Done when:* releasing two named units of three issues those two and raises a variance for the
  third; the setting refuses an unscanned release; a queued release with named serials replays
  correctly; schema regenerated.

### Frontend

- [x] **T11.11 `[F]` Label reader, scan matcher and a unit test runner**
  Refs: §4.15.6, §4.15.8 · P2, P3, P11
  Add Vitest. `src/features/boxes/readLabel.ts` against the shared vectors file;
  `matchScan.ts` against fixture passes (a unit, a box, a pallet, a stranger, a repeat).
  *Done when:* `npm test` runs both suites green and CI runs them in the frontend job; the lockfile
  carries no platform-specific packages.

- [x] **T11.12 `[F]` The scanner says what it read**
  Refs: §4.15.10 · P3
  `BarcodeScanner` passes the read result, shows "Scanned X from the label" with the raw text on a
  tap, and stops hard-coding its manual input's `id`.
  *Done when:* a GS1 or URL label shows the serial it found; two scanners on one screen have
  distinct ids; existing gate-in and gate-out scanning still works.

- [x] **T11.13 `[F]` Gate-in boxes**
  Refs: §4.15.10 · P1, P2, P9, P10
  `Draft.boxes`; "Into a box" on the line sheet; start a box by scanning its code, or leave it blank
  for a generated one; a label listing serials offers them for confirmation; bulk lines into a box;
  a box inside a pallet; the lines card shows the tree with counts. A half-built box survives a
  reload.
  *Done when:* receiving a pallet with a carton of three units and a carton of bulk posts, and a
  reload mid-way loses nothing (E2E in T11.18).

- [x] **T11.14 `[F]` Box screens**
  Refs: §4.15.10 · P4, P7, P8
  `/stock/boxes` and `/stock/boxes/:code`: tree, counts received versus now, units, bulk, history,
  and take out, empty and move. Stock lookup opens a scanned box; serial history shows the unit's
  box and its box events.
  *Done when:* scanning a box on the stock screen opens it; taking a unit out shows in both the box's
  history and the unit's.

- [x] **T11.15 `[F]` Gate-out by box**
  Refs: §4.15.10 · P5, P6, P9, P10
  The line sheet's lookup handles a box: the proposal, the exclusions with reasons, and "Add all";
  a scanned unit shows which box it is in; the request and detail group lines under their box.
  *Done when:* scanning a box adds one line per item and lot with every unit named, and the excluded
  units are listed with why.

- [x] **T11.16 `[F]` Scan the load at release**
  Refs: §4.15.8, §4.15.10 · P11, G1
  A shared `useLoadScan(pass)` over `matchScan`; "Scan the load" in the online and offline release
  sheets; ticks per unit and line, refusals listed, short lines from what is unticked, hand
  confirmation still possible; the payload sends `released_serials`. The require-scan setting in
  Settings.
  *Done when:* scanning two of three units and a stranger refuses the stranger and releases short
  by one, online and from the offline release page.

- [x] **T11.17 `[F]` Scan a pass to open it**
  Refs: §4.15.8 · P11, G5
  `/gate-out/scan`, with entries on the gate-out list and the offline release page. Online it
  resolves through `/qr/scan` and opens `/gate-out/{id}?release=1`; offline it matches the cached
  releasable passes. A pass that cannot be released says why.
  *Done when:* scanning a printed pass opens its release sheet online and offline; an expired pass
  says it is expired.

### Verification

- [x] **T11.18 `[T]` Boxes end to end**
  Refs: §4.15.12
  `e2e/boxes.spec.ts` on the phone project: receive a box of three by manual entry; scan it at
  gate-out and see one line with three serials; release by scanning two units and a stranger.
  *Done when:* the spec passes against a seeded tenant.

- [x] **T11.19 `[I]` Ship and check on production**
  Refs: §12.2
  Push, watch CI deploy, and confirm on Silvertech that a box can be received, looked up and sent
  out. The migrations are additive, so a rollback is the previous image.
  *Done when:* CI is green, the deploy is healthy, and the production check passes.

---

## Phase 12 — Finding an item

C9 (approved 2026-10-03), design §7.3b. Fixes the 200-item cap on every item picker as a side effect.

- [x] **T12.1 `[B]` Search order and a fuller offline item list**
  Refs: §7.3b · C9
  `/item-types?search=` orders starts-with name matches first, then name contains, then code and
  description, alphabetically within each; `is_archived` is filterable. The sync bundle's items gain
  `description` and `category_name`.
  *Done when:* tests show "rru" ranks "RRU 2x40W" above "Antenna for RRU"; the bundle carries the
  new fields; schema regenerated.

- [x] **T12.2 `[F]` `ItemPicker`**
  Refs: §7.3b · C9
  The combobox: online search, offline search over the bundle, recent items, "N of M shown",
  value-by-id, Add new item. Pure helpers unit-tested against the same ranking cases as T12.1.
  *Done when:* unit tests pass; typecheck, lint and build are clean.

- [x] **T12.3 `[F]` Every item picker uses it**
  Refs: §7.3b · C9
  Gate-in line, gate-out line, stock filter, transfers, counts. The E2E specs pick items by typing.
  *Done when:* the gate-in, gate-out and boxes specs pass on the phone project against the demo
  tenant.

- [x] **T12.4 `[F]` Change a gate-in line**
  Refs: D9, D8, P1
  "Change" on every gate-in line reopens the line sheet filled in, including serials with their
  boxes and drums; Save replaces the line in place. Pure line ↔ sheet mapping unit-tested.
  *Done when:* an E2E changes a line's quantity and moves a unit to another box before receiving,
  and the received delivery shows the change.

- [x] **T12.5 `[B]` Lock tracking mode and unit once an item has moved**
  Refs: C10
  Updating an item refuses a change to `default_tracking_mode` or `uom` when any stock movement
  names it (`ITEM_TRACKING_LOCKED`, naming the field); the item payload says whether it is locked.
  *Done when:* API tests cover the refusal, the unlocked case, and every other field still editable.

- [x] **T12.6 `[F]` Edit and archive an item from the catalogue**
  Refs: C10
  Rows open the item sheet filled in; Save patches; archive and restore; locked fields shown fixed
  with the reason; read-only for those without `catalogue.manage`.
  *Done when:* an E2E renames an item and finds it by its new name in the gate-in picker.


- [x] **T12.7 `[B]` Loose cable length beside drums**
  Refs: §7.3c · D10
  `post_movement` refuses a BULK movement of a reel item beyond the loose length (`ON_DRUMS_ONLY`,
  naming the drums); the stock API carries `on_drums` and `loose` for reel items.
  *Done when:* tests receive 1,000 m on two drums and 240 m loose, issue 200 m loose, refuse 100 m
  more naming the drums, and `verify_ledger` stays clean.

- [x] **T12.8 `[F]` "Not on a drum" and "Loose length"**
  Refs: §7.3c · D10
  Gate-in cable lines offer "Not on a drum"; gate-out cable lines offer "Loose length"; stock on
  hand shows the split.
  *Done when:* an E2E receives a coil of cable with no drum number and sends part of it out.
---

## Phase 13 — Material earmarked for a site

Epic Q, design §4.16. Approved 2026-10-03; nothing ships until T13.10 passes. Backend first; the two ledger and
gate-out tasks land alone, as T11.3 and T11.4 did, because every movement goes through them.

- [x] **T13.1 `[B]` Earmark models** — Refs: §4.16.2 · Q1, Q2
  `SerialUnit.earmark_site`, `Reel.earmark_site`, `BulkEarmark`, `EarmarkEvent` (append-only),
  `GateIn.for_site`, `GateInLine.for_site`, `GateOutLine.divert_reason`; RLS; isolation.
  *Done when:* migrations apply; RLS, isolation and append-only tests pass.

- [x] **T13.2 `[B]` Ledger hook for earmarks** — Refs: §4.16.3 · Q2, Q3
  Deliver, divert with a reason, bulk draw order, carry inside the perimeter, corrections reduce;
  `verify_ledger` checks earmarks. Lands alone.
  *Done when:* each rule is tested and the whole backend suite passes unchanged.

- [x] **T13.3 `[B]` Gate-in earmarks** — Refs: §4.16.4 · Q1
  *Done when:* a delivery for site X earmarks its units, drums and bulk; a line can override; an
  offline replay earmarks once.

- [x] **T13.4 `[B]` Gate-out diversions** — Refs: §4.16.5 · Q3
  Destination sites, submit-time diversion detection and reasons, the approval payload, release
  consuming earmarks.
  *Done when:* to site X uses X's earmark with no reason; to site Y is refused without a reason and
  sent with one; release records DELIVERED and DIVERTED.

- [x] **T13.5 `[B]` Change an earmark, and earmarks on reads** — Refs: §4.16.6, §4.16.7 · Q2, Q4
  *Done when:* the change endpoint moves and clears earmarks with a reason; stock rows carry the
  split; units and drums carry their site.

- [x] **T13.6 `[B]` "Material by site" report** — Refs: §4.16.8 · Q5
  *Done when:* on a scenario the four columns are right per site and item, and it exports.

- [x] **T13.7 `[F]` "For site" at gate-in** — Refs: Q1
- [x] **T13.5a `[B]` What is waiting for a site** — Refs: §4.16.8a · Q6
  *Done when:* the endpoint lists a site's earmarked units, drums and bulk at a location, and its
  open jobs by project.
- [x] **T13.8 `[F]` Site-first gate-out, diversions, approval** — Refs: Q3, Q6
  No project destination; choosing a site preloads its earmarked material ticked; job picker when
  needed; diversion reasons; the approver sees diversions.
- [x] **T13.9 `[F]` Earmarks on stock screens, and changing one** — Refs: Q2, Q4
- [x] **T13.10 `[T]` Earmarks end to end, and ship** — Refs: §4.16.10
  *Done when:* the phone E2E in §4.16.10 passes against the demo tenant, CI is green, and production
  serves it.

---

## Phase 14 — Stock within reach, vendor labels and aiming the camera

E7–E10, design §7.3d. Proposed 2026-10-09 from Elias's feedback. One deploy after T14.8; the serial
correction (T14.9) runs on production only after its dry run has been reviewed.

- [x] **T14.0 `[F]` Camera button reads "Scan QR code"** — Refs: §7.3d · E10 (trivial, done first)

- [x] **T14.1 `[B]` ISO 15434 labels in `read_label`** — Refs: §7.3d · E9
  New rule after the gate-pass token; new shared vectors (one serial, two, no `S`, run together,
  trailing EOT).
  *Done when:* the Python vector tests pass.

- [x] **T14.2 `[F]` ISO 15434 labels in `readLabel`** — Refs: §7.3d · E9
  *Done when:* the Vitest vector tests pass against the same file.

- [x] **T14.3 `[B]` `GET /stock/find` and `GET /stock/summary`** — Refs: §7.3d · E8
  *Done when:* ranking and `on_hand` (inside the perimeter only) are tested, the summary figures
  are right on a scenario with 6 earmarked sites, isolation holds, and each is one query plus a
  constant.

- [x] **T14.4 `[F]` Stock in the bar, Approvals under More with a count** — Refs: §7.3d · E7
  *Done when:* an owner's bar reads Home, Gate-in, Gate-out, Stock, More; More shows the count when
  approvals wait and none at zero.

- [x] **T14.5 `[F]` Stock page reads `?item=` and `?earmarked_for=`** — Refs: §7.3d · E8
  *Done when:* both links land with the filter set and shown.

- [x] **T14.6 `[F]` Find stock and In the yard on Home** — Refs: §7.3d · E8
  Typing lists items with quantities; a scan or Enter looks up first; offline and empty-yard states.
  *Done when:* a Vitest test covers the Enter-versus-scan helper and the screen works against a
  local backend.

- [x] **T14.7 `[F]` Aim the camera** — Refs: §7.3d · E10
  Centre-crop decoding on both decoders, nearest-to-centre choice, smaller box with a dimmed
  surround, zoom where supported.
  *Done when:* the Vitest test of `aimRegion` passes, and on a phone a code beside the box is not
  read while one inside it is.

- [ ] **T14.8 `[E2E]` Phone run** — Refs: §7.3d testing · E7, E8
  *Done when:* the bar shows Stock; Home → item → Stock filtered; Home → serial + Enter → the unit;
  More shows Approvals; the whole phone suite stays green. Then push once (T14.9's command built
  by then) and check on Silvertech.

- [ ] **T14.9 `[B]` `correct_label_serials` command** — Refs: §7.3d · E9
  Dry run by default; `--apply` renames the unit and its `GateInSerial` rows, with a
  `SERIAL_CORRECTED` audit row; collisions are reported.
  *Done when:* tested locally; on production the dry run is shown to the owner, then applied on
  their word, and a lookup of `2641098217` finds the rectifier.

## Phase 15 — Finance, stage 1: money out

Epic R (R1–R6), design §4.17. Approved 2026-10-09. One Sonnet agent per task, reviewed and committed
one task at a time; push once after T15.11. T15.1 and T15.2 change shared tables and the approval
engine, so each lands alone with the full backend suite green. After T15.3, the backend tasks
(T15.4–T15.6) run in parallel, and so do the frontend tasks (T15.8–T15.10).

- [x] **T15.1 `[B]` Finance models and migrations** — Refs: §4.17.2, §4.17.7, §4.17.11 · R1–R4
  - `ExpenseStatus` five states; `ExpenseCategory.kind`; the new `ProjectExpense` fields;
    `ExpenseCasualLine`; `Casual`; `AllowanceRequest` (`DocumentType.ALLOWANCE`, series `AR`).
  - `ApprovalRequest.required_permission`, with its CHECK; `OrganizationSettings`
    `finance_director_role` and `allowance_limits` with their defaults; `Attachment.caption` and
    `client_uuid`.
  - `finance.approve` and the seeded Finance role; RLS and isolation fixtures.
  - The data migration: SUBMITTED → PENDING_PM, category kinds and seeds.

  *Done when:* migrations apply to a copy of production-shaped data; RLS, isolation and the
  save-guard tests pass; the full backend suite passes.

- [x] **T15.2 `[B]` Approval engine: finance levels and the pending fix** — Refs: §4.17.3, §4.17.6 · R4
  - `required_levels` finance branch: PM, then `finance.approve`, with the PM and Director skip.
  - `can_approve`: no self-approval on finance entries; `required_permission` matching.
  - Rewrite `approvals/pending` so it returns only the caller's role, user or permission levels.
  - PM reassignment re-addresses open PM-level requests.

  *Done when:* each routing case is tested; the pending leak is gone with gate-out PM approvals
  unchanged; the full backend suite passes.

- [x] **T15.3 `[B]` Finance services and rules** — Refs: §4.17.3–§4.17.5, §4.17.11 · R1–R5
  - In `commercials/finance.py` and `finance_rules.py`: record, request, decide, resubmit, mark
    paid, close float, register casual, `resolve_project`.
  - Overlap (locked), limits, the float balance and the open-float warning.
  - Expense cost counts APPROVED and PAID; `decide_expense` is retired.

  *Done when:* the §4.17.13 backend service and rule cases pass.

- [x] **T15.4 `[B]` Endpoints** — Refs: §4.17.6, §4.17.7 · R1–R5
  - Expense, allowance-request, casual and finance-settings endpoints.
  - Attachment owner rule, captions and the Casual target; project `site`/`status` filters; casual
    ID masking.
  - Schema regenerated.

  *Done when:* each endpoint and permission is tested, isolation holds, and the schema check passes.

- [x] **T15.5 `[B]` Notifications** — Refs: §4.17.9 · R4
  *Done when:* `LEVEL_APPROVERS` resolves correctly, never to the recorder, and each event emails
  or notifies as the matrix says.

- [x] **T15.6 `[B]` Offline sync for finance** — Refs: §4.17.8 · R6
  - Handlers for EXPENSE, ALLOWANCE_REQUEST and CASUAL; casual references within a queue;
    `supersedes_client_uuid`; the bundle additions.

  *Done when:* replay, refusal, supersede and in-batch reference tests pass, and approving offline
  is refused.

- [x] **T15.7 `[F]` Money types, API hooks and rules** — Refs: §4.17.10 · R1–R5
  `features/money/types.ts`, hooks and `rules.ts`, with Vitest.

  *Done when:* the helper tests pass and the typecheck is clean.

- [x] **T15.8 `[F]` Money screens** — Refs: §4.17.10 · R1–R3
  - My expenses; My requests and floats; Casuals.
  - Record expense (moved, with a redirect from the old route), Request allowance, Add casual.
  - Site-first project choice, fuel and casual fields, captioned photos; a Money nav entry.

  *Done when:* each form works against a local backend; typecheck, Vitest and build pass.

- [x] **T15.9 `[F]` Approvals, To pay and Settings → Finance** — Refs: §4.17.6, §4.17.10 · R2, R4, R5
  *Done when:* a PM and then Finance can approve from the Approvals screen; Finance can mark paid and
  close a float; the limits and the Director role can be set.

- [x] **T15.10 `[F]` Offline capture for finance** — Refs: §4.17.8 · R6
  - Queue the three new operations; Dexie version 2 with a photos table; `drainPhotos`.
  - "Waiting to send" in the lists; "Fix and resend" on a refused entry.

  *Done when:* a Vitest test covers the photo step and queue ordering; an entry saved offline lands
  with its photos.

- [x] **T15.11 `[E2E]` Phone run** — Refs: §4.17.13 · R1–R6
  *Done when:* the §4.17.13 E2E scenario passes and the phone suite stays green. Then push once and
  check on Silvertech.

## Phase 16 — Finance, stage 3: clock-in

Approved design §4.18. Approved 2026-10-09.

Epic R (R13), design §4.18. One Sonnet agent per task, reviewed and committed one task at a time; push
once after T16.16. T16.1 to T16.3 change shared tables, the places and the approval engine, so each
lands alone with the full backend suite green. T16.4 touches only `core/geo.py`, the shared fixture and
the place serializers, so it may run beside T16.3. After T16.4 the services (T16.5 `attendance/services.py`,
T16.6 `attendance/sweeps.py` and `routing.py`, T16.7 `attendance/corrections.py`) run in parallel, each in
its own file. T16.8 (endpoints) follows them; T16.9 (notifications) and T16.10 (sync and bundle) run
beside it. T16.11 regenerates the schema once and lays the frontend foundation; then the frontend tasks
T16.12 to T16.15 run in parallel, each owning its own files. Router and URL registration lines are the
only shared touch; the task that lands second resolves them.

- [ ] **T16.1 `[B]` Places and settings: coordinates, radius, OFFICE** — Refs: §4.18.2, §4.18.3, §4.18.8 · R13
  - `Site.radius_m` (default 200, CHECK 20-2000); `Location.latitude`, `longitude`, `radius_m` with the
    range and both-or-neither CHECKs; `LocationType.OFFICE`; `area_history` JSON on both.
  - `OrganizationSettings.clock_auto_close_hour` (default 18) and `clock_accuracy_cap_m` (default 100).
  - OFFICE never gets a `StockNode` and is excluded from stock pickers; `seed_locations_and_nodes` and
    the factories give "Main yard" and test sites coordinates.

  *Done when:* migrations apply to a copy of production-shaped data, existing sites keep working with no
  coordinates, the stock pickers do not list an OFFICE, and the full backend suite passes.

- [ ] **T16.2 `[B]` Attendance models, RLS and permission** — Refs: §4.18.2, §4.18.8 · R13
  - New `attendance` app: `WorkSession` (one-open partial UNIQUE, out ≥ in, place XOR CHECK, `added_by`
    and `added_reason` for 4.18.6a), `WorkDay`, `WorkSessionCorrection` (append-only trigger).
  - `enable_rls` migration and `attendance/isolation.py` fixtures; the immutable-once-approved guard.
  - `attendance.view_all` in `accounts/permissions_registry.py`, `accounts/role_sync` (seeded Finance role).

  *Done when:* RLS, isolation, constraint and append-only tests pass and the full backend suite passes.

- [ ] **T16.3 `[B]` Approval engine: work-day slices** — Refs: §4.18.5 (engine changes 1-6), §4.18.1 · R13
  - `WORK_DAY_DOCUMENT_TYPES`, `_work_day_levels`; `required_levels`/`create_requests` take the slice.
  - `can_approve` self-approval first; `next_pending_request`/`record_decision` take `approval_request`.
  - `open_requests_addressed_to` excludes work days from the gate-out blanket; `_level_approvers`
    returns every open request at the lowest level; `readdress_project_requests` WorkDay branch.

  *Done when:* each of the six changes is tested, a rejected slice supersedes only itself, gate-out and
  finance routing are unchanged, and the full backend suite passes.

- [ ] **T16.4 `[B]` Geo check, required coordinates, project split** — Refs: §4.18.4, §4.18.8, §4.18.5 step 5 · R13
  - `core/geo.py` (`haversine_m`, `check_area`) and `shared/area-cases.json`, read by pytest.
  - `CoordinatesMixin` on the site and location serializers and admin (`COORDINATES_REQUIRED`, system
    rows exempt); `has_coordinates` and `?missing_coordinates=true`; `area_history` push on area change.
  - `commercials/finance.py`: `open_projects_of(site)` split out, `resolve_project` calls it.

  *Done when:* the geo fixture cases pass, a save without coordinates is refused except system rows, an
  area edit pushes history (max 10), and `resolve_project` behaves as before.

- [ ] **T16.5 `[B]` Clock-in and clock-out services** — Refs: §4.18.3, §4.18.5, §4.18.4 · R13
  - `attendance/services.py`: `clock_in` (clockable place, time bounds, area check, offline replay with
    `place_area` and `area_history`, project resolution, close the open session, get-or-create the day) and
    `clock_out` (by `session_client_uuid`, never refused on position, replacing an auto-close).
  - Idempotent on `client_uuid`; audit rows; every error code of §4.18.12 for these paths.

  *Done when:* the §4.18.13 clock-in, replay (new area, old area flagged, outside both, forged
  `place_area`) and concurrency cases pass.

- [ ] **T16.6 `[B]` Auto-close, day formation and routing** — Refs: §4.18.5 · R13
  - `attendance/sweeps.py`: `close_stale_sessions` at the cutoff itself, `form_days` in the organization's
    timezone; hourly beat entry `attendance-sweep` with per-organization fan-out and guarded steps.
  - `attendance/routing.py::route_day`: group by addressee, own-day rules, parallel slices, idempotent,
    unrouted when nobody can approve.

  *Done when:* auto-close in Nairobi and a second zone, and every `route_day` case in §4.18.13, pass.

- [ ] **T16.7 `[B]` Corrections and the Director-added day** — Refs: §4.18.6, §4.18.6a · R13
  - `attendance/corrections.py::correct_session` (EDIT and ADD, rejected slice only, 30 days, reason,
    originals kept, reopens only that slice to the same addressee, links old and new request).
  - `add_work_day` for `finance_director_role` holders: no position, `closed_by=PERSON`, flagged "added by
    the Director", audited, never for their own day; `WORK_DAY_ADD_NOT_ALLOWED`.

  *Done when:* the correction cases (same approver, other slice untouched, window, second round) and the
  Director-add cases (own day refused, approved by the PM or another Director) pass.

- [ ] **T16.8 `[B]` Endpoints and visibility** — Refs: §4.18.7, §4.18.8 · R13
  - `/work-sessions` (open, clock-in, clock-out, correct), `/work-days` (list scopes, detail, decide, add),
    `/attendance/settings`; "awaiting me" and project filters.
  - Visibility (own, PM slice, `attendance.view_all`, Director); `get_document` and `approvals/pending`
    summaries for work days; derived flags.

  *Done when:* each endpoint and permission is tested, isolation holds, and no schema regeneration is
  attempted here (T16.11).

- [ ] **T16.9 `[B]` Notifications** — Refs: §4.18.10 · R13
  `attendance.awaiting_approval` (per slice, "corrected" tag), `attendance.rejected`, `attendance.unrouted`
  in `notifications/matrix.py`.

  *Done when:* `LEVEL_APPROVERS` reaches both PMs of a two-PM day, never the person, and each event goes
  to the channels the matrix says.

- [ ] **T16.10 `[B]` Offline sync and bundle** — Refs: §4.18.9 · R6, R13
  - `SyncOperation.CLOCK_IN`/`CLOCK_OUT` and their `_HANDLERS` entries calling the services.
  - `OfflineBundleView`: place coordinates, radius, `has_coordinates` and the `attendance` settings.

  *Done when:* replay, refusal as `SyncException`, clock-out by `session_client_uuid`, replay overriding an
  auto-close and approving offline refused all pass.

- [ ] **T16.11 `[F]` Schema regeneration and attendance foundation** — Refs: §4.18.4, §4.18.8, §4.18.11 · R13
  - Regenerate `backend/api-schema.yml` and `frontend/src/api/schema.d.ts` once, after T16.8 to T16.10.
  - `features/attendance/`: `area.ts` (reads `shared/area-cases.json`), `position.ts`, `nearby.ts`, types,
    hooks; `attendance.view_all` in `frontend/src/auth/permissions.ts`.

  *Done when:* the Vitest area tests pass on the shared fixture, the schema check passes, and the
  typecheck is clean.

- [ ] **T16.12 `[F]` Place sheets and Settings → Clock-in** — Refs: §4.18.11, §4.18.8 · R13
  - `SiteSheet`/`LocationSheet` in `NetworkPage.tsx` gain an edit mode with latitude, longitude, radius;
    `components/ui/UseMyLocation.tsx`; `quickCreate.tsx` inherits the required coordinates.
  - "No coordinates" badge and filter on the site list; `AttendancePage.tsx` (hour, cap, missing places,
    "Set coordinates" prompt for a new tenant's Main yard).

  *Done when:* a site and a location can be created and edited with coordinates against a local backend;
  typecheck, Vitest and build pass.

- [ ] **T16.13 `[F]` Home clock-in card and My time** — Refs: §4.18.11 · R13
  - Clock-in card (nearest places with distance, open session, elapsed time, refusal wording).
  - `/time` page: own days, Team and Everyone by permission, rejected day with the **Correct** sheet
    (edit or add, required reason); nav entry.

  *Done when:* clock in and out and a correction work against a local backend; typecheck, Vitest and
  build pass.

- [ ] **T16.14 `[F]` Approvals, Days tab and Director add** — Refs: §4.18.11, §4.18.6a, §4.18.7 · R13
  - `DayApprovals.tsx`: sessions, distances, flags, original beside corrected, earlier rejection
    reason; Approve and Reject (reason required); registered as a tab on the Approvals screen.
  - "Add a day" sheet for Director-role holders, showing "added by the Director" wherever it appears.

  *Done when:* a PM approves or rejects their own slice only; a Director can add a day; typecheck,
  Vitest and build pass.

- [ ] **T16.15 `[F]` Offline capture for clock-in** — Refs: §4.18.9, §4.18.11 · R6, R13
  - `ATTENDANCE_OPERATIONS` in `offline/db.ts`, `queued.ts`, `offline.ts`; local open-session reference row.
  - Early refusal from the bundle area; "Waiting to send" and the stays-on-phone refusal.

  *Done when:* Vitest on `queued.ts` passes and a clock-in saved offline lands, flagged where the area
  changed.

- [ ] **T16.16 `[E2E]` Phone run** — Refs: §4.18.13 · R13
  *Done when:* the three §4.18.13 scenarios pass with `context.setGeolocation` and the phone suite stays
  green. Then push once and check on Silvertech.

---

## Phase 17 — Finance, stage 4: assets and suppliers

Approved design §4.20. Approved 2026-10-09.

Epic R (R14–R15), design §4.20. Runs before phase 18, whose site purchases need `network.Supplier` and
`assert_payable`. One Sonnet agent per task, reviewed and committed one task at a time; push once after
T17.17. T17.1 to T17.3 change shared tables and the approval engine, so each lands alone with the full
backend suite green. After T17.3, the services T17.4 (`network/suppliers.py`) and T17.5 (`assets/`)
run in parallel; T17.6 follows both. Then the endpoints T17.7 (suppliers) and T17.8 (assets),
notifications T17.9 and sync T17.10 run in parallel. T17.11 regenerates the schema once; then the
frontend tasks T17.12 to T17.14 and T17.16 run in parallel, and T17.15 follows T17.12 because it uses
the supplier quick-create.

- [ ] **T17.1 `[B]` Supplier model, gate-in FK and history link** — Refs: §4.20.2, §4.20.5 · R15
  - `network.Supplier` with `name_key`/`kra_pin_key` constraints, `SupplierStatus`, `client_uuid`; RLS and
    `network/isolation.py`; `GateIn.supplier` (PROTECT, null); `delete` raises.
  - `receiving` data migration linking `GateIn.supplier` by `name_key` (a no-op on an empty register; never
    creates suppliers, never overwrites).

  *Done when:* migrations apply to production-shaped data with and without matching names; RLS and
  isolation pass; the full backend suite passes.

- [ ] **T17.2 `[B]` Assets app, vehicle FK and permission** — Refs: §4.20.2, §4.20.4, §4.20.7 · R14
  - New `assets` app: `Asset` (tag key unique, vehicle-only fields, CLOSED CHECK, `delete` raises) and
    `AssetHandover` (append-only trigger, both-ends CHECK); `enable_rls` and `assets/isolation.py`.
  - `ProjectExpense.vehicle` (PROTECT, null); `asset.manage` in `accounts/permissions_registry.py`.

  *Done when:* constraint, trigger, RLS and isolation tests pass and the full backend suite passes.

- [ ] **T17.3 `[B]` Approval engine: suppliers** — Refs: §4.20.3, §4.20.9 · R15
  - `required_levels` supplier branch (one level, `required_permission="finance.approve"`, no PM level);
    `SUPPLIER_DOCUMENT_TYPES` in `can_approve`; `requested_by_id` alias; `approvals/pending` and
    `get_document` for `network.Supplier`.

  *Done when:* the registrar cannot approve their own entry, holders of `finance.approve` see it in
  pending, existing finance and gate-out routing is unchanged, and the full backend suite passes.

- [ ] **T17.4 `[B]` Supplier services** — Refs: §4.20.3, §4.20.5, §4.20.2 · R15
  - `network/suppliers.py`: `add_supplier` (duplicate name and PIN errors naming the existing one),
    `update_supplier`, `decide_supplier` (`SUPPLIER_PIN_REQUIRED`), `resubmit`, `set_active`,
    `link_history`, `assert_payable`; audit rows.
  - A sensitive edit of an APPROVED supplier is audited with before and after and notifies Finance; it does
    not reopen approval (see inconsistency notes: confirm with §4.20.2).

  *Done when:* the §4.20.12 supplier cases pass, including `link_history` idempotence and tenant bounds.

- [ ] **T17.5 `[B]` Asset services and fuel read** — Refs: §4.20.4 · R14
  - `assets/services.py`: create (writes the first handover), `handover` under `select_for_update`,
    `close` (final handover to the yard), `fuel_position(asset, from, to)` and the ranking query.
  - `custody.services.assert_can_deactivate` counts held assets (`HolderStillHasMaterial` with `assets`).

  *Done when:* the asset cases of §4.20.12 pass (holder, manager, stranger, concurrent, frozen after close,
  deactivation) and fuel matches `expense_cost` for APPROVED and PAID, with pending shown apart.

- [ ] **T17.6 `[B]` Fuel by vehicle and gate-in supplier on the API** — Refs: §4.20.4, §4.20.5 · R14, R15
  - `commercials/finance.record_expense`: a FUEL expense needs an open VEHICLE or GENERATOR `vehicle`,
    fills `vehicle_reg`; a `vehicle_reg`-only payload is accepted as "not on the register";
    `FUEL_VEHICLE_REQUIRED`, `FUEL_VEHICLE_MISMATCH`, `ASSET_CLOSED`.
  - `GateInSerializer`: accepts `supplier`, sets `supplier_name`, refuses inactive or REJECTED on a new
    gate-in, keeps text-only; `supplier` filter; `link_history` call on add and approve.

  *Done when:* legacy payloads, mismatch, closed vehicle, text-only gate-in and the supplier filter are
  tested and the full backend suite passes.

- [ ] **T17.7 `[B]` Supplier endpoints and attachments** — Refs: §4.20.6, §4.20.7 · R15
  - `/suppliers` (list filters, `payable=true`, PATCH rules, decide, resubmit, deactivate, reactivate,
    `link-history`) in `network/views.py`; payment details and PIN field-gated.
  - `core/attachment_api.py`: targets `network.Supplier` and `assets.Asset` with their caption lists and
    owner and lock rules (the only task that edits that file in this phase).

  *Done when:* each endpoint, permission and attachment rule is tested, no payment data leaks to a member,
  and isolation holds.

- [ ] **T17.8 `[B]` Asset endpoints** — Refs: §4.20.6, §4.20.7 · R14
  - `/assets` register, PATCH, `handover`, `close`, `handovers`, `fuel`, `fuel-summary` in `assets/views.py`.
  - `core/field_permissions.py`: `cost` and `purchase_terms` gated to `asset.manage`, `finance.approve`,
    `project.view_cost`.

  *Done when:* each endpoint and permission is tested, the list omits gated fields for others, and
  isolation holds.

- [ ] **T17.9 `[B]` Notifications and expiry sweep** — Refs: §4.20.4, §4.20.9 · R14, R15
  - `asset.expiry_due` (`Recipient.OWNER`, in-app and email); `_sweep_asset_expiries` in
    `core/sweeps.py`, guarded separately, with `*_alerted_for` catch-up and re-arm.
  - Supplier approval events reuse `finance.*` with `payload.kind = "supplier"`; the sensitive-edit notice.

  *Done when:* the 30-day edge, catch-up, renewal re-arm, one alert only, closed assets skipped, and a
  failing step not stopping the other sweeps are tested.

- [ ] **T17.10 `[B]` Offline sync and bundle** — Refs: §4.20.8 · R6, R15
  - `SyncOperation.SUPPLIER` via `add_supplier`; gate-in `supplier_client_uuid` resolved within a batch;
    supersede on refusal.
  - Bundle `suppliers` (active, no PIN or payment) and `vehicles` (ACTIVE VEHICLE and GENERATOR).

  *Done when:* supplier then gate-in in one batch, duplicate refusal and supersede, a closed vehicle
  refused with `ASSET_CLOSED` at replay, and the bundle contents (no PIN, no payment) are tested.

- [ ] **T17.11 `[F]` Schema regeneration and foundation** — Refs: §4.20.10 · R14, R15
  - Regenerate `backend/api-schema.yml` and `frontend/src/api/schema.d.ts` once, after T17.7 to T17.10.
  - Types, hooks, `asset.manage` in `auth/permissions.ts`; `rules.ts` vehicle-picker rules and the
    supplier-duplicate resolution helper, with Vitest.

  *Done when:* the helper tests pass, the schema check passes and the typecheck is clean.

- [ ] **T17.12 `[F]` Settings → Network: Suppliers** — Refs: §4.20.10, §4.20.5 · R15
  - Suppliers tab with status chips and search; `SupplierSheet` (details, payment, documents with kind
    chooser), Deactivate, "Link past deliveries"; duplicate PIN offers "Use existing".
  - Registered in `features/quickCreate.tsx`.

  *Done when:* a supplier can be added, edited and its documents attached against a local backend;
  typecheck, Vitest and build pass.

- [ ] **T17.13 `[F]` Approvals: Suppliers tab** — Refs: §4.20.10, §4.20.3 · R15
  Sheet with PIN, payment route, documents and who added it; Approve and Reject with reason.

  *Done when:* Finance can approve a supplier from the Approvals screen and the registrar cannot;
  typecheck, Vitest and build pass.

- [ ] **T17.14 `[F]` Assets** — Refs: §4.20.10, §4.20.4 · R14
  - `features/assets/`: register list, `AssetDetailPage` (details, documents, handover history, Hand over,
    Close), Fuel panel with month selector, amber and red expiry chips, nav entry.

  *Done when:* create, hand over, close and the fuel panel work against a local backend; typecheck,
  Vitest and build pass.

- [ ] **T17.15 `[F]` Record expense vehicle picker and gate-in supplier** — Refs: §4.20.10, §4.20.5 · R14, R15
  - `RecordExpensePage.tsx`: vehicle picker from the bundle and "Not ours" (typed registration).
  - `GateInCapturePage.tsx`: supplier `ReferenceSelect` with "Add new supplier"; supplier chip on detail.

  *Done when:* a fuel expense and a gate-in work with the new pickers; typecheck, Vitest and build pass.

- [ ] **T17.16 `[F]` Offline capture for suppliers and vehicles** — Refs: §4.20.8 · R6
  - Queue `SUPPLIER`; gate-in payload and `types.ts` gain `supplier` and `supplier_client_uuid`; bundle
    caches for suppliers and vehicles.
  - "Waiting to send" and "Fix and resend" for a refused supplier, dependent gate-in waiting behind it.

  *Done when:* a Vitest test covers queue ordering with the dependency, and a supplier added offline lands
  with its gate-in.

- [ ] **T17.17 `[E2E]` Phone run** — Refs: §4.20.12 · R14, R15
  *Done when:* the five §4.20.12 scenarios pass and the phone suite stays green. Then push once and check
  on Silvertech.

---

## Phase 18 — Finance, stage 2: POs, budgets and sites

Approved design §4.19. Approved 2026-10-09.

Epic R (R7–R12), design §4.19. Runs after phase 17: it needs `network.Supplier` and `assert_payable`.
One Sonnet agent per task, reviewed and committed one task at a time; push once after T18.22. T18.1 to
T18.5 change shared tables, the `Project.sites` through model and the approval engine, so each lands alone
with the full backend suite green, in that order (all migrations). After T18.5, the services T18.6
(`budget.py`, `costing.py`), T18.8 (`contracts.py`, `jobs/`) and T18.9 (`milestones.py`, reports) run in
parallel; T18.7 follows T18.6, and T18.10 follows T18.9. Then the endpoints (T18.11, T18.12),
notifications (T18.13) and sync (T18.14) run in parallel. T18.15 regenerates the schema once; then the
frontend tasks run in parallel (T18.16, T18.18, T18.19, T18.20, T18.21), with T18.17 after T18.16 and
T18.20 and T18.21 after T18.19, which creates the project tab shell.

- [ ] **T18.1 `[B]` Project sites through model and PO columns** — Refs: §4.19.2, §4.19.6, §4.19.14 · R10, R12
  - `ProjectSite` as the through model of `Project.sites` (`SeparateDatabaseAndState`, `db_table =
    "network_project_sites"`; `organization` backfilled then NOT NULL; `mobilised_on`, `accepted_on`);
    `enable_rls` and isolation fixture.
  - `Project.po_issue_date`, `payment_terms`, `payment_terms_days`, `po_recorded_at` (existing PO projects
    get `opened_at`).
  - `network.services.set_project_sites` (`SITE_HAS_PROJECT_DATA`); explicit serializer `sites`; move every
    fixture and factory that bulk-adds sites to the service (grep first).

  *Done when:* migrations apply to production-shaped data keeping every link and org, `project.sites`
  callers and `ProjectFilter.site` still work, and the full backend suite passes.

- [ ] **T18.2 `[B]` Site purchase models and over-budget columns** — Refs: §4.19.2, §4.19.14 · R7, R9
  - `SitePurchase` and `SitePurchaseLine` (CHECKs, unique number and `client_uuid`, `StatusGuardMixin`,
    line freeze); `DocumentType.SITE_PURCHASE` (series `SP`).
  - `over_budget_by` and `over_budget_reason` on `ProjectExpense`, `AllowanceRequest`, `SitePurchase`.
  - RLS and `commercials/isolation.py`; migration ordered after phase 17's suppliers.

  *Done when:* constraint, save-guard, RLS and isolation tests pass and the full backend suite passes.

- [ ] **T18.3 `[B]` Subcontract models and job link** — Refs: §4.19.2, §4.19.4 · R8
  - `Subcontract` (series `SC`, sites M2M), `SubcontractPayment` (own transition table, `reverses`);
    `Job.subcontract` and `over_contract_reason` (CHECK on SUBCONTRACTED, joins `_DELIVERY_FIELDS`).
  - RLS and isolation fixtures.

  *Done when:* constraint, freeze-on-close, RLS and isolation tests pass; old subcontracted jobs keep
  `subcontract = NULL`; the full backend suite passes.

- [ ] **T18.4 `[B]` Milestone models** — Refs: §4.19.2, §4.19.7 · R11
  - `ProjectMilestone` (unique sequence, condition CHECK), `MilestoneInvoice` and `MilestoneReceipt`
    (append-only with void columns); RLS and isolation fixtures.

  *Done when:* constraint, append-only and isolation tests pass and the full backend suite passes.

- [ ] **T18.5 `[B]` Approval engine: purchases and subcontract payments** — Refs: §4.19.3, §4.19.4, §4.19.10 · R7, R8
  - `SitePurchase` in `FINANCE_DOCUMENT_TYPES`; a sibling set for `SubcontractPayment` with one PM level
    (`PROJECT_HAS_NO_ACTIVE_MANAGER`), recorder refused.
  - `can_approve`, `approvals/pending`, `get_document` for both; `_visible_to` for purchases;
    `readdress_project_requests` for both.

  *Done when:* each routing case (PM-recorded, Director-recorded, recorder-PM refused, inactive PM) is
  tested, existing routing is unchanged, and the full backend suite passes.

- [ ] **T18.6 `[B]` Budget position and purchase cost** — Refs: §4.19.5, §4.19.3, §4.19.14 · R9
  - `commercials/budget.py` (`position`, `check`) with the §4.19.5 table; `costing.purchase_cost`
    (USED_AT_SITE only, signed) and `ProjectCost.purchases`, with `from_snapshot` defaulting to 0.
  - `budget_position` on `ProjectViewSet.performance` and the project serializer, gated by
    `may_see_project_cost`.

  *Done when:* the table-driven budget test (no double count, float, yard purchase before and after
  posting, advance, no-PO project, closed snapshot, concurrent pending) passes.

- [ ] **T18.7 `[B]` Site purchase services and draft delivery** — Refs: §4.19.3, §4.19.5, §4.19.8 · R7, R9
  - `finance.record_site_purchase`, `decide`, `resubmit`, `mark_paid` (`assert_payable`), `reverse_purchase`;
    over-budget wiring into `record_expense` and `request_allowance` too.
  - `receiving.services.draft_gate_in_for_purchase` under `select_for_update`; `YARD_DELIVERY_FAILED`;
    `SITE_PURCHASE_RECEIVED`.

  *Done when:* the §4.19.16 purchase cases pass (cost once, one draft gate-in even when repeated or
  concurrent, reversal rules, unpayable unapproved supplier, idempotency).

- [ ] **T18.8 `[B]` Subcontract services, job link and spend report** — Refs: §4.19.4 · R8
  - `commercials/contracts.py`: create, value change audited, `record_subcontract_payment`, decide, resubmit,
    reverse, `position`; job auto-link, `SUBCONTRACT_AMBIGUOUS`, `SUBCONTRACT_OVER_VALUE`, `SUBCONTRACT_MISMATCH`.
  - `SubcontractorSpendReport` gains `paid`, `owed` and a `subcontract` filter.

  *Done when:* the §4.19.16 subcontract worked example, routing and report columns pass against
  hand-computed figures.

- [ ] **T18.9 `[B]` Milestones, receipts and PO payments report** — Refs: §4.19.7 · R11
  - `commercials/milestones.py`: `milestone_state`, invoice and receipt services (`RECEIPT_EXCEEDS_INVOICED`,
    void with reason, `MILESTONE_LOCKED`), default seeding.
  - `po-payments` report registered in `commercials/reports_finance.py`.

  *Done when:* every state with date edges, partial receipts, void, no terms days, and the report totals
  pass.

- [ ] **T18.10 `[B]` PO attached late, site dates and attachment rules** — Refs: §4.19.6, §4.19.8, §4.19.9 · R10, R12
  - `attach_po` service (same row, `PROJECT_ALREADY_HAS_PO`, not OPEN, no manager, taken number, seeds
    milestones, audit); `has_po` filter on `ProjectFilter` and `days_without_po`.
  - Derived collection and dispatch dates in one grouped query using the `engine.project_of` resolution;
    `is_accepted` needs date and certificate.
  - `PROJECT_RULED_TARGETS` and the new attachment targets and captions.

  *Done when:* the PO-later and site cases pass (history intact, one query count) and each attachment rule
  is tested.

- [ ] **T18.11 `[B]` Purchase and subcontract endpoints** — Refs: §4.19.10 · R7, R8
  - `/site-purchases` (PATCH, decide, resubmit, mark-paid, reverse, `mine`, `payable`), `/subcontracts`,
    `/subcontract-payments` in `commercials/views_purchases.py` and `views_contracts.py`.
  - `JobSerializer` gains `subcontract` and `over_contract_reason`.

  *Done when:* each endpoint and permission is tested and isolation holds.

- [ ] **T18.12 `[B]` Project endpoints** — Refs: §4.19.10, §4.19.8, §4.19.6, §4.19.7 · R9–R12
  - `attach-po`, `budget`, `budget-check`, `/project-sites`, milestones CRUD and `defaults`, invoice and
    receipt POST and void, `GET /projects?po=none`, in `commercials/views_milestones.py` and the project
    views.
  - Milestone amounts hidden without `project.view_margin` or `finance.approve`.

  *Done when:* each endpoint and permission is tested, amounts are hidden from a PM without the
  permission, and isolation holds.

- [ ] **T18.13 `[B]` Notifications and milestone sweep** — Refs: §4.19.7, §4.19.12 · R7, R9, R11, R12
  - `Recipient.FINANCE`; `po.milestone_due`, `po.milestone_overdue`, `po.attached`,
    `purchase.yard_delivery_expected`; `_FINANCE_TARGETS` and `_finance_payload` additions.
  - `_sweep_milestones` in `core/sweeps.py` (due once, overdue every 7 days, PM copied in app).

  *Done when:* sweep idempotence and the 7-day repeat are tested and each event reaches the recipients the
  matrix says.

- [ ] **T18.14 `[B]` Offline sync and bundle** — Refs: §4.19.11, §4.19.5 · R6, R7
  - `SyncOperation.SITE_PURCHASE` and `_apply_site_purchase`; over-budget never refuses a replay.
  - Bundle `suppliers` and per-project `budget_headroom` only for users who may see that cost.

  *Done when:* replay, refusal, supersede, photos, flagged over-budget replay, and bundle gating are tested.

- [ ] **T18.15 `[F]` Schema regeneration and money foundation** — Refs: §4.19.13 · R7–R12
  - Regenerate `backend/api-schema.yml` and `frontend/src/api/schema.d.ts` once, after T18.11 to T18.14.
  - Types and hooks for the new endpoints; `rules.ts` `lineTotal`, `purchaseTotal`, `overBudget`, with Vitest.

  *Done when:* the helper tests pass, the schema check passes and the typecheck is clean.

- [ ] **T18.16 `[F]` Record purchase and Purchases list** — Refs: §4.19.13 · R7, R9
  - `RecordPurchasePage.tsx`: site first, supplier picker, destination toggle, lines editor, `receive_into`,
    receipt `PhotoCapture`, over-budget reason asked before sending.
  - Purchases list in My expenses.

  *Done when:* a USED_AT_SITE and an INTO_YARD purchase record against a local backend; typecheck, Vitest
  and build pass.

- [ ] **T18.17 `[F]` Offline capture for purchases** — Refs: §4.19.11, §4.19.13 · R6
  `queued.ts`, `bundle.ts`, `offline.ts`: queue `SITE_PURCHASE` with photos; "Waiting to send" and "Fix and
  resend".

  *Done when:* a Vitest test covers queue and photo steps and a purchase saved offline lands with its
  photos.

- [ ] **T18.18 `[F]` Approvals, To pay and receiving badge** — Refs: §4.19.13 · R7, R8
  - `FinanceApprovals.tsx`: Purchases tab, PM Subcontract payments tab, over-budget banner, "will create a
    delivery".
  - `ToPayPage.tsx` blocks Mark paid with the supplier's status; the receiving draft list badges "from
    purchase SP-…".

  *Done when:* a PM then Finance approve a purchase and a PM approves a payment; typecheck, Vitest and
  build pass.

- [ ] **T18.19 `[F]` Project tabs, Budget and Sites panels** — Refs: §4.19.13, §4.19.5, §4.19.6 · R9, R10
  - A tab shell for `ProjectsPage.tsx` with lazy placeholders for all five panels (so later tasks only add
    their own files); `BudgetPanel` and `SitesPanel` (dates, derived dates, certificate, accepted badge,
    earmarks link).

  *Done when:* the budget and site dates work against a local backend; typecheck, Vitest and build pass.

- [ ] **T18.20 `[F]` Subcontracts panel and JobSheet** — Refs: §4.19.13, §4.19.4 · R8
  `SubcontractsPanel.tsx` (value, work done, paid, owed, payments, contract document); `JobSheet.tsx`
  subcontract picker and over-contract reason.

  *Done when:* a contract, a payment and a linked job work against a local backend; typecheck, Vitest and
  build pass.

- [ ] **T18.21 `[F]` Milestones panel, Attach PO and Dashboard card** — Refs: §4.19.13, §4.19.7, §4.19.8 · R11, R12
  `MilestonesPanel.tsx`, `AttachPoSheet.tsx`, and the "working without a PO" card on `DashboardPage.tsx`.

  *Done when:* a PO can be attached late, milestones seeded, invoices and receipts recorded; typecheck,
  Vitest and build pass.

- [ ] **T18.22 `[E2E]` Phone run** — Refs: §4.19.16 · R7–R12
  *Done when:* the five §4.19.16 scenarios pass and the phone suite stays green. Then push once and check
  on Silvertech.

## Milestones

| Milestone | Completes | Meaning |
|---|---|---|
| **M1 — Foundation ready** | Phase 1 | Tenants exist, users log in, everything auditable. Nothing to show a customer yet. |
| **M2 — Yard configured** | Phase 2 | Silvertech's catalogue, locations, clients and sites are in the system. |
| **M3 — Stock is real** | Phase 3 | Material can be received and looked up. Replaces the GRN book. |
| **M4 — Control in place** | Phase 4 | **Approved, auditable gate-outs. This is the product.** Usable in production. |
| **M5 — Accountable** | Phase 5 | Reconciliation and custody close the loop on what leaves the yard. |
| **M6 — Complete lifecycle** | Phase 6 | Quarantine, disposal and client returns handled. |
| **M7 — Audit-ready** | Phase 7 | Every report an operator or ISO auditor asks for, exportable. |
| **M8 — Field-hardened** | Phase 8 | Works with poor connectivity, non-repudiable approvals. |
| **M9 — Commercially accountable** | Phase 10 | A PO has a manager, a budget and a margin. Answers *did this job make money*, not just *where is the material*. |
| **M11 — Boxed** | Phase 11 | Cartons and pallets are received, found, sent out and checked at the gate by scan, and every unit in them stays traceable on its own. |

**M4 is the point of no return in value terms.** If the schedule compresses, trade scope from
phases 5–8, never from 1–4.

**Milestones M1–M10 are complete.** The yard is fully accounted for, and phase 10 has put the
commercial layer over it: a PO has a manager, a budget and a margin.

Still outstanding beyond phase 10: phase 9's deployment work (§12.1), and the two things only
Silvertech can supply — the Ujumbe SMS account for the SMS channel, and an approved WhatsApp sender
if they want that channel enabled (it ships written and off, per `D30`).

**Phase 10 carries one risk the earlier phases did not.** T10.1 and T10.6 alter structures the
running system depends on: a rename across three apps, and a new column on the ledger. Both are
additive and reversible, but they are the first tasks in this document that touch data already
worth something. Neither should be batched with anything else.

---

## Approval

This is step 3 of 4. Phases 1–8 are complete. On approval, implementation resumes at **T10.1**, one
task at a time, verified against its requirement and ticked before the next starts.
