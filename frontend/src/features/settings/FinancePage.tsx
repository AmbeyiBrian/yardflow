/**
 * Settings, Finance (Epic R; R4, R5; design §4.17.6, §4.17.10).
 *
 * Three things Finance owns: the daily limits per allowance type (R5, a blank
 * bound means no limit), which role counts as Director so the PM level is
 * skipped for their entries (R4), and the expense categories with the kind
 * that decides what a category demands (fuel asks the vehicle, casual labour
 * asks the casuals). The server stays the authority on every figure.
 */

import { useEffect, useState } from 'react';

import { errorMessage, useList } from '../../api/hooks';
import { PERM } from '../../auth/permissions';
import { useSession } from '../../auth/session';
import { Banner, Button, Card, Checkbox, Field, Input, Select, Spinner } from '../../components/ui';
import { DataList, EmptyState, PageHeader, Sheet } from '../../components/ui/data';
import {
  useCreateExpenseCategory,
  useExpenseCategories,
  useFinanceSettings,
  useUpdateExpenseCategory,
  useUpdateFinanceSettings,
} from '../money/api';
import type { AllowanceLimitKey, AllowanceLimits, CategoryKind, ExpenseCategory } from '../money/types';
import type { Role } from './types';

const LIMIT_ROWS: { key: AllowanceLimitKey; label: string }[] = [
  { key: 'TRANSPORT_WITHIN_NAIROBI', label: 'Transport within Nairobi' },
  { key: 'TRANSPORT_OUTSIDE_NAIROBI', label: 'Transport outside Nairobi' },
  { key: 'NIGHT_OUT', label: 'Night-out' },
  { key: 'TEAM_ALLOWANCE', label: 'Team allowance' },
];

const KIND_LABELS: Record<CategoryKind, string> = {
  GENERAL: 'General',
  FUEL: 'Fuel (asks vehicle and litres)',
  CASUAL_LABOUR: 'Casual labour (asks casuals and days)',
};

type Draft = Record<AllowanceLimitKey, { min: string; max: string }>;

function toDraft(limits: AllowanceLimits | undefined): Draft {
  const draft = {} as Draft;
  for (const { key } of LIMIT_ROWS) {
    draft[key] = { min: limits?.[key]?.min ?? '', max: limits?.[key]?.max ?? '' };
  }
  return draft;
}

export default function FinancePage() {
  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Finance"
        subtitle="Allowance limits, who counts as Director, and expense categories."
      />
      <LimitsAndDirector />
      <Categories />
    </div>
  );
}

function LimitsAndDirector() {
  const settings = useFinanceSettings();
  const roles = useList<Role>('roles', { page_size: 100 });
  const update = useUpdateFinanceSettings();
  const [draft, setDraft] = useState<Draft>(() => toDraft(undefined));
  const [director, setDirector] = useState('');
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (!settings.data) return;
    setDraft(toDraft(settings.data.allowance_limits));
    setDirector(
      settings.data.finance_director_role === null ? '' : String(settings.data.finance_director_role),
    );
  }, [settings.data]);

  function edit(key: AllowanceLimitKey, bound: 'min' | 'max', value: string) {
    setSaved(false);
    setDraft((current) => ({ ...current, [key]: { ...current[key], [bound]: value } }));
  }

  async function save() {
    setError('');
    setSaved(false);
    const limits = {} as AllowanceLimits;
    for (const { key, label } of LIMIT_ROWS) {
      const min = draft[key].min.trim().replace(/,/g, '');
      const max = draft[key].max.trim().replace(/,/g, '');
      for (const value of [min, max]) {
        if (value !== '' && !(Number(value) >= 0)) {
          setError(`${label}: limits must be amounts of 0 or more.`);
          return;
        }
      }
      if (min !== '' && max !== '' && Number(min) > Number(max)) {
        setError(`${label}: the minimum is above the maximum.`);
        return;
      }
      limits[key] = { min: min === '' ? null : min, max: max === '' ? null : max };
    }
    try {
      await update.mutateAsync({
        allowance_limits: limits,
        finance_director_role: director === '' ? null : Number(director),
      });
      setSaved(true);
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }

  if (settings.isLoading) return <Spinner className="text-slate-400" />;
  if (settings.isError) return <Banner tone="error">{errorMessage(settings.error)}</Banner>;

  return (
    <Card className="flex flex-col gap-4">
      <div>
        <h2 className="text-base font-semibold text-slate-900">Allowance limits</h2>
        <p className="text-sm text-slate-600">
          KES a day. Leave a box blank for no limit. A request is checked as the amount
          against the limit times its days.
        </p>
      </div>

      <div className="flex flex-col gap-3">
        {LIMIT_ROWS.map(({ key, label }) => (
          <fieldset key={key} className="flex flex-col gap-2 border-t border-slate-100 pt-3">
            <legend className="text-sm font-medium text-slate-900">{label}</legend>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Minimum a day" htmlFor={`min-${key}`}>
                <Input
                  id={`min-${key}`}
                  inputMode="decimal"
                  className="text-right tabular-nums"
                  value={draft[key].min}
                  onChange={(event) => edit(key, 'min', event.target.value)}
                />
              </Field>
              <Field label="Maximum a day" htmlFor={`max-${key}`}>
                <Input
                  id={`max-${key}`}
                  inputMode="decimal"
                  className="text-right tabular-nums"
                  value={draft[key].max}
                  onChange={(event) => edit(key, 'max', event.target.value)}
                />
              </Field>
            </div>
          </fieldset>
        ))}
      </div>

      <Field
        label="Director role"
        htmlFor="fin-director"
        hint="Expenses recorded by this role skip the PM and go straight to Finance."
      >
        <Select
          id="fin-director"
          value={director}
          onChange={(event) => {
            setSaved(false);
            setDirector(event.target.value);
          }}
        >
          <option value="">None</option>
          {(roles.data?.results ?? []).map((role) => (
            <option key={role.id} value={role.id}>
              {role.name}
            </option>
          ))}
        </Select>
      </Field>

      {error ? <Banner tone="error">{error}</Banner> : null}
      {saved ? <Banner tone="success">Saved.</Banner> : null}

      <div>
        <Button loading={update.isPending} onClick={save}>
          Save
        </Button>
      </div>
    </Card>
  );
}

function Categories() {
  const { hasAny } = useSession();
  // The server still gates writes on catalogue.manage; the controls follow it
  // so nobody is offered an edit that will be refused.
  const mayEdit = hasAny(PERM.CATALOGUE_MANAGE);
  const categories = useExpenseCategories();
  const [editing, setEditing] = useState<ExpenseCategory | 'new' | null>(null);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-base font-semibold text-slate-900">Expense categories</h2>
          <p className="text-sm text-slate-600">
            The kind decides what a category asks for, so renaming it keeps that behaviour.
          </p>
        </div>
        {mayEdit ? <Button onClick={() => setEditing('new')}>Add category</Button> : null}
      </div>

      {categories.isError ? <Banner tone="error">{errorMessage(categories.error)}</Banner> : null}
      {categories.isLoading ? <Spinner className="text-slate-400" /> : null}

      <DataList<ExpenseCategory>
        rows={categories.data?.results ?? []}
        rowKey={(category) => category.id}
        onRowClick={mayEdit ? (category) => setEditing(category) : undefined}
        empty={<EmptyState title="No categories yet." />}
        columns={[
          { header: 'Name', cell: (category) => category.name },
          { header: 'Kind', cell: (category) => KIND_LABELS[category.kind]?.split(' (')[0] ?? category.kind },
          {
            header: 'Status',
            cell: (category) => (category.is_active ? 'Active' : 'Hidden'),
            wideOnly: true,
          },
        ]}
      />

      <CategorySheet editing={editing} onClose={() => setEditing(null)} />
    </div>
  );
}

function CategorySheet({
  editing,
  onClose,
}: {
  editing: ExpenseCategory | 'new' | null;
  onClose: () => void;
}) {
  const existing = editing && editing !== 'new' ? editing : null;
  const [name, setName] = useState('');
  const [code, setCode] = useState('');
  const [kind, setKind] = useState<CategoryKind>('GENERAL');
  const [active, setActive] = useState(true);
  const [error, setError] = useState('');
  const create = useCreateExpenseCategory();
  const update = useUpdateExpenseCategory();

  useEffect(() => {
    setName(existing?.name ?? '');
    setCode(existing?.code ?? '');
    setKind(existing?.kind ?? 'GENERAL');
    setActive(existing?.is_active ?? true);
    setError('');
  }, [editing, existing]);

  async function save() {
    setError('');
    if (!name.trim() || !code.trim()) {
      setError('A category needs a name and a code.');
      return;
    }
    const body = { name: name.trim(), code: code.trim().toUpperCase(), kind, is_active: active };
    try {
      if (existing) await update.mutateAsync({ id: existing.id, ...body });
      else await create.mutateAsync(body);
      onClose();
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }

  const busy = create.isPending || update.isPending;

  return (
    <Sheet
      open={editing !== null}
      title={existing ? 'Edit category' : 'New category'}
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" className="flex-1" onClick={onClose}>
            Back
          </Button>
          <Button className="flex-1" disabled={busy} onClick={save}>
            {busy ? <Spinner /> : 'Save'}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <Field label="Name" htmlFor="cat-name">
          <Input id="cat-name" value={name} onChange={(event) => setName(event.target.value)} />
        </Field>
        <Field label="Code" htmlFor="cat-code">
          <Input id="cat-code" value={code} onChange={(event) => setCode(event.target.value)} />
        </Field>
        <Field label="Kind" htmlFor="cat-kind">
          <Select
            id="cat-kind"
            value={kind}
            onChange={(event) => setKind(event.target.value as CategoryKind)}
          >
            {(Object.keys(KIND_LABELS) as CategoryKind[]).map((value) => (
              <option key={value} value={value}>
                {KIND_LABELS[value]}
              </option>
            ))}
          </Select>
        </Field>
        <Checkbox
          id="cat-active"
          label="Offered when recording an expense"
          checked={active}
          onChange={(event) => setActive(event.target.checked)}
        />
        {error ? <Banner tone="error">{error}</Banner> : null}
      </div>
    </Sheet>
  );
}
