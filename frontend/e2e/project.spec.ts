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
    await page.getByLabel('Our reference').fill(`WO-${poNumber}`);
    await page.getByLabel('PO number').fill(poNumber);
    await page.getByLabel('Title').fill('E2E rollout');

    // O1: the commercial fields appear only once a PO number is typed, because
    // that is the moment they become required.
    await expect(page.getByLabel('Project manager')).toBeVisible();
    await page.getByLabel('Project manager').selectOption({ label: 'Pippa Manager' });
    await page.getByLabel('Contract value').fill('500000');
    await page.getByLabel('Cost budget').fill('300000');

    await page.getByRole('button', { name: /^create$/i }).click();

    // DataList renders cards *and* a table with one hidden by CSS (§7.3), so
    // `.first()` can pick the hidden one. Filtering to what is visible is what
    // makes this assertion work at either width — the same pattern the gate-out
    // spec settled on.
    await expect(
      page.getByText(poNumber).filter({ visible: true }).first(),
    ).toBeVisible({ timeout: 15_000 });

    const projects = await (
      await request.get(`${api}/api/v1/projects?page_size=100`, { headers })
    ).json();
    projectId = projects.results.find(
      (row: { po_number: string }) => row.po_number === poNumber,
    ).id;
    expect(projectId).toBeGreaterThan(0);
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

    jobReference = `JOB-E2E-${Date.now().toString().slice(-6)}`;
    await page.getByRole('button', { name: /add a job/i }).click();
    await page.getByLabel('Reference').fill(jobReference);
    await page.getByLabel('Site').selectOption({ index: 1 });
    await page.getByLabel('Who is responsible').selectOption({ label: 'Tom Technician' });
    await page.getByRole('button', { name: /raise it/i }).click();

    await expect(
      page.getByText(jobReference).filter({ visible: true }).first(),
    ).toBeVisible({ timeout: 15_000 });

    // And the job really is under this project, not merely on its screen.
    const jobs = await (
      await request.get(`${api}/api/v1/jobs?project=${projectId}`, { headers })
    ).json();
    expect(
      jobs.results.some((row: { reference: string }) => row.reference === jobReference),
    ).toBe(true);
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
        reference: `JOB-E2E-NOPE-${Date.now().toString().slice(-6)}`,
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
