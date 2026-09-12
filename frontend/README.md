# YardFlow frontend

React 19 + TypeScript SPA (Vite 8), mobile-first. Design §7.

## Running

```bash
npm install
npm run dev        # http://<tenant>.localhost:5173
```

The dev server proxies `/api` to Django on port 8000 and **passes the Host
header through unchanged** — tenants are addressed by subdomain (§2.2), so
`http://demo.localhost:5173` resolves the `demo` tenant. Changing the Host would
resolve the wrong tenant, or none.

## Scripts

| Script | What it does |
|---|---|
| `npm run dev` | Dev server with HMR |
| `npm run build` | Type-check and production build |
| `npm run typecheck` | Types only |
| `npm run lint` | oxlint |
| `npm run api:types` | Regenerate `src/api/schema.d.ts` from `../backend/api-schema.yml` (N-10) |

## Layout

```
src/
  api/          client.ts (error envelope + token refresh), schema.d.ts (generated)
  auth/         session provider, usePermission(), <Can>, permission codenames
  components/   AppShell, ui/ primitives
  features/     one directory per feature, lazily loaded
  routes/       route definitions and guards
```

## Conventions

- **Permission gates are UX, never security.** `/me` returns resolved
  permissions and navigation follows them, but the server re-checks every call
  (§7.2).
- **Switch on `error.code`, never on the message.** Messages get reworded and
  localised (N-8); codes are the contract (§6.1).
- **44px minimum touch targets**, primary actions bottom-anchored on a phone
  (§7.3).
- **No horizontal page scroll** at any width. Wide content scrolls inside its
  own container.
- Client-owned stock is visually distinct from own stock wherever it appears
  (E1) — see the `--color-client-owned` token.
