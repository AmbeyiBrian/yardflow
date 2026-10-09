/**
 * Project milestones tab (R11, R12; design §4.19.7, §4.19.13).
 *
 * The PO header, the milestones (share, condition, state), what is invoiced and
 * received, and what is still outstanding. Finance (`finance.approve`) edits
 * milestones and records invoices and receipts; everyone else reads. The server
 * owns each milestone's state (DUE / OVERDUE / ...); the totals here are cents
 * sums of what it returned.
 */

import { useState } from 'react';
import { useForm } from 'react-hook-form';

import { applyFieldErrors, errorMessage, useDetail } from '../../api/hooks';
import { PERM } from '../../auth/permissions';
import { useSession } from '../../auth/session';
import { PhotoCapture } from '../../components/PhotoCapture';
import { Banner, Button, Field, Input, Select, Spinner } from '../../components/ui';
import { DataList, EmptyState, Sheet, Stat } from '../../components/ui/data';
import { Money, MoneyInput } from '../../components/ui/money';
import {
  CONDITION_LABELS,
  STATE_LABELS,
  fromCents,
  milestoneAmountCents,
  percentSharesTotal,
  poTotals,
  receiptRoomCents,
  toCents,
} from './milestones';
import {
  useAddDefaultMilestones,
  useAddMilestone,
  useDeleteMilestone,
  useMilestones,
  useRecordInvoice,
  useRecordReceipt,
  useUpdateMilestone,
  type Milestone,
  type MilestoneCondition,
  type MilestoneState,
  type ShareType,
} from './milestonesApi';
import type { Project } from './types';

const STATE_TONES: Record<MilestoneState, string> = {
  NOT_DUE: 'bg-slate-100 text-slate-700',
  DUE: 'bg-amber-100 text-amber-900',
  OVERDUE: 'bg-red-100 text-red-900',
  INVOICED: 'bg-sky-100 text-sky-900',
  PART_PAID: 'bg-sky-100 text-sky-900',
  PAID: 'bg-emerald-100 text-emerald-900',
};

function localToday(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

type Editing = { kind: 'milestone'; milestone: Milestone | null } | null;

export default function MilestonesPanel({ projectId }: { projectId: number }) {
  const { has } = useSession();
  const canRecord = has(PERM.FINANCE_APPROVE);
  const project = useDetail<Project>('projects', String(projectId));
  const list = useMilestones(projectId);
  const defaults = useAddDefaultMilestones(projectId);
  const remove = useDeleteMilestone(projectId);

  const [editing, setEditing] = useState<Editing>(null);
  const [invoicing, setInvoicing] = useState<Milestone | null>(null);
  const [receiving, setReceiving] = useState<Milestone | null>(null);

  if (project.isLoading || list.isLoading) return <Spinner />;
  const p = project.data;
  if (!p) return <Banner tone="error">The project could not be loaded.</Banner>;

  // R12: a project with no PO has no milestones; "Attach PO" is on the project page.
  if (!p.po_number) {
    return (
      <EmptyState
        title="No PO recorded yet."
        hint="Milestones follow the purchase order. Use Attach PO on this project when it arrives."
      />
    );
  }

  const milestones = [...(list.data?.results ?? [])].sort((a, b) => a.sequence - b.sequence);
  const value = p.current_contract_value ?? p.contract_value;
  const totals = poTotals(value, milestones);
  const sharesTotal = percentSharesTotal(milestones);
  const anyPercent = milestones.some((m) => m.share_type === 'PERCENT');
  const sharesIncomplete = milestones.length > 0 && anyPercent && Math.abs(sharesTotal - 100) > 0.001;
  const open = p.status === 'OPEN';
  const noTerms = !p.payment_terms_days;

  const amountOf = (m: Milestone): string | null =>
    m.amount ?? (value ? fromCents(milestoneAmountCents(m.share_type, m.share_value, value)) : null);

  return (
    <div className="flex flex-col gap-4">
      <section className="flex flex-col gap-1 text-sm text-slate-700">
        <p>
          PO <span className="font-medium text-slate-900">{p.po_number}</span>
          {p.po_issue_date ? <> · issued {p.po_issue_date}</> : null}
        </p>
        <p>
          Payment terms:{' '}
          <span className="font-medium text-slate-900">
            {p.payment_terms || '—'}
            {p.payment_terms_days ? ` (${p.payment_terms_days} days)` : ''}
          </span>
        </p>
        {noTerms ? (
          <p className="text-xs text-amber-700">
            Set payment terms: with no days on the PO, no milestone is ever marked overdue.
          </p>
        ) : null}
      </section>

      {sharesIncomplete ? (
        <Banner tone="warning">
          Percent shares total {sharesTotal}%, not 100%. Contracts differ, so nothing is blocked.
        </Banner>
      ) : null}

      <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
        <Stat label="PO value" value={<Money value={value} compact />} />
        <Stat label="Invoiced" value={<Money value={fromCents(totals.invoiced)} compact />} />
        <Stat label="Received" value={<Money value={fromCents(totals.received)} compact />} />
        <Stat
          label="Outstanding"
          value={<Money value={fromCents(totals.outstanding)} compact />}
          hint={`Invoiced, unpaid: ${fromCents(totals.invoicedUnpaid)}`}
        />
      </div>

      {remove.error ? <Banner tone="error">{errorMessage(remove.error)}</Banner> : null}
      {defaults.error ? <Banner tone="error">{errorMessage(defaults.error)}</Banner> : null}

      <DataList<Milestone>
        rows={milestones}
        rowKey={(m) => m.id}
        empty={
          <EmptyState
            title="No milestones yet."
            hint={
              canRecord
                ? 'Add the default three (Deposit, Conditional acceptance, Final acceptance) or your own.'
                : 'Finance adds them from the PO.'
            }
          />
        }
        columns={[
          {
            header: 'Milestone',
            cell: (m) => (
              <span className="font-medium">
                M{m.sequence} · {m.name}
              </span>
            ),
          },
          {
            header: 'Share',
            cell: (m) =>
              m.share_value === null || m.share_value === ''
                ? 'Not set'
                : m.share_type === 'PERCENT'
                  ? `${m.share_value}%`
                  : 'Fixed',
          },
          { header: 'Amount', cell: (m) => <Money value={amountOf(m)} placeholder="—" withCurrency={false} /> },
          {
            header: 'Condition',
            cell: (m) =>
              m.condition === 'DATE' && m.condition_date
                ? `On ${m.condition_date}`
                : CONDITION_LABELS[m.condition],
            wideOnly: true,
          },
          {
            header: 'Invoiced',
            cell: (m) => <Money value={m.invoiced} placeholder="—" withCurrency={false} />,
          },
          {
            header: 'Received',
            cell: (m) => <Money value={m.received} placeholder="—" withCurrency={false} />,
          },
          {
            header: 'State',
            cell: (m) => (
              <span
                className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap ${STATE_TONES[m.state] ?? STATE_TONES.NOT_DUE}`}
              >
                {STATE_LABELS[m.state] ?? m.state}
              </span>
            ),
          },
          {
            header: '',
            cell: (m) =>
              canRecord && open ? (
                <div className="flex flex-wrap gap-1">
                  <Button variant="ghost" onClick={() => setInvoicing(m)}>
                    Invoice
                  </Button>
                  <Button
                    variant="ghost"
                    disabled={receiptRoomCents(toCents(m.invoiced), toCents(m.received)) === 0}
                    onClick={() => setReceiving(m)}
                  >
                    Receipt
                  </Button>
                  <Button variant="ghost" onClick={() => setEditing({ kind: 'milestone', milestone: m })}>
                    Edit
                  </Button>
                  {toCents(m.invoiced) === 0 ? (
                    <Button
                      variant="ghost"
                      disabled={remove.isPending}
                      onClick={() => remove.mutate({ id: m.id })}
                    >
                      Remove
                    </Button>
                  ) : null}
                </div>
              ) : null,
          },
        ]}
      />

      {canRecord && open ? (
        <div className="flex flex-wrap gap-2">
          <Button onClick={() => setEditing({ kind: 'milestone', milestone: null })}>
            Add milestone
          </Button>
          {milestones.length === 0 ? (
            <Button
              variant="ghost"
              disabled={defaults.isPending}
              onClick={() => defaults.mutate({})}
            >
              {defaults.isPending ? <Spinner /> : 'Add default milestones'}
            </Button>
          ) : null}
        </div>
      ) : null}

      <MilestoneSheet
        projectId={projectId}
        milestone={editing?.milestone ?? null}
        open={editing !== null}
        nextSequence={milestones.length + 1}
        onClose={() => setEditing(null)}
      />
      <InvoiceSheet projectId={projectId} milestone={invoicing} onClose={() => setInvoicing(null)} />
      <ReceiptSheet projectId={projectId} milestone={receiving} onClose={() => setReceiving(null)} />
    </div>
  );
}

interface MilestoneForm {
  name: string;
  share_type: ShareType;
  share_value: string;
  condition: MilestoneCondition;
  condition_date: string;
}

/** Add or edit. After an invoice only the condition date may change; the server enforces it (§4.19.7). */
function MilestoneSheet({
  projectId,
  milestone,
  open,
  nextSequence,
  onClose,
}: {
  projectId: number;
  milestone: Milestone | null;
  open: boolean;
  nextSequence: number;
  onClose: () => void;
}) {
  const add = useAddMilestone(projectId);
  const update = useUpdateMilestone(projectId);
  const form = useForm<MilestoneForm>({
    values: {
      name: milestone?.name ?? '',
      share_type: milestone?.share_type ?? 'PERCENT',
      share_value: milestone?.share_value ?? '',
      condition: milestone?.condition ?? 'NONE',
      condition_date: milestone?.condition_date ?? '',
    },
  });
  const condition = form.watch('condition');
  const shareType = form.watch('share_type');
  const pending = add.isPending || update.isPending;

  return (
    <Sheet
      open={open}
      title={milestone ? `Edit M${milestone.sequence}` : `Add M${nextSequence}`}
      onClose={onClose}
      footer={
        <Button
          className="w-full"
          disabled={pending}
          onClick={form.handleSubmit(async (v) => {
            const body = {
              name: v.name,
              share_type: v.share_type,
              share_value: v.share_value || null,
              condition: v.condition,
              condition_date: v.condition === 'DATE' ? v.condition_date || null : null,
            };
            try {
              if (milestone) await update.mutateAsync({ ...body, id: milestone.id });
              else await add.mutateAsync(body);
              onClose();
            } catch (error) {
              applyFieldErrors(error, form.setError);
            }
          })}
        >
          {pending ? <Spinner /> : 'Save'}
        </Button>
      }
    >
      <form className="flex flex-col gap-3">
        <Field label="Name" htmlFor="ms-name" error={form.formState.errors.name?.message}>
          <Input id="ms-name" {...form.register('name', { required: 'Name the milestone.' })} />
        </Field>
        <div className="grid grid-cols-2 gap-2">
          <Field label="Share as" htmlFor="ms-type">
            <Select id="ms-type" {...form.register('share_type')}>
              <option value="PERCENT">Percent of PO value</option>
              <option value="AMOUNT">Fixed amount</option>
            </Select>
          </Field>
          <Field
            label={shareType === 'PERCENT' ? 'Percent' : 'Amount'}
            htmlFor="ms-share"
            error={form.formState.errors.share_value?.message}
          >
            {shareType === 'PERCENT' ? (
              <Input id="ms-share" inputMode="decimal" {...form.register('share_value')} />
            ) : (
              <MoneyInput id="ms-share" defaultValue={milestone?.share_value ?? ''} {...form.register('share_value')} />
            )}
          </Field>
        </div>
        <Field label="Becomes due" htmlFor="ms-cond">
          <Select id="ms-cond" {...form.register('condition')}>
            <option value="NONE">On the PO</option>
            <option value="ALL_SITES_ACCEPTED">When all sites are accepted</option>
            <option value="DATE">On a date</option>
          </Select>
        </Field>
        {condition === 'DATE' ? (
          <Field label="Date" htmlFor="ms-date" error={form.formState.errors.condition_date?.message}>
            <Input id="ms-date" type="date" {...form.register('condition_date', { required: 'Pick the date.' })} />
          </Field>
        ) : null}
        {add.error || update.error ? (
          <Banner tone="error">{errorMessage(add.error ?? update.error)}</Banner>
        ) : null}
      </form>
    </Sheet>
  );
}

interface InvoiceForm {
  invoice_number: string;
  invoice_date: string;
  amount: string;
}

/** Record an invoice, then attach the invoice document to it. */
function InvoiceSheet({
  projectId,
  milestone,
  onClose,
}: {
  projectId: number;
  milestone: Milestone | null;
  onClose: () => void;
}) {
  const record = useRecordInvoice(projectId);
  const [savedId, setSavedId] = useState<number | null>(null);
  const form = useForm<InvoiceForm>({
    values: { invoice_number: '', invoice_date: localToday(), amount: '' },
  });

  const close = () => {
    setSavedId(null);
    form.reset();
    onClose();
  };

  return (
    <Sheet
      open={milestone !== null}
      title={milestone ? `Invoice for M${milestone.sequence} · ${milestone.name}` : 'Invoice'}
      onClose={close}
      footer={
        savedId ? (
          <Button className="w-full" onClick={close}>
            Done
          </Button>
        ) : (
          <Button
            className="w-full"
            disabled={record.isPending}
            onClick={form.handleSubmit(async (v) => {
              if (!milestone) return;
              try {
                const created = await record.mutateAsync({ ...v, milestone: milestone.id });
                setSavedId(created.id);
              } catch (error) {
                applyFieldErrors(error, form.setError);
              }
            })}
          >
            {record.isPending ? <Spinner /> : 'Record invoice'}
          </Button>
        )
      }
    >
      {savedId ? (
        <div className="flex flex-col gap-3">
          <Banner tone="success">Invoice recorded.</Banner>
          <PhotoCapture
            targetType="commercials.MilestoneInvoice"
            targetId={savedId}
            kind="DOCUMENT"
            label="Invoice document"
            caption="Invoice"
          />
        </div>
      ) : (
        <form className="flex flex-col gap-3">
          <Field label="Invoice number" htmlFor="mi-number" error={form.formState.errors.invoice_number?.message}>
            <Input id="mi-number" {...form.register('invoice_number', { required: 'Enter the invoice number.' })} />
          </Field>
          <Field label="Invoice date" htmlFor="mi-date" error={form.formState.errors.invoice_date?.message}>
            <Input id="mi-date" type="date" {...form.register('invoice_date', { required: 'Pick the date.' })} />
          </Field>
          <Field label="Amount" htmlFor="mi-amount" hint="Excluding VAT." error={form.formState.errors.amount?.message}>
            <MoneyInput id="mi-amount" {...form.register('amount', { required: 'Enter the amount.' })} />
          </Field>
          {record.error ? <Banner tone="error">{errorMessage(record.error)}</Banner> : null}
        </form>
      )}
    </Sheet>
  );
}

interface ReceiptForm {
  received_on: string;
  amount: string;
  reference: string;
}

/** Record a payment received; partial receipts are fine, more than invoiced is refused (§4.19.7). */
function ReceiptSheet({
  projectId,
  milestone,
  onClose,
}: {
  projectId: number;
  milestone: Milestone | null;
  onClose: () => void;
}) {
  const record = useRecordReceipt(projectId);
  const form = useForm<ReceiptForm>({
    values: { received_on: localToday(), amount: '', reference: '' },
  });
  const room = milestone ? receiptRoomCents(toCents(milestone.invoiced), toCents(milestone.received)) : 0;
  const typed = toCents(form.watch('amount'));

  const close = () => {
    form.reset();
    onClose();
  };

  return (
    <Sheet
      open={milestone !== null}
      title={milestone ? `Receipt for M${milestone.sequence} · ${milestone.name}` : 'Receipt'}
      onClose={close}
      footer={
        <Button
          className="w-full"
          disabled={record.isPending}
          onClick={form.handleSubmit(async (v) => {
            if (!milestone) return;
            try {
              await record.mutateAsync({ ...v, milestone: milestone.id });
              close();
            } catch (error) {
              applyFieldErrors(error, form.setError);
            }
          })}
        >
          {record.isPending ? <Spinner /> : 'Record receipt'}
        </Button>
      }
    >
      <form className="flex flex-col gap-3">
        <p className="text-sm text-slate-600">
          Invoiced and not yet received: <Money value={fromCents(room)} />. Part payments are fine.
        </p>
        {typed > room ? (
          <Banner tone="warning">This is more than has been invoiced; the server will refuse it.</Banner>
        ) : null}
        <Field label="Received on" htmlFor="mr-date" error={form.formState.errors.received_on?.message}>
          <Input id="mr-date" type="date" {...form.register('received_on', { required: 'Pick the date.' })} />
        </Field>
        <Field label="Amount" htmlFor="mr-amount" error={form.formState.errors.amount?.message}>
          <MoneyInput id="mr-amount" {...form.register('amount', { required: 'Enter the amount.' })} />
        </Field>
        <Field label="Reference" htmlFor="mr-ref" hint="Bank or remittance reference." error={form.formState.errors.reference?.message}>
          <Input id="mr-ref" {...form.register('reference')} />
        </Field>
        {record.error ? <Banner tone="error">{errorMessage(record.error)}</Banner> : null}
      </form>
    </Sheet>
  );
}
