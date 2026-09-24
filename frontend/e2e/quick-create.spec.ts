/**
 * "Add new …" inside a form, and swiping between tabs (§7.3).
 *
 * Both exist for the same person: somebody on a phone, mid-task, who should
 * not have to leave what they are doing. The first covers a storekeeper who is
 * raising a job for a site that is not on the system yet — before this they
 * abandoned the form, went to Settings → Network, created it, and started over.
 * The second covers a manager working through three approval queues with one
 * thumb.
 */

import { expect, test, type Page } from '@playwright/test';

import { PEOPLE, open, signIn } from './fixtures';

const api = process.env.E2E_API_URL ?? 'http://127.0.0.1:8000';
const host = new URL(process.env.E2E_BASE_URL ?? 'http://demo.localhost:5173').hostname;

async function firstProjectId(request: import('@playwright/test').APIRequestContext) {
  const login = await request.post(`${api}/api/v1/auth/login`, {
    headers: { Host: host },
    data: { identifier: PEOPLE.owner, password: process.env.E2E_PASSWORD ?? 'yardflow-demo-password' },
  });
  const { access } = await login.json();
  const projects = await (
    await request.get(`${api}/api/v1/projects?page_size=1`, {
      headers: { Host: host, Authorization: `Bearer ${access}` },
    })
  ).json();
  return projects.results[0]?.id as number | undefined;
}

/** The element a screen's swipe handlers are attached to: its wrapper inside <main>. */
const SWIPE_SURFACE = 'main > div.flex.flex-col.gap-4';

/** A one-finger horizontal swipe across `locator`, as a real touch screen sends it. */
async function swipe(page: Page, selector: string, direction: 'left' | 'right') {
  const box = await page.locator(selector).first().boundingBox();
  if (!box) throw new Error(`nothing to swipe at ${selector}`);
  const y = box.y + Math.min(box.height / 2, 300);
  const from = direction === 'left' ? box.x + box.width * 0.8 : box.x + box.width * 0.2;
  const to = direction === 'left' ? box.x + box.width * 0.2 : box.x + box.width * 0.8;
  await page.evaluate(
    ({ selector, from, to, y }) => {
      const el = document.querySelector(selector) as HTMLElement;
      const touch = (x: number) =>
        new Touch({ identifier: 1, target: el, clientX: x, clientY: y, pageX: x, pageY: y });
      el.dispatchEvent(
        new TouchEvent('touchstart', { bubbles: true, touches: [touch(from)], changedTouches: [touch(from)] }),
      );
      el.dispatchEvent(
        new TouchEvent('touchend', { bubbles: true, touches: [], changedTouches: [touch(to)] }),
      );
    },
    { selector, from, to, y },
  );
}

test.describe('Add new … from inside a form', () => {
  test('the last option adds a site without leaving the job form', async ({ page, request }) => {
    const projectId = await firstProjectId(request);
    test.skip(!projectId, 'no project seeded — run the project spec first');

    await signIn(page, PEOPLE.owner);
    await open(page, `/projects/${projectId}`);
    await page.getByRole('button', { name: /add a job/i }).click();

    const jobSheet = page.getByRole('dialog').first();
    const site = jobSheet.getByLabel('Site');
    // Last, so it reads as an escape hatch rather than a real site.
    await expect(site.locator('option').last()).toHaveText(/add new site/i);

    // Something already typed must survive the detour.
    await jobSheet.getByLabel('Who is responsible').selectOption({ label: 'Tom Technician' });

    await site.selectOption({ label: /add new site/i });

    // The site sheet opens on top; the job sheet stays underneath.
    const siteSheet = page.getByRole('dialog').last();
    await expect(siteSheet).toContainText(/site/i);
    const name = `E2E Quick Site ${Date.now().toString().slice(-6)}`;
    await siteSheet.getByLabel(/^name$/i).fill(name);
    await siteSheet.getByLabel(/client/i).selectOption({ index: 1 });
    await siteSheet.getByRole('button', { name: /create|save|add/i }).first().click();

    // Back on the job form: the new site is selected, and nothing was lost.
    await expect(site.locator('option:checked')).toHaveText(new RegExp(name), { timeout: 20_000 });
    await expect(jobSheet.getByLabel('Who is responsible').locator('option:checked')).toHaveText(
      /Tom Technician/,
    );
    // And the sentinel never became the field's value.
    await expect(site).not.toHaveValue('__add_new__');
  });

  test('somebody who may not create one does not see the option', async ({ page, request }) => {
    const projectId = await firstProjectId(request);
    test.skip(!projectId, 'no project seeded — run the project spec first');

    // A storekeeper can raise a job but has no catalogue.manage.
    await signIn(page, PEOPLE.storekeeper);
    await open(page, `/projects/${projectId}`);
    await page.getByRole('button', { name: /add a job/i }).click();

    const site = page.getByRole('dialog').first().getByLabel('Site');
    await expect(site.locator('option', { hasText: /add new/i })).toHaveCount(0);
  });
});

test.describe('Swiping between tabs', () => {
  test('a swipe moves to the next approval queue', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'phone', 'a thumb gesture is a phone thing');

    await signIn(page, PEOPLE.manager);
    await open(page, '/approvals');

    // Starts on the first queue this manager can act on.
    const strip = page.getByRole('button', { name: 'Expenses' });
    await expect(strip).toBeVisible();

    // The handlers sit on the page's wrapper *inside* <main>, and a touch
    // bubbles up, not down — dispatched on <main> it would never arrive.
    await swipe(page, SWIPE_SURFACE, 'left');
    // Moved one tab to the right: Closeout costs is now selected.
    await expect(page.getByRole('button', { name: 'Closeout costs' })).toHaveClass(/bg-slate-900/);

    await swipe(page, SWIPE_SURFACE, 'right');
    await expect(page.getByRole('button', { name: 'Expenses' })).toHaveClass(/bg-slate-900/);
  });

  test('a swipe that is mostly vertical is a scroll, not a tab change', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'phone', 'a thumb gesture is a phone thing');

    await signIn(page, PEOPLE.manager);
    await open(page, '/approvals');
    const before = await page.locator('button.bg-slate-900').first().textContent();

    await page.evaluate(() => {
      const el = document.querySelector('main > div.flex.flex-col.gap-4') as HTMLElement;
      const t = (x: number, y: number) =>
        new Touch({ identifier: 1, target: el, clientX: x, clientY: y, pageX: x, pageY: y });
      el.dispatchEvent(new TouchEvent('touchstart', { bubbles: true, touches: [t(200, 100)], changedTouches: [t(200, 100)] }));
      el.dispatchEvent(new TouchEvent('touchend', { bubbles: true, touches: [], changedTouches: [t(140, 400)] }));
    });

    await expect(page.locator('button.bg-slate-900').first()).toHaveText(before ?? '');
  });
});
