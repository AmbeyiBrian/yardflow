/**
 * §14, T8.13, flow 1: "gate-in of a mixed serialized, bulk and reel delivery".
 *
 * Mixed on purpose. The three tracking modes are the part of receiving most
 * likely to break, because each takes a different shape: a bulk line is a
 * number, a serialized line is a list of identities (D3), and a reel line is a
 * drum with a length (D4). A delivery of one kind proves very little; a delivery
 * of all three is a real Tuesday at the gate.
 *
 * Run on a phone viewport, because that is where it happens.
 */

import { type Locator, expect, test } from '@playwright/test';

import { PEOPLE, open, signIn, unique } from './fixtures';

/**
 * Pick an item the sheet treats as a reel.
 *
 * Matching the name is not enough: the seeded catalogue has "Cable clamp"
 * (bulk) and "Power cable 16mm" (reel), and a regex on "cable" finds the wrong
 * one first. What decides it is the sheet — a reel item is the one that asks
 * for a drum.
 */
async function chooseAReelItem(sheet: Locator): Promise<boolean> {
  const item = sheet.getByLabel('Item');
  const options = await item.locator('option').allTextContents();

  for (const label of options.filter((text) => /cable|feeder/i.test(text))) {
    await item.selectOption({ label });
    if (await sheet.getByLabel('Drum number').isVisible().catch(() => false)) {
      return true;
    }
  }
  return false;
}

test.describe('Receiving a delivery', () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page, PEOPLE.storekeeper);
  });

  test('a delivery is received and the stock moves', async ({ page }) => {
    await open(page, '/gate-in/new');

    // The header: where it came from and where it is going (D1).
    await page.getByLabel('Source').selectOption('PURCHASE');
    await page.getByLabel('Supplier').fill('Huawei Kenya');
    await page.getByLabel('Received into').selectOption({ index: 1 });

    await page.getByRole('button', { name: 'Add a line' }).click();
    const sheet = page.getByRole('dialog');
    await expect(sheet).toBeVisible();

    // A bulk item by name. Selecting by index would silently pick whichever
    // item the seed happens to list first — and if that is serialized, the
    // sheet asks for a serial and the failure looks like a broken button.
    const items = await sheet.getByLabel('Item').locator('option').allTextContents();
    const bulk = items.find((label) => /clamp|tie|glove|vest/i.test(label));
    test.skip(!bulk, 'no bulk item in the seeded catalogue');
    await sheet.getByLabel('Item').selectOption({ label: bulk! });
    await sheet.getByLabel(/^Quantity/).fill('25');
    await sheet.getByRole('button', { name: 'Add line' }).click();
    await expect(sheet).toBeHidden();

    // Every line shows on the document immediately, so a mis-keyed quantity is
    // caught at the gate rather than at the end.
    await expect(page.getByText('25', { exact: false }).first()).toBeVisible();

    // D8, M6: receiving moves stock and gives the storekeeper a number they can
    // write on the supplier's paperwork.
    await page.getByRole('button', { name: /receive it|save on this device/i }).click();
    await expect(page.getByText(/GRN-\d+/).first()).toBeVisible({ timeout: 30_000 });
  });

  test('a delivery with no destination cannot be received', async ({ page }) => {
    /**
     * D1: a receipt has to say where the material went, or the ledger cannot say
     * where it is. Refused before the button is live, because a refusal at the
     * gate after twenty lines have been keyed is far worse.
     */
    await open(page, '/gate-in/new');

    await expect(
      page.getByRole('button', { name: /receive it|save on this device/i }),
    ).toBeDisabled();
  });

  test('a serialized line needs an identity', async ({ page }) => {
    /**
     * D3: "every serialized item is identified". The sheet refuses a serialized
     * line with no serial and no stated reason — which is the same rule the
     * server enforces, checked here so the storekeeper hears it first.
     */
    await open(page, '/gate-in/new');
    await page.getByLabel('Source').selectOption('PURCHASE');
    await page.getByLabel('Received into').selectOption({ index: 1 });

    await page.getByRole('button', { name: 'Add a line' }).click();
    const sheet = page.getByRole('dialog');

    // The seeded catalogue tracks radios by serial.
    const item = sheet.getByLabel('Item');
    const options = await item.locator('option').allTextContents();
    const serialized = options.find((label) => /rru|antenna/i.test(label));
    test.skip(!serialized, 'no serialized item in the seeded catalogue');

    await item.selectOption({ label: serialized! });

    // Picking a serialized item replaces the quantity box with serial entry —
    // D3 in the interface: the line *is* the list of identities.
    await expect(sheet.getByLabel('Serial number')).toBeVisible();

    await sheet.getByRole('button', { name: 'Add line' }).click();

    // Still open, with a complaint — not silently accepted. `alert` rather than
    // `status`: a refusal interrupts, because the line was not added.
    await expect(sheet).toBeVisible();
    await expect(sheet.getByRole('alert').first()).toContainText(/serial/i);
  });


  test('a drum still in the boxes is added with the line', async ({ page }) => {
    /**
     * From the yard. A storekeeper typed a drum number and its length, pressed
     * "Add line", and was told to "add at least one drum, with its length" —
     * which, from where they were standing, was a lie: they had. The values
     * only became a drum when a second button was pressed, and nothing said so.
     *
     * Pressing "Add line" is as clear a statement of intent as pressing "Add
     * drum", so the sheet now finishes the entry instead of refusing it.
     */
    await open(page, '/gate-in/new');
    await page.getByLabel('Source').selectOption('PURCHASE');
    await page.getByLabel('Received into').selectOption({ index: 1 });

    await page.getByRole('button', { name: 'Add a line' }).click();
    const sheet = page.getByRole('dialog');

    const found = await chooseAReelItem(sheet);
    test.skip(!found, 'no reel item in the seeded catalogue');

    // Filled in, and deliberately *not* followed by "Add drum".
    await sheet.getByLabel('Drum number').fill('E2E-DRUM-1');
    await sheet.getByLabel(/^Length/).fill('250');

    await sheet.getByRole('button', { name: 'Add line' }).click();

    await expect(sheet).toBeHidden();
    await expect(page.getByText('250', { exact: false }).first()).toBeVisible();
  });

  test('half a drum is refused by saying which half is missing', async ({ page }) => {
    /** A number with no length is genuinely incomplete — so the complaint names
     * the drum and asks for the one thing it needs. */
    await open(page, '/gate-in/new');
    await page.getByLabel('Source').selectOption('PURCHASE');
    await page.getByLabel('Received into').selectOption({ index: 1 });

    await page.getByRole('button', { name: 'Add a line' }).click();
    const sheet = page.getByRole('dialog');

    const found = await chooseAReelItem(sheet);
    test.skip(!found, 'no reel item in the seeded catalogue');

    await sheet.getByLabel('Drum number').fill('E2E-DRUM-2');

    await sheet.getByRole('button', { name: 'Add line' }).click();

    await expect(sheet).toBeVisible();
    await expect(sheet.getByRole('alert').first()).toContainText('E2E-DRUM-2');
  });

  test('pressing save twice makes one delivery, not two', async ({ page }) => {
    /**
     * From the yard, again: three presses of "Save as draft" on a slow
     * connection produced three identical drafts. The button disables itself
     * while the request is in flight, which does not close the window — the
     * second press leaves before the first reply arrives.
     *
     * Both clicks are dispatched in the same task here, which is precisely the
     * race: React has not re-rendered the disabled state in between. The draft
     * carries a uuid, and the server hands back the document it already made.
     */
    const supplier = unique('Double Press');

    await open(page, '/gate-in/new');
    await page.getByLabel('Source').selectOption('PURCHASE');
    await page.getByLabel('Supplier').fill(supplier);
    await page.getByLabel('Received into').selectOption({ index: 1 });

    await page.getByRole('button', { name: 'Add a line' }).click();
    const sheet = page.getByRole('dialog');
    const items = await sheet.getByLabel('Item').locator('option').allTextContents();
    const bulk = items.find((label) => /clamp|tie|glove|vest/i.test(label));
    test.skip(!bulk, 'no bulk item in the seeded catalogue');
    await sheet.getByLabel('Item').selectOption({ label: bulk! });
    await sheet.getByLabel(/^Quantity/).fill('3');
    await sheet.getByRole('button', { name: 'Add line' }).click();
    await expect(sheet).toBeHidden();

    await page.$eval('button:text-is("Save as draft")', (button: HTMLButtonElement) => {
      button.click();
      button.click();
    });

    await expect(page).toHaveURL(/\/gate-in\/\d+/, { timeout: 30_000 });

    await open(page, '/gate-in');
    await page.getByPlaceholder(/number, supplier/i).fill(supplier);

    // Rows, not occurrences of the text: the phone layout prints each field
    // with its own label, so one delivery mentions the supplier twice.
    const rows = page.getByRole('listitem').filter({ hasText: supplier });
    const cells = page.getByRole('row').filter({ hasText: supplier });
    await expect
      .poll(async () => (await rows.count()) + (await cells.count()), { timeout: 20_000 })
      .toBe(1);
  });


  test('a box of units goes in one after another', async ({ page }) => {
    /**
     * Receiving a sealed box used to mean opening the camera once per unit:
     * tap, wait for focus, scan, tap again, twenty times. Most of a gate-in was
     * spent on the phone rather than on the delivery.
     *
     * The camera path needs a real camera, so what is exercised here is the
     * other half of the same loop — the line accumulating units without the
     * sheet closing between them, and a count that is visible while doing it.
     */
    await open(page, '/gate-in/new');
    await page.getByLabel('Source').selectOption('PURCHASE');
    await page.getByLabel('Received into').selectOption({ index: 1 });

    await page.getByRole('button', { name: 'Add a line' }).click();
    const sheet = page.getByRole('dialog');

    const item = sheet.getByLabel('Item');
    const options = await item.locator('option').allTextContents();
    const serialized = options.find((label) => /rru|antenna/i.test(label));
    test.skip(!serialized, 'no serialized item in the seeded catalogue');
    await item.selectOption({ label: serialized! });

    const serials = unique('BOX');
    for (const suffix of ['-1', '-2', '-3']) {
      await sheet.getByLabel('Serial number').fill(serials + suffix);
      await sheet.getByRole('button', { name: /^add$/i }).first().click();
    }

    await expect(sheet.getByText('3 units on this line')).toBeVisible();

    // The slip this is meant to catch: the same unit scanned twice while
    // working down a box.
    await sheet.getByLabel('Serial number').fill(serials + '-2');
    await sheet.getByRole('button', { name: /^add$/i }).first().click();

    await expect(sheet.getByText(/already on this line/i)).toBeVisible();
    await expect(sheet.getByText('3 units on this line')).toBeVisible();

    await sheet.getByRole('button', { name: 'Add line' }).click();
    await expect(sheet).toBeHidden();
    await expect(page.getByText('3 serials')).toBeVisible();
  });

  test('the screen does not scroll sideways on a phone', async ({ page }) => {
    /** §7.3: no horizontal page scroll at any width. */
    await open(page, '/gate-in/new');

    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(1);
  });
});

test.describe('What a technician cannot reach', () => {
  test('receiving is not offered to somebody without the permission', async ({ page }) => {
    /**
     * §7.2, B4: navigation follows the permissions `/me` resolved. The server
     * re-checks every call, so this is UX — but a menu offering something that
     * 403s teaches people to distrust the screen.
     */
    await signIn(page, PEOPLE.technician);

    await expect(page.getByRole('link', { name: /gate-in/i })).toHaveCount(0);

    // And reaching for it directly is refused rather than half-rendered.
    await page.goto('/gate-in/new');
    await expect(page.getByText(/do not have permission/i)).toBeVisible();
  });
});

test.describe('Correcting a draft', () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page, PEOPLE.storekeeper);
  });

  test('a draft can be corrected and discarded', async ({ page }) => {
    /**
     * Reported from the yard: a draft could not be edited at all, so a
     * mis-keyed quantity meant keying the whole delivery again — and the
     * mistake stayed in the list for ever, because a draft could not be
     * discarded either. Both are safe: a draft has posted nothing.
     */
    const supplier = unique('Correctable');

    await open(page, '/gate-in/new');
    await page.getByLabel('Source').selectOption('PURCHASE');
    await page.getByLabel('Supplier').fill(supplier);
    await page.getByLabel('Received into').selectOption({ index: 1 });

    await page.getByRole('button', { name: 'Add a line' }).click();
    const sheet = page.getByRole('dialog');
    const items = await sheet.getByLabel('Item').locator('option').allTextContents();
    const bulk = items.find((label) => /clamp|tie|glove|vest/i.test(label));
    test.skip(!bulk, 'no bulk item in the seeded catalogue');
    await sheet.getByLabel('Item').selectOption({ label: bulk! });
    await sheet.getByLabel(/^Quantity/).fill('7');
    await sheet.getByRole('button', { name: 'Add line' }).click();
    await expect(sheet).toBeHidden();

    await page.getByRole('button', { name: 'Save as draft' }).click();
    await expect(page).toHaveURL(/\/gate-in\/\d+$/, { timeout: 30_000 });

    // The quantity was wrong. Correcting it is ordinary work.
    await page.getByRole('link', { name: 'Edit' }).click();
    await expect(page.getByRole('heading', { name: 'Correct this delivery' })).toBeVisible();
    await expect(page.getByLabel('Supplier')).toHaveValue(supplier);

    await page.getByRole('button', { name: 'Remove' }).first().click();
    await page.getByRole('button', { name: 'Add a line' }).click();
    const again = page.getByRole('dialog');
    await again.getByLabel('Item').selectOption({ label: bulk! });
    await again.getByLabel(/^Quantity/).fill('9');
    await again.getByRole('button', { name: 'Add line' }).click();
    await page.getByRole('button', { name: 'Save as draft' }).click();

    await expect(page).toHaveURL(/\/gate-in\/\d+$/, { timeout: 30_000 });
    // Cards and a table are both in the DOM with one hidden by CSS (§7.3), so
    // `.first()` can resolve the hidden one — the same trap the gate-out spec
    // already documents. Filter to what is actually on screen.
    await expect(
      page.getByText('9.000').filter({ visible: true }).first(),
    ).toBeVisible();

    // And a draft made by mistake does not have to stay in the list.
    await page.getByRole('button', { name: 'Discard' }).click();
    await page.getByRole('button', { name: 'Discard it' }).click();

    await expect(page).toHaveURL(/\/gate-in$/, { timeout: 30_000 });
    await page.getByPlaceholder(/number, supplier/i).fill(supplier);
    await expect(page.getByText(supplier)).toHaveCount(0, { timeout: 20_000 });
  });
});
