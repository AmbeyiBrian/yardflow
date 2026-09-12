/**
 * The product mark, everywhere it has to appear (T8.13).
 *
 * Worth a spec of its own for one reason: the app shipped for weeks with a
 * *different company's* logo in the browser tab — a purple mark left behind by
 * the project template that nobody looked at, because a favicon is the one asset
 * you stop seeing after the first day. A test that reads the bytes does not stop
 * seeing it.
 *
 * The rest is the ordinary requirement that the mark survive a reload on both a
 * phone and a desktop, since the two layouts show different forms of it: the
 * sidebar carries the wordmark and is hidden below `md`, so a phone gets the
 * compact mark in the header instead.
 */

import { expect, test } from '@playwright/test';

import { PEOPLE, signIn } from './fixtures';

/** Slate, from the design tokens. The mark is drawn on this. */
const SLATE = '#0f172a';
/** The template's purple, which must never come back. */
const NOT_OURS = '#863bff';

test.describe('the product mark', () => {
  test('names the login screen, and is still there after a reload', async ({ page }) => {
    await page.goto('/login');

    // The mark *is* the heading — it replaced a text `h1`, so the accessible
    // name has to come with it or the page loses its only landmark.
    const heading = page.getByRole('heading', { level: 1 });
    await expect(heading).toHaveAccessibleName(/yardflow/i);
    await expect(heading.locator('svg')).toBeVisible();

    // Vector, not an image element: nothing to 404 and nothing to load late.
    await expect(heading.locator('svg path')).toHaveAttribute('d', /^M/);

    await page.reload();
    await expect(page.getByRole('heading', { level: 1 })).toHaveAccessibleName(
      /yardflow/i,
    );
  });

  test('appears in the shell, and survives a refresh there too', async ({ page }, info) => {
    await signIn(page, PEOPLE.storekeeper);

    // The two layouts show different forms of the mark, so the locator has to
    // differ too. Both elements are always in the DOM — the sidebar is hidden
    // below `md` rather than unmounted — which is why this picks by layout
    // instead of taking the first match and hoping.
    const onPhone = info.project.name === 'phone';
    const mark = onPhone
      ? // The compact Y, deliberately unlabelled: the company name beside it is
        // what a screen reader should read first, so there is no accessible name
        // to locate it by.
        page.locator('header svg').first()
      : page.locator('aside').getByRole('img', { name: 'YardFlow' });

    await expect(mark).toBeVisible();

    const box = await mark.boundingBox();
    expect(box, 'the mark has to occupy actual space').not.toBeNull();
    expect(box!.height).toBeGreaterThan(10);
    // Shape is the cheap way to tell the two apart: the wordmark is about 4.4
    // times as wide as it is tall, the compact Y is roughly square. If either
    // comes back as the other, the wrong one is rendering.
    const aspect = box!.width / box!.height;
    if (onPhone) {
      expect(aspect).toBeLessThan(1.5);
    } else {
      expect(aspect).toBeGreaterThan(3);
    }

    await page.reload();
    await expect(mark).toBeVisible();
  });

  test('is what you wait on, instead of a generic spinner', async ({ page }) => {
    await signIn(page, PEOPLE.storekeeper);

    // Hold the response so the loading state stays put long enough to inspect.
    await page.route('**/api/v1/gate-outs**', async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 4_000));
      await route.fallback();
    });
    void page.goto('/gate-out').catch(() => {});

    const loading = page.getByRole('status').first();
    await expect(loading).toBeVisible();
    // Still announced, because a mark that says nothing to a screen reader is a
    // worse spinner than the ring it replaced.
    await expect(loading).toHaveAccessibleName(/loading/i);

    // The letterform, not a bordered box spun with CSS. Both the wordmark and the
    // compact Y open with the same Y outline, so one assertion covers either.
    await expect(loading.locator('svg path').first()).toHaveAttribute('d', /^M232\.7/);
  });

  test('is what the browser tab and the installed app actually load', async ({ page }) => {
    await page.goto('/login');

    /**
     * Fetch from inside the page, not through `request`.
     *
     * The tenant is addressed by subdomain (§2.2) and Node cannot resolve
     * `demo.localhost` — only the browser treats every `*.localhost` as
     * loopback. Playwright's `request` fixture runs in Node, so it fails on the
     * very hostname the app is served from. Going through the page also means
     * these are the same requests the browser itself would make.
     */
    const fetchText = (url: string) =>
      page.evaluate(async (target) => {
        const response = await fetch(target);
        return { ok: response.ok, status: response.status, body: await response.text() };
      }, url);

    // The tab icon the document points at, followed to its bytes.
    const href = await page.locator('link[rel="icon"]').getAttribute('href');
    expect(href).toBe('/favicon.svg');

    const favicon = await fetchText(href!);
    expect(favicon.ok, `favicon.svg answered ${favicon.status}`).toBeTruthy();
    expect(favicon.body).toContain(SLATE);
    expect(
      favicon.body.toLowerCase(),
      'the template favicon is back — check public/favicon.svg',
    ).not.toContain(NOT_OURS);

    // Android's install prompt needs raster icons declared, or the home-screen
    // shortcut falls back to a screenshot of whatever page was open.
    const manifestHref =
      (await page.locator('link[rel="manifest"]').getAttribute('href')) ??
      '/manifest.webmanifest';
    const manifest = await fetchText(manifestHref);
    expect(manifest.ok, `the manifest answered ${manifest.status}`).toBeTruthy();
    const icons: Array<{ src: string; purpose?: string }> =
      JSON.parse(manifest.body).icons ?? [];
    expect(icons.length).toBeGreaterThan(0);
    expect(icons.some((icon) => icon.purpose === 'maskable')).toBeTruthy();

    // And each declared icon exists. A manifest naming a missing PNG installs a
    // blank icon, which is the failure this whole spec is about.
    for (const icon of icons) {
      const response = await fetchText(icon.src);
      expect(response.ok, `${icon.src} is declared but missing`).toBeTruthy();
    }
  });
});
