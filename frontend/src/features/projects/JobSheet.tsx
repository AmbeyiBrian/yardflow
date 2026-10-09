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

import { useState } from 'react';
import { useForm } from 'react-hook-form';

import { ApiError } from '../../api/client';
import { applyFieldErrors, useAction, useList } from '../../api/hooks';
import { Banner, Button, Field, Select, Spinner, Textarea } from '../../components/ui';
import { ReferenceSelect } from '../../components/ui/ReferenceSelect';
import { Sheet } from '../../components/ui/data';
import { MoneyInput } from '../../components/ui/money';
import type { Site } from '../settings/types';
import { committedCents, wouldExceedContract } from './subcontracts';
import { useSubcontracts } from './subcontractsApi';
import type { Project, Subcontractor } from './types';

interface JobForm {
  /** R8/§4.19.4: blank means the server picks, or the job is not under a contract. */
  subcontract: string;
  over_contract_reason: string;
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
      site: '',
      assignee: '',
      description: '',
      project: '',
      delivery_mode: 'IN_HOUSE',
      subcontractor: '',
      agreed_price: '',
      subcontract: '',
      over_contract_reason: '',
    },
  });
  // §4.19.4: the server said this award passes the contract value.
  const [serverOver, setServerOver] = useState(false);

  const subcontracted = form.watch('delivery_mode') === 'SUBCONTRACTED';
  const projectId = project ? project.id : form.watch('project') || undefined;
  const contractorId = form.watch('subcontractor');
  const contracts = useSubcontracts(projectId, subcontracted);
  // Only the subcontractor's own active contracts can take the job
  // (`SUBCONTRACT_MISMATCH` otherwise).
  const eligible = (contracts.data?.results ?? []).filter(
    (sc) => String(sc.subcontractor) === contractorId && sc.status === 'ACTIVE',
  );
  const chosenContract =
    eligible.find((sc) => String(sc.id) === form.watch('subcontract')) ??
    (eligible.length === 1 ? eligible[0] : undefined);
  const committed = chosenContract?.position?.committed;
  const overValue =
    chosenContract !== undefined &&
    committed !== undefined &&
    wouldExceedContract(
      chosenContract.contract_value,
      committedCents([{ status: 'OPEN', agreed_price: committed }]),
      form.watch('agreed_price'),
    );
  const needsReason = overValue || serverOver;
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
                // M6: the reference comes from the tenant's JOB series.
                site: Number(values.site),
                // The client follows from the site — asking twice would let
                // the two disagree.
                client: chosenSite?.client,
                assignee: Number(values.assignee),
                description: values.description,
                // Taken from the prop when the sheet was opened from a
                // project, rather than from a form field that is not rendered
                // in that case — `setValue` into an unrendered field is a
                // dependency on library behaviour this does not need.
                project: project ? project.id : values.project ? Number(values.project) : null,
                delivery_mode: values.delivery_mode,
                subcontractor: subcontracted ? Number(values.subcontractor) : null,
                agreed_price: subcontracted ? values.agreed_price : null,
                subcontract:
                  subcontracted && values.subcontract ? Number(values.subcontract) : undefined,
                over_contract_reason:
                  subcontracted && needsReason ? values.over_contract_reason : undefined,
              });
              setServerOver(false);
              form.reset();
              onCreated?.();
              onClose();
            } catch (error) {
              if (error instanceof ApiError && error.code === 'SUBCONTRACT_OVER_VALUE') {
                setServerOver(true);
              }
              applyFieldErrors(error, form.setError);
            }
          })}
        >
          {create.isPending ? <Spinner /> : 'Raise it'}
        </Button>
      }
    >
      <form className="flex flex-col gap-3">
        <Field label="Site" htmlFor="job-site" error={form.formState.errors.site?.message}>
          <ReferenceSelect resource="sites" form={form} name="site" rules={{ required: 'Which site?' }} id="job-site">
            <option value="">Choose…</option>
            {(sites.data?.results ?? []).map((site) => (
              <option key={site.id} value={site.id}>
                {site.internal_ref} · {site.name}
              </option>
            ))}
          </ReferenceSelect>
        </Field>

        {project ? null : (
          <Field
            label="Project"
            htmlFor="job-project"
            hint="Optional. Without one the job is not costed to a PO."
          >
            <ReferenceSelect resource="projects" form={form} name="project" id="job-project">
              <option value="">Not under a project</option>
              {(projects.data?.results ?? []).map((row) => (
                <option key={row.id} value={row.id}>
                  {row.po_number || row.reference} {row.title}
                </option>
              ))}
            </ReferenceSelect>
          </Field>
        )}

        <Field
          label="Who is responsible"
          htmlFor="job-assignee"
          hint="H1: a job nobody is named on is a job nobody has to finish."
          error={form.formState.errors.assignee?.message}
        >
          <ReferenceSelect resource="users" form={form} name="assignee" rules={{ required: 'Somebody has to own this.' }}
            id="job-assignee"
          >
            <option value="">Choose…</option>
            {(people.data?.results ?? []).map((person) => (
              <option key={person.id} value={person.id}>
                {person.full_name}
              </option>
            ))}
          </ReferenceSelect>
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
              hint={
                // A select whose only option is "Choose…" reads as a fault in
                // the screen. The register is in settings, and somebody raising
                // a job has no reason to know that.
                !contractors.isLoading && !(contractors.data?.results ?? []).length
                  ? 'None on the register yet — add one under Settings › Network › Subcontractors.'
                  : undefined
              }
            >
              <ReferenceSelect resource="subcontractors" form={form} name="subcontractor" rules={{ required: 'Which contractor?' }}
                id="job-contractor"
              >
                <option value="">Choose…</option>
                {(contractors.data?.results ?? []).map((contractor) => (
                  <option key={contractor.id} value={contractor.id}>
                    {contractor.name}
                  </option>
                ))}
              </ReferenceSelect>
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

            {eligible.length > 1 ? (
              <Field
                label="Which contract"
                htmlFor="job-subcontract"
                hint="More than one active contract with this subcontractor."
              >
                <Select
                  id="job-subcontract"
                  {...form.register('subcontract', { required: 'Which contract?' })}
                >
                  <option value="">Choose…</option>
                  {eligible.map((sc) => (
                    <option key={sc.id} value={sc.id}>
                      {sc.reference}
                    </option>
                  ))}
                </Select>
              </Field>
            ) : eligible.length === 1 ? (
              <p className="text-xs text-slate-600">Under contract {eligible[0].reference}.</p>
            ) : contractorId && projectId && !contracts.isLoading ? (
              <p className="text-xs text-slate-600">Not under a contract.</p>
            ) : null}

            {needsReason ? (
              <>
                <Banner tone="warning">
                  This award takes the contract past its value. It is allowed, but the project
                  manager will see why.
                </Banner>
                <Field
                  label="Why is it over the contract value"
                  htmlFor="job-over-reason"
                  error={form.formState.errors.over_contract_reason?.message}
                >
                  <Textarea
                    id="job-over-reason"
                    {...form.register('over_contract_reason', { required: 'Say why.' })}
                  />
                </Field>
              </>
            ) : null}
          </>
        ) : null}

        <Field label="What is the work" htmlFor="job-description">
          <Textarea id="job-description" {...form.register('description')} />
        </Field>
      </form>
    </Sheet>
  );
}
