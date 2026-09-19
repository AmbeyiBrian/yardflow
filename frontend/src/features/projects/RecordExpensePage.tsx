/**
 * T10.23 — recording an expense from the field (design §7.4; O16, D29).
 *
 * Anyone may record one, because a technician at a fuel station is closer to the
 * fact than anybody back at the yard, and a receipt photographed there is worth
 * more than one remembered on Friday.
 *
 * The receipt is asked for and not required. O16 accepts an unevidenced expense
 * and flags it to the manager, which is the right way round: refusing it loses
 * the **cost**, when all that is actually missing is the evidence. A real
 * receipt that would not photograph is still a real cost.
 */

import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { useNavigate } from 'react-router-dom';

import { applyFieldErrors, useAction, useList } from '../../api/hooks';
import { PhotoCapture } from '../../components/PhotoCapture';
import { Banner, Button, Card, Field, Input, Select, Spinner, Textarea } from '../../components/ui';
import { PageHeader } from '../../components/ui/data';
import { MONEY_INPUT } from '../../components/ui/money';
import type { ExpenseCategory, Project, ProjectExpense } from './types';

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

export default function RecordExpensePage() {
  const navigate = useNavigate();
  const [saved, setSaved] = useState<ProjectExpense | null>(null);

  const projects = useList<Project>('projects', { status: 'OPEN', page_size: 200 });
  const categories = useList<ExpenseCategory>('expense-categories', {
    is_active: true,
    page_size: 100,
  });
  const create = useAction<Record<string, unknown>, ProjectExpense>({
    resource: 'project-expenses',
  });

  const form = useForm({
    defaultValues: {
      project: '',
      category: '',
      amount: '',
      incurred_on: today(),
      description: '',
    },
  });

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Record an expense"
        subtitle="Transport, fuel, hire, permits — anything paid out of pocket on a project."
      />

      {saved ? (
        <Card>
          <Banner tone="success">
            Recorded. It reaches the project&rsquo;s cost once its manager approves it.
          </Banner>
          <div className="mt-3 flex flex-col gap-2">
            {/*
              The photo is attached after saving because an attachment needs
              something to attach to. Asking for it first would mean holding an
              image in memory on a phone that may not survive the walk back.
            */}
            <PhotoCapture
              targetType="commercials.ProjectExpense"
              targetId={String(saved.id)}
              label="Photograph the receipt"
            />
            <Button variant="ghost" onClick={() => setSaved(null)}>
              Record another
            </Button>
            <Button onClick={() => navigate('/projects')}>Done</Button>
          </div>
        </Card>
      ) : (
        <Card>
          <form className="flex flex-col gap-3">
            <Field
              label="Project"
              htmlFor="ex-project"
              error={form.formState.errors.project?.message}
            >
              <Select
                id="ex-project"
                {...form.register('project', { required: 'Which project is this for?' })}
              >
                <option value="">Choose…</option>
                {(projects.data?.results ?? [])
                  .filter((project) => project.po_number)
                  .map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.po_number} {project.title}
                    </option>
                  ))}
              </Select>
            </Field>

            <Field
              label="What kind"
              htmlFor="ex-category"
              error={form.formState.errors.category?.message}
            >
              <Select
                id="ex-category"
                {...form.register('category', { required: 'Pick a category.' })}
              >
                <option value="">Choose…</option>
                {(categories.data?.results ?? []).map((category) => (
                  <option key={category.id} value={category.id}>
                    {category.name}
                  </option>
                ))}
              </Select>
            </Field>

            <Field
              label="Amount"
              htmlFor="ex-amount"
              hint="Excluding VAT."
              error={form.formState.errors.amount?.message}
            >
              <Input
                id="ex-amount"
                inputMode="decimal"
                className={MONEY_INPUT}
                {...form.register('amount', { required: 'How much was it?' })}
              />
            </Field>

            <Field label="When" htmlFor="ex-date">
              <Input id="ex-date" type="date" {...form.register('incurred_on')} />
            </Field>

            <Field label="What for" htmlFor="ex-description">
              <Textarea id="ex-description" {...form.register('description')} />
            </Field>

            <Button
              block
              disabled={create.isPending}
              onClick={form.handleSubmit(async (values) => {
                try {
                  setSaved(await create.mutateAsync(values));
                } catch (error) {
                  applyFieldErrors(error, form.setError);
                }
              })}
            >
              {create.isPending ? <Spinner /> : 'Record it'}
            </Button>
          </form>
        </Card>
      )}
    </div>
  );
}
