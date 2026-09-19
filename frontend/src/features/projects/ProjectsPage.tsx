/**
 * T10.21 — project screens (design §7.4; O1, O2, O12, O13).
 *
 * The rule this page follows throughout: **a figure you were not sent is not a
 * figure that is zero.** O14 has the server omit what the caller may not see,
 * so `undefined` means "withheld" and is rendered as nothing at all — never as
 * a dash, a blank tile or a zero, each of which reads as a fact about the
 * project rather than about the reader.
 *
 * The second rule is that nothing here blocks. A project over budget is stated
 * plainly and everything still works (O12), because the decision to spend past
 * a budget belongs to the person accountable for it, not to a disabled button.
 */

import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { useNavigate, useParams } from 'react-router-dom';

import { applyFieldErrors, useAction, useDetail, useList, useResource } from '../../api/hooks';
import { PERM } from '../../auth/permissions';
import { useSession } from '../../auth/session';
import { Banner, Button, Field, Input, Select, Spinner, Textarea } from '../../components/ui';
import {
  DataList,
  EmptyState,
  ListState,
  PageHeader,
  Sheet,
  Stat,
  StatusBadge,
} from '../../components/ui/data';
import { Money, MoneyInput } from '../../components/ui/money';
import { JobSheet } from './JobSheet';
import type { Client } from '../settings/types';
import type { Project, ProjectJob, ProjectPerformance, ProjectVariation } from './types';

export default function ProjectsPage() {
  const navigate = useNavigate();
  const [sheetOpen, setSheetOpen] = useState(false);
  const { has } = useSession();

  const projects = useList<Project>('projects', { page_size: 100 });

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Projects"
        subtitle="Purchase orders and the work under them."
        actions={
          has(PERM.CATALOGUE_MANAGE) ? (
            <Button onClick={() => setSheetOpen(true)}>New project</Button>
          ) : null
        }
      />

      <ListState query={projects}>
        <DataList<Project>
          rows={projects.data?.results ?? []}
          rowKey={(project) => project.id}
          onRowClick={(project) => navigate(`/projects/${project.id}`)}
          empty={
            <EmptyState
              title="No projects yet."
              hint="A project is usually one purchase order. Work with no PO can still be grouped under one."
            />
          }
          columns={[
            {
              header: 'Reference',
              cell: (project) => (
                <span className="font-medium">{project.po_number || project.reference}</span>
              ),
            },
            { header: 'Title', cell: (project) => project.title || project.description },
            { header: 'Client', cell: (project) => project.client_name ?? '', wideOnly: true },
            { header: 'Manager', cell: (project) => project.manager_name ?? '—' },
            {
              header: 'Value',
              // Absent for anyone without view_margin, and absent is not zero.
              cell: (project) => <Money value={project.current_contract_value} />,
              wideOnly: true,
            },
            { header: 'Status', cell: (project) => <StatusBadge status={project.status} /> },
          ]}
        />
      </ListState>

      <ProjectSheet open={sheetOpen} onClose={() => setSheetOpen(false)} />
    </div>
  );
}

function ProjectSheet({ open, onClose }: { open: boolean; onClose: () => void }) {
  const clients = useList<Client>('clients', { page_size: 200 });
  const people = useList<{ id: number; full_name: string }>('users', { page_size: 200 });
  const create = useAction<Record<string, unknown>>({ resource: 'projects' });

  const form = useForm({
    defaultValues: {
      client: '',
      po_number: '',
      title: '',
      manager: '',
      contract_value: '',
      cost_budget: '',
    },
  });

  const hasPo = Boolean(form.watch('po_number'));

  return (
    <Sheet
      open={open}
      title="New project"
      onClose={onClose}
      footer={
        <Button
          className="w-full"
          disabled={create.isPending}
          onClick={form.handleSubmit(async (values) => {
            try {
              await create.mutateAsync({
                ...values,
                manager: values.manager || null,
                contract_value: values.contract_value || null,
                cost_budget: values.cost_budget || null,
              });
              form.reset();
              onClose();
            } catch (error) {
              applyFieldErrors(error, form.setError);
            }
          })}
        >
          {create.isPending ? <Spinner /> : 'Create'}
        </Button>
      }
    >
      <form className="flex flex-col gap-3">
        <Field label="Client" htmlFor="pr-client" error={form.formState.errors.client?.message}>
          <Select id="pr-client" {...form.register('client', { required: 'Pick a client.' })}>
            <option value="">Choose…</option>
            {(clients.data?.results ?? []).map((client) => (
              <option key={client.id} value={client.id}>
                {client.name}
              </option>
            ))}
          </Select>
        </Field>

        <Field
          label="PO number"
          htmlFor="pr-po"
          hint="Leave empty to group work with no purchase order behind it."
          error={form.formState.errors.po_number?.message}
        >
          <Input id="pr-po" {...form.register('po_number')} />
        </Field>

        <Field label="Title" htmlFor="pr-title">
          <Input id="pr-title" {...form.register('title')} />
        </Field>

        {/*
          O1: a PO needs a manager, a value and a budget. The server refuses it
          in the database either way; asking for them here, only when they are
          actually required, is what stops that refusal being a surprise.
        */}
        {hasPo ? (
          <>
            <Banner tone="info">
              A purchase order needs a manager, a value and a budget. Material cannot
              leave for a project nobody manages.
            </Banner>

            <Field
              label="Project manager"
              htmlFor="pr-manager"
              error={form.formState.errors.manager?.message}
            >
              <Select id="pr-manager" {...form.register('manager')}>
                <option value="">Choose…</option>
                {(people.data?.results ?? []).map((person) => (
                  <option key={person.id} value={person.id}>
                    {person.full_name}
                  </option>
                ))}
              </Select>
            </Field>

            <Field
              label="Contract value"
              htmlFor="pr-value"
              hint="Excluding VAT."
              error={form.formState.errors.contract_value?.message}
            >
              <MoneyInput id="pr-value" {...form.register('contract_value')} />
            </Field>

            <Field
              label="Cost budget"
              htmlFor="pr-budget"
              hint="Excluding VAT. What the manager may spend to deliver it."
              error={form.formState.errors.cost_budget?.message}
            >
              <MoneyInput id="pr-budget" {...form.register('cost_budget')} />
            </Field>
          </>
        ) : null}
      </form>
    </Sheet>
  );
}

export function ProjectDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { has } = useSession();
  const project = useDetail<Project>('projects', id);
  const performance = useResource<ProjectPerformance>(`projects/${id}/performance`);
  const variations = useList<ProjectVariation>('project-variations', { project: id });
  const jobs = useList<ProjectJob>('jobs', { project: id, page_size: 100 });
  const [closing, setClosing] = useState(false);
  const [editing, setEditing] = useState(false);
  const [addingJob, setAddingJob] = useState(false);

  if (project.isLoading) return <Spinner />;
  if (!project.data) return <Banner tone="error">That project could not be loaded.</Banner>;

  const figures = performance.data;
  const record = project.data;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={record.po_number || record.reference}
        subtitle={record.title || record.client_name}
        actions={
          record.status === 'OPEN' ? (
            <>
              {has(PERM.CATALOGUE_MANAGE) ? (
                <Button variant="ghost" onClick={() => setEditing(true)}>
                  Edit
                </Button>
              ) : null}
              {has(PERM.JOB_MANAGE) ? (
                <Button variant="ghost" onClick={() => setAddingJob(true)}>
                  Add a job
                </Button>
              ) : null}
              <Button onClick={() => setClosing(true)}>Close project</Button>
            </>
          ) : (
            <StatusBadge status={record.status} />
          )
        }
      />

      {figures?.is_fully_valued === false ? (
        <Banner tone="warning">
          Some of this project&rsquo;s cost could not be valued
          {figures.unvalued_movements ? `: ${figures.unvalued_movements} movement(s) with no price` : ''}
          {figures.jobs_closed_without_labour
            ? `, ${figures.jobs_closed_without_labour} closed job(s) with no days recorded`
            : ''}
          . The figures below are incomplete rather than low.
        </Banner>
      ) : null}

      {figures?.is_over_budget ? (
        <Banner tone="warning">
          This project is over its budget by{' '}
          <Money value={figures.budget_variance?.replace('-', '')} />.
          Nothing is blocked — it is stated so the decision to keep spending is a
          decision somebody makes.
        </Banner>
      ) : null}

      <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
        {/* Progress is quantities, so everyone who can see the project sees it. */}
        <Stat
          label="Jobs done"
          value={`${figures?.jobs_closed ?? 0}/${figures?.jobs_total ?? 0}`}
          hint={figures?.progress_percent ? `${figures.progress_percent}%` : undefined}
        />
        {figures?.cost_to_date !== undefined ? (
          <Stat label="Cost to date" value={<Money value={figures.cost_to_date} compact />} />
        ) : null}
        {figures?.cost_budget !== undefined && figures.cost_budget !== null ? (
          <Stat
            label="Budget"
            value={<Money value={figures.cost_budget} compact />}
            tone={figures.is_over_budget ? 'bad' : 'neutral'}
          />
        ) : null}
        {figures?.exposure !== undefined ? (
          <Stat
            label="Still out"
            value={<Money value={figures.exposure} compact />}
            hint="Issued and not yet accounted for. Not a cost yet."
          />
        ) : null}
        {figures?.contract_value !== undefined && figures.contract_value !== null ? (
          <Stat label="Contract value" value={<Money value={figures.contract_value} compact />} />
        ) : null}
        {figures?.margin !== undefined && figures.margin !== null ? (
          <Stat
            label="Margin"
            value={<Money value={figures.margin} compact tone={Number(figures.margin) < 0 ? 'bad' : 'good'} />}
            hint={figures.margin_percent ? `${figures.margin_percent}% before overheads` : undefined}
          />
        ) : null}
      </div>

      {figures?.material !== undefined ? (
        <div className="grid grid-cols-2 gap-2 md:grid-cols-5">
          <Stat label="Material" value={<Money value={figures.material} compact />} />
          <Stat label="Loss" value={<Money value={figures.material_loss} compact />} />
          <Stat label="Subcontract" value={<Money value={figures.subcontractor} compact />} />
          <Stat label="Labour" value={<Money value={figures.labour} compact />} />
          <Stat label="Expenses" value={<Money value={figures.expenses} compact />} />
        </div>
      ) : null}

      <section className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold text-slate-900">Jobs</h2>
        <DataList<ProjectJob>
          rows={jobs.data?.results ?? []}
          rowKey={(job) => job.id}
          empty={
            <EmptyState
              title="No jobs yet."
              hint="A project with no jobs has nothing to cost."
            />
          }
          columns={[
            { header: 'Reference', cell: (job) => job.reference || `Job ${job.id}` },
            { header: 'Site', cell: (job) => job.site_ref ?? '' },
            { header: 'Assignee', cell: (job) => job.assignee_name ?? '', wideOnly: true },
            {
              header: 'Delivered by',
              cell: (job) =>
                job.delivery_mode === 'SUBCONTRACTED'
                  ? (job.subcontractor_name ?? 'A subcontractor')
                  : 'Our crew',
            },
            {
              header: 'Price',
              // Only a subcontracted job has one; in-house cost comes from days.
              cell: (job) => <Money value={job.agreed_price} />,
              wideOnly: true,
            },
            { header: 'Status', cell: (job) => <StatusBadge status={job.status} /> },
          ]}
        />
      </section>

      <section className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold text-slate-900">Variations</h2>
        <DataList<ProjectVariation>
          rows={variations.data?.results ?? []}
          rowKey={(variation) => variation.id}
          empty={<EmptyState title="No variations." hint="Scope changes are recorded here." />}
          columns={[
            { header: 'Reference', cell: (variation) => variation.reference },
            { header: 'Value', cell: (variation) => <Money value={variation.value_delta} /> },
            { header: 'Budget', cell: (variation) => <Money value={variation.budget_delta} />, wideOnly: true },
            { header: 'From', cell: (variation) => variation.effective_on, wideOnly: true },
            { header: 'Status', cell: (variation) => <StatusBadge status={variation.status} /> },
          ]}
        />
      </section>

      <EditProjectSheet
        open={editing}
        project={record}
        onClose={() => setEditing(false)}
        onSaved={() => {
          setEditing(false);
          project.refetch();
          performance.refetch();
        }}
      />

      <JobSheet
        open={addingJob}
        project={record}
        onClose={() => setAddingJob(false)}
        onCreated={() => {
          jobs.refetch();
          performance.refetch();
        }}
      />

      <CloseSheet
        open={closing}
        project={record}
        performance={figures}
        onClose={() => setClosing(false)}
        onClosed={() => {
          setClosing(false);
          project.refetch();
          performance.refetch();
        }}
      />
    </div>
  );
}

function EditProjectSheet({
  open,
  project,
  onClose,
  onSaved,
}: {
  open: boolean;
  project: Project;
  onClose: () => void;
  onSaved: () => void;
}) {
  const people = useList<{ id: number; full_name: string }>('users', { page_size: 200 });
  const update = useAction<Record<string, unknown>>({
    resource: 'projects',
    method: 'patch',
    path: () => String(project.id),
    invalidates: ['projects'],
  });

  const form = useForm({
    values: {
      title: project.title ?? '',
      description: project.description ?? '',
      manager: project.manager ? String(project.manager) : '',
      contract_value: project.contract_value ?? '',
      cost_budget: project.cost_budget ?? '',
      starts_on: project.starts_on ?? '',
      target_completion_on: project.target_completion_on ?? '',
    },
  });

  return (
    <Sheet
      open={open}
      title="Edit project"
      onClose={onClose}
      footer={
        <Button
          className="w-full"
          disabled={update.isPending}
          onClick={form.handleSubmit(async (values) => {
            try {
              await update.mutateAsync({
                ...values,
                manager: values.manager ? Number(values.manager) : null,
                contract_value: values.contract_value || null,
                cost_budget: values.cost_budget || null,
                starts_on: values.starts_on || null,
                target_completion_on: values.target_completion_on || null,
              });
              onSaved();
            } catch (error) {
              applyFieldErrors(error, form.setError);
            }
          })}
        >
          {update.isPending ? <Spinner /> : 'Save'}
        </Button>
      }
    >
      <form className="flex flex-col gap-3">
        {/*
          The PO number is not editable. It is what identifies the project
          (D21), and changing it would silently re-point every figure already
          counted against it at a different purchase order.
        */}
        <p className="text-sm text-slate-600">
          PO <span className="font-medium">{project.po_number || '—'}</span> ·
          reference <span className="font-medium">{project.reference}</span>
        </p>

        <Field label="Title" htmlFor="pe-title">
          <Input id="pe-title" {...form.register('title')} />
        </Field>

        <Field label="Description" htmlFor="pe-description">
          <Textarea id="pe-description" {...form.register('description')} />
        </Field>

        <Field
          label="Project manager"
          htmlFor="pe-manager"
          hint="Material for this project can only move once its manager can act."
          error={form.formState.errors.manager?.message}
        >
          <Select id="pe-manager" {...form.register('manager')}>
            <option value="">Nobody yet</option>
            {(people.data?.results ?? []).map((person) => (
              <option key={person.id} value={person.id}>
                {person.full_name}
              </option>
            ))}
          </Select>
        </Field>

        {/*
          Editable by choice. D21 says the original award is never rewritten and
          scope changes are variations — this is the correction path for a value
          keyed in wrong, and the change is recorded in the audit trail.
        */}
        {project.contract_value !== undefined ? (
          <Field
            label="Contract value"
            htmlFor="pe-value"
            hint="Excluding VAT. To change what was agreed, raise a variation — this is for correcting a mistake."
            error={form.formState.errors.contract_value?.message}
          >
            <MoneyInput id="pe-value" {...form.register('contract_value')} />
          </Field>
        ) : null}

        {project.cost_budget !== undefined ? (
          <Field
            label="Cost budget"
            htmlFor="pe-budget"
            hint="Excluding VAT."
            error={form.formState.errors.cost_budget?.message}
          >
            <MoneyInput id="pe-budget" {...form.register('cost_budget')} />
          </Field>
        ) : null}

        <div className="grid grid-cols-2 gap-2">
          <Field label="Starts" htmlFor="pe-starts">
            <Input id="pe-starts" type="date" {...form.register('starts_on')} />
          </Field>
          <Field label="Target" htmlFor="pe-target">
            <Input
              id="pe-target"
              type="date"
              {...form.register('target_completion_on')}
            />
          </Field>
        </div>
      </form>
    </Sheet>
  );
}

function CloseSheet({
  open,
  project,
  performance,
  onClose,
  onClosed,
}: {
  open: boolean;
  project: Project;
  performance?: ProjectPerformance;
  onClose: () => void;
  onClosed: () => void;
}) {
  const [reason, setReason] = useState('');
  const [error, setError] = useState('');
  const close = useAction<{ reason: string }>({
    resource: 'projects',
    path: () => `${project.id}/close`,
    invalidates: ['projects'],
  });

  const openJobs = (performance?.jobs_total ?? 0) - (performance?.jobs_closed ?? 0);

  return (
    <Sheet
      open={open}
      title="Close project"
      onClose={onClose}
      footer={
        <Button
          className="w-full"
          disabled={close.isPending}
          onClick={async () => {
            setError('');
            try {
              await close.mutateAsync({ reason });
              onClosed();
            } catch (caught) {
              const message = (caught as { message?: string })?.message;
              setError(message ?? 'That could not be done.');
            }
          }}
        >
          {close.isPending ? <Spinner /> : 'Close it'}
        </Button>
      }
    >
      <div className="flex flex-col gap-3">
        <p className="text-sm text-slate-600">
          Closing freezes what this project reported. Later corrections still reach
          the ledger, but the figures on this page stop moving.
        </p>

        {openJobs > 0 ? (
          <Banner tone="warning">
            {openJobs} job(s) are still open. You can close anyway, with a reason.
          </Banner>
        ) : null}

        {error ? <Banner tone="error">{error}</Banner> : null}

        <Field label="Reason" htmlFor="pr-close-reason" hint="Required if anything is unfinished.">
          <Textarea
            id="pr-close-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </Field>
      </div>
    </Sheet>
  );
}
