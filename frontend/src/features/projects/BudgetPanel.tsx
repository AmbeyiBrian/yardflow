/** Project budget tab (R9; §4.19.5): budget, spent, committed, pending, remaining. */

import { Banner, Spinner } from '../../components/ui';
import { DataList, EmptyState, Stat } from '../../components/ui/data';
import { Money } from '../../components/ui/money';
import {
  KIND_LABELS,
  committedPlusSpent,
  isOverBudget,
  percentUsed,
  reasonText,
} from './budget';
import { useProjectBudget, type BudgetComponent, type OverBudgetEntry } from './stage2Api';

export default function BudgetPanel({ projectId }: { projectId: number }) {
  const query = useProjectBudget(projectId);
  if (query.isLoading) return <Spinner />;
  const p = query.data;
  if (!p) return <Banner tone="error">The budget could not be loaded.</Banner>;

  // R12: a project with no PO has no budget; the position carries none.
  if (p.budget === null) {
    return (
      <EmptyState
        title="No budget yet."
        hint="This project has no PO recorded, so there is nothing to measure against."
      />
    );
  }

  const over = isOverBudget(p);
  const used = percentUsed(p);
  const components = p.components ?? [];
  const entries = p.over_budget_entries ?? [];

  return (
    <div className="flex flex-col gap-4">
      {over ? (
        <Banner tone="warning">
          Committed and spent together pass the budget. Nothing is blocked.
        </Banner>
      ) : null}

      <div className="grid grid-cols-2 gap-2 md:grid-cols-5">
        <Stat label="Budget" value={<Money value={p.budget} compact />} />
        <Stat label="Spent" value={<Money value={p.spent} compact />} hint="Already cost" />
        <Stat
          label="Committed"
          value={<Money value={p.committed} compact />}
          hint="Will become spend"
        />
        <Stat
          label="Remaining"
          value={<Money value={p.remaining} compact tone={over ? 'bad' : 'good'} />}
          tone={over ? 'bad' : 'neutral'}
          hint={used === null ? undefined : `${used}% used`}
        />
        <Stat
          label="Awaiting approval"
          value={<Money value={p.pending} compact />}
          hint="In neither figure"
        />
      </div>
      <p className="text-xs text-slate-500">
        Spent plus committed: <Money value={committedPlusSpent(p)} />
      </p>

      <section className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold text-slate-900">By kind</h2>
        <DataList<BudgetComponent>
          rows={components}
          rowKey={(c) => c.kind}
          empty={<EmptyState title="Nothing counted yet." />}
          columns={[
            { header: 'Kind', cell: (c) => KIND_LABELS[c.kind] ?? c.kind },
            { header: 'Spent', cell: (c) => <Money value={c.spent} withCurrency={false} /> },
            {
              header: 'Committed',
              cell: (c) => <Money value={c.committed} withCurrency={false} />,
            },
          ]}
        />
      </section>

      <section className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold text-slate-900">Over-budget entries</h2>
        <DataList<OverBudgetEntry>
          rows={entries}
          rowKey={(e) => `${e.document_type}-${e.id}`}
          empty={<EmptyState title="None." hint="No entry has been recorded past the budget." />}
          columns={[
            { header: 'Entry', cell: (e) => e.reference || `${e.document_type} ${e.id}` },
            { header: 'Over by', cell: (e) => <Money value={e.over_budget_by} tone="bad" /> },
            { header: 'Reason', cell: (e) => reasonText(e.over_budget_reason) },
            { header: 'Date', cell: (e) => e.recorded_on ?? '', wideOnly: true },
          ]}
        />
      </section>
    </div>
  );
}
