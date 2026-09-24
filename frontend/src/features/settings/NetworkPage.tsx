/**
 * T2.13 — network and location screens (design §7.4; C5, C6, C7, D6).
 *
 * The criterion: "a site can be created with three differently-labelled
 * references and found by any of them."
 *
 * That is why the reference rows are part of the create form rather than a later
 * edit step. C5 exists because the operator calls a site one thing, the towerco
 * calls it another, and the crew calls it a third — and the person searching has
 * only ever heard one of the three. Recording them one at a time afterwards means
 * the second and third never get entered.
 */

import { useState } from 'react';
import { useFieldArray, useForm } from 'react-hook-form';
import { useSearchParams } from 'react-router-dom';

import { applyFieldErrors, useAction, useList } from '../../api/hooks';
import { Banner, Button, Field, Input, Select, Spinner, Textarea } from '../../components/ui';
import { ReferenceSelect } from '../../components/ui/ReferenceSelect';
import { DataList, EmptyState, ListState, PageHeader, Sheet, StatusBadge } from '../../components/ui/data';
import { SearchField } from '../../components/ui/SearchField';
import { TabStrip } from '../../components/ui/TabStrip';
import { SwipePane } from '../../components/ui/SwipePane';
import { useSwipeTabs } from '../../components/ui/useSwipeTabs';
import type { Subcontractor } from '../projects/types';
import type { Client, Location, Site, Project } from './types';

type Tab = 'sites' | 'clients' | 'projects' | 'subcontractors' | 'locations';

const TABS: { key: Tab; label: string }[] = [
  { key: 'sites', label: 'Sites' },
  { key: 'clients', label: 'Clients' },
  { key: 'projects', label: 'Projects' },
  // O4: the register had an API and no screen, so a job could be marked
  // subcontracted only if somebody had already made a contractor another way.
  { key: 'subcontractors', label: 'Subcontractors' },
  { key: 'locations', label: 'Locations' },
];

export default function NetworkPage() {
  const [params, setParams] = useSearchParams();
  const tab = (params.get('tab') as Tab) || 'sites';
  // Five tabs do not fit a phone's width; a thumb across the content is how
  // people expect to move between them. The strip still works.
  const swipe = useSwipeTabs(
    TABS.map((entry) => entry.key),
    tab,
    (next) => setParams({ tab: next }),
  );

  return (
    <div className="flex flex-col gap-4" {...swipe.handlers}>
      <PageHeader
        title="Network and locations"
        subtitle="Who the work is for, where it happens, and where material is kept."
      />

      <TabStrip tabs={TABS} current={tab} onSelect={(next) => setParams({ tab: next })} />

      <SwipePane {...swipe.pane}>
        {tab === 'sites' ? <SitesTab /> : null}
        {tab === 'clients' ? <ClientsTab /> : null}
        {tab === 'projects' ? <ProjectsTab /> : null}
        {tab === 'subcontractors' ? <SubcontractorsTab /> : null}
        {tab === 'locations' ? <LocationsTab /> : null}
      </SwipePane>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Sites                                                                      */
/* -------------------------------------------------------------------------- */

function SitesTab() {
  const [search, setSearch] = useState('');
  const [sheet, setSheet] = useState(false);
  const sites = useList<Site>('sites', { search: search || undefined, page_size: 50 });
  const clients = useList<Client>('clients', { page_size: 200 });

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-2">
        <Input
          className="max-w-sm"
          placeholder="Any reference, name or code"
          aria-label="Search sites"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <Button onClick={() => setSheet(true)}>New site</Button>
      </div>

      {/* C5: one box, and it searches every reference a site has — because the
          person searching has only ever heard one of them. */}
      <p className="text-sm text-slate-600">
        Searches the internal reference, the name, and every operator or towerco
        reference recorded against a site.
      </p>

      <ListState query={sites}>
        <DataList
          rows={sites.data?.results ?? []}
          rowKey={(row) => row.id}
          empty={
            <EmptyState
              title={search ? `Nothing matches "${search}".` : 'No sites yet.'}
              hint="A site is where installed material ends up, and what a reconciliation is about."
              action={<Button onClick={() => setSheet(true)}>New site</Button>}
            />
          }
          columns={[
            { header: 'Reference', cell: (row) => row.internal_ref },
            { header: 'Name', cell: (row) => row.name },
            { header: 'Client', cell: (row) => row.client_name ?? '—' },
            { header: 'Status', cell: (row) => <StatusBadge status={row.status} /> },
            {
              header: 'Also known as',
              wideOnly: true,
              cell: (row) =>
                row.references?.length ? (
                  <span className="flex flex-col">
                    {row.references.map((reference) => (
                      <span key={reference.id} className="text-xs text-slate-600">
                        {reference.label}: {reference.value}
                      </span>
                    ))}
                  </span>
                ) : (
                  '—'
                ),
            },
            { header: 'County', cell: (row) => row.county || '—', wideOnly: true },
          ]}
        />
      </ListState>

      <SiteSheet
        open={sheet}
        onClose={() => setSheet(false)}
        clients={clients.data?.results ?? []}
      />
    </div>
  );
}

interface SiteForm {
  client: string;
  internal_ref: string;
  name: string;
  region: string;
  county: string;
  notes: string;
  references: { label: string; value: string }[];
}

export function SiteSheet({
  open,
  onClose,
  clients: givenClients,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  /** The tab already has these; a form elsewhere does not, so fetch them. */
  clients?: Client[];
  onCreated?: (record: { id: number }) => void;
}) {
  const fetched = useList<Client>('clients', { page_size: 200 }, { enabled: givenClients === undefined });
  const clients = givenClients ?? fetched.data?.results ?? [];
  const form = useForm<SiteForm>({
    defaultValues: {
      client: '',
      internal_ref: '',
      name: '',
      region: '',
      county: '',
      notes: '',
      // Three rows by default, because three is the normal number (C5) and an
      // empty list invites entering one and moving on.
      references: [
        { label: 'Operator site code', value: '' },
        { label: 'Towerco reference', value: '' },
        { label: 'Crew name', value: '' },
      ],
    },
  });
  const references = useFieldArray({ control: form.control, name: 'references' });
  const [banner, setBanner] = useState<string | null>(null);

  const createSite = useAction<Record<string, unknown>, Site>({ resource: 'sites' });
  const createReference = useAction<Record<string, unknown>>({
    resource: 'site-references',
    invalidates: ['sites', 'site-references'],
  });

  const submit = form.handleSubmit(async (values) => {
    setBanner(null);
    try {
      const site = await createSite.mutateAsync({
        client: Number(values.client),
        internal_ref: values.internal_ref,
        name: values.name,
        region: values.region,
        county: values.county,
        notes: values.notes,
      });

      // The site exists before its aliases can point at it, so these follow
      // rather than being nested. A failure here leaves a findable site with
      // fewer aliases, which is recoverable; the reverse would not be.
      for (const reference of values.references) {
        if (!reference.label.trim() || !reference.value.trim()) continue;
        await createReference.mutateAsync({
          site: site.id,
          label: reference.label.trim(),
          value: reference.value.trim(),
        });
      }

      form.reset();
      onClose();
      onCreated?.(site);
    } catch (error) {
      setBanner(applyFieldErrors(error, form.setError));
    }
  });

  return (
    <Sheet
      open={open}
      title="New site"
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} block>
            Cancel
          </Button>
          <Button onClick={submit} loading={createSite.isPending} block>
            Create
          </Button>
        </>
      }
    >
      <form className="flex flex-col gap-3" onSubmit={submit}>
        {banner ? <Banner tone="error">{banner}</Banner> : null}

        <Field label="Client" htmlFor="site-client" error={form.formState.errors.client?.message}>
          <ReferenceSelect resource="clients" form={form} name="client" rules={{ required: 'Choose a client.' }}
            id="site-client"
            invalid={Boolean(form.formState.errors.client)}
          >
            <option value="">Choose…</option>
            {clients.map((client) => (
              <option key={client.id} value={client.id}>
                {client.name}
              </option>
            ))}
          </ReferenceSelect>
        </Field>

        <Field
          label="Our reference"
          htmlFor="site-ref"
          hint="Checked against the client's own code pattern, if they have one."
          error={form.formState.errors.internal_ref?.message}
        >
          <Input
            id="site-ref"
            placeholder="SLV-1001"
            {...form.register('internal_ref', { required: 'A reference is required.' })}
          />
        </Field>

        <Field label="Name" htmlFor="site-name" error={form.formState.errors.name?.message}>
          <Input
            id="site-name"
            placeholder="Kileleshwa"
            {...form.register('name', { required: 'A name is required.' })}
          />
        </Field>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Region" htmlFor="site-region">
            <Input id="site-region" {...form.register('region')} />
          </Field>
          <Field label="County" htmlFor="site-county">
            <Input id="site-county" {...form.register('county')} />
          </Field>
        </div>

        <fieldset className="flex flex-col gap-2 rounded-lg border border-slate-200 p-3">
          <legend className="px-1 text-sm font-medium text-slate-700">Also known as</legend>
          <p className="text-sm text-slate-600">
            Every name this site goes by. Searching any of them finds it.
          </p>
          {references.fields.map((row, index) => (
            <div key={row.id} className="grid gap-2 sm:grid-cols-[minmax(0,12rem)_minmax(0,1fr)]">
              <Input
                aria-label={`Reference ${index + 1} label`}
                placeholder="Label"
                {...form.register(`references.${index}.label` as const)}
              />
              <Input
                aria-label={`Reference ${index + 1} value`}
                placeholder="Value"
                {...form.register(`references.${index}.value` as const)}
              />
            </div>
          ))}
          <Button
            variant="secondary"
            type="button"
            onClick={() => references.append({ label: '', value: '' })}
          >
            Another reference
          </Button>
        </fieldset>

        <Field label="Notes" htmlFor="site-notes">
          <Textarea id="site-notes" {...form.register('notes')} />
        </Field>
      </form>
    </Sheet>
  );
}

/* -------------------------------------------------------------------------- */
/* Clients                                                                    */
/* -------------------------------------------------------------------------- */

function ClientsTab() {
  const [sheet, setSheet] = useState(false);
  const clients = useList<Client>('clients', { page_size: 100 });

  return (
    <div className="flex flex-col gap-3">
      <div className="flex justify-end">
        <Button onClick={() => setSheet(true)}>New client</Button>
      </div>

      {clients.isLoading ? (
        <Spinner className="text-slate-400" />
      ) : (
        <DataList
          rows={clients.data?.results ?? []}
          rowKey={(row) => row.id}
          empty={<EmptyState title="No clients yet." hint="A client owns sites and, often, stock." />}
          columns={[
            { header: 'Name', cell: (row) => row.name },
            { header: 'Code', cell: (row) => row.code || '—' },
            { header: 'Sites', cell: (row) => row.site_count ?? 0 },
            {
              header: 'Site code pattern',
              wideOnly: true,
              cell: (row) => (
                <code className="text-xs text-slate-600">{row.site_code_pattern || '—'}</code>
              ),
            },
            { header: 'Active', cell: (row) => (row.is_active ? 'yes' : 'no'), wideOnly: true },
          ]}
        />
      )}

      <ClientSheet open={sheet} onClose={() => setSheet(false)} />
    </div>
  );
}

interface ClientForm {
  name: string;
  code: string;
  site_code_pattern: string;
  contact_name: string;
  contact_email: string;
  contact_phone: string;
}

export function ClientSheet({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated?: (record: { id: number }) => void;
}) {
  const form = useForm<ClientForm>({
    defaultValues: {
      name: '',
      code: '',
      site_code_pattern: '',
      contact_name: '',
      contact_email: '',
      contact_phone: '',
    },
  });
  const [banner, setBanner] = useState<string | null>(null);
  const create = useAction<Record<string, unknown>, { id: number }>({ resource: 'clients' });

  const submit = form.handleSubmit(async (values) => {
    setBanner(null);
    try {
      const created = await create.mutateAsync({ ...values });
      form.reset();
      onClose();
      onCreated?.(created);
    } catch (error) {
      setBanner(applyFieldErrors(error, form.setError));
    }
  });

  return (
    <Sheet
      open={open}
      title="New client"
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} block>
            Cancel
          </Button>
          <Button onClick={submit} loading={create.isPending} block>
            Create
          </Button>
        </>
      }
    >
      <form className="flex flex-col gap-3" onSubmit={submit}>
        {banner ? <Banner tone="error">{banner}</Banner> : null}

        <Field label="Name" htmlFor="client-name" error={form.formState.errors.name?.message}>
          <Input id="client-name" {...form.register('name', { required: 'A name is required.' })} />
        </Field>

        <Field label="Code" htmlFor="client-code">
          <Input id="client-code" {...form.register('code')} />
        </Field>

        <Field
          label="Site code pattern"
          htmlFor="client-pattern"
          hint="A regular expression. A site reference that does not match is refused with a message naming the expected shape. Leave empty to accept anything."
        >
          <Input id="client-pattern" placeholder="^KE-\\d{4}$" {...form.register('site_code_pattern')} />
        </Field>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Contact" htmlFor="client-contact">
            <Input id="client-contact" {...form.register('contact_name')} />
          </Field>
          <Field label="Phone" htmlFor="client-phone">
            <Input id="client-phone" inputMode="tel" {...form.register('contact_phone')} />
          </Field>
        </div>

        <Field label="Email" htmlFor="client-email">
          <Input id="client-email" type="email" {...form.register('contact_email')} />
        </Field>
      </form>
    </Sheet>
  );
}

/* -------------------------------------------------------------------------- */
/* Projects                                                                */
/* -------------------------------------------------------------------------- */

function SubcontractorsTab() {
  const [sheet, setSheet] = useState(false);
  const [search, setSearch] = useState('');
  const contractors = useList<Subcontractor>('subcontractors', {
    search: search || undefined,
    page_size: 100,
  });

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <SearchField
          value={search}
          onChange={setSearch}
          label="Search subcontractors"
          placeholder="Name, code or contact"
        />
        <Button onClick={() => setSheet(true)}>New subcontractor</Button>
      </div>

      <p className="text-sm text-slate-600">
        Third parties who deliver jobs. Distinct from a supplier, who sells
        goods: a subcontractor is paid an agreed price per job, and their cost
        has to roll up by party.
      </p>

      {contractors.isLoading ? (
        <Spinner className="text-slate-400" />
      ) : (
        <DataList
          rows={contractors.data?.results ?? []}
          rowKey={(row) => row.id}
          empty={
            <EmptyState
              title="No subcontractors yet."
              hint="A job cannot be marked subcontracted until one exists."
            />
          }
          columns={[
            { header: 'Name', cell: (row) => row.name },
            { header: 'Code', cell: (row) => row.code || '—' },
            { header: 'Contact', cell: (row) => row.contact_name || '—' },
            { header: 'Phone', cell: (row) => row.contact_phone || '—', wideOnly: true },
            {
              header: 'Active',
              cell: (row) => (row.is_active ? 'yes' : 'no'),
              wideOnly: true,
            },
          ]}
        />
      )}

      <SubcontractorSheet open={sheet} onClose={() => setSheet(false)} />
    </div>
  );
}

export function SubcontractorSheet({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated?: (record: { id: number }) => void;
}) {
  const form = useForm({
    defaultValues: {
      name: '',
      code: '',
      contact_name: '',
      contact_email: '',
      contact_phone: '',
      notes: '',
    },
  });
  const [banner, setBanner] = useState<string | null>(null);
  const create = useAction<Record<string, unknown>, { id: number }>({ resource: 'subcontractors' });

  const submit = form.handleSubmit(async (values) => {
    setBanner(null);
    try {
      const created = await create.mutateAsync({ ...values });
      form.reset();
      onClose();
      onCreated?.(created);
    } catch (error) {
      setBanner(applyFieldErrors(error, form.setError));
    }
  });

  return (
    <Sheet
      open={open}
      title="New subcontractor"
      onClose={onClose}
      footer={
        <Button block loading={create.isPending} onClick={submit}>
          Add them
        </Button>
      }
    >
      <form className="flex flex-col gap-3" onSubmit={submit}>
        {banner ? <Banner tone="error">{banner}</Banner> : null}

        <Field label="Name" htmlFor="sc-name" error={form.formState.errors.name?.message}>
          <Input
            id="sc-name"
            {...form.register('name', { required: 'What are they called?' })}
          />
        </Field>

        <Field label="Code" htmlFor="sc-code" hint="Optional, for your own filing.">
          <Input id="sc-code" {...form.register('code')} />
        </Field>

        <Field label="Contact" htmlFor="sc-contact">
          <Input id="sc-contact" {...form.register('contact_name')} />
        </Field>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Phone" htmlFor="sc-phone">
            <Input id="sc-phone" inputMode="tel" {...form.register('contact_phone')} />
          </Field>
          <Field label="Email" htmlFor="sc-email">
            <Input id="sc-email" type="email" {...form.register('contact_email')} />
          </Field>
        </div>

        <Field label="Notes" htmlFor="sc-notes">
          <Textarea id="sc-notes" {...form.register('notes')} />
        </Field>
      </form>
    </Sheet>
  );
}

function ProjectsTab() {
  const [sheet, setSheet] = useState(false);
  const projects = useList<Project>('projects', { page_size: 100 });
  const clients = useList<Client>('clients', { page_size: 200 });

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        {/* C7, D14: optional everywhere. Saying so here stops a storekeeper
            inventing one to get past a screen. */}
        <p className="text-sm text-slate-600">
          Optional. A gate-out can name a project, a site, both, or neither.
        </p>
        <Button onClick={() => setSheet(true)}>New project</Button>
      </div>

      {projects.isLoading ? (
        <Spinner className="text-slate-400" />
      ) : (
        <DataList
          rows={projects.data?.results ?? []}
          rowKey={(row) => row.id}
          empty={<EmptyState title="No projects." hint="They group several sites under one job." />}
          columns={[
            { header: 'Reference', cell: (row) => row.reference },
            { header: 'Client', cell: (row) => row.client_name ?? '—' },
            { header: 'Sites', cell: (row) => row.site_count ?? row.sites.length },
            { header: 'Status', cell: (row) => <StatusBadge status={row.status} /> },
            {
              header: 'Closed with material out',
              wideOnly: true,
              cell: (row) => (row.closed_with_unreconciled ? 'yes' : '—'),
            },
          ]}
        />
      )}

      <ProjectSheet
        open={sheet}
        onClose={() => setSheet(false)}
        clients={clients.data?.results ?? []}
      />
    </div>
  );
}

function ProjectSheet({
  open,
  onClose,
  clients,
}: {
  open: boolean;
  onClose: () => void;
  clients: Client[];
}) {
  const form = useForm<{ client: string; reference: string; description: string }>({
    defaultValues: { client: '', reference: '', description: '' },
  });
  const [banner, setBanner] = useState<string | null>(null);
  const create = useAction<Record<string, unknown>>({ resource: 'projects' });

  const submit = form.handleSubmit(async (values) => {
    setBanner(null);
    try {
      await create.mutateAsync({
        client: Number(values.client),
        reference: values.reference,
        description: values.description,
      });
      form.reset();
      onClose();
    } catch (error) {
      setBanner(applyFieldErrors(error, form.setError));
    }
  });

  return (
    <Sheet
      open={open}
      title="New project"
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} block>
            Cancel
          </Button>
          <Button onClick={submit} loading={create.isPending} block>
            Create
          </Button>
        </>
      }
    >
      <form className="flex flex-col gap-3" onSubmit={submit}>
        {banner ? <Banner tone="error">{banner}</Banner> : null}

        <Field label="Client" htmlFor="wo-client" error={form.formState.errors.client?.message}>
          <ReferenceSelect resource="clients" form={form} name="client" rules={{ required: 'Choose a client.' }}
            id="wo-client"
            invalid={Boolean(form.formState.errors.client)}
          >
            <option value="">Choose…</option>
            {clients.map((client) => (
              <option key={client.id} value={client.id}>
                {client.name}
              </option>
            ))}
          </ReferenceSelect>
        </Field>

        <Field
          label="Reference"
          htmlFor="wo-reference"
          error={form.formState.errors.reference?.message}
        >
          <Input
            id="wo-reference"
            placeholder="PO-2026-118"
            {...form.register('reference', { required: 'A reference is required.' })}
          />
        </Field>

        <Field label="Description" htmlFor="wo-description">
          <Textarea id="wo-description" {...form.register('description')} />
        </Field>
      </form>
    </Sheet>
  );
}

/* -------------------------------------------------------------------------- */
/* Locations                                                                  */
/* -------------------------------------------------------------------------- */

function LocationsTab() {
  const [sheet, setSheet] = useState(false);
  const locations = useList<Location>('locations', { page_size: 200 });
  const rows = locations.data?.results ?? [];

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-slate-600">
          Yards, stores and vehicles. Every yard has a quarantine location
          of its own, created with it — J1 keeps faulty stock out of what is
          available, and that has to be somewhere real.
        </p>
        <Button onClick={() => setSheet(true)}>New location</Button>
      </div>

      {locations.isLoading ? (
        <Spinner className="text-slate-400" />
      ) : (
        <DataList
          rows={rows}
          rowKey={(row) => row.id}
          empty={<EmptyState title="No locations yet." hint="A gate-in has to land somewhere." />}
          columns={[
            {
              header: 'Name',
              cell: (row) => (
                <span className={row.parent ? 'pl-3 text-slate-700' : 'font-medium'}>
                  {row.parent ? '— ' : ''}
                  {row.name}
                </span>
              ),
            },
            { header: 'Type', cell: (row) => row.type.toLowerCase() },
            { header: 'Vehicle', cell: (row) => row.vehicle_reg || '—', wideOnly: true },
            {
              header: 'Origin',
              wideOnly: true,
              cell: (row) => (row.is_system ? 'created by the system' : 'added here'),
            },
            { header: 'Active', cell: (row) => (row.is_active ? 'yes' : 'no') },
          ]}
        />
      )}

      <LocationSheet open={sheet} onClose={() => setSheet(false)} locations={rows} />
    </div>
  );
}

export function LocationSheet({
  open,
  onClose,
  locations: givenLocations,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  /** For the parent picker. The tab has them; a form elsewhere fetches them. */
  locations?: Location[];
  onCreated?: (record: { id: number }) => void;
}) {
  const fetchedLocations = useList<Location>('locations', { page_size: 300 }, { enabled: givenLocations === undefined });
  const locations = givenLocations ?? fetchedLocations.data?.results ?? [];
  const form = useForm<{
    name: string;
    code: string;
    type: string;
    parent: string;
    vehicle_reg: string;
  }>({
    defaultValues: { name: '', code: '', type: 'YARD', parent: '', vehicle_reg: '' },
  });
  const [banner, setBanner] = useState<string | null>(null);
  const create = useAction<Record<string, unknown>, { id: number }>({ resource: 'locations' });
  const type = form.watch('type');

  const submit = form.handleSubmit(async (values) => {
    setBanner(null);
    try {
      const created = await create.mutateAsync({
        name: values.name,
        code: values.code || undefined,
        type: values.type,
        parent: values.parent ? Number(values.parent) : null,
        vehicle_reg: values.vehicle_reg,
      });
      form.reset();
      onClose();
      onCreated?.(created);
    } catch (error) {
      setBanner(applyFieldErrors(error, form.setError));
    }
  });

  return (
    <Sheet
      open={open}
      title="New location"
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} block>
            Cancel
          </Button>
          <Button onClick={submit} loading={create.isPending} block>
            Create
          </Button>
        </>
      }
    >
      <form className="flex flex-col gap-3" onSubmit={submit}>
        {banner ? <Banner tone="error">{banner}</Banner> : null}

        <Field label="Name" htmlFor="loc-name" error={form.formState.errors.name?.message}>
          <Input id="loc-name" {...form.register('name', { required: 'A name is required.' })} />
        </Field>

        <Field label="Type" htmlFor="loc-type">
          <Select id="loc-type" {...form.register('type')}>
            <option value="YARD">Yard</option>
            <option value="STORE">Store</option>
            <option value="VEHICLE">Vehicle</option>
          </Select>
        </Field>

        <Field label="Inside" htmlFor="loc-parent" hint="Leave empty for a top-level yard.">
          <ReferenceSelect resource="locations" form={form} name="parent" id="loc-parent">
            <option value="">Top level</option>
            {locations.map((location) => (
              <option key={location.id} value={location.id}>
                {location.name}
              </option>
            ))}
          </ReferenceSelect>
        </Field>

        {type === 'VEHICLE' ? (
          <Field
            label="Registration"
            htmlFor="loc-vehicle"
            hint="A vehicle is a location, so material in transit is still somewhere."
          >
            <Input id="loc-vehicle" {...form.register('vehicle_reg')} />
          </Field>
        ) : null}

        <Field label="Code" htmlFor="loc-code">
          <Input id="loc-code" {...form.register('code')} />
        </Field>
      </form>
    </Sheet>
  );
}
