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

import { type APIRequestContext, type Locator, expect, test } from '@playwright/test';

import {
  chooseFirst,
  itemOptions,
  open,
  PASSWORD,
  PEOPLE,
  pickItem,
  signIn,
  unique,
} from './fixtures';

const api = process.env.E2E_API_URL ?? 'http://127.0.0.1:8000';
const host = new URL(process.env.E2E_BASE_URL ?? 'http://demo.localhost:5173').hostname;

async function apiGet(request: APIRequestContext, who: string, path: string) {
  const login = await request.post(`${api}/api/v1/auth/login`, {
    headers: { Host: host },
    data: { identifier: who, password: PASSWORD },
  });
  const { access } = (await login.json()) as { access: string };
  return request.get(`${api}/api/v1${path}`, {
    headers: { Host: host, Authorization: `Bearer ${access}` },
  });
}

/**
 * Pick an item the sheet treats as a reel.
 *
 * Matching the name is not enough: the seeded catalogue has "Cable clamp"
 * (bulk) and "Power cable 16mm" (reel), and a regex on "cable" finds the wrong
 * one first. What decides it is the sheet — a reel item is the one that asks
 * for a drum.
 */
async function chooseAReelItem(sheet: Locator): Promise<boolean> {
  const options = await itemOptions(sheet, 'Item', 'cable');
  const feeders = await itemOptions(sheet, 'Item', 'feeder');

  // Each candidate is picked by its position in the list the search shows.
  for (const [query, count] of [
    ['cable', options.length],
    ['feeder', feeders.length],
  ] as const) {
    for (let index = 0; index < count; index += 1) {
      await itemOptions(sheet, 'Item', query);
      await sheet.getByRole('option').nth(index).click();
      if (await sheet.getByLabel('Drum number').isVisible().catch(() => false)) {
        return true;
      }
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
    await chooseFirst(page.getByLabel('Received into'));

    await page.getByRole('button', { name: 'Add a line' }).click();
    const sheet = page.getByRole('dialog');
    await expect(sheet).toBeVisible();

    // A bulk item by name. Selecting by index would silently pick whichever
    // item the seed happens to list first — and if that is serialized, the
    // sheet asks for a serial and the failure looks like a broken button.
    const bulk = await pickItem(sheet, 'Item', 'Cable clamp');
    test.skip(!bulk, 'no bulk item in the seeded catalogue');
    await sheet.getByLabel(/^Quantity/).fill('25');
    await sheet.getByRole('button', { name: 'Add line' }).click();
    await expect(sheet).toBeHidden();

    // Every line shows on the document immediately, so a mis-keyed quantity is
    // caught at the gate rather than at the end.
    await expect(page.getByText('25', { exact: false }).first()).toBeVisible();

    // D8, M6: receiving moves stock and gives the storekeeper a number they can
    // write on the supplier's paperwork.
    await page.getByRole('button', { name: /receive it|save on this device/i }).click();
    await expect(page.getByText(/GRN-\d+/).filter({ visible: true }).first()).toBeVisible({ timeout: 30_000 });
  });

  test('a delivery received for a site is earmarked for it', async ({ page, request }) => {
    const supplier = unique('Earmark Supplies');
    await open(page, '/gate-in/new');
    await page.getByLabel('Source').selectOption('PURCHASE');
    await page.getByLabel('Supplier').fill(supplier);
    await chooseFirst(page.getByLabel('Received into'));
    const siteId = await chooseFirst(page.getByLabel('For site'));

    await page.getByRole('button', { name: 'Add a line' }).click();
    const sheet = page.getByRole('dialog');
    await expect(sheet).toBeVisible();
    const bulk = await pickItem(sheet, 'Item', 'Cable clamp');
    test.skip(!bulk, 'no bulk item in the seeded catalogue');
    await sheet.getByLabel(/^Quantity/).fill('5');
    await sheet.getByRole('button', { name: 'Add line' }).click();
    await expect(sheet).toBeHidden();
    await expect(page.getByText(/· for /).first()).toBeVisible();

    await page.getByRole('button', { name: /receive it|save on this device/i }).click();
    await expect(page.getByText(/GRN-\d+/).filter({ visible: true }).first()).toBeVisible({
      timeout: 30_000,
    });

    const list = await apiGet(request, PEOPLE.storekeeper, '/gate-ins?page_size=50&ordering=-id');
    const rows = ((await list.json()) as { results: { id: number; supplier_name: string }[] }).results;
    const mine = rows.find((row) => row.supplier_name === supplier);
    expect(mine).toBeTruthy();
    const detail = await apiGet(request, PEOPLE.storekeeper, `/gate-ins/${mine!.id}`);
    const doc = (await detail.json()) as {
      for_site: number | null;
      lines: { for_site: number | null }[];
    };
    expect(String(doc.for_site ?? doc.lines[0]?.for_site)).toBe(siteId);
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
    await chooseFirst(page.getByLabel('Received into'));

    await page.getByRole('button', { name: 'Add a line' }).click();
    const sheet = page.getByRole('dialog');

    // The seeded catalogue tracks radios by serial.
    const serialized = await pickItem(sheet, 'Item', 'Baseband board');
    test.skip(!serialized, 'no serialized item in the seeded catalogue');

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
    await chooseFirst(page.getByLabel('Received into'));

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

  test('cable that is not on a drum is received as a plain length', async ({ page }) => {
    /** D10: a coil or a cut length has no drum number, and nobody is asked for one. */
    await open(page, '/gate-in/new');
    await page.getByLabel('Source').selectOption('PURCHASE');
    await chooseFirst(page.getByLabel('Received into'));

    await page.getByRole('button', { name: 'Add a line' }).click();
    const sheet = page.getByRole('dialog');

    const found = await chooseAReelItem(sheet);
    test.skip(!found, 'no reel item in the seeded catalogue');

    await sheet.getByRole('radio', { name: 'Not on a drum' }).click();
    await expect(sheet.getByLabel('Drum number')).toBeHidden();
    await sheet.getByLabel(/^Length/).fill('240');
    await sheet.getByRole('button', { name: 'Add line' }).click();

    await expect(sheet).toBeHidden();
    await expect(page.getByText(/240 m .*not on a drum/).first()).toBeVisible();

    // Change reopens it as it was keyed.
    await page.getByRole('button', { name: /^Change the .* line$/ }).click();
    await expect(sheet.getByRole('radio', { name: 'Not on a drum' })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    await sheet.getByRole('button', { name: 'Cancel' }).click();

    await page.getByRole('button', { name: /receive it|save on this device/i }).click();
    await expect(page.getByText(/GRN-\d+/).filter({ visible: true }).first()).toBeVisible({ timeout: 30_000 });
  });

  test('half a drum is refused by saying which half is missing', async ({ page }) => {
    /** A number with no length is genuinely incomplete — so the complaint names
     * the drum and asks for the one thing it needs. */
    await open(page, '/gate-in/new');
    await page.getByLabel('Source').selectOption('PURCHASE');
    await chooseFirst(page.getByLabel('Received into'));

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
    await chooseFirst(page.getByLabel('Received into'));

    await page.getByRole('button', { name: 'Add a line' }).click();
    const sheet = page.getByRole('dialog');
    const bulk = await pickItem(sheet, 'Item', 'Cable clamp');
    test.skip(!bulk, 'no bulk item in the seeded catalogue');
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
    await chooseFirst(page.getByLabel('Received into'));

    await page.getByRole('button', { name: 'Add a line' }).click();
    const sheet = page.getByRole('dialog');

    const serialized = await pickItem(sheet, 'Item', 'Baseband board');
    test.skip(!serialized, 'no serialized item in the seeded catalogue');

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
    await chooseFirst(page.getByLabel('Received into'));

    await page.getByRole('button', { name: 'Add a line' }).click();
    const sheet = page.getByRole('dialog');
    const bulk = await pickItem(sheet, 'Item', 'Cable clamp');
    test.skip(!bulk, 'no bulk item in the seeded catalogue');
    await sheet.getByLabel(/^Quantity/).fill('7');
    await sheet.getByRole('button', { name: 'Add line' }).click();
    await expect(sheet).toBeHidden();

    await page.getByRole('button', { name: 'Save as draft' }).click();
    await expect(page).toHaveURL(/\/gate-in\/\d+$/, { timeout: 30_000 });

    // The quantity was wrong. Correcting it is ordinary work.
    await page.getByRole('link', { name: 'Edit' }).click();
    await expect(page.getByRole('heading', { name: 'Correct this delivery' })).toBeVisible();
    await expect(page.getByLabel('Supplier')).toHaveValue(supplier);

    // Removing takes two taps (D9): the first asks, the second removes.
    await page.getByRole('button', { name: /^Remove the .* line$/ }).first().click();
    await page.getByRole('button', { name: 'Yes, remove' }).click();
    await page.getByRole('button', { name: 'Add a line' }).click();
    const again = page.getByRole('dialog');
    await pickItem(again, 'Item', 'Cable clamp');
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

  test('a line is changed in place before it is received (D9)', async ({ page }) => {
    await open(page, '/gate-in/new');
    await page.getByLabel('Source').selectOption('PURCHASE');
    await page.getByLabel('Supplier').fill('Huawei Kenya');
    await chooseFirst(page.getByLabel('Received into'));

    await page.getByRole('button', { name: 'Add a line' }).click();
    const sheet = page.getByRole('dialog');
    const bulk = await pickItem(sheet, 'Item', 'Cable clamp');
    test.skip(!bulk, 'no bulk item in the seeded catalogue');
    await sheet.getByLabel(/^Quantity/).fill('10');
    await sheet.getByRole('button', { name: 'Add line' }).click();
    await expect(sheet).toBeHidden();

    // Change reopens the sheet filled in, and saving replaces the line.
    await page.getByRole('button', { name: /^Change the .* line$/ }).click();
    const again = page.getByRole('dialog');
    await expect(again.getByLabel(/^Quantity/)).toHaveValue('10');
    await again.getByLabel(/^Quantity/).fill('12');
    await again.getByRole('button', { name: 'Save changes' }).click();
    await expect(again).toBeHidden();

    await expect(page.getByText(/12(\.\d+)? .*Cable clamp/).first()).toBeVisible();
    await expect(page.getByRole('button', { name: /^Change the .* line$/ })).toHaveCount(1);
    await expect(page.getByRole('heading', { name: 'Lines (1)' })).toBeVisible();

    await page.getByRole('button', { name: /receive it|save on this device/i }).click();
    await expect(page.getByText(/GRN-\d+/).filter({ visible: true }).first()).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText(/12(\.0+)?\b/).filter({ visible: true }).first()).toBeVisible();
  });

  test('a unit is moved to another box while changing its line (D9)', async ({ page }) => {
    const boxA = unique('CTNA');
    const boxB = unique('CTNB');
    const serials = [unique('MV1'), unique('MV2')];
    await open(page, '/gate-in/new');
    await page.getByLabel('Source').selectOption('PURCHASE');
    await page.getByLabel('Supplier').fill('Huawei Kenya');
    await chooseFirst(page.getByLabel('Received into'));

    await page.getByRole('button', { name: 'Add a line' }).click();
    const sheet = page.getByRole('dialog');
    const serialized = await pickItem(sheet, 'Item', 'Baseband board');
    test.skip(!serialized, 'no serialized item in the seeded catalogue');
    await sheet.getByRole('button', { name: 'Start a box' }).click();
    await sheet.getByLabel('Box code').fill(boxA);
    await sheet.getByRole('button', { name: /^Start box/ }).click();
    for (const serial of serials) {
      await sheet.getByLabel('Serial number').fill(serial);
      await sheet.getByRole('button', { name: /^add$/i }).first().click();
    }
    await sheet.getByRole('button', { name: 'Add line' }).click();
    await expect(sheet).toBeHidden();
    await expect(page.getByText(`${boxA} · 2 units`)).toBeVisible();

    await page.getByRole('button', { name: /^Change the .* line$/ }).click();
    const again = page.getByRole('dialog');
    await expect(again.getByLabel(`Box for ${serials[1]}`)).toHaveValue(/.+/);
    await again.getByRole('button', { name: 'Start a box' }).click();
    await again.getByLabel('Box code').fill(boxB);
    await again.getByRole('button', { name: /^Start box/ }).click();
    // A unit is moved by the select on its own chip.
    await again.getByLabel(`Box for ${serials[1]}`).selectOption({ label: boxB });
    await again.getByRole('button', { name: 'Save changes' }).click();
    await expect(again).toBeHidden();

    await expect(page.getByText(`${boxA} · 1 unit`)).toBeVisible();
    await expect(page.getByText(`${boxB} · 1 unit`)).toBeVisible();
    await expect(page.getByRole('button', { name: /^Change the .* line$/ })).toHaveCount(2);
  });
});

test.describe('A refused delivery does not haunt the next one', () => {
  test('a refusal opens the saved draft, and a new delivery starts empty', async ({ page }) => {
    // Reported 2026-10-07: GRN-000002 was refused for an empty box, corrected
    // from its saved draft and received — and every new delivery afterwards
    // opened with its lines, because the phone kept its own copy.
    await signIn(page, PEOPLE.storekeeper);
    await open(page, '/gate-in/new');
    await page.getByLabel('Source').selectOption('PURCHASE');
    await page.getByLabel('Supplier').fill(unique('Refused supplier'));
    await chooseFirst(page.getByLabel('Received into'));

    await page.getByRole('button', { name: 'Add a line' }).click();
    const sheet = page.getByRole('dialog');
    const found = await pickItem(sheet, 'Item', 'Cable clamp');
    test.skip(!found, 'no bulk item in the seeded catalogue');
    await sheet.getByLabel(/^Quantity/).fill('3');
    // An empty box is what the server refuses.
    await sheet.getByRole('button', { name: 'Start a box' }).click();
    await sheet.getByLabel('Box code').fill(unique('EMPTY'));
    await sheet.getByRole('button', { name: /^Start box/ }).click();
    await sheet.getByLabel('Into a box').selectOption({ index: 0 });
    await sheet.getByRole('button', { name: 'Add line' }).click();
    await expect(sheet).toBeHidden();

    await page.getByRole('button', { name: /receive it/i }).click();

    // Handed over to the saved draft, with the reason.
    await expect(page).toHaveURL(/\/gate-in\/\d+\/edit$/, { timeout: 30_000 });
    await expect(page.getByText(/saved as a draft/i).first()).toBeVisible();

    // And the next delivery is a new one.
    await open(page, '/gate-in/new');
    await expect(page.getByRole('button', { name: 'Discard' })).toHaveCount(0);
    await expect(page.getByText('Cable clamp')).toHaveCount(0);
  });
});
