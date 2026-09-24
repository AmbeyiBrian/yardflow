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

/** The pane a tab's content lives in; it follows the finger and slides in on change. */
const PANE = '[data-tab-pane]';

/**
 * A one-finger horizontal swipe across `selector`, as a real touch screen sends
 * it: a start, a few moves along the way, and an end. `to` may be given to end
 * the finger somewhere other than the far side — a short drag that lets go.
 */
async function swipe(
  page: Page,
  selector: string,
  direction: 'left' | 'right',
  { distance, moves = 4 }: { distance?: number; moves?: number } = {},
) {
  const box = await page.locator(selector).first().boundingBox();
  if (!box) throw new Error(`nothing to swipe at ${selector}`);
  const y = box.y + Math.min(box.height / 2, 300);
  const from = direction === 'left' ? box.x + box.width * 0.8 : box.x + box.width * 0.2;
  const travel = distance ?? box.width * 0.6;
  const to = direction === 'left' ? from - travel : from + travel;
  await page.evaluate(
    ({ selector, from, to, y, moves }) => {
      const el = document.querySelector(selector) as HTMLElement;
      const touch = (x: number) =>
        new Touch({ identifier: 1, target: el, clientX: x, clientY: y, pageX: x, pageY: y });
      const fire = (type: string, x: number, down: boolean) =>
        el.dispatchEvent(
          new TouchEvent(type, {
            bubbles: true,
            touches: down ? [touch(x)] : [],
            changedTouches: [touch(x)],
          }),
        );
      fire('touchstart', from, true);
      for (let step = 1; step <= moves; step += 1) {
        fire('touchmove', from + ((to - from) * step) / moves, true);
      }
      fire('touchend', to, false);
    },
    { selector, from, to, y, moves },
  );
}

/** Whether `inner` sits entirely inside `outer`, sideways. */
async function withinSideways(page: Page, outer: string, inner: string) {
  const [o, i] = await Promise.all([
    page.locator(outer).boundingBox(),
    page.locator(inner).boundingBox(),
  ]);
  if (!o || !i) return false;
  return i.x >= o.x - 1 && i.x + i.width <= o.x + o.width + 1;
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

    await site.selectOption('__add_new__');

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
    // Moved one tab to the right: Closeout costs is now selected, and its
    // pane is the one on screen.
    await expect(page.getByRole('button', { name: 'Closeout costs' })).toHaveAttribute(
      'aria-current',
      'page',
    );
    await expect(page.locator('[data-tab-pane="closeouts"]')).toBeVisible();

    await swipe(page, SWIPE_SURFACE, 'right');
    await expect(page.getByRole('button', { name: 'Expenses' })).toHaveAttribute(
      'aria-current',
      'page',
    );
  });

  test('the pane follows the finger, and springs back from a short drag', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'phone', 'a thumb gesture is a phone thing');

    await signIn(page, PEOPLE.manager);
    await open(page, '/approvals');
    await expect(page.locator(PANE)).toBeVisible();

    // A finger part-way across: the content has moved with it. This is what
    // makes it feel like a swipe rather than a tap that happened sideways.
    await page.evaluate((selector) => {
      const el = document.querySelector(selector) as HTMLElement;
      const t = (x: number) =>
        new Touch({ identifier: 1, target: el, clientX: x, clientY: 300, pageX: x, pageY: 300 });
      el.dispatchEvent(new TouchEvent('touchstart', { bubbles: true, touches: [t(300)], changedTouches: [t(300)] }));
      el.dispatchEvent(new TouchEvent('touchmove', { bubbles: true, touches: [t(260)], changedTouches: [t(260)] }));
    }, SWIPE_SURFACE);
    await expect(page.locator(PANE)).toHaveAttribute('style', /translateX\(-40px\)/);

    // Let go short of the threshold: nothing changes and the pane settles back.
    await page.evaluate((selector) => {
      const el = document.querySelector(selector) as HTMLElement;
      const t = (x: number) =>
        new Touch({ identifier: 1, target: el, clientX: x, clientY: 300, pageX: x, pageY: 300 });
      el.dispatchEvent(new TouchEvent('touchend', { bubbles: true, touches: [], changedTouches: [t(270)] }));
    }, SWIPE_SURFACE);
    await expect(page.getByRole('button', { name: 'Expenses' })).toHaveAttribute('aria-current', 'page');
    await expect(page.locator(PANE)).not.toHaveAttribute('style', /translateX/);
  });

  test('a long strip scrolls to keep the selected tab in view', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'phone', 'only a phone is too narrow for nine panes');

    // An owner sees every settings pane — far more than a phone is wide.
    await signIn(page, PEOPLE.owner);
    await open(page, '/settings');
    const strip = 'nav[aria-label="Settings"]';
    const active = `${strip} a[aria-current="page"]`;
    await expect(page.locator(active)).toBeVisible();
    const overflows = await page
      .locator(strip)
      .evaluate((el) => el.scrollWidth > el.clientWidth + 8);
    test.skip(!overflows, 'this owner has too few panes to overflow the strip');

    // Swipe to the far end; each time, the highlighted tab must be on screen.
    for (let step = 0; step < 4; step += 1) {
      const before = await page.locator(active).textContent();
      await swipe(page, SWIPE_SURFACE, 'left');
      await expect(page.locator(active)).not.toHaveText(before ?? '');
      await expect.poll(() => withinSideways(page, strip, active)).toBe(true);
    }
    const scrolled = await page.locator(strip).evaluate((el) => el.scrollLeft);
    expect(scrolled).toBeGreaterThan(0);
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
    // And the pane never moved: a vertical drag is never a half-swipe.
    await expect(page.locator(PANE)).not.toHaveAttribute('style', /translateX/);
  });
});
