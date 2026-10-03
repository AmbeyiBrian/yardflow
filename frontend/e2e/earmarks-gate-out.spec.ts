/**
 * Site-first gate-out and diversions (Epic Q, T13.8).
 *
 * Two units are received for site A. A request to site A lists them ticked and
 * takes the one left ticked. A request to site B for the other is refused until
 * it says why it is using site A's material, and then goes.
 */

import { expect, test } from '@playwright/test';

import { chooseFirst, open, PASSWORD, PEOPLE, signIn, unique } from './fixtures';

const api = process.env.E2E_API_URL ?? 'http://127.0.0.1:8000';
const host = new URL(process.env.E2E_BASE_URL ?? 'http://demo.localhost:5173').hostname;

test.describe('A gate-out that starts from the site', () => {
  test.beforeEach(({}, testInfo) => {
    test.skip(testInfo.project.name !== 'phone', 'the yard does this on a phone');
  });

  test('takes the earmarked unit, and asks why for another site’s', async ({ page, request }) => {
    const login = await request.post(`${api}/api/v1/auth/login`, {
      headers: { Host: host },
      data: { identifier: PEOPLE.owner, password: PASSWORD },
    });
    const { access } = (await login.json()) as { access: string };
    const headers = { Host: host, Authorization: `Bearer ${access}` };

    const items = (await (
      await request.get(`${api}/api/v1/item-types?page_size=200`, { headers })
    ).json()) as { results: { id: number; name: string; uom: string }[] };
    const item = items.results.find((row) => /baseband board/i.test(row.name));
    const locations = (await (
      await request.get(`${api}/api/v1/locations?page_size=50`, { headers })
    ).json()) as { results: { id: number; name: string; type: string }[] };
    const yard =
      locations.results.find((row) => row.name === 'Main yard') ??
      locations.results.find((row) => row.type === 'YARD');
    const sites = (await (
      await request.get(`${api}/api/v1/sites?page_size=5`, { headers })
    ).json()) as { results: { id: number; name: string; internal_ref: string }[] };
    const [siteA, siteB] = sites.results;
    test.skip(!item || !yard || !siteA || !siteB, 'the demo tenant lacks an item, yard or two sites');

    const first = unique('EGO') + '-1';
    const second = unique('EGO') + '-2';
    const gateIn = await request.post(`${api}/api/v1/gate-ins`, {
      headers,
      data: {
        source_type: 'PURCHASE',
        supplier_name: 'E2E earmark',
        to_location: yard!.id,
        for_site: siteA.id,
        received_at: new Date().toISOString(),
        lines: [
          {
            item_type: item!.id,
            tracking_mode: 'SERIALIZED',
            quantity: '2',
            uom: item!.uom,
            condition: 'NEW',
            serials: [{ serial_number: first }, { serial_number: second }],
          },
        ],
      },
    });
    expect(gateIn.ok()).toBeTruthy();
    const posted = await request.post(`${api}/api/v1/gate-ins/${(await gateIn.json()).id}/post`, {
      headers,
      data: {},
    });
    expect(posted.ok()).toBeTruthy();

    const siteLabel = (site: { name: string; internal_ref: string }) =>
      `${site.internal_ref} · ${site.name}`;

    async function startRequest(site: { name: string; internal_ref: string }) {
      await open(page, '/gate-out/new');
      await page.getByLabel('Out of').selectOption({ label: yard!.name });
      await page.getByLabel('Where it is going').selectOption({ label: 'A site' });
      await page.getByLabel('A site').selectOption({ label: siteLabel(site) });
      await chooseFirst(page.getByLabel('Who is taking it'));
    }

    // The job question: asked only when the site's open jobs span projects.
    async function chooseJobIfAsked() {
      const which = page.getByLabel('Which job');
      if (await which.count()) await chooseFirst(which);
    }

    // No project to choose from, any more.
    await signIn(page, PEOPLE.storekeeper);
    await open(page, '/gate-out/new');
    await expect(
      page.getByLabel('Where it is going').locator('option', { hasText: 'A project' }),
    ).toHaveCount(0);

    // Site A: both units are waiting, ticked.
    await startRequest(siteA);
    await expect(page.getByText(`Waiting for ${siteA.name}`)).toBeVisible({ timeout: 20_000 });
    await expect(page.getByRole('checkbox', { name: first })).toBeChecked();
    await expect(page.getByRole('checkbox', { name: second })).toBeChecked();

    await page.getByRole('checkbox', { name: second }).uncheck();
    await page.getByRole('button', { name: 'Add ticked' }).click();
    await expect(page.getByText(first).first()).toBeVisible();
    await expect(page.getByText(second)).toHaveCount(0);
    await chooseJobIfAsked();

    await page.getByRole('button', { name: /send for approval/i }).click();
    await expect(page).toHaveURL(/\/gate-out\/\d+$/, { timeout: 30_000 });
    await expect(page.getByText(/GP-\d+/).filter({ visible: true }).first()).toBeVisible({
      timeout: 30_000,
    });

    // Site B: nothing is earmarked for it; the other unit is added by serial.
    await startRequest(siteB);
    await expect(page.getByText(/^Nothing is earmarked for /)).toBeVisible({ timeout: 20_000 });
    await page.getByRole('button', { name: 'Add', exact: true }).click();
    const sheet = page.getByRole('dialog');
    await sheet.getByLabel('Scan a serial or a drum').fill(second);
    await sheet.getByRole('button', { name: /^add$/i }).first().click();
    await expect(sheet.getByText(second).first()).toBeVisible({ timeout: 20_000 });
    // The item's details arrive after the scan; the label gains its unit then.
    await expect(sheet.getByLabel(/^How much \(/)).toBeVisible();
    await sheet.getByRole('button', { name: 'Add', exact: true }).last().click();
    await expect(sheet).toBeHidden();
    await chooseJobIfAsked();

    // Refused: it is site A's, and a reason is needed.
    await page.getByRole('button', { name: /send for approval/i }).click();
    const reason = page.getByLabel('Why is it going here instead?').filter({ visible: true });
    await expect(reason).toBeVisible({ timeout: 20_000 });
    await expect(page.getByRole('alert').filter({ hasText: siteA.name }).first()).toBeVisible();

    await reason.fill('Site A was postponed');
    await page.getByRole('button', { name: /send for approval/i }).click();
    await expect(page).toHaveURL(/\/gate-out\/\d+$/, { timeout: 30_000 });
    const number = page.getByText(/GP-\d+/).filter({ visible: true }).first();
    await expect(number).toBeVisible({ timeout: 30_000 });

    // The detail says so, for whoever decides.
    await expect(page.getByText(`Diverted from ${siteA.name}`)).toBeVisible();
    await expect(page.getByText(/Site A was postponed/)).toBeVisible();
  });
});
