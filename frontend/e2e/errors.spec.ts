/**
 * What a failure says to the person reading it (§6.1; N-1).
 *
 * The gate-in screen once showed a red banner reading "Request failed (404)." — a
 * status code, in front of a storekeeper, on a screen that otherwise said
 * "Nothing received yet". It named neither what had happened nor what to do, and
 * the empty list underneath it implied the yard was empty when in fact the
 * request had failed.
 *
 * Every error our own API raises arrives as `{"error": {"code", "message"}}` and
 * that message is what gets shown. This is about the responses that carry no
 * message: a proxy page, a gateway timeout, a stale tab asking for a URL that no
 * longer routes. Those get a sentence written for a person.
 */

import { expect, test } from '@playwright/test';

import { PEOPLE, open, signIn } from './fixtures';

test.describe('when a request fails', () => {
  test('a bare 404 is explained, not printed as a status code', async ({ page }) => {
    await signIn(page, PEOPLE.storekeeper);

    // A 404 with a body the app cannot read — which is what a proxy or a
    // no-longer-routed URL returns, and what produced the original report.
    await page.route('**/api/v1/gate-ins**', (route) =>
      route.fulfill({ status: 404, contentType: 'text/html', body: '<h1>Not Found</h1>' }),
    );

    await page.goto('/gate-in');

    const banner = page.getByRole('alert').filter({ hasText: /not available|reload/i }).first();
    await expect(banner).toBeVisible();
    // The specific regression: no raw status codes on screen.
    await expect(page.locator('body')).not.toContainText('Request failed');
    await expect(page.locator('body')).not.toContainText('(404)');
  });

  test("our API's own message is preferred over the generic one", async ({ page }) => {
    await signIn(page, PEOPLE.storekeeper);

    // The one 404 this endpoint really can produce: cursor pagination answers
    // `NotFound` for a cursor it cannot decode. When the API has something
    // specific to say, that is what should be shown.
    await page.route('**/api/v1/gate-ins**', (route) =>
      route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ error: { code: 'NOT_FOUND', message: 'Invalid cursor' } }),
      }),
    );

    await page.goto('/gate-in');

    await expect(page.getByRole('alert').filter({ hasText: 'Invalid cursor' })).toBeVisible();
  });

  test('a server error says the entry is not lost', async ({ page }) => {
    await signIn(page, PEOPLE.storekeeper);

    await page.route('**/api/v1/gate-ins**', (route) =>
      route.fulfill({ status: 500, contentType: 'text/html', body: 'boom' }),
    );

    await page.goto('/gate-in');

    await expect(
      page.getByRole('alert').filter({ hasText: /server had a problem/i }),
    ).toBeVisible();
  });
});

test.describe('A screen that failed to load', () => {
  test('does not read as an empty yard', async ({ page }) => {
    /**
     * The three states of a list were confusable. The error banner rendered
     * *alongside* the list, so a failed request drew "Nothing received yet."
     * underneath it — and the commonest reading of a broken screen was that
     * the yard was empty. A storekeeper who believes that goes looking for
     * material that is sitting right there.
     */
    await signIn(page, PEOPLE.storekeeper);

    // The request fails the way a flaky connection fails: no response.
    await page.route('**/api/v1/gate-ins?**', (route) => route.abort('failed'));
    await open(page, '/gate-in');

    // Waiting for the state rather than reading the page straight away: the
    // client retries a failed request a few times before it gives up, which is
    // the right behaviour and takes a moment.
    await expect(page.getByRole('button', { name: /try again/i })).toBeVisible({
      timeout: 30_000,
    });

    const body = await page.locator('main').innerText();
    expect(body).not.toContain('Nothing received yet');
    expect(body).toMatch(/not a sign that there is nothing here/i);
  });

  test('recovers when the connection does', async ({ page }) => {
    await signIn(page, PEOPLE.storekeeper);

    // Failing until the test says otherwise. Aborting only the *first* attempt
    // proves nothing: the client retries on its own and would recover without
    // the button ever being pressed.
    let offline = true;
    await page.route('**/api/v1/gate-ins?**', (route) =>
      offline ? route.abort('failed') : route.continue(),
    );
    await open(page, '/gate-in');
    await expect(page.getByRole('button', { name: /try again/i })).toBeVisible({
      timeout: 30_000,
    });

    offline = false;
    await page.getByRole('button', { name: /try again/i }).click();

    // The list, or an honest empty state — either is fine; the error is not.
    await expect(page.getByRole('button', { name: /try again/i })).toBeHidden({
      timeout: 20_000,
    });
  });
});
