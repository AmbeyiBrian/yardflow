/**
 * Can you actually get there from a phone? (design §7.3; N-1, B4)
 *
 * The bottom bar fits five targets and an owner has twelve destinations. The bar
 * rendered `items.slice(0, 5)` and dropped the rest — so on a phone an owner
 * could not open Reports, Settings, Exceptions, Custody, Client stock, Quarantine
 * or Jobs *at all*. Every one of them sat in the sidebar, which is hidden below
 * `md`, so the gap was invisible to anyone testing on a laptop and total for
 * everyone working in a yard.
 *
 * The rule this pins: whatever the sidebar offers a role, a phone can reach too.
 * Not "the nav renders" — reachable, by tapping, with a finger.
 */

import { expect, test, type Page } from '@playwright/test';

import { PASSWORD, PEOPLE, signIn } from './fixtures';

/** Every nav destination a finger can currently reach. */
async function reachable(page: Page): Promise<string[]> {
  return page.evaluate(() =>
    [...document.querySelectorAll('nav a, aside a, [role="dialog"] a')]
      .filter((anchor) => {
        const box = anchor.getBoundingClientRect();
        return box.width > 0 && box.height > 0;
      })
      .map((anchor) => anchor.getAttribute('href') ?? '')
      .filter(Boolean),
  );
}

test.describe('phone navigation', () => {
  // These describe the phone layout, so they only mean anything in that project.
  test.beforeEach(() => {
    test.skip(test.info().project.name !== 'phone', 'about the phone layout');
  });

  test('every screen the role is allowed is reachable by tapping', async ({ page }) => {
    await signIn(page, PEOPLE.owner);

    // What the role is entitled to, read off the desktop sidebar — which is in
    // the DOM at every width, just hidden. That makes this a comparison against
    // the app's own answer rather than a list in a test that can drift.
    // `aside nav a` and not `aside a`: the sidebar footer also holds the
    // notification bell, which a phone reaches from its own header, so counting
    // it here would assert a tab for something that already has one.
    const entitled = await page.evaluate(() =>
      [...document.querySelectorAll('aside nav a')].map((a) => a.getAttribute('href') ?? ''),
    );
    expect(entitled.length).toBeGreaterThan(6);

    const tabs = await reachable(page);
    expect(tabs.length, 'the bar should not try to show everything').toBeLessThan(
      entitled.length,
    );

    await page.getByRole('button', { name: /^more$/i }).click();
    const withSheet = new Set(await reachable(page));

    const unreachable = entitled.filter((href) => !withSheet.has(href));
    expect(unreachable, 'these are in the sidebar but unreachable on a phone').toEqual([]);
  });

  test('a storekeeper reaches quarantine, which is not a tab', async ({ page }) => {
    // The concrete case behind the rule: J1/J2 exist so material cannot sit in
    // quarantine unnoticed, and the person who puts it there works on a phone.
    await signIn(page, PEOPLE.storekeeper);

    await page.getByRole('button', { name: /^more$/i }).click();
    await page.getByRole('dialog').getByRole('link', { name: /quarantine/i }).click();

    await expect(page).toHaveURL(/\/quarantine/);
  });

  test('the sheet gets out of the way once it has been used', async ({ page }) => {
    await signIn(page, PEOPLE.owner);
    const more = page.getByRole('button', { name: /^more$/i });

    // Navigating closes it. Otherwise it covers the screen it just opened and the
    // first thing you do on arriving is dismiss the menu.
    await more.click();
    await page.getByRole('dialog').getByRole('link', { name: /^reports$/i }).click();
    await expect(page).toHaveURL(/\/reports/);
    await expect(page.getByRole('dialog')).toBeHidden();

    // Tapping the backdrop closes it — the dismissal people try first.
    await more.click();
    await expect(page.getByRole('dialog')).toBeVisible();
    // Near the top: the backdrop spans the viewport but the sheet covers its
    // centre, which is where a plain `.click()` aims.
    await page.getByRole('button', { name: /close menu/i }).click({ position: { x: 30, y: 30 } });
    await expect(page.getByRole('dialog')).toBeHidden();

    // And Escape, for the keyboard.
    await more.click();
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog')).toBeHidden();
  });

  test('More stays tappable while the sheet is open', async ({ page }) => {
    await signIn(page, PEOPLE.owner);
    const more = page.getByRole('button', { name: /^more$/i });

    await more.click();
    await expect(page.getByRole('dialog')).toBeVisible();
    await expect(more).toHaveAttribute('aria-expanded', 'true');

    // The backdrop used to cover the bar, which left it looking disabled and put
    // the button that closes the sheet behind the overlay. Playwright refuses to
    // click an element something else would intercept, so this asserts the
    // layering as much as the toggle.
    await more.click();
    await expect(page.getByRole('dialog')).toBeHidden();
  });

  test('a role with few screens gets no More button at all', async ({ page }) => {
    // A technician has a handful of destinations; spending a slot on a menu that
    // holds one item would be worse than showing the item.
    await page.goto('/login');
    await page.getByLabel(/email or phone/i).fill(PEOPLE.technician);
    await page.getByLabel(/password/i).fill(PASSWORD);
    await page.getByRole('button', { name: /sign in/i }).click();
    await expect(page).not.toHaveURL(/\/login/);
    await page.getByRole('button', { name: /sign out/i }).first().waitFor();

    const entitled = await page.evaluate(
      () => document.querySelectorAll('aside nav a').length,
    );
    const tabs = await reachable(page);

    if (entitled <= 5) {
      await expect(page.getByRole('button', { name: /^more$/i })).toBeHidden();
      expect(tabs.length).toBe(entitled);
    } else {
      await expect(page.getByRole('button', { name: /^more$/i })).toBeVisible();
    }
  });
});

test.describe('desktop navigation', () => {
  test.beforeEach(() => {
    test.skip(test.info().project.name !== 'desktop', 'about the sidebar');
  });

  test('the sidebar shows everything, with no menu to open', async ({ page }) => {
    await signIn(page, PEOPLE.owner);

    const links = await reachable(page);
    expect(links.length).toBeGreaterThan(6);
    await expect(page.getByRole('button', { name: /^more$/i })).toBeHidden();
  });
});
