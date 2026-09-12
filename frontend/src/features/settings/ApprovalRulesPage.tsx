/**
 * Who signs for what (F3, §5.1, §5.2).
 *
 * The endpoint has always existed and nothing in the app called it, so the only
 * way to write an approval rule was the Django admin — the platform owner's
 * side of the fence, for a setting that is entirely the tenant's.
 *
 * The effect was worse than a missing screen. A tenant with no rules approves
 * every gate-out itself, correctly and by design: a yard that has named no
 * approvers must not have passes stranded waiting on nobody. But nothing said
 * so, so approval looked like a feature and did nothing, and a technician's
 * request for client-owned material approving itself in the same second read as
 * a permissions bug.
 *
 * Hence the first thing this screen does: when the list is empty, say what that
 * means in a sentence, before anybody has to ask.
 */

import { useState } from 'react';

import { errorMessage, useAction, useList } from '../../api/hooks';
import { PERM } from '../../auth/permissions';
import { useSession } from '../../auth/session';
import { Banner, Button, Card, Field, Select, Spinner } from '../../components/ui';
import { EmptyState, PageHeader, Sheet } from '../../components/ui/data';
import type { ItemCategory, Role } from './types';

type Criticality = 'NONE' | 'LOW' | 'MEDIUM' | 'HIGH';

interface ApprovalRule {
  id: number;
  category: number | null;
  category_name: string;
  criticality: Criticality;
  required_role: number;
  role_name: string;
  sequence: number;
  is_active: boolean;
  description: string;
}

const CRITICALITY: { value: Criticality; label: string; hint: string }[] = [
  {
    value: 'HIGH',
    label: 'High criticality',
    hint: 'Radios, antennas, anything expensive or hard to replace.',
  },
  { value: 'MEDIUM', label: 'Medium criticality', hint: 'Ordinary installation material.' },
  { value: 'LOW', label: 'Low criticality', hint: 'Consumables and sundries.' },
  {
    value: 'NONE',
    label: 'Everything, whatever its criticality',
    hint: 'Every gate-out needs this signature. Thorough, and slow.',
  },
];

/** The rule as a sentence, because that is how somebody checks it is right. */
function asSentence(rule: ApprovalRule): string {
  const level = rule.criticality.toLowerCase();
  const what =
    rule.criticality === 'NONE'
      ? 'Anything leaving the yard'
      : `${level.charAt(0).toUpperCase()}${level.slice(1)}-criticality material`;
  const scope = rule.category_name ? ` in ${rule.category_name}` : '';
  return `${what}${scope} must be approved by the ${rule.role_name}.`;
}

export default function ApprovalRulesPage() {
  const { hasAny } = useSession();
  const mayEdit = hasAny(PERM.SETTINGS_MANAGE);

  const rules = useList<ApprovalRule>('approval-rules', { page_size: 100 });
  const roles = useList<Role>('roles', { page_size: 100 });
  const categories = useList<ItemCategory>('item-categories', { page_size: 200 });

  const [sheet, setSheet] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const [form, setForm] = useState({
    criticality: 'HIGH' as Criticality,
    required_role: '',
    category: '',
    sequence: '1',
  });

  const create = useAction<Record<string, unknown>, ApprovalRule>({
    resource: 'approval-rules',
    invalidates: ['approval-rules'],
  });
  const update = useAction<{ id: number; is_active: boolean }, ApprovalRule>({
    resource: 'approval-rules',
    method: 'patch',
    path: (body) => String(body.id),
    invalidates: ['approval-rules'],
  });
  const remove = useAction<{ id: number }, unknown>({
    resource: 'approval-rules',
    method: 'delete',
    path: (body) => String(body.id),
    invalidates: ['approval-rules'],
  });

  async function add() {
    setBanner(null);
    if (!form.required_role) {
      setBanner('Choose who has to approve it.');
      return;
    }
    try {
      await create.mutateAsync({
        criticality: form.criticality,
        required_role: Number(form.required_role),
        category: form.category ? Number(form.category) : null,
        sequence: Number(form.sequence) || 1,
        is_active: true,
      });
      setSheet(false);
      setForm({ criticality: 'HIGH', required_role: '', category: '', sequence: '1' });
    } catch (error) {
      setBanner(errorMessage(error));
    }
  }

  if (rules.isLoading) return <Spinner className="text-slate-400" />;
  if (rules.isError) return <Banner tone="error">{errorMessage(rules.error)}</Banner>;

  const list = rules.data?.results ?? [];
  const active = list.filter((rule) => rule.is_active);

  return (
    <div className="flex flex-col gap-4 pb-16">
      <PageHeader
        title="Approvals"
        subtitle="Who has to sign before material leaves the yard."
        actions={mayEdit ? <Button onClick={() => setSheet(true)}>Add a rule</Button> : undefined}
      />

      {banner ? <Banner tone="error">{banner}</Banner> : null}

      {/* The state every new organization is in, said plainly. Without this the
          first auto-approved pass reads as a broken permission. */}
      {active.length === 0 ? (
        <Banner tone="warning">
          No rules are in force, so <strong>every gate-out approves itself</strong>.
          Nothing is waiting on anybody, and each pass records that no rule
          applied. Add a rule to change that.
        </Banner>
      ) : null}

      <Card className="flex flex-col gap-2">
        {list.length === 0 ? (
          <EmptyState
            title="No approval rules yet."
            hint="A rule names the material and the role that must sign for it."
          />
        ) : (
          <ul className="flex flex-col gap-2">
            {list.map((rule) => (
              <li
                key={rule.id}
                className="flex flex-wrap items-start justify-between gap-3 rounded-lg border border-slate-200 p-3"
              >
                <div className="min-w-0">
                  <p className="text-sm text-slate-900">{asSentence(rule)}</p>
                  <p className="text-xs text-slate-500">
                    {/* Sequence is how two signatures are ordered — worth
                        showing only when there is more than one rule. */}
                    {list.length > 1 ? `Level ${rule.sequence}` : 'One signature'}
                    {rule.is_active ? '' : ' · switched off'}
                  </p>
                </div>
                {mayEdit ? (
                  <div className="flex shrink-0 items-center gap-1">
                    <Button
                      variant="ghost"
                      className="min-h-0 px-2 py-1 text-sm"
                      onClick={() =>
                        void update
                          .mutateAsync({ id: rule.id, is_active: !rule.is_active })
                          .catch((error) => setBanner(errorMessage(error)))
                      }
                    >
                      {rule.is_active ? 'Switch off' : 'Switch on'}
                    </Button>
                    <Button
                      variant="ghost"
                      className="min-h-0 px-2 py-1 text-sm text-red-700"
                      onClick={() =>
                        void remove
                          .mutateAsync({ id: rule.id })
                          .catch((error) => setBanner(errorMessage(error)))
                      }
                    >
                      Remove
                    </Button>
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <p className="text-sm text-slate-600">
        A pass is routed on the <strong>highest criticality</strong> on it: one
        antenna among twenty cable ties makes the whole pass an antenna. Where
        two rules match, both signatures are collected, in level order.
      </p>

      <Sheet
        open={sheet}
        title="Add an approval rule"
        onClose={() => setSheet(false)}
        footer={
          <>
            <Button variant="secondary" block onClick={() => setSheet(false)}>
              Cancel
            </Button>
            <Button block loading={create.isPending} onClick={() => void add()}>
              Add the rule
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          <Field
            label="What it covers"
            htmlFor="rule-criticality"
            hint={CRITICALITY.find((entry) => entry.value === form.criticality)?.hint}
          >
            <Select
              id="rule-criticality"
              value={form.criticality}
              onChange={(event) =>
                setForm({ ...form, criticality: event.target.value as Criticality })
              }
            >
              {CRITICALITY.map((entry) => (
                <option key={entry.value} value={entry.value}>
                  {entry.label}
                </option>
              ))}
            </Select>
          </Field>

          <Field
            label="Who has to approve it"
            htmlFor="rule-role"
            hint="Anybody holding this role can sign. It is a role, not a person, so a holiday does not stop the yard."
          >
            <Select
              id="rule-role"
              value={form.required_role}
              onChange={(event) => setForm({ ...form, required_role: event.target.value })}
            >
              <option value="">Choose…</option>
              {(roles.data?.results ?? []).map((role) => (
                <option key={role.id} value={role.id}>
                  {role.name}
                </option>
              ))}
            </Select>
          </Field>

          <Field
            label="Only for one category"
            htmlFor="rule-category"
            hint="Leave this alone unless the rule is meant for one kind of material only."
          >
            <Select
              id="rule-category"
              value={form.category}
              onChange={(event) => setForm({ ...form, category: event.target.value })}
            >
              <option value="">Any category</option>
              {(categories.data?.results ?? []).map((category) => (
                <option key={category.id} value={category.id}>
                  {category.name}
                </option>
              ))}
            </Select>
          </Field>

          <Field
            label="Level"
            htmlFor="rule-sequence"
            hint="Two rules at different levels mean two signatures, collected in order. Leave it at 1 for a single approval."
          >
            <Select
              id="rule-sequence"
              value={form.sequence}
              onChange={(event) => setForm({ ...form, sequence: event.target.value })}
            >
              <option value="1">Level 1</option>
              <option value="2">Level 2</option>
              <option value="3">Level 3</option>
            </Select>
          </Field>
        </div>
      </Sheet>
    </div>
  );
}
