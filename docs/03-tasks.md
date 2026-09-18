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

- [ ] **T10.15 `[B]` Expenses**
  Refs: §4.14 · O16, D29
  `ExpenseCategory` seeded; `ProjectExpense` recorded by anyone, approved by the PM, append-only
  after approval with correction by reversing entry, unevidenced entries flagged.
  *Done when:* an expense reaches project cost only on approval, and an approved one cannot be
  edited.

- [ ] **T10.16 `[B]` PM level on disposals**
  Refs: §5.4 · O10
  The PM is **prepended** to the disposal's own matched levels rather than replacing them.
  *Done when:* disposing project material needs the PM and the existing approver, in that order.

- [ ] **T10.17 `[B]` The costing engine**
  Refs: §4.14, §10 · O11
  `commercials/costing.py` — the six figures in §4.14's table, one queryset each, nothing stored.
  *Done when:* a full PO lifecycle produces a cost equal to the four lines summed by hand;
  repricing an item type afterwards leaves a closed project unchanged; and an expectation resolved
  late **reduces the loss with nobody editing anything**.

- [ ] **T10.18 `[B]` Financial permissions**
  Refs: §10 · O14
  `project.view_cost`, `project.view_margin`, `project.view_rates`, applied at the serializer.
  *Done when:* storekeeper, PM and owner responses are asserted **field by field**; a withheld
  figure is absent, and the PM's labour total is accompanied by nothing that divides into a rate.

- [ ] **T10.19 `[B]` Project performance reports**
  Refs: §10 · O12
  Four report classes: project performance, projects ranked, self-approved releases, uncosted and
  overlapping labour.
  *Done when:* each runs through the existing report machinery and exports to Excel and PDF.

- [ ] **T10.20 `[B]` Project close and snapshot**
  Refs: §4.14 · O13
  Warn on open jobs and unreconciled material, reason required, `ProjectSnapshot` written, a closed
  project refuses gate-outs and variations, owner reopen recorded.
  *Done when:* a reversal posted after close does not move the closed project's reported figures.

- [ ] **T10.21 `[F]` Project screens**
  Refs: §7.4 · O1, O2, O12, O13
  List, detail with cost against budget, variations, close.
  *Done when:* a PM sees cost and budget, an owner also sees value and margin, and a storekeeper
  cannot reach the screens at all.

- [ ] **T10.22 `[F]` PM approval screens**
  Refs: §7.4 · O6, O8, O16
  Queues for material, closeouts and expenses. The material approval shows the budget position and
  what this release adds; a project over budget is stated and **still approvable**.
  *Done when:* an over-budget release can be approved after the overrun is shown, and a gate-out on
  a project with an inactive PM says so in those words and names reassignment as the remedy.

- [ ] **T10.23 `[F]` Days and expense capture**
  Refs: §7.4 · O15, O16
  Days per person on the closeout, with the over-a-day warning inline. Expense capture with a
  receipt photo, reusing the attachment control.
  *Done when:* a technician records days and an expense on a phone without leaving the job.

- [ ] **T10.24 `[T]` End-to-end project lifecycle**
  Refs: §14
  Playwright on a mobile viewport: create a PO project, subcontract one job, issue material through
  PM approval, close out with days, record and approve an expense, close the project, read the
  performance report.
  *Done when:* it runs in CI against a seeded tenant.

---

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

**M4 is the point of no return in value terms.** If the schedule compresses, trade scope from
phases 5–8, never from 1–4.

**Milestones M1–M8 are complete.** The yard is fully accounted for; what phase 10 adds is the
commercial layer over it.

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
