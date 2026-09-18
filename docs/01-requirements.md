# Yard Inventory & Gate Control System — Requirements

**Working name:** YardFlow (placeholder — rename freely)
**First customer:** Silvertech, a telecommunications installation subcontractor
**Status:** Draft v1 — awaiting approval
**Date:** 2026-08-17

---

## 1. Purpose

Silvertech installs and maintains telecommunications equipment as a subcontractor to network
operators and vendors (Airtel, Safaricom, GOtv, Huawei). They operate a yard holding three
distinct kinds of material:

- **Client-owned (consignment) stock** — issued by an operator for installation, and fully
  auditable back to that operator
- **Own stock** — purchased by Silvertech for use across jobs
- **Tools and returnable equipment** — issued to technicians and expected back

Today the process is entirely manual: hard-copy delivery notes and a GRN book. The operators
audit their consignment stock, and Silvertech maintains an internal ISO quality standard. Both
require that stock movements be recorded, traceable, and producible on request.

This system replaces the paper process with a controlled, auditable, mobile-first record of
everything entering and leaving the yard, with approval gates on outbound movements.

It is built **multi-tenant from day one** so it can be sold to other contractors in the same
industry with minimal effort.

---

## 2. Glossary

| Term | Meaning |
|---|---|
| **Tenant / Organization** | One customer company. Silvertech is tenant #1. |
| **Yard** | A physical stock-holding site belonging to a tenant. |
| **Location** | A stock-holding place: a yard, a store within a yard, or a vehicle. |
| **Gate-in (GRN)** | Material entering a location. |
| **Gate-out (Gate Pass)** | Material leaving a location, subject to approval. |
| **Client** | An operator or vendor Silvertech works for (Safaricom, Huawei…). |
| **Site** | A physical telecom site where work is performed. |
| **Work order** | The original name for the grouping of work across sites. Epic O turns this layer into the **project**. |
| **Project** | A grouping of jobs across sites, usually one purchase order, with a value, a budget and a manager. |
| **PO** | A purchase order from a client. One PO is one project. |
| **Project manager (PM)** | The named person accountable for one project's delivery and its budget. |
| **Variation** | An amendment to a project's contract value or budget. Added, never edited into the original. |
| **Subcontractor** | A third party delivering jobs on a project, paid an agreed price per job. |
| **Delivery mode** | Whether a job is delivered in-house or by a subcontractor. |
| **Exposure** | Material issued to a project and not yet accounted for. Not a cost until the closeout says what happened to it. |
| **Day rate** | A costing rate for a person's time, held per user and falling back to their role. Not pay. |
| **Item type** | A catalogue entry (e.g. "RRU 2x40W", "LDF4 feeder cable"). |
| **Tracking mode** | How a given item type is counted: serialized, bulk, or reel. |
| **Stock lot** | A quantity of an item type at a location, with a known owner. |
| **Custody** | Material currently held by a person rather than a location. |
| **Recovery** | Equipment retrieved from a decommissioned or demolished site. |
| **Reversal** | A correcting entry that negates a posted movement, never an edit. |

---

## 3. Actors

| Actor | Description |
|---|---|
| **Platform admin** | Us. Creates tenants, manages plans and limits. Cross-tenant. |
| **Owner** | Tenant principal. Final approver, sees everything. |
| **Admin** | Configures the tenant: users, roles, catalogue, settings. Always present. |
| **Storekeeper** | Runs the yard. Also acts as gate guard. Raises gate-outs, receives gate-ins, verifies loads at the gate. |
| **Approver** | Any role granted approval rights. Not a fixed person. |
| **Technician** | Field staff. Requests material, receives it, closes out jobs, returns material. Often also the driver. |
| **Project manager** | Accountable for one project's delivery and budget. Sole approver of material leaving for it; confirms its closeouts, approves its expenses, sets subcontractor prices, closes it. Sees its cost, not its margin. |

**Roles are data, not code.** The seven above are seeded defaults. A tenant may create, rename or
delete roles and assign granular permissions to them.

---

## 4. Decisions already made

These were settled during discovery and are not open for re-litigation in design.

| # | Decision |
|---|---|
| D1 | Standalone project. Django REST backend, React frontend. Hosted on AWS at reasonable cost. |
| D2 | Multi-tenant, **shared schema with tenant foreign key**, enforced at the query layer plus Postgres row-level security. |
| D3 | Tracks **both client-owned and own stock**, separately and unambiguously. |
| D4 | **Role-based approvals** with escalation driven by **item category criticality**. Configurable. |
| D5 | **Mobile-first responsive web**, with desktop views for the storekeeper and reports. |
| D6 | Technicians get **full mobile accounts**. |
| D7 | **No self-serve signup or billing in v1.** Tenants are onboarded manually via a platform admin console. |
| D8 | Login by **email or phone plus password**, JWT sessions. |
| D9 | **WebAuthn/fingerprint step-up on the approval action only** in v1. |
| D10 | Item catalogue is **user-definable**, seeded with a starter telecom list. |
| D11 | Tracking mode **defaults per item type, overridable per receipt**. |
| D12 | **Reel/drum tracking required** for cable. |
| D13 | Site register carries **multiple references per site**; no hardcoded site-code format. |
| D14 | **Optional** work order layer above sites. |
| D15 | **No client portal**, now or planned. Client ownership is tracked; external access is out of scope. |
| D16 | **Money tracking off by default**, switchable per tenant. Quantities are the primary currency. |
| D17 | **Offline capture limited to gate-in and gate-out**, syncing when connectivity returns. |
| D18 | No data migration. Starting clean. |
| D19 | No third-party integrations in v1, but the design must not preclude them. |
| D20 | **The work-order layer becomes the project layer** (amends D14). One grouping, not two: a project without a PO number is the old optional work order. |
| D21 | **One PO is one project.** Scope changes are variations that amend it; the original award is never edited. |
| D22 | **Project material routes to the project manager as the only approval level** (amends D4 for that material). No delegation, no escalation — it waits. Criticality routing continues to govern everything else. |
| D23 | **Project cost is derived from the ledger, never typed.** Material costs when installed, consumed or unaccounted for; material issued and still outstanding is exposure, not cost. |
| D24 | **Every figure is VAT-exclusive.** No invoices or payables in v1. Project cost is material, subcontractor price, own labour and recorded direct expenses. |
| D25 | **Money tracking (D16) is required once a project carries a PO.** A tenant that does not use priced projects may still leave it off. |
| D26 | **Own labour is costed**, so in-house and subcontracted work can be compared. Days per person are entered on the closeout that already exists; the rate is per person, falling back to the role's. |
| D27 | **Rates and prices are captured onto the record when it posts** — unit cost onto the movement, day rate onto the labour entry. Repricing anything never rewrites a closed project. |
| D28 | **A project whose PM is inactive is unblocked only by reassigning the PM.** There is no fallback approver anywhere in the project approval path. |
| D29 | **Direct expenses are recorded by whoever incurs them and approved by the PM** before they reach project cost. They are the only cost line with no ledger movement and no contract behind it — just a receipt. |
| D30 | **Notifications ship on SMS and email.** WhatsApp sits behind the existing channel adapter and is switched on once the Business sender is approved — nothing waits on Meta. |
| D31 | **The technician submits the closeout; the storekeeper may submit on their behalf.** One endpoint, with `submitted_by` and `on_behalf_of` recording which, so who really does it is answerable from the data after a month of use. |
| D32 | **An approved gate pass expires if not released.** Configurable per tenant, 24 hours by default. An approval is a decision about a particular load on a particular day. |
| D33 | **One approver satisfies a level.** Seniority is expressed by adding levels, never by requiring two people at one level. |
| D34 | **Cable is reported as metres consumed**, with the remainder returning on the same drum at a new remaining length. |
| D35 | **Overdue custody escalates technician → storekeeper → owner.** No supervisor role is introduced. |
| D36 | **The storekeeper is also the gate guard at Silvertech**, but release remains a separate permission so another tenant can put a dedicated guard on it. |

---

## 5. User stories

### Epic A — Tenancy and platform administration

**A1.** As a platform admin, I want to create a tenant with a name, subdomain and initial owner
account, so that a new customer can start using the system.
- Creating a tenant seeds default roles, a starter item catalogue, default settings and one yard.
- The owner receives an invitation to set their password.
- Subdomains are unique across the platform and immutable after creation.

**A2.** As a platform admin, I want to view and suspend tenants, so that I can manage
non-paying or dormant customers.
- A suspended tenant's users can log in but cannot post any movement; they see a notice.
- Suspension never deletes data.

**A3.** As any user, I want all my data scoped to my tenant automatically, so that no customer
can ever see another's stock.
- Every tenant-owned table carries an organization reference.
- Queries are scoped by default; unscoped access must be explicit and is confined to platform admin code paths.
- Row-level security is enabled as a second barrier.
- **Acceptance is a test suite** asserting that every API endpoint returns 404 (not 403) for another tenant's object IDs.

**A4.** As an admin, I want to upload my company logo and set my company details, so that
printed gate passes and GRNs carry my branding.

- **The tenant does this themselves**, from their own Settings. The first build put the logo field
  on the model and rendered it on documents, but provided nowhere to upload it — so a customer's
  branding depended on the platform owner opening the Django admin for them.
- **Their documents, their mark.** The logo appears in the header of the gate pass, GRN, disposal
  certificate and return waybill, where it is capped at **18 mm tall by 45 mm wide** — a document
  belongs to the company whose material is moving.
- **The product keeps its own.** YardFlow's wordmark stays in the app shell and in the footer line
  of every document ("Produced by …"). A tenant brands what is theirs, not the tool.
- **Constraints are stated before the upload, not after**: PNG, JPEG or SVG, at most 3 MB, and at
  least 300 px on the long edge so it does not print blurred. A wide logo of about 900 × 360 px
  suits the space; the screen shows exactly how it will print.

---

### Epic B — Identity, roles and permissions

**B1.** As a user, I want to log in with either my email address or my phone number plus a
password, so that field staff without email can still use the system.
- Both identifiers are unique within a tenant.
- Sessions use JWT with refresh; refresh tokens are revocable.
- A phone number is stored in **one canonical shape** (`+254722123456`). `0722 123 456`,
  `254722123456` and `+254722123456` are the same phone; storing them as different strings meant the
  per-tenant uniqueness check missed duplicates and somebody invited under one form could not sign in
  with the other.

**B2.** As a user, I want to reset my password by SMS or email, so that I can recover access.

**B3.** As an admin, I want to create users, assign them one or more roles, and deactivate them,
so that access matches who actually works here.
- Deactivating a user preserves all their historical records.
- A user cannot be deleted if they have posted movements — only deactivated.
- **Edge case:** deactivating a user holding items in custody must warn and require the custody to be reassigned or returned first.

**B4.** As an admin, I want to define roles and tick the permissions each role holds, so that the
system fits how my company actually divides duties.
- Permissions are granular (e.g. `gate_out.request`, `gate_out.approve`, `gate_out.release`, `catalogue.manage`, `report.view_all`).
- At least one active user must hold owner-level permissions at all times; the system prevents removing the last one.

**B5.** As an approver, I want to register my fingerprint on my phone, so that I can authorise
gate-outs without typing a password.
- WebAuthn platform authenticator, enrolled per device, multiple devices allowed.
- Password remains a working fallback on devices without a sensor.
- **Edge case:** losing a device must not lock the user out; an admin can revoke enrolled credentials.

**B6.** As an admin, I want every login, failed login and permission change recorded, so that I
can answer an auditor's questions about access.

---

### Epic C — Master data

**C1.** As an admin, I want to define item categories with a criticality flag, so that the system
knows which material needs tighter approval.
- Categories are hierarchical (at least two levels).
- Criticality drives approval routing (see Epic F).

**C2.** As an admin, I want to define custom fields per category, so that I can capture the
attributes that matter for that kind of equipment.
- Field types: text, number, date, dropdown, boolean.
- Fields may be marked required at gate-in.

**C3.** As an admin, I want a catalogue of item types, each with a default tracking mode, unit of
measure and category, so that stock is recorded consistently.
- Tracking modes: **serialized** (each unit individually identified), **bulk** (counted quantity), **reel** (a measured length on a numbered drum).
- The catalogue ships seeded with common telecom items (antennas, RRUs, BBUs, feeder cable, jumpers, connectors, batteries, rectifiers, tools, PPE) which the tenant may rename or delete.
- Item types cannot be deleted once movements exist — only archived.

**C4.** As an admin, I want to define locations — yards, stores and vehicles — so that stock is
held somewhere specific.
- Locations form a tree (yard → store).
- Vehicles are locations, so material in transit remains visible.
- A location holding stock cannot be deleted, only deactivated.

**C5.** As an admin, I want to register clients, so that consignment stock is attributable.

**C6.** As an admin, I want a site register where each site can carry **several references**, so
that the same physical site can be matched to whatever code appears on a work order.
- A site has: internal reference, name, client, region, GPS coordinates, site type, status.
- A site carries zero or more **external references**, each with a label (e.g. "Safaricom site ID", "Towerco ref") and a value.
- Optional radio identifiers (Cell ID, eNodeB/gNodeB ID) may be captured as free fields, never validated.
- An admin may optionally set a **validation pattern per client**, so that site codes for that client are checked on entry.
- Decommissioned sites remain in the register permanently, because recoveries originate from them.
- **Rationale:** operator site-code formats are internal and unpublished, and towercos use their own. Any hardcoded format would be wrong for the second customer.

**C7.** As a storekeeper, I want to create work orders grouping one or more sites under a client,
so that consumption can be reported per rollout.
- Work orders are **optional**. Material may be issued directly to a site.
- A work order has a status (open / closed) and closing it warns if material remains unreconciled.

**C8.** As an admin, I want to configure master settings for my tenant, so that the system
matches how we work.
- Money tracking: **off by default**. When on, unit costs and stock valuation appear.
- Minimum stock levels and reorder alerts: **off by default**, enabled per item when on.
- QR label generation and printing: optional.
- Internal asset tag auto-generation: optional, with a configurable prefix format.
- Client return documentation (waybills): optional.
- Photo and document attachments: optional, and separately markable as required at gate-in or gate-out.
- Record retention period and whether admins may amend posted documents (see M4).
- Notification channels and event matrix (see Epic L).

---

### Epic D — Gate-in (receiving)

**D1.** As a storekeeper, I want to record material arriving at the yard against a source type, so
that its origin and ownership are unambiguous.
- Source types: **purchase** (own stock), **client issue** (consignment), **recovery** (from a decommissioned or demolished site), **return from site** (unused material coming back), **warranty/faulty return**, **transfer** (from another location).
- Ownership (own vs a named client) is set at gate-in and carried on the stock permanently.
- Every gate-in produces a numbered GRN.

**D2.** As a storekeeper, I want to add lines to a gate-in choosing item type, quantity and
condition, so that mixed deliveries are recorded in one document.
- Condition per line: new, used-serviceable, faulty, damaged, scrap.
- Faulty, damaged and scrap lines land in a quarantine location, not free stock (see Epic J).

**D3.** As a storekeeper, I want the tracking mode to default from the item type but be
overridable on the line, so that a batch of recovered units with unreadable serials can still be
received.
- Serialized lines require one identifier per unit.
- If a unit has no readable manufacturer serial and asset tags are enabled, the system generates an internal asset tag.
- If asset tags are disabled and no serial is available, the line must be received as bulk, and the system records why.
- **Edge case:** duplicate serial within the tenant must be rejected with a clear message identifying where the existing one sits.

**D4.** As a storekeeper, I want to receive cable onto a numbered drum with a starting length, so
that I can track how much remains on each drum.
- A reel line creates a drum record with drum number, item type, initial length and remaining length.
- Drum numbers are unique within the tenant.

**D5.** As a storekeeper, I want to record a recovery against the site it came from, so that the
operator can be shown what was retrieved.
- Recovery lines require a source site.
- Recovered client-owned equipment retains that client's ownership.

**D6.** As a storekeeper, I want to attach photos and the supplier or client delivery note to a
gate-in, so that the paper trail is preserved.
- Optional by default; an admin may make attachments mandatory.
- Where they are mandatory, the screen that refuses to post is the screen that offers to attach
  one, and the refusal names what it wants. A rule with nowhere to satisfy it is a dead end.

**D7.** As a storekeeper, I want to scan an existing barcode where one is present, so that
receiving is fast.
- Camera-based scanning on a phone. Manual entry always available.
- **Edge case:** many recoveries arrive with no barcode at all; the flow must not assume one exists.
- Scanning reads QR and Data Matrix as well as the 1D formats, since some suppliers label units with
  a QR code.
- For a delivery of identified units the camera **stays open** and the count climbs as each is
  taken. Closing it after every read meant a sealed box of twenty was twenty separate openings, and
  most of a gate-in was spent on the phone rather than on the delivery. A repeat of the same label
  under the lens is read once; the same unit scanned twice from the box is called out, because a
  count that is silently short is worse than one that is questioned.

**D8.** As a storekeeper, I want to save a gate-in as a draft and post it when complete, so that a
large delivery can be entered over time.
- Only posting affects stock. Drafts are freely editable; posted documents are not (see M4).
- A draft may be **discarded**. It holds no number and has moved no stock, so nothing is lost and
  nothing is left with a gap. A posted document is voided instead, never deleted (M6).
- Correcting a draft uses the capture screen itself, so the correction is the same work as the
  entry.

---

### Epic E — Stock

**E1.** As any authorised user, I want to see current stock on hand by item, location, owner and
condition, so that I can answer "do we have it?".
- Filterable and searchable, including by serial number.
- Client-owned stock is visually distinct from own stock everywhere it appears, and **names the
  client**: "client owned" on its own raises the question it is meant to answer.
- The same item at the same location is counted separately per owner and condition. Anything that
  takes stock out must therefore **say which lot it is taking**, and must offer that choice where
  the line is entered — not discover the mismatch at approval time. A refusal for want of stock
  names what is actually on the shelf and whose it is.

**E2.** As any authorised user, I want to look up a serial number and see its entire history, so
that I can trace one unit end to end.
- Shows every movement: received, issued, installed, recovered, returned, quarantined, disposed.
- This is the single most likely question from an operator audit.

**E3.** As a storekeeper, I want to see remaining length per drum, so that I can pick the right
drum for a job.
- Issuing from a drum decrements its remaining length.
- **Edge case:** a drum reaching zero is closed automatically; issuing more than remains is rejected.

**E4.** As a storekeeper, I want to transfer stock between locations, so that material moving to
a vehicle or another store stays visible.
- A transfer to a location outside the yard perimeter is a gate-out and follows Epic F.

**E5.** As a storekeeper, I want to perform a stock count and record variances, so that the system
matches physical reality.
- A count is a document listing expected versus counted quantity per item.
- Posting a count creates adjustment movements with a mandatory reason.
- Adjustments to client-owned stock always require approval, regardless of category.

**E6.** As an admin with minimum stock enabled, I want alerts when an item falls below its
reorder level, so that we do not run out mid-job.

---

### Epic F — Gate-out request and approval

**F1.** As a storekeeper or technician, I want to raise a gate-out request specifying destination,
purpose and lines, so that material can leave the yard under control.
- Destination is a site, a work order, a client, a supplier, or another location.
- Purpose types: installation, maintenance, return to client, transfer, disposal, tool issue.
- Each line: item type, quantity or serials or drum-and-length, and expected return where applicable.
- A request must name the **person taking custody** (usually a technician).

**F2.** As a technician, I want to request material for a job from my phone, so that the
storekeeper can prepare it before I arrive.
- Technician requests enter the same approval flow.

**F3.** As the system, I want to route a gate-out to the correct approver based on configurable
rules, so that control matches risk.
- Rules are a table: **item category criticality → required approver role**.
- The rule engine is written so further dimensions (client-owned, quantity threshold, monetary value, destination) can be enabled later **without schema change** — but only category criticality is active in v1.
- A gate-out containing lines from several categories takes the **highest** applicable approval level.
- Requests may auto-approve where the rules say no approval is required.
- **Edge case:** a requester who also holds approval rights must not approve their own request unless a setting explicitly permits it. Default: not permitted.
- **These rules do not apply to project material.** A gate-out attributed to a project routes to that project's manager instead, as the only level — see O6. Criticality routing governs everything else.
- Rules are written by the tenant from **Settings → Approvals**, not by the platform owner. A new
  organization is seeded with one — high-criticality material needs the owner's approval — so
  approval arrives switched on and visible rather than silently empty.

**F4.** As an approver, I want to be notified and approve or reject from my phone, so that jobs
are not delayed.
- Notification via WhatsApp, SMS, email or in-app, per the tenant's configured channels.
- The notification carries a deep link; **the user must still authenticate** — fingerprint where enrolled, password otherwise.
- Rejection requires a reason.
- Approval records who, when, from what device, and by what authentication method.

**F5.** As an admin, I want to configure delegation, so that approvals continue when the owner is
away.
- A delegate is named for a period, with the delegated permissions.
- Delegated approvals are recorded as "X on behalf of Y", never as Y.
- Optional escalation timeout: unanswered after N hours, escalate to a named fallback.
- A delegation must lend **something** — a role or named permissions. One that lends neither is
  refused by the database, not only the form: on screen it reads exactly like cover being in place,
  so somebody goes on leave believing approvals will continue when they will not.

**F6.** As a requester, I want to amend a rejected or draft request and resubmit it, so that a
correctable mistake does not need a fresh document.
- Amending an approved gate-out **voids the approval and re-triggers routing**.
- Re-triggering routing asks the same question submission asks: is there anybody to route to? Where
  no rule applies the pass auto-approves again, recorded as such. Leaving it awaiting an approval
  nobody can give strands it with no way forward — which happened to a real pass.
- A request may be amended while it is already awaiting approval; the requester correcting a line
  while it sits in somebody's queue is ordinary.
- The version history is retained and visible.

**F7.** As a storekeeper, I want to release a gate-out partially, so that a job can start with what
is available.
- Released quantities are recorded per line; the balance stays outstanding.
- A partially released gate-out remains open until fully released, cancelled, or closed with a reason.

**F8.** As a requester or approver, I want to cancel a gate-out before release, so that abandoned
requests do not linger.
- Cancellation requires a reason and is not possible after any release.

---

### Epic G — Gate release

**G1.** As the storekeeper acting at the gate, I want to verify the physical load against the
approved gate pass and confirm release, so that only approved material leaves.
- Release is a distinct action from approval, with its own permission.
- The system will not permit release of an unapproved or expired gate pass.
- **Edge case:** if the physical load differs from the approved list, the storekeeper records the actual quantity, and the difference is flagged as a release variance requiring an approver's acknowledgement.

**G2.** As the storekeeper, I want to record the vehicle registration and the driver on release,
so that the load is attributable.

**G3.** As the storekeeper, I want to capture the recipient's signature or a photo of the loaded
vehicle at release, so that there is evidence of handover.
- Optional by default; an admin may make it mandatory.

**G4.** As a storekeeper, I want to print or share a gate pass PDF, so that the driver carries a
document as they do today.
- Gate passes carry a sequential, gap-free number per tenant and a QR code resolving to the record.

**G5.** As a storekeeper, I want to scan a gate pass QR code at the gate, so that I can pull up
the right document quickly.

---

### Epic H — Job execution and reconciliation

**H1.** As an admin or storekeeper, I want to assign a site or job to a named person who is
responsible for closing it out, so that accountability is explicit.
- A job has an assignee, a client, a site, an optional work order and a status.

**H2.** As the assigned technician, I want to close out a job by reporting what was installed,
what is being returned and what was consumed, so that the yard knows what to expect back.
- Installed serialized units are recorded against the site and leave stock permanently.
- Consumed bulk quantities are recorded as consumed.
- Returning quantities are declared, creating an expected return.
- Recovered equipment taken from the site is declared, creating an expected gate-in.
- Close-out can be done from a phone, including attaching site photos.

**H3.** As a storekeeper, I want to confirm returns on gate-in against the technician's
declaration, so that discrepancies surface immediately.
- A difference between declared and actual creates a **variance** requiring investigation and an approver's sign-off.
- Variances appear on an exceptions report until resolved.

**H4.** As an owner, I want to see per-site and per-work-order reconciliation — issued versus
installed versus returned versus unaccounted — so that I can answer the operator.

**H5.** As an owner, I want a job to be blocked from closing while material remains unaccounted
for, unless someone with authority overrides it with a reason.

---

### Epic I — Custody and returns

**I1.** As a storekeeper, I want a live view of what each technician currently holds, so that I
know where my tools and material are.
- Custody is created on gate-out release to a named person, and cleared on return or on
  confirmed installation/consumption.

**I2.** As a storekeeper, I want expected return dates on tools and returnable material, so that
overdue items can be chased.
- Default return period configurable per item type.

**I3.** As a storekeeper, I want to be alerted when an item is overdue, and for the holder to be
reminded, so that tools stop disappearing.
- Escalating reminders: to the holder, then to their supervisor, then to the owner.

**I4.** As an owner, I want an overdue report by person and by item, so that I can act on
persistent offenders.

**I5.** As a storekeeper, I want to transfer custody from one person to another, so that a
handover in the field is recorded.
- Requires acknowledgement by the receiving person.

---

### Epic J — Damaged, faulty and scrap

**J1.** As a storekeeper, I want to place material in quarantine, so that unusable stock is not
issued by mistake.
- Quarantine is a system location. Quarantined stock never appears as available.
- Reason and condition are mandatory.

**J2.** As a storekeeper, I want to move quarantined material to repair, back to serviceable, to
the client, or to scrap, so that it does not sit there forever.
- Each outcome is an explicit, approved decision with a recorded reason.

**J3.** As an owner, I want disposal of scrap to require approval and produce a disposal record,
so that write-offs are controlled and auditable.
- Disposal of client-owned material always requires approval regardless of category criticality.

---

### Epic K — Returns to client

**K1.** As a storekeeper, I want to raise a return-to-client gate-out for consignment material or
recovered equipment, so that the operator gets their property back.
- Lines carry the client's ownership and, for recoveries, the originating site.

**K2.** As a storekeeper, I want to produce a return waybill or delivery note PDF, so that the
client's receiving store signs for it.
- Optional feature per tenant setting.

**K3.** As a storekeeper, I want to record the client's acknowledgement — signed document upload
or reference number — so that our liability for that material ends on the record.
- Until acknowledged, returned material shows as "in transit to client" and remains our exposure.

---

### Epic L — Notifications

**L1.** As an admin, I want to choose which channels are active — WhatsApp, SMS, email, in-app —
so that we use what our staff actually read.

**L2.** As an admin, I want an event matrix mapping each event to channels and recipient roles,
so that people are not spammed.

Default matrix, editable per tenant:

| Event | Recipients | Default channels |
|---|---|---|
| Gate-out awaiting approval | Approvers for that level | WhatsApp + in-app |
| Gate-out approved | Requester, storekeeper | In-app |
| Gate-out rejected | Requester | WhatsApp + in-app |
| Gate-out released | Requester, custody holder | In-app |
| Approval escalated on timeout | Fallback approver, owner | WhatsApp + SMS |
| Item overdue for return | Holder, then supervisor, then owner | SMS + in-app |
| Return variance raised | Storekeeper, owner | In-app |
| Stock below minimum | Storekeeper | In-app |
| Job closed with unaccounted material | Owner | WhatsApp + in-app |
| Client return unacknowledged after N days | Storekeeper, owner | In-app |

Acceptance criteria, added after the first build shipped the matrix read-only:

- **Editable by whoever holds `settings.manage`** — in practice the Owner and Admin roles. The
  original implementation locked the matrix on the grounds that "who is told what is a control,
  not a preference". That conflated two separate questions. *Who may change it* is what the
  permission system answers, and `settings.manage` is already described as close to owner-level;
  *whether the change is visible afterwards* is what the audit trail answers. Neither requires
  taking the decision away from the person accountable for it.
- **Every change is audited** — who turned what off, and when. A variance nobody heard about
  should have a traceable reason.
- **Events that carry a control are warned about, not blocked.** Turning off "gate-out awaiting
  approval" means approvers are not told; the request still appears on their Approvals screen.
  The interface says so at the point of change. Nothing in this system is *only* a notification —
  the work always remains visible in the app — which is what makes an informed choice safe.
- **A channel switched off tenant-wide beats the matrix**, so a company with no SMS budget cannot
  have SMS reintroduced by a per-event setting.

**L3.** As the system, I want notification delivery to be queued and retried, so that a failed SMS
does not stall an approval.
- Delivery status is visible to admins.
- **Failure of a notification never blocks the underlying transaction.**

**L4.** As an owner, I want SMS to run on credits I buy, so that the cost of messaging is mine to
control and to see.
- A newly provisioned tenant starts with an opening balance, so its own first invitation can go by
  SMS. Without it a technician who has a phone and no email cannot be invited at all.

- **One SMS costs one credit, and a credit costs KES 1.** Priced per message sent to one person,
  which is the unit an owner can reason about.
- Credits are bought from the platform and added to the tenant's balance. Every purchase and every
  consumption is a **ledger entry**, never an edited number — the same rule the stock ledger
  follows, for the same reason: a balance somebody can type is a balance nobody can defend.
- **A message is charged when the provider accepts it**, not when it is attempted. A failed send
  costs nothing.
- **At zero, SMS stops and says so.** The message is recorded as failed with a reason an
  administrator can read, and is not retried — retrying a message there is no credit for wastes
  attempts and hides the cause. Everything else continues: in-app and email are unaffected, and
  `L3` still holds — no notification failure blocks a business transaction.
- The balance is visible where SMS is switched on, with a warning while it is low, because the
  failure this prevents is silent: messages that simply stop arriving.

---

### Epic M — Reporting, audit and compliance

**M1.** As an owner, I want the day-one report set, so that I can satisfy an operator audit and
our internal ISO requirement:
- Stock on hand — current, and **as at any past date**
- Movement history by item, by serial, by drum
- Outstanding gate-outs
- Overdue returns and current custody
- Consumption per site and per work order
- Client-owned stock position, per client
- Variance and exceptions register
- Recoveries by originating site
- Disposals and write-offs

**M2.** As an owner, I want every report exportable to Excel and PDF, so that I can send it to a
client on request.

**M3.** As an auditor-facing user, I want an immutable audit trail on every record, so that I can
show who did what and when.
- Records the actor, timestamp, action, before/after values, and for approvals the authentication
  method used.
- The audit trail is append-only and cannot be edited or deleted by any tenant user.

**M4.** As an admin, I want to configure whether posted documents may be amended, so that the
control level matches our standard.
- **Default: posted documents are immutable.** Corrections are made by reversal and re-entry.
- If a tenant enables amendment, it requires elevated permission, a mandatory reason, and the
  original version is retained and visible.
- The audit trail is immutable regardless of this setting.

**M5.** As an admin, I want a configurable retention period, so that we keep records as long as
our clients require.
- Retention never silently deletes; expiry flags records for review.

**M6.** As an owner, I want document numbers to be sequential and gap-free per tenant per
document type, so that an auditor can see nothing was removed.
- A voided document keeps its number and is marked void; numbers are never reused.

---

### Epic N — Offline capture

**N1.** As a storekeeper at the gate, I want gate-in and gate-out capture to work without a
connection, so that a network drop does not stop the yard.
- Scope is deliberately limited to these two flows. All other screens require connectivity.
- Offline-captured documents are held locally and marked pending sync.

**N2.** As a storekeeper, I want offline work to sync automatically when connectivity returns, so
that I do not have to remember.
- Sync is idempotent; a retried submission must not double-post.
- The same protection applies **online**: a second press of a save button, or a retry of a request
  whose reply was lost, must not create a second document. Every captured document carries an
  identity decided by the client and is matched on it by the server. Disabling a button while a
  request is in flight is not sufficient — the second press can leave before the first reply
  arrives.

**N3.** As the system, I want to handle offline conflicts safely, so that stock never goes wrong.
- **Release of an unapproved gate-out is never possible offline** — approval requires connectivity.
  Only approved gate passes already downloaded to the device can be released offline.
- If stock has changed such that an offline document is no longer valid, it syncs into an
  exception queue for the storekeeper to resolve rather than silently failing or force-posting.

---

### Epic O — Projects, PO performance and PM control

Epic O introduces the commercial layer. Everything before it answers *where is the material*; this
answers *did the work make money*. The two must never disagree, so every figure here is derived
from the same append-only ledger — nothing in this epic is a number a person can type over.

**The work-order layer becomes the project layer.** `WorkOrder` was always the optional grouping of
jobs across sites; a project is that same layer with a PO, a value and a manager attached. It stays
optional in the sense that matters — a project without a PO number is exactly the old work order,
and material may still go straight to a site. There is no second grouping.

**O1.** As an owner or admin, I want to open a project from a purchase order, so that a PO has one
place where its scope, its budget and its performance live.
- Fields: client, **PO number**, title, description, **contract value**, **cost budget**, **project
  manager**, sites, start and target dates, status.
- **One PO is one project.** The PO number is unique per tenant. Scope that grows is recorded as a
  variation (O2), never as a second project against the same PO.
- **Contract value** is what the client pays. **Cost budget** is what the PM may spend to deliver
  it. They are separate figures because they are visible to different people (O14).
- All monetary values are **VAT-exclusive**, and every field that takes money says so on screen. A
  VAT-inclusive figure keyed in by mistake overstates a project by 16% and nothing downstream would
  catch it.
- A project with no PO number is permitted — it is the legacy work-order case — and then contract
  value, budget and PM are optional. A project **with** a PO number requires all three.
- Status: open, closed, cancelled. A closed or cancelled project accepts no new gate-outs.

**O2.** As an owner, I want scope changes recorded as variations, so that the original award and
what it became are both visible.
- A variation carries: reference, description, change to contract value, change to cost budget,
  date, who raised it, who approved it.
- Current contract value is the original plus approved variations. **The original is never edited**,
  for the same reason the ledger is never edited — in a dispute, what was first agreed is the
  question being asked.
- Variations are approved by the owner, not the PM. The PM spends the budget; they do not set it.
- A variation may be negative — descopes happen.

**O3.** As a PM, I want each job on my project marked as in-house or subcontracted, so that its cost
is known.
- A job belongs to **at most one** project.
- Every job on a project carries a **delivery mode**: `IN_HOUSE` or `SUBCONTRACTED`.
- A subcontracted job names the **subcontractor** and the **agreed price** for that job.
- The agreed price is set by the PM (O9); changing it after it is set records the old value, the new
  value, who changed it and when.
- Delivery mode may change while the job is open. It may not change once the job is closed — that
  would rewrite a cost already counted.
- **A PO may be mixed**: some jobs in-house, some subcontracted, some split across several
  contractors. The project carries no delivery mode of its own.

**O4.** As an admin, I want a register of subcontractors, so that cost rolls up by contractor rather
than by whatever someone typed.
- Fields: name, code, contact name, phone, email, active flag. Tenant-scoped like every other master
  record.
- Unique name per tenant. A subcontractor referenced by any job may be deactivated, never deleted.
- This is a register of **contractors who do work**, not of suppliers who sell goods. Suppliers stay
  as free text on gate-in (still out of scope, section 7).

**O5.** As a storekeeper, I want a gate-out to name the job it is for, so that material can be
attributed to the right project.
- A gate-out may name a **job**. When it does, the project follows from that job and the pass is
  **project material**.
- Destination rules are unchanged — exactly one destination, as the database already enforces. The
  job is an attribution, not a destination.
- Where the destination is a site, the named job must be a job **at that site**.
- The named job must be open, and its project open.
- A gate-out with no job is not project material and behaves exactly as it does today.
- **Edge case:** one gate pass cannot serve two projects; the storekeeper raises two passes. Same
  rule, same reason, as the existing refusal of two destinations — material that cannot be
  attributed to one project cannot be reconciled against one either.

**O6.** As a PM, I want to be the approver on material leaving for my project, so that nothing is
spent against my budget without me.
- When a gate-out is project material, the **required approver is that project's PM**, and the
  criticality rules in F3 **do not apply to it**. The PM is the only level.
- Non-project gate-outs are untouched: F3's criticality routing continues exactly as today.
- The PM is a **named user**, not a role. This is a real extension to the approval engine, which
  today routes only to roles.
- **There is no delegation and no escalation on this level.** An unanswered request waits for the
  PM. This is deliberate, and it is the accepted cost of single-signature control.
- If the PM leaves or their account is deactivated, project material cannot move until an **owner
  reassigns the PM**. There is no fallback approver anywhere in this path. This is the single point
  of failure the design knowingly accepts (D28), and it should be said plainly on screen when a
  project's PM is inactive rather than presenting as an approval that is merely slow.
- A PM **may** approve a gate-out they raised themselves. It is recorded as raised and approved by
  the same person and appears in the owner's report (O12) — permitted, but never invisible. This is
  a deliberate exception to F3's default that a requester may not approve their own request.
- The approval screen shows the project, its budget, its cost to date and what this release would
  add, so the decision is made against a number rather than a feeling.
- Rejection requires a reason, as F4 already requires.

**O7.** As an owner, I want to be told when something expensive leaves on a project, so that
single-signature approval is not also unwatched.
- A tenant setting holds a **notification threshold** as a money value.
- When a project gate-out above the threshold is approved, the owner and admin are notified, naming
  the project, the pass, its value and the PM who approved it.
- **This blocks nothing.** The material moves; the notification follows.

**O8.** As a PM, I want to confirm job closeouts on my project, so that what lands on my cost is
what I agree happened.
- After the storekeeper confirms the returns (H3), a closeout on a project job goes to the PM.
- The PM sees the declared installed, consumed, returning and unaccounted quantities, with the cost
  each carries.
- Rejection returns it to the storekeeper with a reason.
- **Ledger postings do not wait for the PM.** Installed and consumed material posts on the
  storekeeper's confirmation exactly as §4.9 specifies today. The PM's step is acceptance of the
  cost, not a gate on the record — a ledger that waits for a financial signature stops being a
  record of what happened.
- The PM may query a closeout without holding the ledger hostage: rejection is a request for a
  corrected closeout, and any movement already posted in error is undone by a reversal (D8), never
  by withholding the posting in the first place.

**O9.** As a PM, I want to set and see the agreed subcontractor price on my jobs, so that I am
managing to a real cost.
- Setting or changing an agreed price is a PM action, recorded in the audit trail.
- The price is per job, not per project, so a partly delivered PO shows partly accrued cost.

**O10.** As a PM, I want disposal of my project's material to need my agreement, so that write-offs
do not appear on my cost without me.
- Where material being disposed or written off is attributed to a project, the PM is added as a
  level on top of the existing disposal approval rules (Epic J).
- Unlike O6 this **adds** a level rather than replacing one — disposal is permanent, and the
  existing control stays.

**O11.** As the system, I want project cost derived from the ledger, so that the yard's record and
the commercial record cannot disagree.
- **Material cost** counts material the closeout confirms as **installed**, **consumed** or
  **unaccounted for**, valued at the item type's unit cost **as at the movement**, captured onto the
  movement when it posts. Repricing an item type must not rewrite a closed project's history.
- **Unaccounted material of your own is a cost**, at that captured unit cost. Material issued to a
  project and never accounted for is a loss the PO absorbed, and a loss outside the P&L is a loss
  nobody manages.
- **Material still out** — issued to the project and neither returned nor accounted for — is
  reported as **exposure**, separately, and is **not** cost. It becomes cost only when the closeout
  says what happened to it.
- **Client-owned free-issue material carries no cost** while it behaves. A shortfall at closeout
  **does** become a project cost, at the value the client carries it at, because that is what the
  operator will debit.
- **Subcontractor cost** accrues at the agreed price when a subcontracted job **closes**.
- **Labour cost** accrues from the days recorded on a closeout, at the rate captured onto each
  labour entry (O15). In-house and subcontracted jobs therefore both carry a delivery cost and can
  be compared without misleading anyone.
- **Direct expenses** accrue when the PM approves them (O16).
- These four are the whole of project cost. Anything not in them — finance overheads, office costs,
  depreciation — is out of scope and the report says margin is stated before them.
- Cost is **computed, never stored as an editable figure**. Nothing in this epic offers a screen
  where a person types a project's cost.

**O12.** As an owner, I want project performance reporting, so that I can see which POs make money.
- Per project: contract value and current value after variations, cost budget, cost to date split
  into **material, subcontractor, labour and expenses**, exposure, losses from unaccounted material, variance
  against budget, margin, and progress measured as jobs closed against jobs opened.
- In-house and subcontracted projects may be ranked against each other, because both now carry a
  delivery cost. But a job closed with **no days recorded** costs nothing to deliver and would
  flatter its project, so the report shows how many closed jobs are missing days and will not
  present a margin as final while any are.
- Across projects: a list ranked by margin, by overrun and by exposure, filterable by client, PM and
  status.
- Projects over budget are **flagged, not blocked** — nothing in this epic stops a gate-out or an
  approval on budget grounds. The overrun surfaces here and on the owner's dashboard.
- A separate view lists gate-outs a PM raised and approved themselves (O6).
- Exportable, like the other reports in Epic M.

**O13.** As a PM, I want to close my project, so that its performance becomes final.
- Closing warns about open jobs and unreconciled material, the way work-order close already does
  (C7).
- Closing with either requires a reason, recorded on the project — the same pattern as
  `closed_with_variance` on a job.
- On close the performance figures are **snapshotted**, so a closed project reports what it reported
  on the day it closed even if a later reversal moves the underlying ledger.
- A closed project accepts no new gate-outs and no new variations. Reopening is an owner action and
  is recorded.

**O14.** As an owner, I want project financial data restricted, so that margins are not visible on a
yard phone.
- Three new permissions: **view project cost**, **view project value and margin**, and **view day
  rates**.
- **PM:** cost, budget and variance on **their own** projects. Not contract value, not margin, not
  other people's projects.
- **Owner and admin:** everything, on every project, day rates included.
- **Storekeeper and technician:** no financial data at all. Enforced in the API serializers, not by
  hiding fields in the interface — a restricted user's API response must not contain the numbers.
- **Labour is shown to a PM as a single figure**, never split by person. A PM who could see both a
  person's days and that person's labour cost could divide one by the other and read their rate,
  so the split is withheld, not merely hidden on screen.

**O15.** As an admin, I want technician time costed to jobs, so that work delivered in-house can be
compared with work given to a contractor without misleading anyone.
- A **day rate** is held **per user**, falling back to the rate on their **role**. Where neither
  exists, time cannot be costed and the job says so — it must not quietly cost zero.
- Days worked are recorded **on the job closeout that already exists** (H2), per person. No new
  form and no new habit. One closeout may name several people with different days.
- Days are recorded to one decimal place, so half days work.
- **A person cannot silently exceed one day.** On submission the closeout totals that person's days
  already recorded across every job for the same date and warns when the total passes one. It
  warns rather than refuses: a technician apportioning fractions in a yard at dusk will guess, and a
  refusal would block a late closeout because of an earlier one. The overlap is recorded so the
  owner can see it.
- The rate is **captured onto the labour entry** when the closeout is confirmed, so a later rate
  change never rewrites a closed project (D27).
- Labour applies to **in-house jobs**. A subcontracted job carries its agreed price instead;
  recording both against one job is refused, because it counts the same delivery twice.
- Days are **self-reported**. D31 lets a storekeeper submit a closeout on a technician's behalf, so
  the days may be secondhand — and they now carry money, which they did not before. The report
  names closed jobs with no days rather than letting them cost nothing (O12).
- Setting or changing a rate is an owner or admin action, recorded in the audit trail. Rates are
  **costing figures, not pay**, and nothing in this system calculates what anyone is owed.

**O16.** As a technician or storekeeper, I want to record what a job cost me out of pocket, so that
the project's margin includes the costs that do not pass through the yard.
- An expense carries: project, optional job, **category**, amount, date, description, who incurred
  it, and an **attachment** — a photograph of the receipt.
- Categories are tenant-configurable, seeded with transport, fuel, equipment hire, wayleaves and
  permits, accommodation, and other.
- **Anyone may record one; the PM approves it.** It reaches project cost only on approval. This is
  the only cost line with no ledger movement and no contract behind it, so it is also the only one
  where a second person looks at the figure before it counts.
- Rejection requires a reason and returns it to whoever recorded it.
- An expense with no attachment may be recorded but is flagged to the PM as unevidenced.
- Approved expenses are **append-only**, like everything else that affects a figure. A mistake is
  corrected by a reversing entry, not by editing the original.
- Expenses are **not** captured offline. D17 limits offline work to gate-in and gate-out, and
  nothing in this epic widens it.

---

## 6. Non-functional requirements

| # | Requirement |
|---|---|
| N-1 | Mobile-first responsive React. Must be usable on a low-end Android phone over a cellular connection. |
| N-2 | Any list view returns within 2 seconds at 100,000 movement records. |
| N-3 | Tenant isolation verified by automated tests on every endpoint. |
| N-4 | All traffic over TLS. Secrets in AWS Secrets Manager, never in the repo. |
| N-5 | Daily automated database backups with point-in-time recovery, and a documented restore drill. |
| N-6 | Hosted on AWS at modest cost: a single small RDS Postgres instance, containerised app, S3 for attachments. Designed to serve dozens of tenants before any scaling work is needed. |
| N-7 | Attachments stored in S3 with pre-signed, time-limited URLs. Never publicly readable. |
| N-8 | English only in v1. Strings externalised so localisation is possible later. |
| N-9 | Currency KES, timezone Africa/Nairobi, both configurable per tenant. |
| N-10 | API documented (OpenAPI) so future integrations are straightforward. |
| N-11 | Structured logging and error tracking in production. |

---

## 7. Out of scope for v1

- Self-serve signup, subscription plans and billing
- Client/external portal access — explicitly not planned
- Accounting, ERP or client-system integrations
- Batch/lot tracking as a distinct mode (only serialized, bulk and reel exist)
- Native mobile applications
- Supplier management beyond naming a supplier on a gate-in. Epic O adds a **subcontractor**
  register; suppliers stay free text
- Payroll. Day rates are a **costing** rate, not pay, and nothing here calculates what anyone is
  owed
- Subcontractor payables — their claims, invoices, payments and retention. Only the agreed price
  per job is held
- Milestone or certificate billing, invoices raised to clients, and receivables
- VAT, retention and withholding tax. Every figure in Epic O is VAT-exclusive
- Maintenance scheduling and calibration tracking for tools
- Data migration from the existing paper records

---

## 8. Accepted risks

| # | Risk | Impact |
|---|---|---|
| R1 | **Self-reported days are the weakest link in the new numbers.** Labour cost (O15) rests on a field filled in at closeout, and D31 allows a storekeeper to fill it in secondhand. A job closed with no days costs nothing to deliver and flatters its project. O12 names those jobs, but a report is not a control. | High. It is the one place where poor field discipline now moves a money figure, not just a quantity. |
| R2 | **PM self-approval is permitted** (O6), criticality routing is off for project material (D22), and there is no second signature anywhere. One person can raise and release any material on their own project. The only check is a report the owner has to actually read. | High. Accepted deliberately, but it is the largest control weakening in this epic and should be reviewed after real use. |

---

## 9. Approval

This document is step 1 of 4. On approval it is followed by:

2. **Design** — data model, API surface, component structure, key flows, error handling, testing approach, each part referencing the requirements above
3. **Tasks** — a sequenced implementation checklist
4. **Implementation** — one task at a time, verified against its requirement

Every open question raised during discovery has been answered and folded into section 4. What remains
in section 8 are two risks accepted with open eyes, not decisions outstanding.
