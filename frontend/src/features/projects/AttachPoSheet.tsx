/**
 * Attach a PO to a project that started without one (R12; design §4.19.8).
 *
 * One action: `POST /projects/{id}/attach-po` sets the PO on the same row, so
 * every job, expense and document already on it stays. The server seeds the
 * default milestones. The PO PDF is an attachment on the project captioned "PO".
 */

import { useState } from 'react';
import { useForm } from 'react-hook-form';

import { applyFieldErrors, errorMessage, useList } from '../../api/hooks';
import { PhotoCapture } from '../../components/PhotoCapture';
import { Banner, Button, Field, Input, Select, Spinner } from '../../components/ui';
import { Sheet } from '../../components/ui/data';
import { MoneyInput } from '../../components/ui/money';
import { useAttachPo } from './milestonesApi';
import type { Project } from './types';

interface Form {
  po_number: string;
  po_issue_date: string;
  contract_value: string;
  cost_budget: string;
  payment_terms: string;
  payment_terms_days: string;
  manager: string;
}

export function AttachPoSheet({
  open,
  project,
  onClose,
  onAttached,
}: {
  open: boolean;
  project: Project;
  onClose: () => void;
  onAttached: () => void;
}) {
  const attach = useAttachPo();
  const people = useList<{ id: number; full_name: string }>('users', { page_size: 200 });
  const [attached, setAttached] = useState(false);
  const form = useForm<Form>({
    values: {
      po_number: '',
      po_issue_date: '',
      contract_value: '',
      cost_budget: '',
      payment_terms: '',
      payment_terms_days: '',
      manager: project.manager ? String(project.manager) : '',
    },
  });

  const finish = () => {
    const done = attached;
    setAttached(false);
    form.reset();
    onClose();
    if (done) onAttached();
  };

  const required = { required: 'Required.' };
  const errors = form.formState.errors;

  return (
    <Sheet
      open={open}
      title="Attach PO"
      onClose={finish}
      footer={
        attached ? (
          <Button className="w-full" onClick={finish}>
            Done
          </Button>
        ) : (
          <Button
            className="w-full"
            disabled={attach.isPending}
            onClick={form.handleSubmit(async (v) => {
              try {
                await attach.mutateAsync({
                  project: project.id,
                  po_number: v.po_number,
                  po_issue_date: v.po_issue_date,
                  contract_value: v.contract_value,
                  cost_budget: v.cost_budget,
                  payment_terms: v.payment_terms,
                  payment_terms_days: v.payment_terms_days ? Number(v.payment_terms_days) : null,
                  manager: v.manager ? Number(v.manager) : null,
                });
                setAttached(true);
              } catch (error) {
                applyFieldErrors(error, form.setError);
              }
            })}
          >
            {attach.isPending ? <Spinner /> : 'Attach PO'}
          </Button>
        )
      }
    >
      {attached ? (
        <div className="flex flex-col gap-3">
          <Banner tone="success">
            PO attached. Everything already recorded on this project stays with it.
          </Banner>
          <PhotoCapture
            targetType="network.Project"
            targetId={project.id}
            kind="DOCUMENT"
            label="PO document (PDF)"
            caption="PO"
          />
        </div>
      ) : (
        <form className="flex flex-col gap-3">
          <Banner tone="info">
            Spend recorded before the PO is not re-checked, so the project may open already over
            budget. That is shown, not hidden.
          </Banner>
          <Field label="PO number" htmlFor="ap-po" error={errors.po_number?.message}>
            <Input id="ap-po" {...form.register('po_number', required)} />
          </Field>
          <Field label="PO issue date" htmlFor="ap-date" error={errors.po_issue_date?.message}>
            <Input id="ap-date" type="date" {...form.register('po_issue_date', required)} />
          </Field>
          <Field label="Contract value" htmlFor="ap-value" hint="Excluding VAT." error={errors.contract_value?.message}>
            <MoneyInput id="ap-value" {...form.register('contract_value', required)} />
          </Field>
          <Field label="Cost budget" htmlFor="ap-budget" hint="Excluding VAT." error={errors.cost_budget?.message}>
            <MoneyInput id="ap-budget" {...form.register('cost_budget', required)} />
          </Field>
          <Field label="Payment terms" htmlFor="ap-terms" hint="As typed on the PO." error={errors.payment_terms?.message}>
            <Input id="ap-terms" {...form.register('payment_terms')} />
          </Field>
          <Field
            label="Payment terms, in days"
            htmlFor="ap-days"
            hint="The number the overdue clock uses."
            error={errors.payment_terms_days?.message}
          >
            <Input id="ap-days" inputMode="numeric" {...form.register('payment_terms_days')} />
          </Field>
          {/* The existing CHECK needs a manager on a PO project. */}
          <Field
            label="Project manager"
            htmlFor="ap-manager"
            hint={project.manager ? undefined : 'A PO project needs a manager.'}
            error={errors.manager?.message}
          >
            <Select id="ap-manager" {...form.register('manager')}>
              <option value="">Choose…</option>
              {(people.data?.results ?? []).map((person) => (
                <option key={person.id} value={person.id}>
                  {person.full_name}
                </option>
              ))}
            </Select>
          </Field>
          {attach.error ? <Banner tone="error">{errorMessage(attach.error)}</Banner> : null}
        </form>
      )}
    </Sheet>
  );
}
