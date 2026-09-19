/**
 * T10.24 — the project lifecycle end to end (§14; Epic O).
 *
 * The flow this walks is the one Epic O exists for: a PO arrives, material goes
 * out against it under the manager's signature, the work is closed out with the
 * days it took, a cost is claimed and approved, and the project closes with a
 * margin somebody can be shown.
 *
 * Three people, three sessions, deliberately. The whole of D22 is that project
 * material routes to **one named person** and nobody else, and a flow driven as
 * a single superuser would pass while that was broken.
 */

import { expect, test } from '@playwright/test';

import { PASSWORD, PEOPLE, open, signIn } from './fixtures';

test.describe.configure({ mode: 'serial' });

const api = process.env.E2E_API_URL ?? 'http://127.0.0.1:8000';
const host = new URL(process.env.E2E_BASE_URL ?? 'http://demo.localhost:5173').hostname;
const tenant = { Host: host };

/** Carried between the steps of the flow. */
let poNumber = '';
let projectId = 0;
// Unique per run, like the PO number: a job reference is unique per tenant, so
// a re-run against a tenant that was not reseeded would collide with itself.
let jobReference = '';

async function authed(request: Parameters<typeof asOwner>[0], who: string) {
  const login = await request.post(`${api}/api/v1/auth/login`, {
    headers: tenant,
    data: { identifier: who, password: PASSWORD },
  });
  const { access } = await login.json();
  return { ...tenant, Authorization: `Bearer ${access}` };
}

async function asOwner(request: import('@playwright/test').APIRequestContext) {
  return authed(request, PEOPLE.owner);
}

/**
 * A client to hang the project on.
 *
 * Created here rather than assumed, because `seed_demo` seeds the catalogue and
 * the yard but no commercial parties — and a spec that skipped when the fixture
 * was missing would go quietly green while testing nothing.
 */
async function ensureClient(
  request: import('@playwright/test').APIRequestContext,
  headers: Record<string, string>,
): Promise<number> {
  const existing = await (
    await request.get(`${api}/api/v1/clients?page_size=20`, { headers })
  ).json();
  if (existing.results.length) return existing.results[0].id;

  const created = await request.post(`${api}/api/v1/clients`, {
    headers,
    data: { name: 'E2E Operator' },
  });
  return (await created.json()).id;
}

test.describe('A purchase order, end to end', () => {
  test('an owner opens a project against a PO', async ({ page, request }) => {
    const headers = await asOwner(request);

    await ensureClient(request, headers);
    const people = await (
      await request.get(`${api}/api/v1/users?page_size=50`, { headers })
    ).json();
    const manager = people.results.find(
      (row: { email: string }) => row.email === PEOPLE.manager,
    );
    expect(manager, 'the demo tenant needs a project manager — re-run seed_demo').toBeTruthy();

    poNumber = `PO-E2E-${Date.now().toString().slice(-6)}`;

    await signIn(page, PEOPLE.owner);
    await open(page, '/projects');

    await page.getByRole('button', { name: /new project/i }).click();
    await page.getByLabel('Client').selectOption({ index: 1 });
    // M6: no reference field any more — the tenant's PROJECT series numbers it.
    await expect(page.getByLabel('Our reference')).toHaveCount(0);
    await page.getByLabel('PO number').fill(poNumber);
    await page.getByLabel('Title').fill('E2E rollout');

    // O1: the commercial fields appear only once a PO number is typed, because
    // that is the moment they become required.
    await expect(page.getByLabel('Project manager')).toBeVisible();
    await page.getByLabel('Project manager').selectOption({ label: 'Pippa Manager' });
    await page.getByLabel('Contract value').fill('500000');
    await page.getByLabel('Cost budget').fill('300000');

    // The tenant's currency is shown, and a bare 500000 is echoed as something
    // a person can actually check. `40000000` in a box is not money.
    // Scoped to the sheet: a project created by an earlier run can carry the
    // same figure in the list behind it, and at phone width that copy is in the
    // DOM but hidden.
    await expect(
      page.getByRole('dialog').getByText('KES 500,000.00').first(),
    ).toBeVisible();

    await page.getByRole('button', { name: /^create$/i }).click();

    // Found the way a person would: by the reference on their paperwork. This
    // also covers what the list shows — `po_number` is the client's and typed,
    // `reference` is ours and allocated, and the list used to show whichever
    // existed, so a project could not be found by the one you happened to know.
    //
    // Searching rather than scanning: the demo tenant accumulates projects, and
    // a test that waits for a row somewhere in a long list is timing how fast
    // the list renders rather than whether the project exists.
    await page.getByLabel('Search projects').fill(poNumber);

    // DataList renders cards *and* a table with one hidden by CSS (§7.3), so
    // `.first()` can pick the hidden one.
    await expect(
      page.getByText(poNumber).filter({ visible: true }).first(),
    ).toBeVisible({ timeout: 20_000 });

    const projects = await (
      await request.get(`${api}/api/v1/projects?page_size=100`, { headers })
    ).json();

    const created = projects.results.find(
      (row: { po_number: string }) => row.po_number === poNumber,
    );
    projectId = created.id;
    expect(projectId).toBeGreaterThan(0);
    // Allocated from the series, not typed — PRJ-000001 and counting.
    expect(created.reference).toMatch(/^[A-Z]+-\d+$/);
  });

  test('an owner edits it, and a job is raised under it', async ({ page, request }) => {
    // Both screens were missing until now: the create sheet only created, and
    // no screen anywhere called POST /jobs — so H1 named an actor who could
    // not carry it out.
    const headers = await asOwner(request);

    await signIn(page, PEOPLE.owner);
    await open(page, `/projects/${projectId}`);

    await page.getByRole('button', { name: /^edit$/i }).click();
    await page.getByLabel('Title').fill('E2E rollout, corrected');
    await page.getByRole('button', { name: /^save$/i }).click();

    await expect(page.getByText('E2E rollout, corrected').first()).toBeVisible({
      timeout: 15_000,
    });

    // Tiles abbreviate: nine zeros defeat the scanning a tile is for. The
    // exact figure stays on the title attribute, and the tables stay exact.
    await expect(page.getByText('KES 500K').first()).toBeVisible();

    await page.getByRole('button', { name: /add a job/i }).click();
    // M6: the job numbers itself too.
    await expect(page.getByLabel('Reference')).toHaveCount(0);
    await page.getByLabel('Site').selectOption({ index: 1 });
    await page.getByLabel('Who is responsible').selectOption({ label: 'Tom Technician' });
    await page.getByRole('button', { name: /raise it/i }).click();

    // The job is under this project, and carries a number nobody typed.
    //
    // Polled rather than read once: the click returns as soon as the request is
    // in flight, and asserting straight afterwards raced the POST — which
    // failed as "no jobs" and looked like the project link was broken.
    await expect
      .poll(
        async () => {
          const response = await request.get(
            `${api}/api/v1/jobs?project=${projectId}`,
            { headers },
          );
          return (await response.json()).results.length;
        },
        { timeout: 15_000 },
      )
      .toBe(1);

    const jobs = await (
      await request.get(`${api}/api/v1/jobs?project=${projectId}`, { headers })
    ).json();
    jobReference = jobs.results[0].reference;
    expect(jobReference).toMatch(/^[A-Z]+-\d+$/);

    await expect(
      page.getByText(jobReference).filter({ visible: true }).first(),
    ).toBeVisible({ timeout: 15_000 });
  });

  test('a subcontractor is put on the register, and a job goes to them', async ({
    page,
    request,
  }) => {
    // O4 built the register and the job sheet read from it, but nothing created
    // a contractor — so "delivered by a subcontractor" was a select with one
    // option in it, and the whole subcontracting half of Epic O was unreachable
    // from the UI. This walks the gap that closed: make one, then use it.
    const headers = await asOwner(request);
    const name = `E2E Riggers ${Date.now().toString().slice(-6)}`;

    await signIn(page, PEOPLE.owner);
    await open(page, '/settings/network?tab=subcontractors');

    await page.getByRole('button', { name: /new subcontractor/i }).click();
    const register = page.getByRole('dialog');
    await register.getByLabel('Name').fill(name);
    await register.getByLabel('Contact', { exact: true }).fill('Ruth Rigger');
    await register.getByRole('button', { name: /add them/i }).click();

    // Searched rather than scanned: the register only ever grows (no destroy),
    // so a re-run against a live tenant would be reading page one of many.
    await page.getByLabel('Search subcontractors').fill(name);
    await expect(
      page.getByText(name).filter({ visible: true }).first(),
    ).toBeVisible({ timeout: 20_000 });

    // And now the job sheet can actually offer them.
    await open(page, `/projects/${projectId}`);
    await page.getByRole('button', { name: /add a job/i }).click();

    const sheet = page.getByRole('dialog');
    await sheet.getByLabel('Site').selectOption({ index: 1 });
    await sheet.getByLabel('Who is responsible').selectOption({ label: 'Tom Technician' });
    await sheet.getByLabel('Who delivers it').selectOption('SUBCONTRACTED');
    await sheet.getByLabel('Subcontractor').selectOption({ label: name });
    await sheet.getByLabel('Agreed price').fill('75000');
    await sheet.getByRole('button', { name: /raise it/i }).click();

    // Polled: the click returns while the POST is still in flight.
    await expect
      .poll(
        async () => {
          const jobs = await (
            await request.get(`${api}/api/v1/jobs?project=${projectId}`, { headers })
          ).json();
          return jobs.results.filter(
            (row: { subcontractor_name?: string }) => row.subcontractor_name === name,
          ).length;
        },
        { timeout: 20_000 },
      )
      .toBe(1);
  });

  test('a technician cannot raise a job', async ({ request }) => {
    // H1: raising work and finishing it are different acts. This used to be
    // allowed, while the storekeeper H1 actually names was locked out.
    const headers = await authed(request, PEOPLE.technician);
    const sites = await (
      await request.get(`${api}/api/v1/sites?page_size=5`, { headers })
    ).json();

    const response = await request.post(`${api}/api/v1/jobs`, {
      headers,
      data: {
        client: sites.results[0].client,
        site: sites.results[0].id,
        assignee: sites.results[0].id,
      },
    });

    expect(response.status()).toBe(403);
  });

  test('a PO with no manager is refused', async ({ request }) => {
    // O1: refused by the database, so it is refused here too — a project nobody
    // manages is one nobody can release material for.
    const headers = await asOwner(request);
    const clientId = await ensureClient(request, headers);

    const response = await request.post(`${api}/api/v1/projects`, {
      headers,
      data: {
        client: clientId,
        reference: `WO-BAD-${Date.now().toString().slice(-6)}`,
        po_number: `PO-BAD-${Date.now().toString().slice(-6)}`,
      },
    });

    expect(response.status()).toBe(400);
    expect(JSON.stringify(await response.json())).toContain('manager');
  });

  test('the manager sees cost but not margin', async ({ request }) => {
    // O14, field by field. A margin the manager was not sent must be **absent**,
    // not null — null would be a claim about the project.
    const headers = await authed(request, PEOPLE.manager);
    const performance = await (
      await request.get(`${api}/api/v1/projects/${projectId}/performance`, { headers })
    ).json();

    expect(performance).toHaveProperty('cost_to_date');
    expect(performance).not.toHaveProperty('margin');
    expect(performance).not.toHaveProperty('contract_value');
  });

  test('the owner sees both', async ({ request }) => {
    const headers = await asOwner(request);
    const performance = await (
      await request.get(`${api}/api/v1/projects/${projectId}/performance`, { headers })
    ).json();

    expect(performance).toHaveProperty('cost_to_date');
    expect(performance).toHaveProperty('margin');
  });

  test('a storekeeper sees no money at all', async ({ request }) => {
    const headers = await authed(request, PEOPLE.storekeeper);
    const response = await request.get(`${api}/api/v1/projects/${projectId}`, {
      headers,
    });
    const project = await response.json();

    expect(project.po_number).toBe(poNumber);
    expect(project).not.toHaveProperty('contract_value');
    expect(project).not.toHaveProperty('cost_budget');
  });

  test('anyone records an expense and only the manager approves it', async ({ request }) => {
    const storekeeper = await authed(request, PEOPLE.storekeeper);
    const categories = await (
      await request.get(`${api}/api/v1/expense-categories?page_size=20`, {
        headers: storekeeper,
      })
    ).json();
    test.skip(!categories.results.length, 'no seeded expense categories');

    // O16: recorded by whoever incurred it.
    const created = await request.post(`${api}/api/v1/project-expenses`, {
      headers: storekeeper,
      data: {
        project: projectId,
        category: categories.results[0].id,
        amount: '4500.00',
        incurred_on: new Date().toISOString().slice(0, 10),
        description: 'E2E fuel',
      },
    });
    expect(created.status()).toBe(201);
    const expense = await created.json();
    expect(expense.status).toBe('SUBMITTED');

    // D29: the storekeeper who raised it cannot make it count.
    const refused = await request.post(
      `${api}/api/v1/project-expenses/${expense.id}/decide`,
      { headers: storekeeper, data: { approved: true } },
    );
    expect(refused.status()).toBeGreaterThanOrEqual(400);

    const manager = await authed(request, PEOPLE.manager);
    const approved = await request.post(
      `${api}/api/v1/project-expenses/${expense.id}/decide`,
      { headers: manager, data: { approved: true } },
    );
    expect(approved.ok()).toBe(true);
    expect((await approved.json()).status).toBe('APPROVED');
  });

  test('the approved expense reaches the project cost', async ({ request }) => {
    // O11: derived, not typed. Nothing wrote this figure anywhere.
    const headers = await asOwner(request);
    const performance = await (
      await request.get(`${api}/api/v1/projects/${projectId}/performance`, { headers })
    ).json();

    expect(Number(performance.expenses)).toBe(4500);
    expect(Number(performance.cost_to_date)).toBeGreaterThanOrEqual(4500);
  });

  test('the manager closes it and the figures stop moving', async ({ request }) => {
    const manager = await authed(request, PEOPLE.manager);

    const closed = await request.post(`${api}/api/v1/projects/${projectId}/close`, {
      headers: manager,
      data: { reason: 'E2E complete.' },
    });
    expect(closed.ok()).toBe(true);

    const owner = await asOwner(request);
    const before = await (
      await request.get(`${api}/api/v1/projects/${projectId}/performance`, {
        headers: owner,
      })
    ).json();

    // A further expense cannot even be raised against a closed project (O13),
    // which is the first half of the figures being final.
    const categories = await (
      await request.get(`${api}/api/v1/expense-categories?page_size=20`, {
        headers: owner,
      })
    ).json();
    const refused = await request.post(`${api}/api/v1/project-expenses`, {
      headers: owner,
      data: {
        project: projectId,
        category: categories.results[0].id,
        amount: '999.00',
        incurred_on: new Date().toISOString().slice(0, 10),
      },
    });
    expect(refused.status()).toBe(400);

    const after = await (
      await request.get(`${api}/api/v1/projects/${projectId}/performance`, {
        headers: owner,
      })
    ).json();
    expect(after.cost_to_date).toBe(before.cost_to_date);
  });

  test('the performance report lists it', async ({ request }) => {
    const headers = await asOwner(request);
    const report = await (
      await request.get(`${api}/api/v1/reports/project-performance`, { headers })
    ).json();

    const row = (report.rows ?? []).find(
      (entry: { reference: string }) => entry.reference === poNumber,
    );
    expect(row).toBeTruthy();
    // The report formats every value through its column spec, so the screen,
    // the spreadsheet and the PDF cannot disagree (§10, M2). That means
    // thousands separators here, and a test reading it has to say so.
    expect(Number(String(row.expenses).replace(/,/g, ''))).toBe(4500);
  });
});
