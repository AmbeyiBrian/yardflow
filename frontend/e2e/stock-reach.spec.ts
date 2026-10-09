/**
 * Stock within reach on a phone (E7, E8, T14.8).
 *
 * The owner could not find the scanner or the item search, because on a phone
 * Stock lived under More. Now it is a tab, Home answers "do we have it?", and
 * Approvals waits under More instead.
 */

import { expect, test } from '@playwright/test';

import { PASSWORD, PEOPLE, signIn } from './fixtures';

const api = process.env.E2E_API_URL ?? 'http://127.0.0.1:8000';
const host = new URL(process.env.E2E_BASE_URL ?? 'http://demo.localhost:5173').hostname;

test.describe('Stock within reach', () => {
  test.beforeEach(({}, testInfo) => {
    test.skip(testInfo.project.name !== 'phone', 'this is about the phone bar');
  });

  test('Stock is a tab, and Approvals is under More', async ({ page }) => {
    await signIn(page, PEOPLE.owner);
    const bar = page.getByRole('navigation').filter({ has: page.getByRole('button', { name: /^more/i }) });
    await expect(bar.getByRole('link', { name: 'Stock' })).toBeVisible();
    await expect(bar.getByRole('link', { name: 'Approvals' })).toHaveCount(0);

    await page.getByRole('button', { name: /^more/i }).click();
    await page.getByRole('dialog').getByRole('link', { name: /approvals/i }).click();
    await expect(page).toHaveURL(/\/approvals/);
  });

  test('typing a name on Home lands on Stock filtered to that item', async ({ page }) => {
    await signIn(page, PEOPLE.owner);
    await page.getByLabel('Find stock').fill('Baseband');
    const row = page.getByRole('button', { name: /baseband board/i }).first();
    await expect(row).toBeVisible({ timeout: 20_000 });
    await row.click();

    await expect(page).toHaveURL(/\/stock\?item=\d+/);
    await expect(page.getByRole('combobox', { name: 'Item' })).toHaveValue(/baseband/i, { timeout: 20_000 });
  });

  test('a serial and Enter on Home opens that unit', async ({ page, request }) => {
    const login = await request.post(`${api}/api/v1/auth/login`, {
      headers: { Host: host },
      data: { identifier: PEOPLE.owner, password: PASSWORD },
    });
    const { access } = (await login.json()) as { access: string };
    const serials = (await (
      await request.get(`${api}/api/v1/serials?page_size=1`, {
        headers: { Host: host, Authorization: `Bearer ${access}` },
      })
    ).json()) as { results: { serial_number: string }[] };
    const serial = serials.results[0]?.serial_number;
    test.skip(!serial, 'the demo tenant has no serialized units');

    await signIn(page, PEOPLE.owner);
    await page.getByLabel('Find stock').fill(serial!);
    await page.getByRole('button', { name: 'Find', exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/stock/serials/${encodeURIComponent(serial!)}`), {
      timeout: 20_000,
    });
  });

  test('Home shows the yard at a glance', async ({ page }) => {
    await signIn(page, PEOPLE.owner);
    await expect(page.getByText('In the yard', { exact: true })).toBeVisible({ timeout: 20_000 });
  });
});
