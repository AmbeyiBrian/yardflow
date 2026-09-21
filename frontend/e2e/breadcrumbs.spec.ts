/**
 * Breadcrumbs, and the one queue they lead back to (§7.6; Epic P).
 *
 * Two changes are covered here because they are the same change: navigation
 * that tells you where you are. The trail is only honest if the structure above
 * it is, and for one day it was not — the project queues were a second screen
 * asking for the same act as Approvals, so a manager had two places to check.
 *
 * This project has no unit runner, so the pure parts of the trail — parents,
 * placeholder, permission — are exercised here rather than in isolation. Where
 * that makes an assertion indirect it is said so in the test.
 */

import { expect, test } from '@playwright/test';

import { PASSWORD, PEOPLE, open, signIn } from './fixtures';

const api = process.env.E2E_API_URL ?? 'http://127.0.0.1:8000';
const host = new URL(process.env.E2E_BASE_URL ?? 'http://demo.localhost:5173').hostname;
const tenant = { Host: host };

async function authed(request: import('@playwright/test').APIRequestContext, who: string) {
  const login = await request.post(`${api}/api/v1/auth/login`, {
    headers: tenant,
    data: { identifier: who, password: PASSWORD },
  });
  return { ...tenant, Authorization: `Bearer ${(await login.json()).access}` };
}

/** Any project, so the trail has a record to name. */
async function someProject(request: import('@playwright/test').APIRequestContext) {
  const headers = await authed(request, PEOPLE.owner);
  const projects = await (
    await request.get(`${api}/api/v1/projects?page_size=1`, { headers })
  ).json();
  return projects.results[0] as { id: number; reference: string } | undefined;
}

test.describe('Breadcrumbs', () => {
  test('a detail screen says where it sits, and the crumb goes there', async ({
    page,
    request,
  }, testInfo) => {
    // Desktop only: on a phone the trail collapses to the parent by design
    // (P4), which the next test covers on its own terms.
    test.skip(testInfo.project.name !== 'desktop', 'full trail is a wide-screen thing');

    const project = await someProject(request);
    test.skip(!project, 'no project seeded — run the project spec first');

    await signIn(page, PEOPLE.owner);
    await open(page, `/projects/${project!.id}`);

    const trail = page.getByRole('navigation', { name: 'Breadcrumb' });
    await expect(trail).toBeVisible();

    // P3: the record's own reference, never the id out of the URL. This is also
    // what catches a placeholder that is never replaced. Narrowed to the
    // visible copy: the phone markup is in the DOM too, hidden by CSS.
    await expect(
      trail.getByText(project!.reference, { exact: true }).filter({ visible: true }),
    ).toBeVisible({ timeout: 20_000 });

    // P4: you are here, so the last crumb is not a link.
    await expect(trail.locator('[aria-current="page"]')).toHaveText(project!.reference);

    await trail.getByRole('link', { name: 'Projects' }).click();
    await expect(page).toHaveURL(/\/projects$/);
  });

  test('on a phone it is one link to the level above', async ({ page, request }, testInfo) => {
    // P4: a full trail does not fit at 360px, and the crumb people use on a
    // phone is the one that goes back.
    test.skip(testInfo.project.name !== 'phone', 'the collapse is a phone thing');

    const project = await someProject(request);
    test.skip(!project, 'no project seeded — run the project spec first');

    await signIn(page, PEOPLE.owner);
    await open(page, `/projects/${project!.id}`);

    const trail = page.getByRole('navigation', { name: 'Breadcrumb' });
    const links = trail.getByRole('link').filter({ visible: true });
    await expect(links).toHaveCount(1);
    await expect(links.first()).toHaveText(/Projects/);

    await links.first().click();
    await expect(page).toHaveURL(/\/projects$/);
  });

  test('a top-level screen has no trail', async ({ page }) => {
    // P5: `Home › Projects` under a sidebar entry reading Projects is noise.
    await signIn(page, PEOPLE.owner);
    await open(page, '/projects');

    await expect(page.getByRole('navigation', { name: 'Breadcrumb' })).toHaveCount(0);
  });

  test('the expense form sits under the project it was opened from', async ({
    page,
    request,
  }, testInfo) => {
    test.skip(testInfo.project.name !== 'desktop', 'full trail is a wide-screen thing');

    // The interesting case for P2: there is no `/expenses` list to derive a
    // parent from, so a trail built by chopping the URL would invent one.
    const project = await someProject(request);
    test.skip(!project, 'no project seeded — run the project spec first');

    await signIn(page, PEOPLE.owner);
    await open(page, `/expenses/new?project=${project!.id}`);

    // Both markups are in the DOM with one hidden by CSS (P4), so every locator
    // here is narrowed to what is actually on screen.
    const trail = page.getByRole('navigation', { name: 'Breadcrumb' });
    await expect(trail.getByRole('link', { name: 'Projects' })).toBeVisible();
    await expect(
      trail.getByText(project!.reference, { exact: true }).filter({ visible: true }),
    ).toBeVisible({ timeout: 20_000 });
    await expect(trail.locator('[aria-current="page"]')).toHaveText('Record an expense');
  });
});

test.describe('Leaving a screen that names its crumb', () => {
  // The bug this guards: `useCrumb` and the crumb provider fed each other a
  // never-ending stream of state updates from above the router's outlet, and a
  // navigation transition starved under it — the URL changed, the old screen
  // stayed. Intermittent, so each case tries the round trip more than once.

  test('a project detail lets you go to Stock', async ({ page, request }) => {
    const project = await someProject(request);
    test.skip(!project, 'no project seeded — run the project spec first');

    await signIn(page, PEOPLE.owner);
    for (let attempt = 0; attempt < 3; attempt += 1) {
      await open(page, `/projects/${project!.id}`);
      await expect(page.getByRole('heading', { level: 1 })).toContainText(
        project!.reference,
        { timeout: 20_000 },
      );

      await page.getByRole('link', { name: /^stock$/i }).first().click();

      await expect(page).toHaveURL(/\/stock$/);
      await expect(page.getByRole('heading', { level: 1 })).toHaveText(/stock/i, {
        timeout: 15_000,
      });
    }
  });

  test('a report lets you go back to the catalogue', async ({ page }) => {
    await signIn(page, PEOPLE.owner);
    for (let attempt = 0; attempt < 3; attempt += 1) {
      await open(page, '/reports/stock-on-hand');
      await expect(page.getByRole('heading', { level: 1 })).toHaveText(/stock on hand/i, {
        timeout: 20_000,
      });

      await page.getByRole('link', { name: /all reports/i }).first().click();

      await expect(page).toHaveURL(/\/reports$/);
      await expect(page.getByRole('heading', { level: 1 })).toHaveText(/^reports$/i, {
        timeout: 15_000,
      });
    }
  });
});

test.describe('Approvals is the one queue', () => {
  test('the project decisions are tabs on it', async ({ page }) => {
    // They were briefly a screen of their own, which meant checking two lists
    // for the same act.
    await signIn(page, PEOPLE.manager);
    await open(page, '/approvals');

    await expect(page.getByRole('button', { name: 'Expenses' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Closeout costs' })).toBeVisible();
  });

  test('the old address still works', async ({ page }) => {
    // Redirected rather than deleted: it was in the sidebar, so it may be in a
    // bookmark or in a notification already sent.
    await signIn(page, PEOPLE.manager);
    await open(page, '/my-projects');

    await expect(page).toHaveURL(/\/approvals$/);
  });

  test('there is one entry in the sidebar, not two', async ({ page }) => {
    await signIn(page, PEOPLE.manager);

    await expect(page.getByRole('link', { name: 'Waiting on you' })).toHaveCount(0);
  });
});
