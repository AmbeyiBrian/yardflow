/**
 * Finance stage 1 on a phone (Epic R, T15.11; design §4.17.13).
 *
 * A technician with no signal registers a casual, records a casual-labour
 * expense with two photos and asks for a transport allowance twice with
 * overlapping dates. Back online the first request lands, the second is refused
 * for overlap and stays on the phone with the reason, and "Fix and resend" with
 * clear dates sends it. The project manager then approves the expense, Finance
 * approves it and marks it paid, and the project's cost rises once.
 *
 * Offline, the way a phone really does it: everything the forms need is fetched
 * while there is still signal (the Sync page's "Send now" fills the offline
 * bundle) and every screen is opened once so its code is loaded, then the
 * network is cut and the person moves about the app by tapping, never by
 * reloading — a reload with no network would lose the app itself, which is a
 * different problem from the one under test.
 */

import { type APIRequestContext, type Page, expect, test } from '@playwright/test';

import { PASSWORD, PEOPLE, open, signIn, unique } from './fixtures';

test.describe.configure({ mode: 'serial' });

const api = process.env.E2E_API_URL ?? 'http://127.0.0.1:8000';
const host = new URL(process.env.E2E_BASE_URL ?? 'http://demo.localhost:5173').hostname;
const tenant = { Host: host };

// A 1x1 PNG: a real image, so the upload is accepted as one.
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64',
);
const photo = (name: string) => ({ name, mimeType: 'image/png', buffer: PNG });

/** Carried between the steps. */
const run = unique('FIN');
const siteName = `E2E Finance Site ${run}`;
const siteRef = `FS-${run}`;
const casualName = `Casual ${run}`;
const casualId = `E2E${Date.now().toString().slice(-8)}`;
// A distinctive amount, so the approval and payment rows can be found by it.
const amount = 3000 + (Date.now() % 900);
const money = (value: number) =>
  `KES ${value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
// Allowance dates are per person across all runs, so each run takes its own
// distant window instead of colliding with the last one.
const base = new Date(Date.UTC(2032, 0, 1) + (Date.now() % 20000) * 4 * 86_400_000);
const day = (offset: number) =>
  new Date(base.getTime() + offset * 86_400_000).toISOString().slice(0, 10);

let projectId = 0;
let siteId = 0;
let expenseId = 0;

async function headersFor(request: APIRequestContext, who: string) {
  const login = await request.post(`${api}/api/v1/auth/login`, {
    headers: tenant,
    data: { identifier: who, password: PASSWORD },
  });
  const { access } = await login.json();
  return { ...tenant, Authorization: `Bearer ${access}` };
}

async function getJson(request: APIRequestContext, who: string, path: string) {
  const response = await request.get(`${api}/api/v1/${path}`, {
    headers: await headersFor(request, who),
  });
  expect(response.ok(), `${path}: ${response.status()}`).toBeTruthy();
  return response.json();
}

async function expenses(request: APIRequestContext) {
  return (await getJson(request, PEOPLE.owner, `project-expenses?project=${projectId}&page_size=50`))
    .results as { id: number; status: string; amount: string; payment_reference?: string }[];
}

async function projectCost(request: APIRequestContext): Promise<number> {
  const performance = await getJson(request, PEOPLE.owner, `projects/${projectId}/performance`);
  return Number(performance.expenses);
}

async function myRequests(request: APIRequestContext) {
  return (await getJson(request, PEOPLE.technician, 'allowance-requests?mine=true&page_size=200'))
    .results as { id: number; from_date: string; to_date: string; status: string }[];
}

/** Choose the casual whose label carries `name`: queued ones have a `q:` value. */
async function pickCasual(page: Page, name: string) {
  const select = page.getByLabel('Casual 1');
  await expect(select.locator('option', { hasText: name })).toHaveCount(1, { timeout: 15_000 });
  const value = await select.locator('option', { hasText: name }).getAttribute('value');
  await select.selectOption(value ?? '');
}

async function fillRequest(page: Page, from: string, to: string, reason: string) {
  await page.getByLabel('From', { exact: true }).fill(from);
  await page.getByLabel('To', { exact: true }).fill(to);
  await page.getByLabel('Amount', { exact: true }).fill('500');
  await page.getByLabel('Reason').fill(reason);
}

test.describe('Money, stage 1, on a phone', () => {
  test.beforeEach(({}, testInfo) => {
    test.skip(testInfo.project.name !== 'phone', 'a technician does this on a phone');
  });

  test('a project with a manager, on a site of its own', async ({ request }) => {
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
      // R13: a site needs coordinates now.
      data: { client, internal_ref: siteRef, name: siteName, latitude: '-1.286389', longitude: '36.817223' },
    });
    expect(site.ok(), await site.text()).toBeTruthy();
    siteId = (await site.json()).id;

    const project = await request.post(`${api}/api/v1/projects`, {
      headers: owner,
      data: {
        client,
        po_number: `PO-${run}`,
        title: `E2E finance ${run}`,
        manager: manager.id,
        contract_value: '900000',
        cost_budget: '500000',
        sites: [siteId],
      },
    });
    expect(project.ok(), await project.text()).toBeTruthy();
    projectId = (await project.json()).id;
  });

  // One test, not three: the queue lives in this phone's IndexedDB, and each
  // Playwright test gets a fresh browser context — a new phone.
  test('offline capture, then sync, refusal and a fixed resend', async ({
    page,
    context,
    request,
  }) => {
    await signIn(page, PEOPLE.technician);

    // Signal still on: fill the offline bundle, then open each screen once so
    // its code is loaded. Done by tapping, as the person would.
    await open(page, '/sync');
    await page.getByRole('button', { name: 'Send now' }).click();
    await expect(page.getByText(/^Up to date./)).toBeVisible({ timeout: 20_000 });

    await page.goto('/money');
    for (const [link, heading] of [
      ['Add casual', 'Add a casual'],
      ['Request allowance', 'Request an allowance'],
      ['Record expense', 'Record an expense'],
    ] as const) {
      await page.getByRole('link', { name: link, exact: true }).click();
      await expect(page.getByRole('heading', { name: heading })).toBeVisible();
      await page.goBack();
      await expect(page.getByRole('heading', { name: 'Money', exact: true })).toBeVisible();
    }

    await context.setOffline(true);

    // 1. The casual, with the photo of the ID.
    await page.getByRole('link', { name: 'Add casual', exact: true }).click();
    await page.getByLabel('Name', { exact: true }).fill(casualName);
    await page.getByLabel('ID number').fill(casualId);
    await page.getByLabel('Phone').fill('0712345678');
    await page.locator('input[type=file]').setInputFiles(photo('id.png'));
    await expect(page.getByRole('img', { name: 'ID' })).toBeVisible();
    await page.getByRole('button', { name: 'Add casual' }).click();
    await expect(page.getByText(/Saved on this phone/)).toBeVisible();
    await page.getByRole('button', { name: 'Done' }).click();
    await expect(page.getByText(casualName)).toBeVisible();
    await expect(page.getByText('Waiting to send').first()).toBeVisible();

    // 2. The expense: casual labour naming that casual, with two photos.
    await page.getByRole('link', { name: 'Record expense', exact: true }).click();
    await page.getByLabel('Site', { exact: true }).selectOption({ label: `${siteRef} ${siteName}` });
    await expect(page.getByText(`E2E finance ${run}`)).toBeVisible();
    await page.getByLabel('What kind').selectOption({ label: 'Casual labour' });
    await pickCasual(page, casualName);
    await page.getByLabel('Days worked 1').fill('3');
    await page.getByLabel('Amount', { exact: true }).fill(String(amount));
    await page.getByLabel('Scope of work').fill('Clearing the compound');
    await page.getByLabel('What for').fill(`Labour ${run}`);
    await page.locator('input[type=file]').setInputFiles([photo('one.png'), photo('two.png')]);
    await expect(page.getByRole('img', { name: 'Receipt' })).toHaveCount(2);
    await page.getByRole('button', { name: 'Record it' }).click();
    await expect(page.getByText(/Saved on this phone/)).toBeVisible();
    await expect(page.getByText('Waiting to send').first()).toBeVisible();

    // 3. Transport, within Nairobi, twice with overlapping dates.
    for (const [from, to, reason] of [
      [day(0), day(2), `First ${run}`],
      [day(1), day(3), `Second ${run}`],
    ]) {
      await page.getByRole('link', { name: 'Request allowance', exact: true }).click();
      await page.getByLabel('What for', { exact: true }).selectOption({ label: 'Transport' });
      await page.getByLabel('Where to').selectOption('WITHIN_NAIROBI');
      await fillRequest(page, from, to, reason);
      await page
        .getByLabel('Site', { exact: true })
        .selectOption({ label: `${siteRef} ${siteName}` });
      await page.getByRole('button', { name: 'Send request' }).click();
      await expect(page.getByText(/Saved on this phone/)).toBeVisible();
    }
    await page.getByRole('button', { name: 'Requests and floats' }).click();
    await expect(page.getByText('Waiting to send')).toHaveCount(2);

    // Back online: the app drains the queue by itself.
    await context.setOffline(false);

    await expect
      .poll(async () => (await expenses(request)).length, { timeout: 60_000 })
      .toBe(1);
    expenseId = (await expenses(request))[0].id;

    const casuals = await getJson(request, PEOPLE.owner, `casuals?search=${casualId}`);
    expect(casuals.results.length).toBe(1);

    // The expense has both photos, and the casual its ID photo.
    await expect
      .poll(
        async () =>
          (
            await getJson(
              request,
              PEOPLE.owner,
              `attachments?target_type=commercials.ProjectExpense&target_id=${expenseId}`,
            )
          ).results?.length ?? 0,
        { timeout: 60_000 },
      )
      .toBe(2);
    await expect
      .poll(
        async () =>
          (
            await getJson(
              request,
              PEOPLE.owner,
              `attachments?target_type=commercials.Casual&target_id=${casuals.results[0].id}`,
            )
          ).results?.length ?? 0,
        { timeout: 60_000 },
      )
      .toBe(1);

    // The first request landed; the second did not.
    await expect
      .poll(async () => (await myRequests(request)).filter((r) => r.from_date === day(0)).length, {
        timeout: 60_000,
      })
      .toBe(1);
    expect((await myRequests(request)).filter((r) => r.from_date === day(1))).toHaveLength(0);

    // And it stays on the phone with the reason.
    await page.getByRole('link', { name: 'Money', exact: true }).first().click();
    await page.getByRole('button', { name: 'Requests and floats' }).click();
    await expect(page.getByText(/ALLOWANCE_OVERLAP|overlap/i).first()).toBeVisible({
      timeout: 20_000,
    });
    await expect(page.getByRole('link', { name: 'Fix and resend' })).toBeVisible();

    // Fixing the dates sends it.
    await page.getByRole('link', { name: 'Fix and resend' }).click();
    await expect(page.getByLabel('Reason')).toHaveValue(`Second ${run}`);
    await fillRequest(page, day(10), day(11), `Second ${run}`);
    await page.getByRole('button', { name: 'Fix and resend' }).click();

    await expect
      .poll(async () => (await myRequests(request)).filter((r) => r.from_date === day(10)).length, {
        timeout: 60_000,
      })
      .toBe(1);
  });

  test('the project manager approves the expense', async ({ page, request }) => {
    const before = await projectCost(request);
    await signIn(page, PEOPLE.manager);
    await open(page, '/approvals');
    await page.getByRole('button', { name: 'Expenses', exact: true }).click();
    await page.getByText(money(amount)).filter({ visible: true }).first().click();
    const sheet = page.getByRole('dialog');
    await expect(sheet.getByText(siteName)).toBeVisible();
    // Both photos are shown to the approver.
    await expect(sheet.getByRole('img')).toHaveCount(2, { timeout: 20_000 });
    await sheet.getByRole('button', { name: 'Approve' }).click();
    await expect(sheet).toBeHidden();

    await expect
      .poll(async () => (await expenses(request))[0].status, { timeout: 20_000 })
      .toBe('PENDING_FINANCE');
    // Not cost yet: it reaches the project only on Finance's approval.
    expect(await projectCost(request)).toBe(before);
  });

  test('Finance approves and marks it paid, and the cost rises once', async ({ page, request }) => {
    const before = await projectCost(request);
    await signIn(page, PEOPLE.owner);

    await open(page, '/approvals');
    await page.getByRole('button', { name: 'Expenses', exact: true }).click();
    await page.getByText(money(amount)).filter({ visible: true }).first().click();
    await page.getByRole('dialog').getByRole('button', { name: 'Approve' }).click();
    await expect(page.getByRole('dialog')).toBeHidden();
    await expect
      .poll(async () => (await expenses(request))[0].status, { timeout: 20_000 })
      .toBe('APPROVED');
    expect(await projectCost(request)).toBe(before + amount);

    await open(page, '/money/to-pay');
    await page.getByText(money(amount)).filter({ visible: true }).first().click();
    const sheet = page.getByRole('dialog');
    await sheet.getByLabel('Payment reference').fill(`MPESA${run}`);
    await sheet.getByRole('button', { name: 'Mark paid' }).click();
    await expect(sheet).toBeHidden();

    await expect
      .poll(async () => (await expenses(request))[0].status, { timeout: 20_000 })
      .toBe('PAID');
    expect((await expenses(request))[0].payment_reference).toBe(`MPESA${run}`);
    // Paid is still cost, and only once.
    expect(await projectCost(request)).toBe(before + amount);
  });
});
