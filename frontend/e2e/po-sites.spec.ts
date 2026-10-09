/**
 * Finance stage 2 on a phone: POs, budgets and sites (Epic R, T18.22; design §4.19.16).
 *
 * A project with a PO, a budget and a manager has a site and an approved
 * supplier. A supervisor records two purchases on the phone, one used at the
 * site and one into the yard. The PM then Finance approve them: the yard one
 * makes a draft delivery for the storekeeper (badged "From purchase") and the
 * site one, once Finance pays it, is spend on the project's Budget tab. A
 * purchase past the budget asks for its reason. On the Sites tab the PM sets the
 * dates and uploads a certificate, and the site becomes Accepted. Finance adds
 * the default milestones, an invoice and a part receipt. A project with no PO
 * takes one late. A subcontract payment Finance enters is approved by the PM
 * and shows as paid.
 *
 * Online throughout: the offline queue for purchases is covered by its unit
 * tests and the sync tests; this is the screens, against a real backend.
 */

import { type APIRequestContext, type Page, expect, test } from '@playwright/test';

import { PASSWORD, PEOPLE, open, signIn, unique } from './fixtures';

test.describe.configure({ mode: 'serial' });

const api = process.env.E2E_API_URL ?? 'http://127.0.0.1:8000';
const host = new URL(process.env.E2E_BASE_URL ?? 'http://demo.localhost:5173').hostname;
const tenant = { Host: host };

// A 1x1 PNG: a real image, so the upload is accepted as one.
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64',
);
const photo = (name: string) => ({ name, mimeType: 'image/png', buffer: PNG });

const run = unique('PS');
const siteRef = `PS-${run}`;
const siteName = `E2E PO Site ${run}`;
const siteLabel = `${siteRef} ${siteName}`;
const noPoSiteRef = `PN-${run}`;
const noPoSiteName = `E2E No-PO Site ${run}`;
const supplierName = `E2E PO Supplier ${run}`;
const subcontractorName = `E2E Subcontractor ${run}`;

const money = (value: number) =>
  `KES ${value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const today = () => {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};
const daysAgo = (n: number) => {
  const d = new Date(Date.now() - n * 86_400_000);
  const pad = (v: number) => String(v).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};

// Distinctive amounts, so rows can be found by them.
const SITE_PRICE = 1500; // 2 x 1500
const SITE_TOTAL = 3000;
const YARD_PRICE = 400; // 3 x 400
const YARD_TOTAL = 1200;
const OVER_TOTAL = 20_000;
// The 50,000 subcontract is committed against it too.
const BUDGET = 60_000;
const SUBCONTRACT_VALUE = 50_000;
const PO_VALUE = 100_000;
const INVOICE = 5000;
const RECEIPT = 2000;
const SUB_PAYMENT = 8000;

let projectId = 0;
let noPoProjectId = 0;
let subcontractId = 0;
let siteNumber = '';
let yardNumber = '';

async function headersFor(request: APIRequestContext, who: string) {
  const login = await request.post(`${api}/api/v1/auth/login`, {
    headers: tenant,
    data: { identifier: who, password: PASSWORD },
  });
  const { access } = await login.json();
  return { ...tenant, Authorization: `Bearer ${access}` };
}

async function getJson(request: APIRequestContext, who: string, path: string) {
  const response = await request.get(`${api}/api/v1/${path}`, {
    headers: await headersFor(request, who),
  });
  expect(response.ok(), `${path}: ${response.status()}`).toBeTruthy();
  return response.json();
}

type Purchase = {
  id: number;
  number: string;
  status: string;
  amount: string;
  destination: string;
  is_over_budget?: boolean;
  over_budget_reason?: string;
};

async function purchases(request: APIRequestContext): Promise<Purchase[]> {
  return (await getJson(request, PEOPLE.owner, `site-purchases?project=${projectId}&page_size=50`))
    .results;
}

async function purchaseByAmount(request: APIRequestContext, value: number) {
  return (await purchases(request)).find((p) => Number(p.amount) === value);
}

/** Open a project and a tab on it. */
async function openTab(page: Page, id: number, tab: string) {
  await open(page, `/projects/${id}`);
  await page.getByRole('button', { name: tab, exact: true }).click();
}

/** Fill the shared part of the purchase form. */
async function startPurchase(page: Page, destination: 'USED_AT_SITE' | 'INTO_YARD') {
  await open(page, '/money/purchases/new');
  await page.getByLabel('Site', { exact: true }).selectOption({ label: siteLabel });
  const supplier = page.getByLabel('Supplier', { exact: true });
  const option = supplier.locator('option', { hasText: supplierName });
  await expect(option).toHaveCount(1, { timeout: 20_000 });
  await supplier.selectOption({ label: (await option.textContent())!.trim() });
  await page
    .getByRole('group', { name: 'Destination' })
    .getByRole('button', { name: destination === 'INTO_YARD' ? 'Into the yard' : 'Used at the site' })
    .click();
}

/** Approve a purchase at the Approvals tab, as whoever is signed in. */
async function approvePurchase(page: Page, number: string) {
  await open(page, '/approvals');
  await page.getByRole('button', { name: 'Purchases', exact: true }).click();
  await page.getByText(number).filter({ visible: true }).first().click();
  const sheet = page.getByRole('dialog');
  await sheet.getByRole('button', { name: 'Approve' }).click();
  await expect(sheet).toBeHidden({ timeout: 20_000 });
}

test.describe('POs, budgets and sites, on a phone', () => {
  test.beforeEach(({}, testInfo) => {
    test.skip(testInfo.project.name !== 'phone', 'a supervisor does this on a phone');
  });

  test('setup: a project with a PO, budget and manager; an approved supplier; a subcontract', async ({
    request,
  }) => {
    const owner = await headersFor(request, PEOPLE.owner);
    const clients = await (
      await request.get(`${api}/api/v1/clients?page_size=5`, { headers: owner })
    ).json();
    const client = clients.results[0].id;
    const users = await (
      await request.get(`${api}/api/v1/users?page_size=100`, { headers: owner })
    ).json();
    const manager = users.results.find((u: { email: string }) => u.email === PEOPLE.manager);
    expect(manager).toBeTruthy();

    const site = await request.post(`${api}/api/v1/sites`, {
      headers: owner,
      data: { client, internal_ref: siteRef, name: siteName, latitude: '-1.286389', longitude: '36.817223' },
    });
    expect(site.ok(), await site.text()).toBeTruthy();
    const siteId = (await site.json()).id;

    const project = await request.post(`${api}/api/v1/projects`, {
      headers: owner,
      data: {
        client,
        po_number: `PO-${run}`,
        title: `E2E PO ${run}`,
        manager: manager.id,
        contract_value: String(PO_VALUE),
        cost_budget: String(BUDGET),
        payment_terms: '30 days',
        payment_terms_days: 30,
        sites: [siteId],
      },
    });
    expect(project.ok(), await project.text()).toBeTruthy();
    projectId = (await project.json()).id;

    // A project that started without a PO, on a site of its own.
    const noPoSite = await request.post(`${api}/api/v1/sites`, {
      headers: owner,
      data: {
        client,
        internal_ref: noPoSiteRef,
        name: noPoSiteName,
        latitude: '-1.3',
        longitude: '36.8',
      },
    });
    expect(noPoSite.ok(), await noPoSite.text()).toBeTruthy();
    const noPo = await request.post(`${api}/api/v1/projects`, {
      headers: owner,
      data: { client, title: `E2E no PO ${run}`, manager: manager.id, sites: [(await noPoSite.json()).id] },
    });
    expect(noPo.ok(), await noPo.text()).toBeTruthy();
    noPoProjectId = (await noPo.json()).id;

    // An approved supplier: added by the storekeeper, given its PIN and a payment route and
    // approved by Finance (who may not approve what they added themselves, R15).
    const supplier = await request.post(`${api}/api/v1/suppliers`, {
      headers: await headersFor(request, PEOPLE.storekeeper),
      data: { name: supplierName },
    });
    expect(supplier.ok(), await supplier.text()).toBeTruthy();
    const supplierId = (await supplier.json()).id;
    const patched = await request.patch(`${api}/api/v1/suppliers/${supplierId}`, {
      headers: owner,
      data: {
        kra_pin: `P${Date.now().toString().slice(-9)}Z`,
        mpesa_type: 'TILL',
        mpesa_number: '123456',
      },
    });
    expect(patched.ok(), await patched.text()).toBeTruthy();
    const decided = await request.post(`${api}/api/v1/suppliers/${supplierId}/decide`, {
      headers: owner,
      data: { approved: true },
    });
    expect(decided.ok(), await decided.text()).toBeTruthy();
    await expect
      .poll(async () => (await getJson(request, PEOPLE.owner, `suppliers/${supplierId}`)).status)
      .toBe('APPROVED');

    // A subcontract on the PO project, made by its manager.
    const contractor = await request.post(`${api}/api/v1/subcontractors`, {
      headers: owner,
      data: { name: subcontractorName },
    });
    expect(contractor.ok(), await contractor.text()).toBeTruthy();
    const pm = await headersFor(request, PEOPLE.manager);
    const subcontract = await request.post(`${api}/api/v1/subcontracts`, {
      headers: pm,
      data: {
        project: projectId,
        subcontractor: (await contractor.json()).id,
        sites: [siteId],
        contract_value: String(SUBCONTRACT_VALUE),
      },
    });
    expect(subcontract.ok(), await subcontract.text()).toBeTruthy();
    subcontractId = (await subcontract.json()).id;
  });

  test('a supervisor records a purchase used at the site, and one into the yard', async ({
    page,
    request,
  }) => {
    await signIn(page, PEOPLE.technician);

    // Used at the site: a free-text line, two at 1,500.
    await startPurchase(page, 'USED_AT_SITE');
    await page.getByLabel('Description 1').fill(`Cement ${run}`);
    await page.getByLabel('Quantity 1').fill('2');
    await page.getByLabel('Unit price 1').fill(String(SITE_PRICE));
    await expect(page.getByText(`Total ${SITE_TOTAL.toFixed(2)}`, { exact: true })).toBeVisible();
    await page.locator('input[type=file]').setInputFiles(photo('receipt.png'));
    await page.getByRole('button', { name: 'Record it', exact: true }).click();
    await expect(page).toHaveURL(/\/money\/purchases\/\d+/, { timeout: 30_000 });

    // Into the yard: a catalogue line and a place to receive it.
    await startPurchase(page, 'INTO_YARD');
    const receiveInto = page.getByLabel('Receive into');
    await receiveInto
      .locator('option:not([value=""])')
      .first()
      .waitFor({ state: 'attached', timeout: 20_000 });
    await receiveInto.selectOption({ index: 1 });
    await page.getByPlaceholder('Catalogue item (type to search)').fill('Cable clamp');
    const option = page.getByRole('option', { name: /cable clamp/i }).first();
    try {
      await option.waitFor({ state: 'visible', timeout: 10_000 });
    } catch {
      test.skip(true, 'no Cable clamp in the seeded catalogue');
    }
    await option.click();
    await page.getByLabel('Quantity 1').fill('3');
    await page.getByLabel('Unit price 1').fill(String(YARD_PRICE));
    await page.getByRole('button', { name: 'Record it', exact: true }).click();
    await expect(page).toHaveURL(/\/money\/purchases\/\d+/, { timeout: 30_000 });

    const site = await purchaseByAmount(request, SITE_TOTAL);
    const yard = await purchaseByAmount(request, YARD_TOTAL);
    expect(site?.destination).toBe('USED_AT_SITE');
    expect(yard?.destination).toBe('INTO_YARD');
    expect(site?.status).toMatch(/PENDING_PM/);
    siteNumber = site!.number;
    yardNumber = yard!.number;
  });

  test('the project manager approves both, then Finance does', async ({ page, request }) => {
    await signIn(page, PEOPLE.manager);
    await approvePurchase(page, siteNumber);
    await approvePurchase(page, yardNumber);
    await expect
      .poll(async () => (await purchaseByAmount(request, SITE_TOTAL))?.status, { timeout: 20_000 })
      .toBe('PENDING_FINANCE');

    await page.getByRole('button', { name: /sign out/i }).first().click();
    await signIn(page, PEOPLE.owner);
    await approvePurchase(page, siteNumber);
    await approvePurchase(page, yardNumber);
    await expect
      .poll(async () => (await purchaseByAmount(request, SITE_TOTAL))?.status, { timeout: 20_000 })
      .toBe('APPROVED');
    await expect
      .poll(async () => (await purchaseByAmount(request, YARD_TOTAL))?.status, { timeout: 20_000 })
      .toBe('APPROVED');
  });

  test('the yard purchase appears as a draft delivery in receiving, badged', async ({
    page,
    request,
  }) => {
    await signIn(page, PEOPLE.storekeeper);
    await open(page, '/gate-in');
    await expect(page.getByText(`From purchase ${yardNumber}`).first()).toBeVisible({
      timeout: 20_000,
    });
    const gateIns = await getJson(request, PEOPLE.owner, `gate-ins?search=${yardNumber}&page_size=10`);
    const drafts = (gateIns.results as { status: string }[]).filter((g) => g.status === 'DRAFT');
    expect(drafts.length).toBeGreaterThanOrEqual(1);
  });

  test('Finance marks the used-at-site purchase paid', async ({ page, request }) => {
    await signIn(page, PEOPLE.owner);
    await open(page, '/money/to-pay');
    await page.getByText(siteNumber).filter({ visible: true }).first().click();
    const sheet = page.getByRole('dialog');
    await sheet.getByLabel('Payment reference').fill(`MPESA${run}`);
    await sheet.getByRole('button', { name: 'Mark paid' }).click();
    await expect(sheet).toBeHidden({ timeout: 20_000 });
    await expect
      .poll(async () => (await purchaseByAmount(request, SITE_TOTAL))?.status, { timeout: 20_000 })
      .toBe('PAID');
  });

  test('the Budget tab shows the spend, and a purchase over budget asks for a reason', async ({
    page,
    request,
  }) => {
    await signIn(page, PEOPLE.owner);
    await openTab(page, projectId, 'Budget');
    // Spent is the used-at-site purchase; committed is the yard one not yet posted.
    // The tiles are compact (KES 3K); the exact sum sits under them.
    await expect(
      page.getByText(`Spent plus committed: ${money(SITE_TOTAL + YARD_TOTAL + SUBCONTRACT_VALUE)}`),
    ).toBeVisible({ timeout: 20_000 });
    const tile = (label: string) => page.locator('div.rounded-xl', { hasText: new RegExp(`^${label}`, 'i') }).first();
    await expect(tile('Budget')).toContainText('KES 60K');
    await expect(tile('Spent')).toContainText('KES 3K');
    await expect(tile('Committed')).toContainText('KES 51.2K');

    await page.getByRole('button', { name: /sign out/i }).first().click();
    await signIn(page, PEOPLE.technician);
    await startPurchase(page, 'USED_AT_SITE');
    await page.getByLabel('Description 1').fill(`Generator hire ${run}`);
    await page.getByLabel('Quantity 1').fill('1');
    await page.getByLabel('Unit price 1').fill(String(OVER_TOTAL));
    await page.getByRole('button', { name: 'Record it', exact: true }).click();

    // Not sent: the form asks why first.
    await expect(page.getByText(/over its budget/i)).toBeVisible({ timeout: 20_000 });
    await page.getByLabel('Why over budget').fill('Hired in a generator after the grid failed');
    await page.getByRole('button', { name: 'Record it with this reason' }).click();
    await expect(page).toHaveURL(/\/money\/purchases\/\d+/, { timeout: 30_000 });

    const over = await purchaseByAmount(request, OVER_TOTAL);
    expect(over?.over_budget_reason).toContain('generator');
    expect(over?.is_over_budget).toBeTruthy();
  });

  test('the project manager sets the dates and uploads a certificate: the site is Accepted', async ({
    page,
  }) => {
    await signIn(page, PEOPLE.manager);
    await openTab(page, projectId, 'Sites');
    await expect(page.getByText('Not accepted', { exact: true })).toBeVisible({ timeout: 20_000 });

    await page.getByLabel('Mobilised on').fill(daysAgo(10));
    await page.getByLabel('Accepted on').fill(daysAgo(2));
    await page.getByRole('button', { name: 'Save dates' }).click();
    // A date alone is not acceptance (§4.19.6): the certificate completes it.
    await expect(page.getByText(/no certificate is attached/i)).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText('Not accepted', { exact: true })).toBeVisible();

    await page.locator('input[type=file]').first().setInputFiles(photo('certificate.png'));
    await expect(page.getByText('Accepted', { exact: true })).toBeVisible({ timeout: 30_000 });
  });

  test('Finance adds the default milestones, an invoice and a part receipt', async ({ page }) => {
    await signIn(page, PEOPLE.owner);
    await openTab(page, projectId, 'Milestones');
    await page.getByRole('button', { name: 'Add default milestones' }).click();
    await expect(page.getByText(/M1 · /).filter({ visible: true }).first()).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText(/M3 · /).filter({ visible: true }).first()).toBeVisible();

    await page.getByRole('button', { name: 'Invoice', exact: true }).filter({ visible: true }).first().click();
    let sheet = page.getByRole('dialog');
    await sheet.getByLabel('Invoice number').fill(`INV-${run}`);
    await sheet.getByLabel('Amount').fill(String(INVOICE));
    await sheet.getByRole('button', { name: 'Record invoice' }).click();
    await expect(sheet.getByText('Invoice recorded.')).toBeVisible({ timeout: 20_000 });
    await sheet.getByRole('button', { name: 'Done' }).click();
    await expect(sheet).toBeHidden();

    await page.getByRole('button', { name: 'Receipt', exact: true }).filter({ visible: true }).first().click();
    sheet = page.getByRole('dialog');
    await sheet.getByLabel('Amount').fill(String(RECEIPT));
    await sheet.getByLabel('Reference').fill(`RTGS${run}`);
    await sheet.getByRole('button', { name: 'Record receipt' }).click();
    await expect(sheet).toBeHidden({ timeout: 20_000 });

    // Outstanding is the PO value less what has been received; the invoiced balance is in its hint.
    await expect(
      page.locator('div.rounded-xl', { hasText: /^Outstanding/i }).first(),
    ).toContainText('KES 98K', { timeout: 20_000 });
    await expect(page.getByText(`Invoiced, unpaid: ${(INVOICE - RECEIPT).toFixed(2)}`)).toBeVisible();
    await expect(page.getByText('Part paid').filter({ visible: true }).first()).toBeVisible();
  });

  test('a project without a PO gets one: Attach PO', async ({ page }) => {
    await signIn(page, PEOPLE.owner);
    await open(page, `/projects/${noPoProjectId}`);
    await page.getByRole('button', { name: 'Attach PO' }).click();
    const sheet = page.getByRole('dialog');
    await sheet.getByLabel('PO number').fill(`LATE-${run}`);
    await sheet.getByLabel('PO issue date').fill(today());
    await sheet.getByLabel('Contract value').fill('250000');
    await sheet.getByLabel('Cost budget').fill('150000');
    await sheet.getByLabel('Payment terms', { exact: true }).fill('45 days');
    await sheet.getByLabel('Payment terms, in days').fill('45');
    await sheet.getByRole('button', { name: 'Attach PO' }).click();
    await expect(sheet.getByText(/PO attached/)).toBeVisible({ timeout: 20_000 });
    await sheet.getByRole('button', { name: 'Done' }).click();

    // The PO is now the project's heading, with its milestones seeded.
    await expect(page.getByRole('heading', { name: `LATE-${run}` })).toBeVisible({
      timeout: 20_000,
    });
    await page.getByRole('button', { name: 'Milestones', exact: true }).click();
    await expect(page.getByText(/M1 · /).filter({ visible: true }).first()).toBeVisible({ timeout: 20_000 });
    await page.getByRole('button', { name: 'Budget', exact: true }).click();
    await expect(page.locator('div.rounded-xl', { hasText: /^Budget/i }).first()).toContainText(
      'KES 150K',
      { timeout: 20_000 },
    );
  });

  test('a subcontract payment: Finance records, the project manager approves, paid and owed show', async ({
    page,
    request,
  }) => {
    await signIn(page, PEOPLE.owner);
    await openTab(page, projectId, 'Subcontracts');
    await expect(page.getByText(subcontractorName)).toBeVisible({ timeout: 20_000 });
    await page.getByRole('button', { name: 'Record payment' }).click();
    const sheet = page.getByRole('dialog');
    await sheet.getByLabel('Amount').fill(String(SUB_PAYMENT));
    await sheet.getByLabel('Reference').fill(`SUB${run}`);
    await sheet.getByRole('button', { name: 'Send for approval' }).click();
    await expect(sheet).toBeHidden({ timeout: 20_000 });
    await expect(page.getByText(`SUB${run}`)).toBeVisible({ timeout: 20_000 });

    // Not paid until the PM approves.
    const before = await getJson(request, PEOPLE.owner, `subcontracts/${subcontractId}`);
    expect(Number(before.position.paid)).toBe(0);

    await page.getByRole('button', { name: /sign out/i }).first().click();
    await signIn(page, PEOPLE.manager);
    await open(page, '/approvals');
    await page.getByRole('button', { name: 'Subcontract payments', exact: true }).click();
    await page.getByText(money(SUB_PAYMENT)).filter({ visible: true }).first().click();
    const decide = page.getByRole('dialog');
    await decide.getByRole('button', { name: 'Approve' }).click();
    await expect(decide).toBeHidden({ timeout: 20_000 });

    await expect
      .poll(
        async () =>
          Number((await getJson(request, PEOPLE.owner, `subcontracts/${subcontractId}`)).position.paid),
        { timeout: 20_000 },
      )
      .toBe(SUB_PAYMENT);

    await page.getByRole('button', { name: /sign out/i }).first().click();
    await signIn(page, PEOPLE.owner);
    await openTab(page, projectId, 'Subcontracts');
    const dl = page.locator('dl').filter({ hasText: 'Contract value' }).first();
    await expect(dl).toContainText(money(SUBCONTRACT_VALUE));
    await expect(dl.locator('div', { hasText: /^Paid/ }).first()).toContainText(money(SUB_PAYMENT), {
      timeout: 20_000,
    });
  });
});
