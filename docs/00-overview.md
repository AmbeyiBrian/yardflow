# YardFlow, end to end

A guided tour of the whole system: what it is for, the one idea it is built on,
what happens to a piece of material from the moment it arrives to the moment it
is written off, and where each of those things lives in the code.

Read this first. The other documents go deeper:

| Document | What it holds |
| --- | --- |
| [`01-requirements.md`](01-requirements.md) | The user stories, grouped into epics `A`–`N`, with acceptance criteria |
| [`02-design.md`](02-design.md) | How it is built, section by section (`§1`–`§18`) |
| [`03-tasks.md`](03-tasks.md) | The 125 implementation tasks, each tied to a requirement and a design section |
| [`../README.md`](../README.md) | How to run it, test it, and where credentials go |
| [`brand/README.md`](brand/README.md) | The wordmark, its alternatives, and how to regenerate it |

Throughout, `D3` or `§7.2` are cross-references into those two documents. They are
worth following: nearly every non-obvious decision here exists because a
requirement forced it.

---

## 1. What this is

**Silvertech** is a telecoms installation subcontractor in Nairobi. Operators
hand them equipment — antennas, radios, cable drums, batteries — to install at
sites across the country. Their own stock and the operators' stock sit in the
same yard. Technicians take material out in the morning and bring some of it
back; some of it is installed, some is consumed, some comes back damaged, some
never comes back at all.

The commercial problem is not knowing which. An operator asks what happened to
the twelve radios issued in March, and the answer takes a week of asking people.
Material that was never returned is invisible until it is invoiced for.

YardFlow is the record that answers those questions. It is:

- **Multi-tenant** — one deployment, many subcontractors, each on its own
  subdomain and unable to see another's data (`A1`, `A3`)
- **Mobile-first** — the people who use it are standing in a yard holding a
  phone, often on one bar of signal (`N-1`)
- **A ledger, not a spreadsheet** — every movement is recorded and nothing is
  ever edited after the fact (`D8`, `M3`)

### Who uses it

| Actor | What they do |
| --- | --- |
| **Storekeeper** | Receives deliveries, raises gate-out requests, releases material at the gate, counts stock |
| **Technician** | Sees what they are carrying, closes out jobs, returns what is left |
| **Approver** | Decides on gate-out requests and disposals |
| **Owner / admin** | Sets up the catalogue, people and rules; reads the reports |
| **Platform admin** | Silvertech's own supplier — creates tenants, suspends them, never reads their data |

---

## 2. The one idea

Everything that happens to material is **a movement between two nodes**.

A **node** is anywhere material can be. There are seven kinds
(`locations/models.py`):

```
LOCATION   a yard, a store, a vehicle, the quarantine bay
PERSON     material in somebody's custody
SITE       material installed at a site
CLIENT     material returned to the operator who owns it
EXTERNAL   suppliers and client-issuing stores — where receipts come from
CONSUMED   bulk material used up on site
SCRAP      disposed material
```

A **movement** says: this much of this item went from this node to that node, at
this time, posted by this person. Eleven types, all the same shape:

```
RECEIPT  ISSUE  TRANSFER  INSTALL  CONSUME  RETURN
QUARANTINE  RESTORE  DISPOSE  ADJUST  REVERSAL
```

That is the whole model. It matters because it collapses what would otherwise be
six separate subsystems into one query:

- *What is in the yard?* — sum the movements into and out of `LOCATION` nodes
- *Who is holding what?* — the same sum, for `PERSON` nodes
- *What did we install at that site?* — the same sum, for one `SITE` node
- *What do we still owe the client?* — the same sum, for their `CLIENT` node
- *What was issued vs installed vs returned vs unaccounted?* (`H4`) — one
  aggregation grouped by destination node type

Six bespoke reports could disagree with each other. One ledger cannot.

**The ledger is append-only.** No row is ever updated or deleted — database
triggers refuse it, not just the application (`§3.2`). A mistake is corrected by
posting a `REVERSAL`, which leaves both the error and the correction visible.
That is what makes the record worth anything in a dispute.

---

## 3. The life of a delivery

This is the system end to end. Each step names the screen, the endpoint and the
requirement behind it.

### 3.1 It arrives at the gate — *gate-in*

A lorry arrives. The storekeeper opens **Gate-in → Receive a delivery**
(`features/receiving/`), and captures what is on it: supplier or client, delivery
note number, and a line per item. Serialized items (radios, antennas) need their
serial numbers — the interface will not accept a serialized line without them, or
without a stated reason why not (`D3`). Cable drums get a drum number and a
length (`D4`). Photographs of the delivery note and of any damage are attached
(`D6`).

A delivery starts as a **draft**. Nothing is stock yet. When the storekeeper
posts it, three things happen in one transaction:

1. A `RECEIPT` movement per line, from an `EXTERNAL` node into a `LOCATION` node
2. Serial units and drums are created and become trackable individually
3. A **GRN number** is allocated from a gap-free counter (`§3.4`)

Now it is stock (`D8`). If anything arrived damaged, it goes into the quarantine
bay instead of general stock — a `LOCATION` node like any other, which is what
makes damaged material impossible to issue by accident (`J1`).

> **Why drafts exist:** a delivery is captured at the gate, in the rain, on a
> phone. Posting is a separate, deliberate act — so a half-captured delivery is
> never mistaken for stock.

### 3.2 Somebody needs it — *gate-out request*

A technician needs material for a job. The storekeeper raises a request in
**Gate-out → Request material** (`features/dispatch/`): what, how much, which
site or work order, and who will carry it.

The request is checked against what is actually available, not against a number
in a field — the ledger is the source of truth. Client-owned stock is visually
distinct everywhere it appears (`E1`), because issuing an operator's radio as
though it were your own is the mistake that costs money.

### 3.3 Somebody has to say yes — *approval*

Nothing leaves the yard on one person's say-so (`F1`). The **approval engine**
(`approvals/`) routes the request by what is in it:

- Ordinary material — one approver
- High-criticality or high-value items — a second level (`F3`)
- Below a configured threshold — auto-approved, and recorded as such (`F2`)

The rules are data, not code, so an owner can change them without a release
(`F6`). Two things are enforced no matter what: **you cannot approve your own
request** (`F4`), and a high-criticality approval can require a fingerprint on
the approver's own phone (WebAuthn step-up, `B5`/`D9`).

If nobody answers, it escalates on a clock (`F5`).

### 3.4 It leaves the yard — *gate release*

The approved pass appears in **ready to release**. At the gate the storekeeper
records what physically goes onto the vehicle — which may be **less** than was
approved. A short release is allowed but never silent: it needs a reason, and it
lands on the exceptions register (`G2`, `H3`).

On release:

- An `ISSUE` movement moves the material from the yard `LOCATION` to the
  technician's `PERSON` node — they now hold it, and the system knows it (`I1`)
- A **gate pass** is generated as a PDF with a QR code (`G4`) — the document the
  driver carries and the guard checks
- Signatures are captured on the phone (`G3`)

### 3.5 It gets used — *job execution*

At site, the technician closes out the job (`features/jobs/`): what was
installed, what was consumed, what is coming back, what is being returned as
faulty. Each of those is a movement to a different node type — `SITE`,
`CONSUMED`, back to a `LOCATION`, or into quarantine.

Closeout is where `H4` is answered: **issued vs installed vs returned vs
unaccounted**, per job, computed from the ledger rather than typed in. Anything
unaccounted for stays visible until somebody explains it.

### 3.6 It comes back — or it does not — *custody*

Material in a `PERSON` node has an expected return date. A nightly sweep
(`custody/`) finds what is overdue and notifies both the holder and their
supervisor (`I3`). **Custody** and **Exceptions** are top-level screens because
an owner checks them daily.

Custody can be transferred between people, with both sides recorded (`I2`).

### 3.7 It is damaged, or it belongs to somebody else — *disposition*

Material in quarantine needs a decision (`J2`): repair, return to the client, or
scrap. Scrapping needs approval and produces a **disposal certificate** (`J3`).

Client-owned material returned to its operator produces a **return waybill** for
their store to sign (`K2`). Until they acknowledge it, the exposure is still
yours — the **Client stock** screen shows exactly what an operator could invoice
you for today (`K1`, `K3`).

### 3.8 Somebody asks a question — *reporting*

Twelve reports (`reporting/reports.py`), all reading the same ledger:

| Report | Answers |
| --- | --- |
| Stock on hand | What is in the yard now |
| Stock as at a date | What was in the yard on any past date |
| Movement history | Everything that happened, filtered |
| Serial history | The whole life of one serial number |
| Client-owned position | What each operator's material is doing |
| Outstanding gate passes | What has left and not been closed |
| Overdue returns and custody | Who is holding what, too long |
| Consumption per site or work order | What each job cost in material |
| Variance and exceptions register | Every short release and discrepancy |
| Recoveries by originating site | What came back from where |
| Disposals and write-offs | What was scrapped, and who approved it |
| Installed base | What is standing at each site |

Every one exports to Excel and PDF from the same column definition, so the
screen, the spreadsheet and the printout cannot disagree (`M2`).

---

## 4. What keeps it honest

Four mechanisms do the heavy lifting. Each exists because a specific failure
would otherwise be possible.

### Tenancy — four layers (`§2`)

One tenant must never see another's data (`A3`). Rather than trust a `WHERE`
clause, there are four independent layers:

1. **Manager** — the default queryset is scoped, and *raises* if no tenant is in
   context rather than returning everything
2. **Middleware** — resolves the tenant from the subdomain, and publishes it to
   Postgres for the life of the transaction
3. **Row-level security** — Postgres itself refuses rows from another tenant,
   enforced with `FORCE` so even the table owner obeys
4. **API** — another tenant's object answers **404, never 403**, so an ID is
   never confirmed to an outsider

A test suite walks every registered endpoint and asserts every verb returns 404
across the tenant boundary. Forgetting to scope a new endpoint fails the build.

### The append-only ledger (`§3.2`)

Database triggers refuse `UPDATE` and `DELETE` on movements. Corrections are
reversals. This is the difference between a record and a story.

### Gap-free numbering (`§3.4`)

GRNs, gate passes and certificates are numbered without gaps, from a locked
counter row. A missing number in a sequence is what an auditor looks for, so the
sequence must be defensible.

### Approvals that cannot be dodged (`§5`)

Self-approval is refused at the server, not just hidden in the interface. So is
approving offline — a phone with no signal cannot manufacture an approval
(`§8.3`); it can only release a pass that was *already* approved before it went
offline.

---

## 5. Offline

A yard has poor signal, and a gate cannot stop working because of it (`N1`–`N3`).

Two flows work with no network: **gate-in capture** and **gate-out release**.
The service worker precaches those two bundles and nothing else — a technician
should not download the reporting suite to receive a delivery.

Captured work goes into an IndexedDB queue and is sent when there is signal.
Three rules make that safe:

- **Every submission carries a client-generated UUID.** Sending it ten times
  posts it once (`sync/services.py`). Verified by actually sending ten.
- **The queue never invents authority.** An offline release of an unapproved
  pass is refused with `OFFLINE_APPROVAL_NOT_ALLOWED` when it reaches the
  server.
- **Conflicts are shown, not resolved silently.** If the yard changed underneath
  a queued item, it lands on the sync exceptions screen for a human.

---

## 6. Notifications

Events fan out to channels (`notifications/`): in-app, SMS, and WhatsApp when it
is enabled. Dispatch happens on transaction commit, so nothing is announced that
did not actually happen (`L3`).

SMS goes through **UjumbeSMS**. Two things about that adapter are worth knowing,
both learned the hard way and both documented in `§9.3`: success is read from
`status.type`, not the status code (their codes are not HTTP-shaped — a live
account answers `1008 / success`), and a response the adapter cannot classify
fails **without retrying**, because a message that may already have gone out
must not be sent twice.

`manage.py sms_selftest` checks credentials against the balance endpoint without
texting anybody. A system check warns at startup if the provider is selected but
a credential is missing.

WhatsApp is written and tested against a mock, and stays off until Meta approves
a sender.

---

## 7. The shape of the code

```
backend/
  config/          settings, urls, celery
  core/            tenancy, audit, numbering, attachments, error envelope
  accounts/        users, roles, permissions, WebAuthn, delegation
  catalogue/       item categories, custom fields, item types
  network/         clients, sites, work orders
  locations/       locations and the StockNode graph
  stock/           movements, balances, serials, drums, counts
  receiving/       gate-in
  dispatch/        gate-out, release, variances
  approvals/       rules, requests, the routing engine
  jobs/            jobs, closeout, reconciliation
  custody/         expectations, transfers, overdue sweeps
  disposition/     quarantine decisions, disposals, client returns
  notifications/   events, deliveries, channel adapters
  reporting/       the twelve reports and their exports
  sync/            idempotency and offline submissions
  platform_admin/  the cross-tenant console

frontend/src/
  api/             client, hooks, generated schema types
  auth/            session, permissions
  components/      shell, logo, shared UI
  features/        one folder per area: receiving, dispatch, jobs,
                   disposition, stock, reports, settings, notifications
  offline/         Dexie queue, sync screen, service worker registration
  routes/          route table, lazy loading, stale-bundle recovery
```

Roughly 250 Python modules and 58 TypeScript ones, over 51 migrations.

### The API

REST, versioned at `/api/v1`, described by a committed OpenAPI schema that a test
compares against the code — the schema cannot drift.

Two conventions worth knowing:

- **No trailing slashes.** Every URL has exactly one form. A request that
  includes one is redirected to the canonical form with a **307** (temporary, and
  the method survives), never a permanent redirect — a permanent one gets cached
  by browsers and can collide with an older cached redirect to form a loop.
- **One error shape.** Every failure is
  `{"error": {"code", "message", "field_errors?", "details?"}}` — including
  unmatched paths, so a client always has something to show a person.

State changes are POST sub-resources — `POST /gate-outs/{id}/submit`,
`/approve`, `/release` — rather than a `PATCH` on a status field. The verb says
what happened, and the audit trail says who did it.

---

## 8. Running it

```bash
# infrastructure
docker compose up -d          # Postgres 16 and Redis

# backend
cd backend
../.venv/Scripts/python.exe manage.py migrate
../.venv/Scripts/python.exe manage.py seed_demo
../.venv/Scripts/python.exe manage.py runserver 127.0.0.1:8000

# frontend
cd frontend
npm install
npm run dev
```

Then open **http://demo.localhost:5173**. Tenants are addressed by subdomain, and
every browser resolves `*.localhost` to loopback without any hosts-file entry.

Demo sign-ins are in the root README.

### Tests

| Suite | What it covers |
| --- | --- |
| `pytest` (backend) | ~1050 tests: the ledger, tenancy isolation, approvals, numbering, adapters |
| `playwright test` (frontend) | The two yard flows end to end, on a phone viewport and a desktop one, against a real backend |
| `ruff`, `mypy`, `tsc`, `oxlint` | Lint and types |

The end-to-end suite deliberately drives a **real backend against a seeded
tenant** rather than mocks. Both of the worst bugs this project has had — writes
failing on a trailing slash, and scanned serials being silently dropped — passed
every mocked expectation there was.

---

## 9. Where it stands

All 125 tasks in `03-tasks.md` are implemented and verified.

Outstanding, in the order it matters:

1. **One real SMS.** Credentials are in place and verified against the provider's
   balance endpoint; the messaging endpoint's success code has not been observed
   on a live send. `manage.py sms_selftest --to 07…` closes that.
2. **WhatsApp credentials.** The adapter is finished and off, pending an approved
   Meta sender and templates.
3. **Deployment (`§12.1`).** Deliberately out of scope so far — the AWS side is
   designed but not built.
4. **Empty-vs-failed list states.** A list screen currently renders its error
   banner and its empty state together, so a failed request reads as an empty
   yard. The rule to apply: loading, failed-with-retry, and genuinely empty are
   three states, and only one is shown at a time.
5. **Two E2E tests** are failing after the dev service worker was turned off — the
   manifest link is no longer injected in development, so the installability
   assertions have nothing to read.
