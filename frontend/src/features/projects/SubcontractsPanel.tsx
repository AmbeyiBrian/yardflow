/**
 * Project subcontracts tab (R8; §4.19.4, §4.19.13): contract value, work done,
 * paid and owed, the contract document, and the payments Finance enters for the
 * PM to approve.
 */

import { useState } from 'react';

import { errorMessage, useList, useResource } from '../../api/hooks';
import { PERM } from '../../auth/permissions';
import { useSession } from '../../auth/session';
import { PhotoCapture, type Attachment } from '../../components/PhotoCapture';
import { Banner, Button, Card, Checkbox, Field, Input, Select, Spinner, Textarea } from '../../components/ui';
import { EmptyState, StatusBadge, Sheet } from '../../components/ui/data';
import { Money, MoneyInput } from '../../components/ui/money';
import { dateOnly } from './budget';
import { fromCents, owedCents, owedLabel, paidCents, toCents, workDoneCents } from './subcontracts';
import {
  useCreateSubcontract,
  useRecordPayment,
  useSubcontract,
  useSubcontracts,
  useUpdateSubcontract,
  type Subcontract,
  type SubcontractPayment,
} from './subcontractsApi';
import { useProjectSites } from './stage2Api';
import type { Subcontractor } from './types';

const CONTRACT = 'Contract';
const INVOICE = 'Invoice';
const CONTRACT_TARGET = 'commercials.Subcontract';
const PAYMENT_TARGET = 'commercials.SubcontractPayment';

export default function SubcontractsPanel({
  projectId,
  isManager = false,
}: {
  projectId: number;
  /** The project's PM: creates and edits contracts (§4.19.4). */
  isManager?: boolean;
}) {
  const { has } = useSession();
  const isFinance = has(PERM.FINANCE_APPROVE);
  const canManage = isManager || isFinance;
  const list = useSubcontracts(projectId);
  const [editing, setEditing] = useState<Subcontract | 'new' | null>(null);

  if (list.isLoading) return <Spinner />;
  const rows = list.data?.results ?? [];

  return (
    <div className="flex flex-col gap-3">
      {canManage ? (
        <div>
          <Button onClick={() => setEditing('new')}>Add subcontract</Button>
        </div>
      ) : null}
      {rows.length === 0 ? (
        <EmptyState
          title="No subcontracts on this project."
          hint="Jobs given to a subcontractor still work without one; they just are not under a contract."
        />
      ) : (
        rows.map((row) => (
          <SubcontractCard
            key={row.id}
            id={row.id}
            summary={row}
            canManage={canManage}
            canRecord={isFinance}
            onEdit={(sc) => setEditing(sc)}
          />
        ))
      )}
      {editing ? (
        <SubcontractSheet
          projectId={projectId}
          subcontract={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
        />
      ) : null}
    </div>
  );
}

function SubcontractCard({
  id,
  summary,
  canManage,
  canRecord,
  onEdit,
}: {
  id: number;
  summary: Subcontract;
  canManage: boolean;
  canRecord: boolean;
  onEdit: (sc: Subcontract) => void;
}) {
  const detail = useSubcontract(id);
  const sc = detail.data ?? summary;
  const [paying, setPaying] = useState(false);
  const docs = useResource<Attachment[] | { results: Attachment[] }>('attachments', {
    target_type: CONTRACT_TARGET,
    target_id: String(id),
    page_size: 100,
  });
  const files = Array.isArray(docs.data) ? docs.data : (docs.data?.results ?? []);

  // The server's position is the figure of record; the local sums only fill in
  // when the detail carries jobs and payments but no position.
  const pos = sc.position;
  const workDone = pos?.work_done ?? (sc.jobs ? fromCents(workDoneCents(sc.jobs)) : undefined);
  const paid = pos?.paid ?? (sc.payments ? fromCents(paidCents(sc.payments)) : undefined);
  const owed =
    pos?.owed ??
    (workDone !== undefined && paid !== undefined
      ? fromCents(owedCents(toCents(workDone), toCents(paid)))
      : undefined);

  return (
    <Card className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        <div>
          <p className="text-sm font-semibold text-slate-900">
            {sc.reference} · {sc.subcontractor_name ?? `Subcontractor ${sc.subcontractor}`}
          </p>
          <p className="text-xs text-slate-500">
            {sc.sites.length} site{sc.sites.length === 1 ? '' : 's'}
          </p>
        </div>
        <StatusBadge status={sc.status} />
      </div>

      <dl className="grid grid-cols-2 gap-2 text-sm">
        <Figure label="Contract value" value={sc.contract_value} />
        <Figure label="Work done" value={workDone} />
        <Figure label="Paid" value={paid} />
        <Figure
          label={owed !== undefined ? owedLabel(toCents(owed)) : 'Owed'}
          value={owed !== undefined && toCents(owed) < 0 ? fromCents(-toCents(owed)) : owed}
        />
      </dl>
      {pos?.awaiting_approval && toCents(pos.awaiting_approval) > 0 ? (
        <p className="text-xs text-slate-600">
          <Money value={pos.awaiting_approval} /> awaiting the PM&rsquo;s approval.
        </p>
      ) : null}
      {pos?.paid_exceeds_contract_value ? (
        <Banner tone="warning">Paid is past the contract value.</Banner>
      ) : null}
      {pos?.paid_exceeds_work_done && !pos.paid_exceeds_contract_value ? (
        <Banner tone="info">Paid is ahead of the work done (an advance).</Banner>
      ) : null}
      {sc.payment_terms ? (
        <p className="text-sm text-slate-700">
          <span className="text-xs text-slate-500">Payment terms </span>
          {sc.payment_terms}
        </p>
      ) : null}

      {(sc.jobs ?? []).length ? (
        <div className="flex flex-col gap-1">
          <p className="text-xs font-medium text-slate-500">Jobs under this contract</p>
          {(sc.jobs ?? []).map((job) => (
            <div key={job.id} className="flex items-center justify-between gap-2 text-sm">
              <span className="text-slate-900">
                {job.reference ?? `Job ${job.id}`} <span className="text-xs text-slate-500">{job.status}</span>
              </span>
              <Money value={job.agreed_price} placeholder="—" />
            </div>
          ))}
          {(sc.jobs ?? []).some((j) => j.over_contract_reason) ? (
            <p className="text-xs text-amber-700">
              Awarded past the contract value:{' '}
              {(sc.jobs ?? [])
                .filter((j) => j.over_contract_reason)
                .map((j) => `${j.reference ?? j.id}: ${j.over_contract_reason}`)
                .join('; ')}
            </p>
          ) : null}
        </div>
      ) : null}

      <div className="flex flex-col gap-1">
        <p className="text-xs font-medium text-slate-500">Payments</p>
        {(sc.payments ?? []).length === 0 ? (
          <p className="text-sm text-slate-500">None yet.</p>
        ) : (
          (sc.payments ?? []).map((p) => <PaymentRow key={p.id} payment={p} canAttach={canRecord} />)
        )}
        {canRecord ? (
          <div className="pt-1">
            <Button variant="ghost" onClick={() => setPaying(true)}>
              Record payment
            </Button>
          </div>
        ) : null}
      </div>

      {canManage ? (
        <PhotoCapture
          targetType={CONTRACT_TARGET}
          targetId={id}
          kind="DOCUMENT"
          label="Signed contract"
          caption={CONTRACT}
          onChange={() => docs.refetch()}
        />
      ) : (
        files.map((a) => (
          <a
            key={a.id}
            href={a.download_url}
            target="_blank"
            rel="noreferrer"
            className="text-sm text-sky-700 underline"
          >
            {a.filename}
          </a>
        ))
      )}

      {canManage ? (
        <div>
          <Button variant="ghost" onClick={() => onEdit(sc)}>
            Edit contract
          </Button>
        </div>
      ) : null}

      {paying ? <PaymentSheet subcontract={sc} onClose={() => setPaying(false)} /> : null}
    </Card>
  );
}

function Figure({ label, value }: { label: string; value?: string | null }) {
  // Withheld figures (no `project.view_cost`) arrive absent: say nothing.
  if (value === undefined) return null;
  return (
    <div>
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className="font-medium text-slate-900">
        <Money value={value} />
      </dd>
    </div>
  );
}

function PaymentRow({ payment, canAttach }: { payment: SubcontractPayment; canAttach: boolean }) {
  // An invoice may be added by Finance while the payment is still open (§4.19.9).
  const open = payment.status === 'PENDING_PM' || payment.status === 'REJECTED';
  return (
    <div className="flex flex-col gap-1 rounded border border-slate-200 p-2 text-sm">
      <div className="flex items-center justify-between gap-2">
        <span className="text-slate-900">
          {dateOnly(payment.paid_on)} · {payment.reference}
        </span>
        <Money value={payment.amount} />
      </div>
      <div className="flex items-center justify-between gap-2">
        <StatusBadge status={payment.status} />
        {payment.status === 'REJECTED' && payment.rejection_reason ? (
          <span className="text-xs text-red-700">{payment.rejection_reason}</span>
        ) : null}
      </div>
      {canAttach && open ? (
        <PhotoCapture
          targetType={PAYMENT_TARGET}
          targetId={payment.id}
          kind="DOCUMENT"
          label="Invoice"
          caption={INVOICE}
        />
      ) : null}
    </div>
  );
}

/** PM's add/edit sheet. Sites must belong to the project; value changes are audited server-side (§4.19.4). */
function SubcontractSheet({
  projectId,
  subcontract,
  onClose,
}: {
  projectId: number;
  subcontract: Subcontract | null;
  onClose: () => void;
}) {
  const create = useCreateSubcontract();
  const update = useUpdateSubcontract();
  const contractors = useList<Subcontractor>('subcontractors', { is_active: true, page_size: 100 });
  const projectSites = useProjectSites(projectId);
  const [subcontractor, setSubcontractor] = useState(String(subcontract?.subcontractor ?? ''));
  const [value, setValue] = useState(subcontract?.contract_value ?? '');
  const [terms, setTerms] = useState(subcontract?.payment_terms ?? '');
  const [sites, setSites] = useState<number[]>(subcontract?.sites ?? []);
  const [error, setError] = useState<string | null>(null);
  const pending = create.isPending || update.isPending;

  const save = async () => {
    setError(null);
    if (!subcontract && !subcontractor) return setError('Which subcontractor?');
    if (!value) return setError('What is the contract value?');
    if (sites.length === 0) return setError('Choose at least one site.');
    try {
      if (subcontract) {
        await update.mutateAsync({
          id: subcontract.id,
          sites,
          contract_value: value,
          payment_terms: terms,
        });
      } else {
        await create.mutateAsync({
          project: projectId,
          subcontractor: Number(subcontractor),
          sites,
          contract_value: value,
          payment_terms: terms,
        });
      }
      onClose();
    } catch (e) {
      setError(errorMessage(e));
    }
  };

  return (
    <Sheet
      open
      title={subcontract ? `Edit ${subcontract.reference}` : 'Add subcontract'}
      onClose={onClose}
      footer={
        <Button className="w-full" disabled={pending} onClick={save}>
          {pending ? <Spinner /> : 'Save'}
        </Button>
      }
    >
      <div className="flex flex-col gap-3">
        {error ? <Banner tone="error">{error}</Banner> : null}
        <Field label="Subcontractor" htmlFor="sc-contractor">
          <Select
            id="sc-contractor"
            value={subcontractor}
            disabled={Boolean(subcontract)}
            onChange={(e) => setSubcontractor(e.target.value)}
          >
            <option value="">Choose…</option>
            {(contractors.data?.results ?? []).map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Contract value" htmlFor="sc-value" hint="Excluding VAT. Changes are kept in the audit trail.">
          <MoneyInput id="sc-value" defaultValue={value} onChange={(e) => setValue(e.target.value)} />
        </Field>
        <fieldset className="flex flex-col gap-2">
          <legend className="text-sm font-medium text-slate-700">Sites covered</legend>
          {(projectSites.data?.results ?? []).map((s) => (
            <Checkbox
              key={s.id}
              label={s.site_ref || s.site_name || `Site ${s.site}`}
              checked={sites.includes(s.site)}
              onChange={(e) =>
                setSites((cur) =>
                  e.target.checked ? [...cur, s.site] : cur.filter((x) => x !== s.site),
                )
              }
            />
          ))}
        </fieldset>
        <Field label="Payment terms" htmlFor="sc-terms">
          <Textarea id="sc-terms" value={terms} onChange={(e) => setTerms(e.target.value)} />
        </Field>
        <p className="text-xs text-slate-500">Attach the signed contract on the card once it is saved.</p>
      </div>
    </Sheet>
  );
}

/**
 * Finance records; the PM approves (§4.19.4). The server refuses a recorder who
 * is the PM (`PAYMENT_NEEDS_OTHER_APPROVER`), a future date, and a missing reference.
 */
function PaymentSheet({ subcontract, onClose }: { subcontract: Subcontract; onClose: () => void }) {
  const record = useRecordPayment();
  const today = new Date().toISOString().slice(0, 10);
  const [amount, setAmount] = useState('');
  const [paidOn, setPaidOn] = useState(today);
  const [reference, setReference] = useState('');
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    setError(null);
    if (!amount || toCents(amount) <= 0) return setError('Enter an amount above zero.');
    if (!paidOn || paidOn > today) return setError('The payment date cannot be in the future.');
    if (!reference.trim()) return setError('A payment reference is required.');
    try {
      await record.mutateAsync({
        subcontract: subcontract.id,
        amount,
        paid_on: paidOn,
        reference: reference.trim(),
      });
      onClose();
    } catch (e) {
      setError(errorMessage(e));
    }
  };

  return (
    <Sheet
      open
      title={`Record payment · ${subcontract.reference}`}
      onClose={onClose}
      footer={
        <Button className="w-full" disabled={record.isPending} onClick={save}>
          {record.isPending ? <Spinner /> : 'Send for approval'}
        </Button>
      }
    >
      <div className="flex flex-col gap-3">
        {error ? <Banner tone="error">{error}</Banner> : null}
        <Banner tone="info">The project&rsquo;s PM approves this before it counts as paid.</Banner>
        <Field label="Amount" htmlFor="pay-amount">
          <MoneyInput id="pay-amount" onChange={(e) => setAmount(e.target.value)} />
        </Field>
        <Field label="Paid on" htmlFor="pay-date">
          <Input id="pay-date" type="date" max={today} value={paidOn} onChange={(e) => setPaidOn(e.target.value)} />
        </Field>
        <Field label="Reference" htmlFor="pay-ref" hint="Bank or M-Pesa reference.">
          <Input id="pay-ref" value={reference} onChange={(e) => setReference(e.target.value)} />
        </Field>
        <p className="text-xs text-slate-500">Attach the invoice from the payment once it is recorded.</p>
      </div>
    </Sheet>
  );
}
