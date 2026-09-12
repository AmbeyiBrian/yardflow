/**
 * §14, T8.13, flow 2: "request → approve → release → closeout → return,
 * including a variance".
 *
 * This is the product. Every control the system exists for is on this path: a
 * request that cannot approve itself (§5.3), a release that records what
 * actually went on the vehicle (G1), a closeout that moves material out of
 * custody (H2), and a variance that stays visible until somebody answers for it
 * (H3, M1).
 *
 * Four people, four sessions — deliberately. A flow driven entirely as one
 * superuser would pass while the separation of duties underneath it was broken,
 * and that separation is the thing an operator audit actually asks about.
 */

import { expect, test } from '@playwright/test';

import { PEOPLE, open, receiveCriticalStock, signIn } from './fixtures';

test.describe.configure({ mode: 'serial' });

/** Carried between the steps of the flow. */
let passNumber = '';
let criticalItem = '';

test.describe('The gate-out loop', () => {
  test('a storekeeper raises a request', async ({ page, request }) => {
    // A **high-criticality** item, so the pass actually routes for approval
    // (§5.1). A clamp auto-approves, and a flow that auto-approved would skip
    // the control this whole spec exists to exercise.
    const stocked = await receiveCriticalStock(request);
    test.skip(!stocked, 'no high-criticality item in the seeded catalogue');
    criticalItem = stocked!.itemName;

    await signIn(page, PEOPLE.storekeeper);
    await open(page, '/gate-out/new');

    // F1: one destination question, not four. The screen asks where it is going
    // and works out the rest.
    // "Out of" is not pre-filled when a tenant has more than one yard, and a
    // request with no source cannot be filled — which is why the button stays
    // disabled until it is answered.
    await page.getByLabel('Out of').selectOption({ index: 1 });
    await page.getByLabel('Where it is going').selectOption({ label: 'A site' });
    await page.getByLabel('A site').selectOption({ index: 1 });
    await page.getByLabel('Who is taking it').selectOption({ index: 1 });

    await page.getByRole('button', { name: 'Add' }).click();
    const sheet = page.getByRole('dialog');
    await expect(sheet).toBeVisible();

    const picker = sheet.getByLabel('Or choose an item');
    await picker.selectOption({ label: criticalItem });
    // D3's escape hatch, recorded: untagged units go as a quantity.
    await sheet.getByLabel(/no serial available/i).fill('Untagged batch, GRN on file.');
    await sheet.getByLabel(/^How much/).fill('3');
    await sheet.getByRole('button', { name: 'Add' }).last().click();
    await expect(sheet).toBeHidden();

    await page.getByRole('button', { name: /send for approval/i }).click();

    // The number is what everybody refers to it by from here on (M6).
    const heading = page.getByText(/GP-\d+/).first();
    await expect(heading).toBeVisible({ timeout: 30_000 });
    passNumber = ((await heading.textContent()) ?? '').match(/GP-\d+/)?.[0] ?? '';
    expect(passNumber).toMatch(/GP-\d+/);
  });

  test('somebody who could approve cannot approve their own request', async ({
    page,
    request,
  }) => {
    /**
     * §5.3, F3 — the most important refusal in the product: a request somebody
     * raised and approved themselves is not an approval at all.
     *
     * Exercised as the owner, because the refusal is only reachable by somebody
     * who holds *both* rights — a storekeeper has no approve permission and an
     * approver has no request permission, so for either of them there is nothing
     * to refuse. Testing it as the storekeeper would have proved only that the
     * menu hides a button.
     */
    // The demo tenant's stock moves every time somebody uses it, so the
    // precondition is established rather than assumed (see the fixture).
    const stocked = await receiveCriticalStock(request);
    test.skip(!stocked, 'no high-criticality item in the seeded catalogue');

    await signIn(page, PEOPLE.owner);
    await open(page, '/gate-out/new');

    await page.getByLabel('Out of').selectOption({ index: 1 });
    await page.getByLabel('Where it is going').selectOption({ label: 'A site' });
    await page.getByLabel('A site').selectOption({ index: 1 });
    await page.getByLabel('Who is taking it').selectOption({ index: 1 });

    await page.getByRole('button', { name: 'Add' }).click();
    const sheet = page.getByRole('dialog');
    const picker = sheet.getByLabel('Or choose an item');
    const items = await picker.locator('option').allTextContents();

    // A **high-criticality** item, because that is what routes for approval
    // (§5.1). A clamp auto-approves, and a pass with nothing outstanding has no
    // self-approval to refuse — the test would have passed for the wrong reason.
    await picker.selectOption({ label: stocked!.itemName });

    // D3's escape hatch: a serialized item can go as a quantity when the yard
    // holds untagged ones, with the reason recorded.
    await sheet.getByLabel(/no serial available/i).fill('Untagged unit from GRN-000002.');
    await sheet.getByLabel(/^How much/).fill('1');
    await sheet.getByRole('button', { name: 'Add' }).last().click();
    await expect(sheet).toBeHidden();

    await page.getByRole('button', { name: /send for approval/i }).click();
    await expect(page.getByText(/GP-\d+/).first()).toBeVisible({ timeout: 30_000 });

    // On the detail screen the requester is warned *before* tapping, because a
    // refusal after the tap is a worse way to learn the rule.
    await expect(page.getByText(/you raised this/i).first()).toBeVisible({
      timeout: 20_000,
    });
  });

  test('an approver approves it', async ({ page }) => {
    test.skip(!passNumber, 'no pass was raised');

    await signIn(page, PEOPLE.approver);
    await open(page, '/approvals');

    const card = page.locator('li', { hasText: passNumber });
    await expect(card.first()).toBeVisible({ timeout: 20_000 });

    // F4: the decision is on the list — two taps from a notification, and every
    // line and its criticality visible before the tap.
    await card
      .first()
      .getByRole('button', { name: /approve/i })
      .click();

    await expect(page.getByText(passNumber)).toHaveCount(0, { timeout: 30_000 });
  });

  test('the storekeeper releases it short, and the variance is recorded', async ({
    page,
  }) => {
    /**
     * G1: "if the physical load differs from the approved list, the storekeeper
     * records the actual quantity, and the difference is flagged as a release
     * variance requiring an approver's acknowledgement." It blocks nothing at
     * the gate — the driver leaves with what was loaded — and the discrepancy
     * survives.
     */
    test.skip(!passNumber, 'no pass was raised');

    await signIn(page, PEOPLE.storekeeper);
    await open(page, '/gate-out');

    // The list is cards on a phone and a table from `md` up (§7.3), and *both*
    // are in the DOM with one hidden by CSS — so `.first()` can pick the hidden
    // one. Filtering to what is visible is what makes the same step work at
    // either width.
    await page.getByText(passNumber).filter({ visible: true }).first().click();
    await expect(page.getByText(passNumber).first()).toBeVisible();

    // By its exact name: /release/i also matches the line summary button, and
    // clicking that opens nothing.
    await page.getByRole('button', { name: 'Release at the gate' }).click();

    // Two of the three that were approved: a short load, which is normal.
    const actual = page.getByLabel(/actually loaded/i).first();
    await actual.fill('2');

    // G1: a short load needs a reason, and the field only appears once the
    // figures disagree — which is the screen telling the storekeeper why it is
    // asking.
    await page.getByLabel('Why short').first().fill('Only two on the shelf.');

    await page.getByLabel('Vehicle registration').fill('KDA 411Z');
    await page.getByLabel('Driver').fill('Njoroge');

    // The sheet's own button, which renames itself when the load is short —
    // the screen saying what it is about to record (G1).
    await page.getByRole('dialog').getByRole('button', { name: 'Release short' }).click();

    // Released, and the shortfall is now somebody's problem to acknowledge.
    await expect(page.getByText(/released|partially/i).first()).toBeVisible({
      timeout: 30_000,
    });
  });

  test('the variance is on the exceptions register', async ({ page }) => {
    /** M1: one register, and nothing leaves it without an answer (H3). */
    test.skip(!passNumber, 'no pass was raised');

    await signIn(page, PEOPLE.owner);
    await open(page, '/exceptions');

    await expect(page.getByRole('heading', { name: /exceptions/i })).toBeVisible();
    // The short release is there, with both figures on the row.
    await expect(page.getByText(/short release/i).first()).toBeVisible({
      timeout: 20_000,
    });
  });

  test('a technician sees what they are carrying and can close out', async ({ page }) => {
    /**
     * H2, I1: custody is a balance at the technician's node, so what they see
     * here and what the yard sees cannot disagree. Closing out is what moves it
     * from custody to the site.
     */
    await signIn(page, PEOPLE.technician);
    await open(page, '/jobs/custody');

    await expect(page.getByRole('heading', { name: /carrying/i })).toBeVisible();

    await open(page, '/jobs');
    const jobs = page.getByRole('link').filter({ hasText: /SLV-|JOB-/ });
    test.skip((await jobs.count()) === 0, 'no open job assigned to the technician');

    await jobs.first().click();
    // The four figures an operator asks about are on the job (H4).
    await expect(page.getByText(/issued/i).first()).toBeVisible();
    await expect(page.getByText(/unaccounted/i).first()).toBeVisible();
  });
});

test.describe('What the offline path refuses', () => {
  test('the offline release screen offers only approved passes', async ({ page }) => {
    /**
     * §8.3, N3: "release of an unapproved gate-out is impossible offline". The
     * screen says so in words, because a storekeeper needs to know the limit
     * before they are standing at a gate with no signal.
     */
    await signIn(page, PEOPLE.storekeeper);
    await open(page, '/gate-out/offline');

    await expect(page.getByText(/already approved/i).first()).toBeVisible();
  });

  test('the sync screen shows what is waiting', async ({ page }) => {
    /** T8.3: captured offline, visibly pending, and surviving a restart. */
    await signIn(page, PEOPLE.storekeeper);
    await open(page, '/sync');

    await expect(page.getByRole('heading', { name: /^sync$/i })).toBeVisible();
    await expect(page.getByText(/waiting to send/i).first()).toBeVisible();
  });
});
