/**
 * Finance stage 4 on a phone (Epic R, T17.17; design §4.20.12).
 *
 * A storekeeper adds a supplier from inside the gate-in form and receives a
 * delivery naming it. Finance checks the supplier and approves it. The owner
 * adds a vehicle and hands it to a technician, who records a fuel expense
 * choosing that vehicle; after the project manager and Finance approve it the
 * vehicle's fuel panel shows the spend and litres. A second fill, for a hired
 * truck that is not in the register, is ticked "Not ours" and keeps the typed
 * registration.
 *
 * Online throughout: the offline variants of the supplier add and the fuel pick
 * are covered by their unit tests and the sync tests; this is the screens.
 */

import { type APIRequestContext, expect, test } from '@playwright/test';

import { chooseSupplier, PASSWORD, PEOPLE, open, pickItem, signIn, unique } from './fixtures';

test.describe.configure({ mode: 'serial' });

const api = process.env.E2E_API_URL ?? 'http://127.0.0.1:8000';
const host = new URL(process.env.E2E_BASE_URL ?? 'http://demo.localhost:5173').hostname;
const tenant = { Host: host };

const run = unique('AS');
const supplierName = `E2E Supplier ${run}`;
const siteRef = `AS-${run}`;
const siteName = `E2E Assets Site ${run}`;
const vehicleName = `E2E Hilux ${run}`;
const registration = `KZ${Date.now().toString().slice(-6)}`;
const hiredReg = `KH${Date.now().toString().slice(-6)}`;
const amount = 4000 + (Date.now() % 900);
const hiredAmount = 1000 + (Date.now() % 900);
const litres = 30 + (Date.now() % 50);
const money = (value: number) =>
  `KES ${value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

let projectId = 0;
let supplierId = 0;
let assetId = 0;

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

type Expense = {
  id: number;
  status: string;
  amount: string;
  vehicle: number | null;
  vehicle_reg: string;
};

async function expenses(request: APIRequestContext): Promise<Expense[]> {
  return (await getJson(request, PEOPLE.owner, `project-expenses?project=${projectId}&page_size=50`))
    .results;
}

async function expenseByAmount(request: APIRequestContext, value: number) {
  return (await expenses(request)).find((e) => Number(e.amount) === value);
}

test.describe('Assets and suppliers, on a phone', () => {
  test.beforeEach(({}, testInfo) => {
    test.skip(testInfo.project.name !== 'phone', 'this is done on a phone');
  });

  test('a storekeeper adds a supplier at the gate and receives a delivery naming it', async ({
    page,
    request,
  }) => {
    await signIn(page, PEOPLE.storekeeper);
    await open(page, '/gate-in/new');
    await page.getByLabel('Source').selectOption('PURCHASE');
    await chooseSupplier(page, supplierName);

    const receiveInto = page.getByLabel('Received into');
    await receiveInto
      .locator('option:not([value=""]):not([data-add-new])')
      .first()
      .waitFor({ state: 'attached', timeout: 20_000 });
    await receiveInto.selectOption({ index: 1 });

    await page.getByRole('button', { name: 'Add a line' }).click();
    const sheet = page.getByRole('dialog');
    const bulk = await pickItem(sheet, 'Item', 'Cable clamp');
    test.skip(!bulk, 'no bulk item in the seeded catalogue');
    await sheet.getByLabel(/^Quantity/).fill('7');
    await sheet.getByRole('button', { name: 'Add line' }).click();
    await expect(sheet).toBeHidden();

    await page.getByRole('button', { name: /receive it|save on this device/i }).click();
    await expect(page.getByText(/GRN-\d+/).filter({ visible: true }).first()).toBeVisible({
      timeout: 30_000,
    });

    // It landed PENDING, and the delivery is linked to it.
    const found = await getJson(request, PEOPLE.owner, `suppliers?search=${run}&page_size=10`);
    expect(found.results).toHaveLength(1);
    expect(found.results[0].status).toBe('PENDING');
    supplierId = found.results[0].id;

    const gateIns = await getJson(request, PEOPLE.owner, `gate-ins?search=${run}&page_size=10`);
    const linked = (gateIns.results as { supplier: number | null }[]).filter(
      (g) => g.supplier === supplierId,
    );
    expect(linked.length).toBeGreaterThanOrEqual(1);
  });

  test('Finance adds the PIN and approves the supplier', async ({ page, request }) => {
    // The PIN and a payment route are Finance's to add (§4.20.3): over the API,
    // as the Network screen would do it. The approval itself is by the screen.
    const owner = await headersFor(request, PEOPLE.owner);
    const patch = await request.patch(`${api}/api/v1/suppliers/${supplierId}`, {
      headers: owner,
      data: {
        kra_pin: `P${Date.now().toString().slice(-9)}Z`,
        mpesa_type: 'TILL',
        mpesa_number: '123456',
      },
    });
    expect(patch.ok(), await patch.text()).toBeTruthy();

    await signIn(page, PEOPLE.owner);
    await open(page, '/approvals');
    await page.getByRole('button', { name: 'Suppliers', exact: true }).click();
    await page.getByText(supplierName).filter({ visible: true }).first().click();
    const sheet = page.getByRole('dialog');
    await expect(sheet.getByText('Added by')).toBeVisible();
    await sheet.getByRole('button', { name: 'Approve' }).click();
    await expect(sheet).toBeHidden({ timeout: 20_000 });

    await expect
      .poll(
        async () => (await getJson(request, PEOPLE.owner, `suppliers/${supplierId}`)).status,
        { timeout: 20_000 },
      )
      .toBe('APPROVED');
  });

  test('the owner adds a vehicle and hands it to a technician', async ({ page, request }) => {
    const owner = await headersFor(request, PEOPLE.owner);
    const users = await (
      await request.get(`${api}/api/v1/users?page_size=100`, { headers: owner })
    ).json();
    const tech = users.results.find((u: { email: string }) => u.email === PEOPLE.technician);
    const manager = users.results.find((u: { email: string }) => u.email === PEOPLE.manager);
    expect(tech && manager).toBeTruthy();

    // A project for the fuel to be charged to, set up over the API.
    const clients = await (
      await request.get(`${api}/api/v1/clients?page_size=5`, { headers: owner })
    ).json();
    const client = clients.results[0].id;
    const site = await request.post(`${api}/api/v1/sites`, {
      headers: owner,
      data: { client, internal_ref: siteRef, name: siteName, latitude: "-1.2921", longitude: "36.8219" },
    });
    expect(site.ok(), await site.text()).toBeTruthy();
    const project = await request.post(`${api}/api/v1/projects`, {
      headers: owner,
      data: {
        client,
        po_number: `PO-${run}`,
        title: `E2E assets ${run}`,
        manager: manager.id,
        contract_value: '900000',
        cost_budget: '500000',
        sites: [(await site.json()).id],
      },
    });
    expect(project.ok(), await project.text()).toBeTruthy();
    projectId = (await project.json()).id;

    await signIn(page, PEOPLE.owner);
    await open(page, '/assets');
    await page.getByRole('button', { name: 'New asset' }).click();
    const add = page.getByRole('dialog');
    await add.getByLabel('Name', { exact: true }).fill(vehicleName);
    await add.getByLabel('Registration').fill(registration);
    await add.getByRole('button', { name: 'Add asset' }).click();

    // It opens on the asset's own page.
    await expect(page).toHaveURL(/\/assets\/\d+/, { timeout: 20_000 });
    assetId = Number(page.url().match(/\/assets\/(\d+)/)?.[1]);
    await expect(page.getByRole('heading', { name: vehicleName })).toBeVisible();

    await page.getByRole('button', { name: 'Hand over', exact: true }).click();
    const sheet = page.getByRole('dialog');
    await sheet.getByLabel('Give it to').selectOption({ label: tech.full_name });
    await sheet.getByRole('button', { name: 'Hand over', exact: true }).click();
    await expect(sheet).toBeHidden({ timeout: 20_000 });

    // The history shows the yard handing it to the technician.
    await expect(page.getByText(new RegExp(`→ ${tech.full_name}`))).toBeVisible({
      timeout: 20_000,
    });
  });

  test('the technician records fuel for that vehicle, and a hired truck as Not ours', async ({
    page,
    request,
  }) => {
    await signIn(page, PEOPLE.technician);

    for (const [value, litresValue, hired] of [
      [amount, String(litres), false],
      [hiredAmount, '', true],
    ] as const) {
      await open(page, '/money/expenses/new');
      await page
        .getByLabel('Site', { exact: true })
        .selectOption({ label: `${siteRef} ${siteName}` });
      await expect(page.getByText(`E2E assets ${run}`)).toBeVisible();

      const kind = page.getByLabel('What kind');
      const fuelLabel = await kind
        .locator('option')
        .filter({ hasText: /fuel/i })
        .first()
        .textContent();
      expect(fuelLabel, 'a fuel category in the demo tenant').toBeTruthy();
      await kind.selectOption({ label: fuelLabel!.trim() });

      if (hired) {
        await page.getByLabel(/^Not ours/).check();
        await page.getByLabel('Vehicle registration').fill(hiredReg);
      } else {
        const vehicle = page.getByLabel('Vehicle', { exact: true });
        const option = vehicle.locator('option', { hasText: registration });
        await expect(option).toHaveCount(1, { timeout: 20_000 });
        await vehicle.selectOption({ label: (await option.textContent())!.trim() });
      }
      if (litresValue) await page.getByLabel('Litres').fill(litresValue);
      await page.getByLabel('Amount', { exact: true }).fill(String(value));
      await page.getByLabel('What for').fill(`Fuel ${run}`);
      await page.getByRole('button', { name: 'Record it' }).click();
      await expect(page.getByText(/Saved on this phone|recorded|sent/i).first()).toBeVisible({
        timeout: 20_000,
      });
    }

    await expect
      .poll(async () => (await expenses(request)).length, { timeout: 30_000 })
      .toBe(2);
    const ours = await expenseByAmount(request, amount);
    expect(ours?.vehicle).toBe(assetId);
    const hired = await expenseByAmount(request, hiredAmount);
    expect(hired?.vehicle).toBeNull();
    expect(hired?.vehicle_reg).toBe(hiredReg);
  });

  test('after the project manager and Finance approve, the vehicle shows its fuel', async ({
    page,
    request,
  }) => {
    // Pending fuel is not counted yet.
    await signIn(page, PEOPLE.manager);
    await open(page, '/approvals');
    await page.getByRole('button', { name: 'Expenses', exact: true }).click();
    await page.getByText(money(amount)).filter({ visible: true }).first().click();
    await page.getByRole('dialog').getByRole('button', { name: 'Approve' }).click();
    await expect(page.getByRole('dialog')).toBeHidden();
    await expect
      .poll(async () => (await expenseByAmount(request, amount))?.status, { timeout: 20_000 })
      .toBe('PENDING_FINANCE');

    await page.getByRole('button', { name: /sign out/i }).first().click();
    await signIn(page, PEOPLE.owner);
    await open(page, '/approvals');
    await page.getByRole('button', { name: 'Expenses', exact: true }).click();
    await page.getByText(money(amount)).filter({ visible: true }).first().click();
    await page.getByRole('dialog').getByRole('button', { name: 'Approve' }).click();
    await expect(page.getByRole('dialog')).toBeHidden();
    await expect
      .poll(async () => (await expenseByAmount(request, amount))?.status, { timeout: 20_000 })
      .toBe('APPROVED');

    await open(page, `/assets/${assetId}`);
    await expect(page.getByText('Fuel', { exact: true })).toBeVisible();
    await expect(page.getByText(money(amount), { exact: true }).first()).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText(new RegExp(`^${litres}(\\.0+)?$`)).first()).toBeVisible();
  });
});
