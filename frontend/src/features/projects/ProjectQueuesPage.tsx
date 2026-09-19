/**
 * T10.22 — the two project decisions that are not gate passes (design §7.4; O8, O16).
 *
 * These are **tabs on the Approvals screen**, not a screen of their own. They
 * were a screen of their own for exactly one day, and it was wrong: approving an
 * expense and approving a gate pass are the same act to the person doing them —
 * agreeing to something before it counts — and splitting them meant a manager
 * checking two lists and trusting neither.
 *
 * What is genuinely different about these two is only that the thing agreed to
 * is a **number** rather than a movement, which is a fact about the record and
 * not about the decision. So they are tabs, and the queue lives with every
 * other queue.
 */

import { type ReactNode, useState } from 'react';

import { errorMessage, useAction, useList } from '../../api/hooks';
import { Banner, Button, Card, Field, Spinner, Textarea } from '../../components/ui';
import { DataList, EmptyState, ListState, Sheet } from '../../components/ui/data';
import { SearchField } from '../../components/ui/SearchField';
import { Money } from '../../components/ui/money';
import type { ProjectExpense } from './types';

export function ExpenseQueue() {
  const [deciding, setDeciding] = useState<ProjectExpense | null>(null);
  const [search, setSearch] = useState('');
  const expenses = useList<ProjectExpense>('project-expenses/pending', {
    search: search || undefined,
    page_size: 100,
  });

  return (
    <>
      <SearchField
        value={search}
        onChange={setSearch}
        label="Search expenses"
        placeholder="What it was for"
      />

      <ListState query={expenses}>
        <DataList<ProjectExpense>
          rows={expenses.data?.results ?? []}
          rowKey={(expense) => expense.id}
          onRowClick={(expense) => setDeciding(expense)}
          empty={<EmptyState title="Nothing waiting." hint="Expenses appear here for approval." />}
          columns={[
            { header: 'Project', cell: (expense) => expense.project_reference ?? '' },
            { header: 'What', cell: (expense) => expense.category_name ?? '' },
            { header: 'Amount', cell: (expense) => <Money value={expense.amount} /> },
            { header: 'When', cell: (expense) => expense.incurred_on, wideOnly: true },
            { header: 'Who', cell: (expense) => expense.recorded_by_name ?? '', wideOnly: true },
          ]}
        />
      </ListState>

      <DecideExpenseSheet
        expense={deciding}
        onClose={() => setDeciding(null)}
        onDecided={() => {
          setDeciding(null);
          expenses.refetch();
        }}
      />
    </>
  );
}

function DecideExpenseSheet({
  expense,
  onClose,
  onDecided,
}: {
  expense: ProjectExpense | null;
  onClose: () => void;
  onDecided: () => void;
}) {
  const [reason, setReason] = useState('');
  const [error, setError] = useState('');
  const decide = useAction<{ id: number; approved: boolean; reason: string }>({
    resource: 'project-expenses',
    path: (body) => `${body.id}/decide`,
    invalidates: ['project-expenses', 'project-expenses/pending', 'projects'],
  });

  async function run(approved: boolean) {
    if (!expense) return;
    setError('');
    if (!approved && !reason.trim()) {
      setError('Rejecting an expense needs a reason.');
      return;
    }
    try {
      await decide.mutateAsync({ id: expense.id, approved, reason });
      setReason('');
      onDecided();
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }

  return (
    <Sheet
      open={expense !== null}
      title="Expense"
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" className="flex-1" onClick={() => run(false)}>
            Reject
          </Button>
          <Button className="flex-1" disabled={decide.isPending} onClick={() => run(true)}>
            {decide.isPending ? <Spinner /> : 'Approve'}
          </Button>
        </>
      }
    >
      {expense ? (
        <div className="flex flex-col gap-3">
          <Card>
            <dl className="flex flex-col gap-1 text-sm">
              <Row label="Project" value={expense.project_reference ?? ''} />
              <Row label="Category" value={expense.category_name ?? ''} />
              <Row label="Amount" value={<Money value={expense.amount} />} />
              <Row label="Incurred" value={expense.incurred_on} />
              <Row label="Recorded by" value={expense.recorded_by_name ?? ''} />
            </dl>
            {expense.description ? (
              <p className="mt-2 text-sm text-slate-600">{expense.description}</p>
            ) : null}
          </Card>

          <Banner tone="info">
            Approving adds this to the project&rsquo;s cost. An approved expense
            cannot be edited afterwards — a mistake is corrected by a reversing
            entry, so both stay visible.
          </Banner>

          {error ? <Banner tone="error">{error}</Banner> : null}

          <Field label="Reason" htmlFor="ex-reason" hint="Required to reject.">
            <Textarea
              id="ex-reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </Field>
        </div>
      ) : null}
    </Sheet>
  );
}

interface Closeout {
  id: number;
  job: number;
  job_reference?: string;
  submitted_by_name?: string;
  submitted_at: string | null;
  cost_acceptance: string;
}

export function CloseoutQueue() {
  const [deciding, setDeciding] = useState<Closeout | null>(null);
  const closeouts = useList<Closeout>('job-closeouts', {
    cost_acceptance: 'PENDING',
    page_size: 100,
  });

  return (
    <>
      <Banner tone="info">
        The material on these has already moved. What is being accepted here is
        what it cost your project, not whether it happened.
      </Banner>

      <ListState query={closeouts}>
        <DataList<Closeout>
          rows={closeouts.data?.results ?? []}
          rowKey={(closeout) => closeout.id}
          onRowClick={(closeout) => setDeciding(closeout)}
          empty={<EmptyState title="Nothing waiting." />}
          columns={[
            { header: 'Job', cell: (closeout) => closeout.job_reference ?? String(closeout.job) },
            { header: 'Reported by', cell: (closeout) => closeout.submitted_by_name ?? '' },
            { header: 'When', cell: (closeout) => closeout.submitted_at ?? '', wideOnly: true },
          ]}
        />
      </ListState>

      <AcceptCostSheet
        closeout={deciding}
        onClose={() => setDeciding(null)}
        onDecided={() => {
          setDeciding(null);
          closeouts.refetch();
        }}
      />
    </>
  );
}

function AcceptCostSheet({
  closeout,
  onClose,
  onDecided,
}: {
  closeout: Closeout | null;
  onClose: () => void;
  onDecided: () => void;
}) {
  const [reason, setReason] = useState('');
  const [error, setError] = useState('');
  const decide = useAction<{ id: number; accepted: boolean; reason: string }>({
    resource: 'job-closeouts',
    path: (body) => `${body.id}/accept-cost`,
    invalidates: ['job-closeouts', 'projects'],
  });

  async function run(accepted: boolean) {
    if (!closeout) return;
    setError('');
    if (!accepted && !reason.trim()) {
      setError('Querying a closeout needs a reason.');
      return;
    }
    try {
      await decide.mutateAsync({ id: closeout.id, accepted, reason });
      setReason('');
      onDecided();
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }

  return (
    <Sheet
      open={closeout !== null}
      title="Accept what it cost"
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" className="flex-1" onClick={() => run(false)}>
            Query it
          </Button>
          <Button className="flex-1" disabled={decide.isPending} onClick={() => run(true)}>
            {decide.isPending ? <Spinner /> : 'Accept'}
          </Button>
        </>
      }
    >
      {closeout ? (
        <div className="flex flex-col gap-3">
          <p className="text-sm text-slate-600">
            Querying asks for a corrected closeout. It does not take the material
            back — that has already moved, and anything posted in error is undone
            by a reversal so both stay on the record.
          </p>

          {error ? <Banner tone="error">{error}</Banner> : null}

          <Field label="Reason" htmlFor="co-reason" hint="Required to query.">
            <Textarea
              id="co-reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </Field>
        </div>
      ) : null}
    </Sheet>
  );
}

function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex justify-between gap-3">
      <dt className="text-slate-500">{label}</dt>
      <dd className="text-right font-medium text-slate-900">{value}</dd>
    </div>
  );
}
