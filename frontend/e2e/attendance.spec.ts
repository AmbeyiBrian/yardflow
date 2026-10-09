/**
 * Clock-in on a phone (Epic R, T16.16; design §4.18.13).
 *
 * Three stories, each with the phone's position set by Playwright:
 *
 *  1. Outside the area, clock-in is refused with the distance; inside it
 *     succeeds; with location off it is refused; clock-out follows.
 *  2. Offline, clock in and out; the owner moves the area meanwhile; online
 *     again both land, flagged "Area changed".
 *  3. The day is formed, the project manager rejects it with a reason, the
 *     person adds a correction, and the same manager sees original beside
 *     corrected and approves.
 *
 * A fresh technician is made for every run (through `support/attendance_tool.py`,
 * which also runs the sweep's day formation as if two days had passed, because
 * a day is only routed once its date is over) so no earlier run's days get in
 * the way. Everything else is done through the interface.
 */

import { execFileSync } from 'node:child_process';
import path from 'node:path';

import { type APIRequestContext, type Page, expect, test } from '@playwright/test';

import { PASSWORD, PEOPLE, signIn, unique } from './fixtures';

test.describe.configure({ mode: 'serial' });

const api = process.env.E2E_API_URL ?? 'http://127.0.0.1:8000';
const host = new URL(process.env.E2E_BASE_URL ?? 'http://demo.localhost:5173').hostname;
const tenant = { Host: host };

const run = unique('ATT');
const siteName = `E2E Clock Site ${run}`;
const siteRef = `CS-${run}`;
const techName = `Tess Tech ${run}`;
const techEmail = `tess.${run.toLowerCase()}@demo.local`;

// Far from every demo place, so this site is the only one in reach.
const CENTRE = { latitude: -1.5, longitude: 37.2 };
// One degree of latitude is about 111.2 km: 0.00576 is about 640 m.
const NORTH_640 = { latitude: CENTRE.latitude + 0.00576, longitude: CENTRE.longitude };
const accuracy = 10;

let siteId = 0;

const backend = path.resolve(process.cwd(), '../backend');
const python = path.resolve(backend, '../.venv/Scripts/python.exe');
const tool = path.resolve(process.cwd(), 'e2e/support/attendance_tool.py').replace(/\\/g, '/');

/** Run the dev-side helper against the demo tenant; returns its output. */
function runTool(name: 'make-user' | 'form-days', extra: Record<string, string> = {}): string {
  return execFileSync(python, ['manage.py', 'shell', '-c', `exec(open(r'${tool}').read())`], {
    cwd: backend,
    env: { ...process.env, E2E_TOOL: name, E2E_TENANT: 'demo', ...extra },
    encoding: 'utf8',
  });
}

async function headersFor(request: APIRequestContext, who: string) {
  const login = await request.post(`${api}/api/v1/auth/login`, {
    headers: tenant,
    data: { identifier: who, password: PASSWORD },
  });
  const { access } = await login.json();
  return { ...tenant, Authorization: `Bearer ${access}` };
}

async function getJson(request: APIRequestContext, who: string, apiPath: string) {
  const response = await request.get(`${api}/api/v1/${apiPath}`, {
    headers: await headersFor(request, who),
  });
  expect(response.ok(), `${apiPath}: ${response.status()}`).toBeTruthy();
  return response.json();
}

async function mySessions(request: APIRequestContext) {
  const page = await getJson(request, techEmail, 'work-sessions?page_size=50');
  return (page.results ?? page) as {
    id: number;
    clock_out_at: string | null;
    flags: string[];
    place_name: string;
  }[];
}

/** The Home clock-in card is the only card on `/` with this heading. */
async function openHome(page: Page) {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: /^Clock in$|^Clocked in at/ })).toBeVisible({
    timeout: 20_000,
  });
}

async function pickSite(page: Page) {
  await page.getByRole('radio', { name: new RegExp(siteName) }).check();
}

test.describe('Clock-in on a phone', () => {
  test.beforeEach(({}, testInfo) => {
    test.skip(testInfo.project.name !== 'phone', 'a technician clocks in on a phone');
  });

  test('a technician, and a site with an area, on a project with a manager', async ({
    request,
  }) => {
    expect(
      runTool('make-user', {
        E2E_EMAIL: techEmail,
        E2E_PASSWORD: PASSWORD,
        E2E_NAME: techName,
      }),
    ).toContain('E2E_OK');

    const owner = await headersFor(request, PEOPLE.owner);
    const clients = await (
      await request.get(`${api}/api/v1/clients?page_size=5`, { headers: owner })
    ).json();
    const client = clients.results[0].id;
    const users = await (
      await request.get(`${api}/api/v1/users?page_size=100`, { headers: owner })
    ).json();
    const manager = users.results.find((u: { email: string }) => u.email === PEOPLE.manager);
    expect(manager).toBeTruthy();

    const site = await request.post(`${api}/api/v1/sites`, {
      headers: owner,
      data: {
        client,
        internal_ref: siteRef,
        name: siteName,
        latitude: String(CENTRE.latitude),
        longitude: String(CENTRE.longitude),
        radius_m: 200,
      },
    });
    expect(site.ok(), await site.text()).toBeTruthy();
    siteId = (await site.json()).id;

    const project = await request.post(`${api}/api/v1/projects`, {
      headers: owner,
      data: {
        client,
        po_number: `PO-${run}`,
        title: `E2E clock ${run}`,
        manager: manager.id,
        contract_value: '900000',
        cost_budget: '500000',
        sites: [siteId],
      },
    });
    expect(project.ok(), await project.text()).toBeTruthy();
  });

  test('with location off, clocking in is refused and says why', async ({ page, context }) => {
    await context.clearPermissions();
    await signIn(page, techEmail);
    await page.goto('/');
    await expect(page.getByText(/Turn location on to clock in/).first()).toBeVisible({
      timeout: 20_000,
    });
    await expect(page.getByRole('button', { name: /^Clock in at/ })).toHaveCount(0);
  });

  test('outside the area is refused with the distance; inside it succeeds; then clock out', async ({
    page,
    context,
    request,
  }) => {
    await context.grantPermissions(['geolocation']);
    await context.setGeolocation({ ...NORTH_640, accuracy });
    await signIn(page, techEmail);
    await openHome(page);

    // About 640 m away: the place is listed with how far, and the server refuses.
    await expect(
      page.getByText(new RegExp(`You are 6\\d\\d m from ${siteName} \\(limit 200 m\\)`)),
    ).toBeVisible();
    await pickSite(page);
    await page.getByRole('button', { name: /^Clock in at/ }).click();
    await expect(
      // The server names the place as "ref — name".
      page.getByRole('alert').filter({ hasText: new RegExp(`You are 6\\d\\d m from .*${siteName}`) }),
    ).toBeVisible();
    expect(await mySessions(request)).toHaveLength(0);

    // Standing at the site.
    await context.setGeolocation({ ...CENTRE, accuracy });
    await page.getByRole('button', { name: 'Read my position again' }).click();
    await expect(page.getByText(new RegExp(`You are \\d{1,3} m from ${siteName}$`))).toBeVisible();
    await pickSite(page);
    await page.getByRole('button', { name: /^Clock in at/ }).click();
    await expect(page.getByRole('heading', { name: `Clocked in at ${siteName}` })).toBeVisible();
    await expect(page.getByText(`E2E clock ${run}`)).toBeVisible();
    expect((await mySessions(request)).filter((s) => !s.clock_out_at)).toHaveLength(1);

    // Survives a reload: the open session is the server's answer.
    await page.reload();
    await expect(page.getByRole('heading', { name: `Clocked in at ${siteName}` })).toBeVisible();

    await page.getByRole('button', { name: 'Clock out' }).click();
    await expect(page.getByRole('heading', { name: 'Clocked out' })).toBeVisible();
    expect((await mySessions(request)).filter((s) => !s.clock_out_at)).toHaveLength(0);
  });

  test('offline clock-in and clock-out land flagged when the owner moved the area', async ({
    page,
    context,
    request,
  }) => {
    await context.grantPermissions(['geolocation']);
    await context.setGeolocation({ ...CENTRE, accuracy });
    await signIn(page, techEmail);

    // Signal still on: fill the offline bundle, as a phone does before the yard.
    await page.goto('/sync');
    await page.getByRole('button', { name: 'Send now' }).click();
    await expect(page.getByText(/^Up to date./)).toBeVisible({ timeout: 20_000 });
    await page.goto('/time');
    await expect(page.getByRole('heading', { name: 'My time' })).toBeVisible();
    await expect(page.getByRole('heading', { name: /^Clock in$/ })).toBeVisible();
    await expect(page.getByRole('radio', { name: new RegExp(siteName) })).toBeVisible();

    await context.setOffline(true);

    await pickSite(page);
    await page.getByRole('button', { name: /^Clock in at/ }).click();
    await expect(page.getByRole('heading', { name: `Clocked in at ${siteName}` })).toBeVisible();
    await expect(page.getByText(/Waiting to send/).first()).toBeVisible();

    await page.getByRole('button', { name: 'Clock out' }).click();
    await expect(page.getByRole('heading', { name: 'Clocked out' })).toBeVisible();
    await expect(page.getByText(/Waiting to send/).first()).toBeVisible();

    // Nothing has reached the server yet: still the one session from before.
    expect(await mySessions(request)).toHaveLength(1);

    // Meanwhile the owner moves the site's pin 300 m away: the phone's position
    // is now outside the current area but inside the one it held.
    const owner = await headersFor(request, PEOPLE.owner);
    const moved = await request.patch(`${api}/api/v1/sites/${siteId}`, {
      headers: owner,
      data: { latitude: String(CENTRE.latitude + 0.0027), longitude: String(CENTRE.longitude) },
    });
    expect(moved.ok(), await moved.text()).toBeTruthy();

    await context.setOffline(false);
    await page.goto('/sync');
    await page.getByRole('button', { name: 'Send now' }).click();
    await expect(page.getByText(/^Up to date./)).toBeVisible({ timeout: 30_000 });

    await expect
      .poll(async () => (await mySessions(request)).length, { timeout: 30_000 })
      .toBe(2);
    const sessions = await mySessions(request);
    expect(sessions.every((s) => s.clock_out_at)).toBeTruthy();
    expect(sessions.some((s) => s.flags.includes('AREA_CHANGED'))).toBeTruthy();

    await page.goto('/time');
    await expect(page.getByText(/Waiting to send/)).toHaveCount(0);
  });

  test('the day is formed and the manager rejects it with a reason', async ({ page }) => {
    // A day is routed once its date is over; the sweep does it hourly.
    expect(runTool('form-days', { E2E_DAYS_AHEAD: '2' })).toMatch(/formed=[1-9]/);

    await signIn(page, PEOPLE.manager);
    await page.goto('/approvals');
    await page.getByRole('button', { name: 'Days', exact: true }).click();
    await page.getByRole('button', { name: new RegExp(techName) }).click();

    const sheet = page.getByRole('dialog');
    await expect(sheet.getByText(siteName).first()).toBeVisible();
    // A reason is required to reject.
    await sheet.getByRole('button', { name: 'Reject' }).click();
    await expect(sheet.getByText('Say why you are rejecting it.').first()).toBeVisible();
    await sheet.getByLabel('Reason').fill('Start time looks too late');
    await sheet.getByRole('button', { name: 'Reject' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.getByRole('button', { name: new RegExp(techName) })).toHaveCount(0);
  });

  test('the person corrects the rejected day; the manager sees both and approves', async ({
    page,
    browser,
    request,
  }) => {
    await signIn(page, techEmail);
    await page.goto('/time');
    const row = page.getByRole('button', { name: /Rejected: Start time looks too late/ });
    await expect(row).toBeVisible({ timeout: 20_000 });
    await row.click();

    const sheet = page.getByRole('dialog');
    await expect(sheet.getByText('Rejected: Start time looks too late')).toBeVisible();
    await sheet.getByRole('button', { name: 'Add a correction' }).first().click();

    // Start 30 minutes earlier than recorded.
    const started = sheet.getByLabel('Started');
    const recorded = new Date(await started.inputValue());
    const earlier = new Date(recorded.getTime() - 30 * 60_000);
    const pad = (n: number) => String(n).padStart(2, '0');
    await started.fill(
      `${earlier.getFullYear()}-${pad(earlier.getMonth() + 1)}-${pad(earlier.getDate())}T${pad(earlier.getHours())}:${pad(earlier.getMinutes())}`,
    );
    // Required reason.
    await sheet.getByRole('button', { name: 'Send correction' }).click();
    await expect(sheet.getByText('Say why the time is being corrected.')).toBeVisible();
    await sheet.getByLabel('Why').fill('Phone had no signal when I arrived');
    await sheet.getByRole('button', { name: 'Send correction' }).click();
    await expect(sheet.getByText(/Corrected to/)).toBeVisible();

    // The same manager, in a phone of their own.
    const context = await browser.newContext({
      baseURL: process.env.E2E_BASE_URL ?? 'http://demo.localhost:5173',
      viewport: { width: 412, height: 915 },
    });
    const pm = await context.newPage();
    try {
      await signIn(pm, PEOPLE.manager);
      await pm.goto('/approvals');
      await pm.getByRole('button', { name: 'Days', exact: true }).click();
      await pm.getByRole('button', { name: new RegExp(techName) }).click();
      const pmSheet = pm.getByRole('dialog');
      await expect(pmSheet.getByText('Original:')).toBeVisible();
      await expect(pmSheet.getByText('Corrected:')).toBeVisible();
      await expect(pmSheet.getByText('Why: Phone had no signal when I arrived')).toBeVisible();
      await pmSheet.getByRole('button', { name: 'Approve' }).click();
      await expect(pm.getByRole('dialog')).toHaveCount(0);
    } finally {
      await context.close();
    }

    const days = await getJson(request, techEmail, 'work-days?page_size=20');
    const mine = (days.results as { status: string }[]) ?? [];
    expect(mine.length).toBeGreaterThan(0);
    expect(mine[0].status).toBe('APPROVED');
  });
});
