/**
 * Shared helpers for the end-to-end flows (design §14; T8.13).
 *
 * Two decisions worth stating, because both are about making a failure legible:
 *
 * **Sign in through the form, not by injecting a token.** The login screen is
 * part of both flows §14 names, and a suite that skipped it would not notice the
 * day it broke. It takes two seconds.
 *
 * **Locators are by role and accessible name**, not by test ids. A button a
 * screen reader cannot find is a button a storekeeper with a cracked screen and
 * large text cannot find either — so the same locator that makes the test pass
 * makes the app usable. Where a name is ambiguous the spec narrows by region
 * rather than adding a test id.
 */

import { type APIRequestContext, type Page, expect } from '@playwright/test';

/** The seeded demo tenant (`manage.py seed_demo`). */
export const PASSWORD = 'yardflow-demo-password';

export const PEOPLE = {
  owner: 'owner@demo.local',
  storekeeper: 'store@demo.local',
  approver: 'approver@demo.local',
  technician: 'tech@demo.local',
} as const;

export async function signIn(page: Page, identifier: string): Promise<void> {
  await page.goto('/login');
  await page.getByLabel(/email or phone/i).fill(identifier);
  await page.getByLabel(/password/i).fill(PASSWORD);
  await page.getByRole('button', { name: /sign in/i }).click();

  // Wait for the *navigation*, not for a heading. The login screen has an `h1`
  // of its own ("YardFlow"), so asserting on a heading returned instantly and
  // every later step raced the login — the next `goto` bounced straight back to
  // /login and the failure looked like a missing form field.
  await expect(page).not.toHaveURL(/\/login/, { timeout: 20_000 });

  // And the shell has rendered, so the session resolved and /me came back.
  await expect(page.getByRole('button', { name: /sign out/i }).first()).toBeVisible({
    timeout: 20_000,
  });
}

export async function signOut(page: Page): Promise<void> {
  await page.getByRole('button', { name: /sign out/i }).first().click();
  await expect(page).toHaveURL(/\/login/);
}

/**
 * Go to a screen by URL rather than by tapping through the shell.
 *
 * The navigation itself is exercised where it is the subject of a test; using it
 * as transport everywhere would make one broken nav item fail every spec, which
 * hides what actually went wrong.
 */
export async function open(page: Page, path: string): Promise<void> {
  await page.goto(path);
  await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible();
}

/** A distinctive value, so a failed assertion names the run that produced it. */
export function unique(prefix: string): string {
  return `${prefix}-${Date.now().toString().slice(-6)}`;
}

/* -------------------------------------------------------------------------- */
/* Preconditions                                                              */
/* -------------------------------------------------------------------------- */

/**
 * Put stock of a high-criticality item in the yard, through the API.
 *
 * The demo tenant is a *demo*, not a fixture: its stock moves every time
 * somebody uses it, and a UI test that assumed a particular antenna was still
 * on the shelf would pass on Monday and fail on Friday for no reason anybody
 * could act on.
 *
 * So the precondition is established explicitly, over the API, as a storekeeper
 * would have done it. The assertions afterwards are still entirely through the
 * interface — what is set up here is the state, never the behaviour under test.
 */
export async function receiveCriticalStock(
  request: APIRequestContext,
  quantity = 3,
): Promise<{ itemId: number; itemName: string; locationId: number } | null> {
  // Straight at Django, with the tenant in the Host header (§2.2). Node's DNS
  // does not resolve `*.localhost` the way a browser does, so a request context
  // pointed at the subdomain fails with ENOTFOUND — the API is addressed by IP
  // and the tenant travels in the header, which is what the middleware reads
  // anyway.
  const api = process.env.E2E_API_URL ?? 'http://127.0.0.1:8000';
  const host = new URL(process.env.E2E_BASE_URL ?? 'http://demo.localhost:5173').hostname;
  const tenant = { Host: host };

  const login = await request.post(`${api}/api/v1/auth/login`, {
    headers: tenant,
    data: { identifier: PEOPLE.storekeeper, password: PASSWORD },
  });
  const { access } = await login.json();
  const headers = { ...tenant, Authorization: `Bearer ${access}` };

  const items = await (
    await request.get(`${api}/api/v1/item-types?page_size=200`, { headers })
  ).json();
  const critical = items.results.find(
    (row: { name: string; criticality?: string }) => /rru|antenna/i.test(row.name),
  );
  const locations = await (
    await request.get(`${api}/api/v1/locations?page_size=50`, { headers })
  ).json();
  const yard = locations.results.find((row: { type: string }) => row.type === 'YARD');
  if (!critical || !yard) return null;

  const gateIn = await (
    await request.post(`${api}/api/v1/gate-ins`, {
      headers,
      data: {
        source_type: 'PURCHASE',
        supplier_name: 'E2E precondition',
        to_location: yard.id,
        received_at: new Date().toISOString(),
        lines: [
          {
            item_type: critical.id,
            tracking_mode: 'BULK',
            quantity: String(quantity),
            uom: critical.uom,
            condition: 'NEW',
            // D3: untagged units go as a quantity *with the reason recorded*.
            no_serial_reason: 'E2E precondition — no labels on this batch.',
          },
        ],
      },
    })
  ).json();

  await request.post(`${api}/api/v1/gate-ins/${gateIn.id}/post`, {
    headers,
    data: {},
  });
  return { itemId: critical.id, itemName: critical.name, locationId: yard.id };
}
