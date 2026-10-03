/**
 * Earmarks on the unit page (Epic Q, T13.9): a unit received for a site says
 * so, and the earmark can be cleared with a reason, without moving the stock.
 */

import { expect, test } from '@playwright/test';

import { open, PASSWORD, PEOPLE, signIn, unique } from './fixtures';

const api = process.env.E2E_API_URL ?? 'http://127.0.0.1:8000';
const host = new URL(process.env.E2E_BASE_URL ?? 'http://demo.localhost:5173').hostname;

test.describe('An earmark on a unit', () => {
  test.beforeEach(({}, testInfo) => {
    test.skip(testInfo.project.name !== 'phone', 'the yard does this on a phone');
  });

  test('is shown, then cleared with a reason', async ({ page, request }) => {
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
    ).json()) as { results: { id: number; type: string }[] };
    const yard = locations.results.find((row) => row.type === 'YARD');
    const sites = (await (
      await request.get(`${api}/api/v1/sites?page_size=5`, { headers })
    ).json()) as { results: { id: number; name: string }[] };
    const site = sites.results[0];
    test.skip(!item || !yard || !site, 'the demo tenant lacks a serialized item, yard or site');

    const serial = unique('EMK');
    const gateIn = await request.post(`${api}/api/v1/gate-ins`, {
      headers,
      data: {
        source_type: 'PURCHASE',
        supplier_name: 'E2E earmark',
        to_location: yard!.id,
        for_site: site.id,
        received_at: new Date().toISOString(),
        lines: [
          {
            item_type: item!.id,
            tracking_mode: 'SERIALIZED',
            quantity: '1',
            uom: item!.uom,
            condition: 'NEW',
            serials: [{ serial_number: serial }],
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

    await signIn(page, PEOPLE.owner);
    await open(page, `/stock/serials/${encodeURIComponent(serial)}`);
    await expect(page.getByText(`Earmarked for ${site.name}`)).toBeVisible({ timeout: 20_000 });

    await page.getByRole('button', { name: 'Change earmark' }).click();
    const sheet = page.getByRole('dialog');
    await sheet.getByLabel('For site').selectOption({ label: 'No site' });
    await sheet.getByLabel('Why').fill('Plan changed');
    await sheet.getByRole('button', { name: 'Save' }).click();

    await expect(page.getByText('Not earmarked')).toBeVisible({ timeout: 20_000 });
  });
});
