/**
 * A tab that was open when a new version was deployed (T8.13; §7.1).
 *
 * Every screen is code-split, so the loaded page holds chunk URLs from *its*
 * build. Deploy again and those filenames change — the next tap on Approvals,
 * Gate-out or Gate-in asks for a file that no longer exists, gets a 404, and
 * React's `lazy` rejects.
 *
 * That used to unmount the whole tree and leave a blank white screen: no message,
 * nothing to tap, and no reason to suspect a reload would fix it. On a phone in a
 * yard it is indistinguishable from a dead app, and it happened on the three
 * screens people use most.
 *
 * Two behaviours are pinned here, because the first one is invisible when it
 * works and the second is the safety net under it:
 *
 * 1. A stale chunk reloads itself and the screen opens.
 * 2. A chunk that is *really* gone shows a message and a button — never a blank
 *    page, and never a reload loop.
 */

import { expect, test } from '@playwright/test';

import { PEOPLE, signIn } from './fixtures';

/** The gate-out screen's chunk, by any name the dev server or a build gives it. */
const GATE_OUT_CHUNK = '**/*GateOutPages*';

test.describe('when the app was updated while this tab was open', () => {
  test('the screen reloads itself and opens', async ({ page }) => {
    await signIn(page, PEOPLE.storekeeper);

    // 404 once — which is what a chunk from the previous build does — then serve
    // normally, which is what the reload finds.
    let served = false;
    await page.route(GATE_OUT_CHUNK, async (route) => {
      if (served) return route.fallback();
      served = true;
      await route.fulfill({ status: 404, body: 'Not Found' });
    });

    await page.goto('/gate-out');

    // The recovery is a reload, so give it the navigation and then look for the
    // real screen rather than the error.
    await expect(page.getByRole('heading', { name: /gate.?out/i }).first()).toBeVisible({
      timeout: 30_000,
    });
    expect(served, 'the chunk should have been requested and refused once').toBe(true);
    await expect(page.getByText(/newer version is available/i)).toBeHidden();
  });

  test('a chunk answered with the app shell still recovers', async ({ page }) => {
    // What production actually did. The SPA catch-all sent index.html — 200,
    // text/html — for a chunk that no longer existed, so the failure was not a
    // 404 but a module that turned out to be a web page. The detector only knew
    // the 404 wording; the import rejected with something else; the transition
    // kept the old screen up with the new URL in the bar; and it looked stuck.
    await signIn(page, PEOPLE.storekeeper);

    let served = false;
    await page.route(GATE_OUT_CHUNK, async (route) => {
      if (served) return route.continue();
      served = true;
      await route.fulfill({
        status: 200,
        contentType: 'text/html; charset=utf-8',
        body: '<!doctype html><html><body>the shell, not the chunk</body></html>',
      });
    });

    await page.getByRole('link', { name: /gate.?out/i }).first().click();

    await expect(page.getByRole('heading', { name: /gate.?out/i }).first()).toBeVisible({
      timeout: 20_000,
    });
    expect(served, 'the chunk should have been requested and answered wrongly once').toBe(true);
    await expect(page.getByText(/newer version is available/i)).toBeHidden();
  });

  test('a chunk that is really gone gets a message, not a blank page', async ({ page }) => {
    await signIn(page, PEOPLE.storekeeper);

    // Never serves. One reload is allowed, then it has to give up and say so.
    await page.route(GATE_OUT_CHUNK, (route) =>
      route.fulfill({ status: 404, body: 'Not Found' }),
    );

    await page.goto('/gate-out');

    await expect(page.getByText(/newer version is available/i)).toBeVisible({
      timeout: 30_000,
    });
    // The two things that actually recover, both reachable.
    await expect(page.getByRole('button', { name: /^reload$/i })).toBeVisible();
    await expect(page.getByRole('button', { name: /home screen/i })).toBeVisible();

    // And it settles rather than reloading forever: the guard is one reload per
    // session, so the message is still there a moment later.
    await page.waitForTimeout(3_000);
    await expect(page.getByText(/newer version is available/i)).toBeVisible();
  });
});
