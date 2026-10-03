/**
 * Boxes end to end (Epic P, T11.18).
 *
 * The three moments a carton passes through, on a phone: it arrives at the
 * gate as one box of units (P1), it is asked for at gate-out by scanning the
 * box rather than each unit (P6), and at the gate the load is checked by the
 * scanner, which refuses what the pass does not cover and lets the rest go
 * short (P11, G1).
 *
 * The camera needs a real lens, so every "scan" here goes through the
 * scanner's own typed field — the same path a handheld scanner gun takes.
 */

import { expect, test, type APIRequestContext } from '@playwright/test';

import { chooseFirst, open, PASSWORD, PEOPLE, pickItem, signIn, unique } from './fixtures';

test.describe.configure({ mode: 'serial' });

const api = process.env.E2E_API_URL ?? 'http://127.0.0.1:8000';
const host = new URL(process.env.E2E_BASE_URL ?? 'http://demo.localhost:5173').hostname;

async function token(request: APIRequestContext, who: string) {
  const login = await request.post(`${api}/api/v1/auth/login`, {
    headers: { Host: host },
    data: { identifier: who, password: PASSWORD },
  });
  return ((await login.json()) as { access: string }).access;
}

async function get(request: APIRequestContext, who: string, path: string) {
  const access = await token(request, who);
  const response = await request.get(`${api}/api/v1${path}`, {
    headers: { Host: host, Authorization: `Bearer ${access}` },
  });
  return response;
}

const boxCode = unique('CTN');
const serials = ['-1', '-2', '-3'].map((suffix) => unique('BXU') + suffix);
let passId = '';

test.describe('A box from the gate to the truck', () => {
  test.beforeEach(({}, testInfo) => {
    test.skip(testInfo.project.name !== 'phone', 'the yard does this on a phone');
  });

  test('a carton of three units is received as one box', async ({ page, request }) => {
    await signIn(page, PEOPLE.storekeeper);
    await open(page, '/gate-in/new');
    await page.getByLabel('Source').selectOption('PURCHASE');
    await page.getByLabel('Supplier').fill('Huawei Kenya');
    await page.getByLabel('Received into').selectOption({ label: 'Main yard' });

    await page.getByRole('button', { name: 'Add a line' }).click();
    const sheet = page.getByRole('dialog');
    const serialized = await pickItem(sheet, 'Item', 'Baseband board');
    test.skip(!serialized, 'no serialized item in the seeded catalogue');

    // P1: start the box, then the units go into it as they are scanned.
    await sheet.getByRole('button', { name: 'Start a box' }).click();
    await sheet.getByLabel('Box code').fill(boxCode);
    await sheet.getByRole('button', { name: /^Start box/ }).click();
    await expect(sheet.getByLabel('Into a box')).toHaveValue(/.+/);

    for (const serial of serials) {
      await sheet.getByLabel('Serial number').fill(serial);
      await sheet.getByRole('button', { name: /^add$/i }).first().click();
    }
    await expect(sheet.getByText('3 units on this line')).toBeVisible();
    await sheet.getByRole('button', { name: 'Add line' }).click();
    await expect(sheet).toBeHidden();

    await page.getByRole('button', { name: /receive it|save on this device/i }).click();
    await expect(page.getByText(/GRN-\d+/).filter({ visible: true }).first()).toBeVisible({ timeout: 30_000 });

    // The box exists, at the yard, with the three units in it.
    const lookup = await get(request, PEOPLE.storekeeper, `/stock/lookup?q=${boxCode}`);
    expect(lookup.status()).toBe(200);
    const found = (await lookup.json()) as { kind: string; object: { units_now: number } };
    expect(found.kind).toBe('box');
    expect(found.object.units_now).toBe(3);
  });

  test('scanning the box at gate-out asks for all three units on one line', async ({ page }) => {
    await signIn(page, PEOPLE.storekeeper);
    await open(page, '/gate-out/new');
    await page.getByLabel('Out of').selectOption({ label: 'Main yard' });
    await page.getByLabel('Where it is going').selectOption({ label: 'A site' });
    await chooseFirst(page.getByLabel('A site'));
    await chooseFirst(page.getByLabel('Who is taking it'));

    await page.getByRole('button', { name: 'Add', exact: true }).click();
    const sheet = page.getByRole('dialog');
    await sheet.getByLabel('Scan a serial or a drum').fill(boxCode);
    await sheet.getByRole('button', { name: /^add$/i }).first().click();

    // P6: the proposal, then one line naming every unit.
    await expect(sheet.getByText(/: 3 units can go/)).toBeVisible({ timeout: 20_000 });
    await sheet.getByRole('button', { name: 'Add all' }).click();
    await expect(sheet).toBeHidden();
    for (const serial of serials) {
      await expect(page.getByText(serial).first()).toBeVisible();
    }

    await page.getByRole('button', { name: /send for approval/i }).click();
    await expect(page).toHaveURL(/\/gate-out\/\d+$/, { timeout: 30_000 });
    await expect(page.getByText(/GP-\d+/).filter({ visible: true }).first()).toBeVisible({ timeout: 30_000 });
    passId = page.url().match(/gate-out\/(\d+)/)?.[1] ?? '';
    expect(passId).not.toBe('');
  });

  test('at the gate the scanner refuses a stranger and releases short', async ({
    page,
    request,
  }) => {
    test.skip(!passId, 'no pass was raised');

    // The approval is not what this spec is about; a pass that routed for one
    // is approved here by somebody who may (the requester may not).
    const detail = await get(request, PEOPLE.approver, `/gate-outs/${passId}`);
    const pass = (await detail.json()) as { status: string };
    if (pass.status === 'PENDING_APPROVAL') {
      const access = await token(request, PEOPLE.approver);
      const approved = await request.post(`${api}/api/v1/gate-outs/${passId}/approve`, {
        headers: { Host: host, Authorization: `Bearer ${access}` },
        data: {},
      });
      expect(approved.status(), await approved.text()).toBeLessThan(300);
    }

    await signIn(page, PEOPLE.storekeeper);
    await open(page, `/gate-out/${passId}`);
    await page.getByRole('button', { name: 'Release at the gate' }).click();
    const sheet = page.getByRole('dialog');
    const scan = sheet.getByLabel('Scan or type a serial, box or pallet code');

    // P11: two of the three are loaded; something else is held up to the lens.
    for (const value of [serials[0], serials[1], 'NOT-ON-THIS-PASS-1']) {
      await scan.fill(value);
      await sheet.getByRole('button', { name: /^add$/i }).first().click();
    }
    await expect(sheet.getByText(/NOT-ON-THIS-PASS-1/).first()).toBeVisible();
    await expect(sheet.getByText(/is not on this pass/i).first()).toBeVisible();
    await expect(sheet.getByText(/2 of 3 scanned/)).toBeVisible();

    // G1: the third unit is short, with a reason, and the release still goes.
    await sheet.getByLabel('Why short').fill('Third unit not loaded — truck full.');
    await sheet.getByLabel(/vehicle/i).fill('KDA 123A');
    await sheet.getByLabel(/driver/i).fill('John Driver');
    await sheet.getByRole('button', { name: 'Release short' }).click();
    await expect(sheet).toBeHidden({ timeout: 30_000 });

    // Exactly the two scanned units left; the third is still in the box.
    const box = await get(request, PEOPLE.storekeeper, `/boxes/${boxCode}`);
    const body = (await box.json()) as { status: string; units: { serial_number: string }[] };
    expect(body.status).toBe('OPEN');
    expect(body.units.map((unit) => unit.serial_number)).toEqual([serials[2]]);
  });
});
