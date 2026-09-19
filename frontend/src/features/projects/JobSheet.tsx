/**
 * Raising a job, and saying how it will be delivered (§7.4; H1, O3).
 *
 * H1 asks for this — "assign a site or job to a named person, so that
 * accountability is explicit" — and it had no screen at all until now. The
 * assignee is required for the reason H1 gives: a job nobody is named on is a
 * job nobody has to finish.
 *
 * The delivery half is O3's. Mode, contractor and price travel together because
 * the database takes both or neither: a contractor with no price contributes
 * nothing to the project and flatters it, and a price with no contractor cannot
 * be rolled up by party.
 */

import { useEffect } from 'react';
import { useForm } from 'react-hook-form';

import { applyFieldErrors, useAction, useList } from '../../api/hooks';
import { Banner, Button, Field, Input, Select, Spinner, Textarea } from '../../components/ui';
import { Sheet } from '../../components/ui/data';
import { MoneyInput } from '../../components/ui/money';
import type { Site } from '../settings/types';
import type { Project, Subcontractor } from './types';

interface JobForm {
  reference: string;
  site: string;
  assignee: string;
  description: string;
  delivery_mode: 'IN_HOUSE' | 'SUBCONTRACTED';
  subcontractor: string;
  agreed_price: string;
}

export function JobSheet({
  open,
  onClose,
  onCreated,
  project,
}: {
  open: boolean;
  onClose: () => void;
  onCreated?: () => void;
  /** Pre-filled and fixed when raised from a project's own page. */
  project?: Project;
}) {
  const sites = useList<Site>('sites', { page_size: 300 });
  const people = useList<{ id: number; full_name: string }>('users', { page_size: 200 });
  const projects = useList<Project>('projects', { status: 'OPEN', page_size: 200 });
  const contractors = useList<Subcontractor>('subcontractors', {
    is_active: true,
    page_size: 100,
  });
  const create = useAction<Record<string, unknown>>({
    resource: 'jobs',
    invalidates: ['jobs', 'projects'],
  });

  const form = useForm<JobForm & { project: string }>({
    defaultValues: {
      reference: '',
      site: '',
      assignee: '',
      description: '',
      project: '',
      delivery_mode: 'IN_HOUSE',
      subcontractor: '',
      agreed_price: '',
    },
  });

  useEffect(() => {
    if (project) form.setValue('project', String(project.id));
  }, [project, form]);

  const subcontracted = form.watch('delivery_mode') === 'SUBCONTRACTED';
  const chosenSite = sites.data?.results.find(
    (site) => String(site.id) === form.watch('site'),
  );

  return (
    <Sheet
      open={open}
      title={project ? `New job on ${project.po_number || project.reference}` : 'New job'}
      onClose={onClose}
      footer={
        <Button
          className="w-full"
          disabled={create.isPending}
          onClick={form.handleSubmit(async (values) => {
            try {
              await create.mutateAsync({
                reference: values.reference,
                site: Number(values.site),
                // The client follows from the site — asking twice would let
                // the two disagree.
                client: chosenSite?.client,
                assignee: Number(values.assignee),
                description: values.description,
                project: values.project ? Number(values.project) : null,
                delivery_mode: values.delivery_mode,
                subcontractor: subcontracted ? Number(values.subcontractor) : null,
                agreed_price: subcontracted ? values.agreed_price : null,
              });
              form.reset();
              onCreated?.();
              onClose();
            } catch (error) {
              applyFieldErrors(error, form.setError);
            }
          })}
        >
          {create.isPending ? <Spinner /> : 'Raise it'}
        </Button>
      }
    >
      <form className="flex flex-col gap-3">
        <Field label="Reference" htmlFor="job-reference" error={form.formState.errors.reference?.message}>
          <Input id="job-reference" {...form.register('reference')} />
        </Field>

        <Field label="Site" htmlFor="job-site" error={form.formState.errors.site?.message}>
          <Select id="job-site" {...form.register('site', { required: 'Which site?' })}>
            <option value="">Choose…</option>
            {(sites.data?.results ?? []).map((site) => (
              <option key={site.id} value={site.id}>
                {site.internal_ref} · {site.name}
              </option>
            ))}
          </Select>
        </Field>

        {project ? null : (
          <Field
            label="Project"
            htmlFor="job-project"
            hint="Optional. Without one the job is not costed to a PO."
          >
            <Select id="job-project" {...form.register('project')}>
              <option value="">Not under a project</option>
              {(projects.data?.results ?? []).map((row) => (
                <option key={row.id} value={row.id}>
                  {row.po_number || row.reference} {row.title}
                </option>
              ))}
            </Select>
          </Field>
        )}

        <Field
          label="Who is responsible"
          htmlFor="job-assignee"
          hint="H1: a job nobody is named on is a job nobody has to finish."
          error={form.formState.errors.assignee?.message}
        >
          <Select
            id="job-assignee"
            {...form.register('assignee', { required: 'Somebody has to own this.' })}
          >
            <option value="">Choose…</option>
            {(people.data?.results ?? []).map((person) => (
              <option key={person.id} value={person.id}>
                {person.full_name}
              </option>
            ))}
          </Select>
        </Field>

        <Field label="Who delivers it" htmlFor="job-mode">
          <Select id="job-mode" {...form.register('delivery_mode')}>
            <option value="IN_HOUSE">Our own crew</option>
            <option value="SUBCONTRACTED">A subcontractor</option>
          </Select>
        </Field>

        {subcontracted ? (
          <>
            <Banner tone="info">
              A subcontracted job needs both the contractor and the agreed price.
              A price with no party cannot be rolled up; a party with no price
              makes the project look cheaper than it is.
            </Banner>

            <Field
              label="Subcontractor"
              htmlFor="job-contractor"
              error={form.formState.errors.subcontractor?.message}
            >
              <Select
                id="job-contractor"
                {...form.register('subcontractor', { required: 'Which contractor?' })}
              >
                <option value="">Choose…</option>
                {(contractors.data?.results ?? []).map((contractor) => (
                  <option key={contractor.id} value={contractor.id}>
                    {contractor.name}
                  </option>
                ))}
              </Select>
            </Field>

            <Field
              label="Agreed price"
              htmlFor="job-price"
              hint="Excluding VAT. Counts against the project when the job closes."
              error={form.formState.errors.agreed_price?.message}
            >
              <MoneyInput
                id="job-price"
                {...form.register('agreed_price', { required: 'What was agreed?' })}
              />
            </Field>
          </>
        ) : null}

        <Field label="What is the work" htmlFor="job-description">
          <Textarea id="job-description" {...form.register('description')} />
        </Field>
      </form>
    </Sheet>
  );
}
