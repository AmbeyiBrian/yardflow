# YardFlow — Yard Inventory & Gate Control System

Controlled, auditable, mobile-first record of everything entering and leaving a yard, with
approval gates on outbound movements. Multi-tenant from day one.

- **Start here — the system end to end:** [docs/00-overview.md](docs/00-overview.md)
- **Requirements:** [docs/01-requirements.md](docs/01-requirements.md)
- **Design:** [docs/02-design.md](docs/02-design.md)
- **Task checklist:** [docs/03-tasks.md](docs/03-tasks.md)

Implementation follows the task list strictly, one task at a time.

---

## Layout

```
backend/          Django 5 + DRF project (project package: config)
  config/         settings (base/dev/prod), urls, celery
  core/           tenancy base classes, audit, numbering, attachments, settings
  accounts/       User, Role, Permission, WebAuthn, Delegation
  catalogue/      ItemCategory, CategoryCustomField, ItemType
  network/        Client, Site, SiteReference, WorkOrder
  locations/      Location, StockNode
  stock/          StockMovement, StockBalance, SerialUnit, Reel, StockCount
  receiving/      GateIn and lines
  dispatch/       GateOut, lines, release, variances
  approvals/      ApprovalRule, ApprovalRequest, ApprovalAction, engine
  jobs/           Job, JobCloseout, reconciliation
  custody/        CustodyExpectation, transfers, overdue sweeps
  disposition/    quarantine decisions, Disposal
  notifications/  events, deliveries, channel adapters
  reporting/      report queries, exports
  sync/           idempotency, offline submission handling
  platform_admin/ cross-tenant console
frontend/         React 18 + TypeScript SPA (Vite) — scaffolded in T1.27
docs/             requirements, design, tasks
docker-compose.yml  local Postgres 16 + Redis
```

See design §1.2 for why the apps are split this way.

---

## Local development

**Everything runs locally. No AWS account, no cloud credentials** (design §12.0). Files go to
local disk, email to the console, SMS to the log. Moving to AWS is Phase 9 and blocks nothing.

### Prerequisites

- Python 3.12
- Node.js 20+
- Docker Desktop (for Postgres and Redis)

### 1. Start the infrastructure

```bash
docker compose up -d
```

Brings up Postgres 16 on `localhost:5432` and Redis on `localhost:6379`. Both have health
checks; `docker compose ps` should show them healthy.

Alternatively, if you already run a native PostgreSQL 16, point `DATABASE_URL` at it instead —
only one of the two can hold port 5432.

### 1a. Database roles (T1.7)

Row-level security is only a real barrier if the application cannot bypass it, so there are two
roles:

| Role | Used for | Privileges |
|---|---|---|
| `postgres` (or any owner) | Creating the database, running migrations, creating RLS policies | Privileged |
| `yardflow_app` | Every application connection | `NOSUPERUSER`, `NOBYPASSRLS`, `NOCREATEROLE` |

`yardflow_app` additionally holds `CREATEDB`, purely so `pytest-django` can build its test
database. That confers no ability to read another tenant's rows.

```sql
CREATE ROLE yardflow_app LOGIN PASSWORD '...'
  NOSUPERUSER NOCREATEROLE NOBYPASSRLS CREATEDB;
GRANT CONNECT ON DATABASE yardflow TO yardflow_app;
GRANT USAGE, CREATE ON SCHEMA public TO yardflow_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO yardflow_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO yardflow_app;
```

Policies are created with `FORCE ROW LEVEL SECURITY`, not merely `ENABLE`. A plain `ENABLE`
exempts the table owner, which would make the guarantee depend on which role happened to run the
migration — true in production, quietly untrue in CI. `FORCE` removes that variable. Run
migrations with the owner role:

```bash
DATABASE_URL="$MIGRATION_DATABASE_URL" python manage.py migrate
```

### 2. Create the Python environment

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e "backend[dev]"
```

### 3. Configure the environment

```bash
cp backend/.env.example backend/.env
```

`backend/.env` is git-ignored. **No secret ever belongs in the repository** (N-4); in production
these values come from AWS Secrets Manager.

### 4. Run the backend

```bash
cd backend
python manage.py check
python manage.py migrate
python manage.py runserver
```

### 5. Run the frontend

Scaffolded in T1.27. Once present:

```bash
cd frontend
npm install
npm run dev
```

The Vite dev server proxies `/api` to Django, so a single origin serves the app in development.

### Tenant subdomains locally

Organizations are resolved from the request subdomain (design §2.2), e.g.
`silvertech.localhost:5173`. Modern browsers resolve any `*.localhost` name to loopback with no
hosts-file editing needed.

---

## Tests

```bash
# The backend suite, plus the lint and type gates CI enforces.
cd backend
pytest
ruff check .
mypy .
python manage.py openapi --file api-schema.yml   # the committed schema (N-10)

# The frontend gates.
cd ../frontend
npm run typecheck
npm run lint
npm run build
```

Coverage effort concentrates on the ledger, the approval engine and the tenancy layers — the
places where a bug is both expensive and invisible (design §14).

### End-to-end (T8.13)

Playwright drives the two flows §14 names against a **real backend and a seeded tenant**, on a
phone viewport and a desktop one. A mocked end-to-end test proves the frontend agrees with a
fixture somebody wrote — and that agreement is exactly what went wrong twice in this project.

```bash
# Once, to fetch the browser.
cd frontend
npx playwright install chromium

# Both servers have to be running, and the tenant seeded:
#   cd backend && python manage.py seed_demo
#   cd backend && python manage.py runserver 8000
#   cd frontend && npm run dev

npm run e2e            # both viewports
npm run e2e:phone      # the one that matters most
```

The specs establish their own preconditions through the API — the demo tenant's stock moves every
time somebody uses it, and a UI test that assumed a particular antenna was still on the shelf
would fail on Friday for no reason anybody could act on. What is set up that way is *state*; every
assertion goes through the interface.

### Notification providers — where credentials go

Everything lives in **`backend/.env`**, which is git-ignored (`N-4`: no secret
ever enters the repository). `backend/.env.example` lists every key with a
comment; copy the block you need and fill it in. In production the same names
come from AWS Secrets Manager instead — no code path differs.

**SMS — UjumbeSMS.** Four values, and all of them matter:

```ini
# backend/.env
SMS_BACKEND=notifications.channels.ujumbe_sms.UjumbeSmsBackend
UJUMBE_SMS_API_KEY=            # from the UjumbeSMS dashboard
UJUMBE_SMS_ACCOUNT_EMAIL=      # the email the account logs in with
UJUMBE_SMS_SENDER_ID=          # the approved sender ID, e.g. SILVERTECH
# Only if your account was issued against the other host UjumbeSMS publishes:
# UJUMBE_SMS_BASE_URL=https://api.ujumbe.co.ke
```

`SMS_BACKEND` is the switch: without it the logging adapter stays in place and
prints messages instead of sending them, so a developer's machine can never
reach a real technician (§12.0). The account email is not optional — UjumbeSMS
authenticates on the key *and* the email, and the key alone returns 401.

Then check it **without messaging anybody**:

```bash
cd backend
python manage.py sms_selftest              # asks the provider for the balance
python manage.py sms_selftest --to 0722…   # sends one real message, opt-in
```

The balance check exercises the same credentials, headers and host as a send, so
it answers "does this key work?" and "are there credits?" — the two things that
go wrong — without spending a message on a colleague. A failure names which of
the three settings to look at.

> **Write `KEY=value`, with no space before the equals sign.** django-environ
> matches that exactly and silently skips `KEY = value`, so a credential can be
> sitting in `.env` and still be absent from the settings — which looks from the
> outside like a wrong API key. `manage.py check` now warns when the provider is
> selected and a credential is empty, which is the fastest way to spot it.

The adapter is written against UjumbeSMS's contract:
`POST {host}/api/messaging`, headers `X-Authorization` and `Email`, body
`{"data":[{"message_bag":{"numbers","message","sender"}}]}`, response
`{"status":{"code","type","description"},"meta":{…}}`. **Success is read from
`status.type`, not the code** — the codes are not HTTP-shaped, and a live account
answers a balance query with `1008 / success`. Numbers are normalised to
`254…` on the way out, because a technician may be stored as `+254…`, `07…` or
`7…` depending on who created them (`B1`) and a number the gateway cannot parse
is a message nobody receives.

**WhatsApp — Meta Cloud API.** Finished, unit-tested against a mock, and
**off**:

```ini
# backend/.env — leave these blank until Meta approves the sender
WHATSAPP_ACCESS_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=
```

`Q1`/§9.2: the Business API needs an approved sender and pre-registered message
templates, which takes weeks and carries per-message cost. The approval loop is
the product's core value and must not wait on Meta's queue — so where the
notification matrix specifies WhatsApp, in-app carries the message until the
sender is approved. Enabling it later is these two values plus turning
`whatsapp` on in the tenant's notification settings screen. No code change, no
deployment.

### Scheduled work

Several requirements are only met by something on a clock — overdue custody (`I3`), approval
escalation (`F5`), gate-pass expiry (`Q3`), unacknowledged client returns (`K3`). They run from
`CELERY_BEAT_SCHEDULE`:

```bash
cd backend
celery -A config worker -l info      # the worker
celery -A config beat -l info        # the clock
```

Without them the product still works; overdue items simply do not chase themselves.

### The product mark

The name is set as a wordmark in Archivo Semi-Condensed Bold, converted to
outlines. It lives in one place — `frontend/src/components/Logo.tsx`, exporting
`Wordmark` (the full name) and `LogoMark` (the bare `Y`, cut from the same
outlines for the browser tab, the phone header and the Android home screen).
Both are inline SVG filled with `currentColor`, so there is no webfont to load
and one asset serves light and dark surfaces alike.

Everything else derives from those two:

| Where | What |
| --- | --- |
| Login screen, sidebar | `Wordmark` |
| Phone header | `LogoMark` |
| Browser tab | `public/favicon.svg` |
| iOS and Android home screens | `public/apple-touch-icon.png`, `public/pwa-*.png` |
| Gate pass, GRN, certificate, waybill | `backend/templates/documents/_wordmark.html` |

The raster icons are committed, so a normal build needs nothing extra. Regenerate
them only when the mark itself changes:

```bash
cd frontend
node scripts/rasterize-icons.mjs   # PNGs from the two SVGs
```

Candidates, the licence position and the generator that produced the outlines are
in [`docs/brand/`](docs/brand/README.md). `e2e/branding.spec.ts` asserts the mark
renders on both layouts and that every icon the manifest declares actually
exists — the app shipped for a while with a leftover mark from the project
template, which is the kind of thing nobody sees after the first day.

---

## Conventions

- **Posted documents are immutable.** Corrections are reversals, never edits (M4).
- **The stock ledger is append-only.** No `UPDATE`, no `DELETE`, enforced by a database trigger
  as well as in Python (§3.2).
- **Cross-tenant access returns 404, never 403**, so object IDs are never confirmed to
  outsiders (§2.4).
- **State changes are explicit sub-resource `POST` actions**, never a `PATCH` on `status` (§6).
- `all_objects` is the only unscoped manager and is confined to `platform_admin/`; CI fails the
  build if it appears anywhere else (§2.1).
