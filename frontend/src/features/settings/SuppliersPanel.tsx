/**
 * Settings → Network → Suppliers (design §4.20.10; R15).
 *
 * A supplier sells goods; a subcontractor does work. Any member may add one
 * (only the name is required, so a gate-in clerk can add "Kenya Cable Ltd" in
 * ten seconds); Finance approves it later, which is what makes it payable
 * (§4.20.3). This file holds the panel and `SupplierSheet`, which the gate-in
 * form also opens through `quickCreate.tsx`.
 */

import { useState } from 'react';
import { useForm } from 'react-hook-form';

import { applyFieldErrors, errorMessage } from '../../api/hooks';
import { PERM } from '../../auth/permissions';
import { useSession } from '../../auth/session';
import { PhotoCapture } from '../../components/PhotoCapture';
import { Banner, Button, Field, Input, Select, Spinner, Textarea } from '../../components/ui';
import { DataList, EmptyState, Sheet } from '../../components/ui/data';
import { cn } from '../../components/ui/cn';
import { SearchField } from '../../components/ui/SearchField';
import {
  useCreateSupplier,
  useLinkSupplierHistory,
  useResubmitSupplier,
  useSetSupplierActive,
  useSuppliers,
  useUpdateSupplier,
  type Supplier,
} from './suppliersApi';
import {
  SUPPLIER_DOCUMENT_KINDS,
  duplicateOf,
  supplierChip,
  type ExistingSupplier,
  type SupplierChip,
} from './supplierRules';

const CHIP_TONES: Record<SupplierChip, string> = {
  Pending: 'bg-amber-100 text-amber-900',
  Approved: 'bg-emerald-100 text-emerald-900',
  Rejected: 'bg-red-100 text-red-900',
  Inactive: 'bg-slate-200 text-slate-700',
};

export function SupplierChipBadge({ supplier }: { supplier: Pick<Supplier, 'status' | 'is_active'> }) {
  const chip = supplierChip(supplier);
  return (
    <span
      className={cn(
        'inline-block rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap',
        CHIP_TONES[chip],
      )}
    >
      {chip}
    </span>
  );
}

const STATUS_FILTERS = [
  { value: '', label: 'All' },
  { value: 'PENDING', label: 'Pending' },
  { value: 'APPROVED', label: 'Approved' },
  { value: 'REJECTED', label: 'Rejected' },
  { value: 'INACTIVE', label: 'Inactive' },
];

export function SuppliersPanel() {
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState('');
  const [editing, setEditing] = useState<Supplier | 'new' | null>(null);

  // "Inactive" is not an approval status: it is `is_active=false` (§4.20.2).
  const suppliers = useSuppliers({
    search: search || undefined,
    status: status && status !== 'INACTIVE' ? status : undefined,
    is_active: status === 'INACTIVE' ? false : undefined,
  });

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <SearchField
          value={search}
          onChange={setSearch}
          label="Search suppliers"
          placeholder="Name, KRA PIN or contact"
        />
        <Select
          aria-label="Filter by status"
          value={status}
          onChange={(event) => setStatus(event.target.value)}
          className="max-w-40"
        >
          {STATUS_FILTERS.map((f) => (
            <option key={f.value} value={f.value}>
              {f.label}
            </option>
          ))}
        </Select>
        <Button onClick={() => setEditing('new')}>New supplier</Button>
      </div>

      <p className="text-sm text-slate-600">
        Businesses that sell you goods. Anyone can add one; Finance approves it
        before it can be paid.
      </p>

      {suppliers.isLoading ? (
        <Spinner className="text-slate-400" />
      ) : suppliers.isError ? (
        <Banner tone="error">{errorMessage(suppliers.error)}</Banner>
      ) : (
        <DataList
          rows={suppliers.data?.results ?? []}
          rowKey={(row) => row.id}
          onRowClick={(row) => setEditing(row)}
          empty={
            <EmptyState
              title="No suppliers yet."
              hint="Add one here, or from the gate-in screen when a delivery arrives."
            />
          }
          columns={[
            { header: 'Name', cell: (row) => row.name },
            { header: 'Status', cell: (row) => <SupplierChipBadge supplier={row} /> },
            { header: 'Contact', cell: (row) => row.contact_name || '—' },
            { header: 'Phone', cell: (row) => row.phone || '—', wideOnly: true },
            { header: 'KRA PIN', cell: (row) => row.kra_pin || '—', wideOnly: true },
          ]}
        />
      )}

      {editing ? (
        <SupplierSheet
          key={editing === 'new' ? 'new' : editing.id}
          open
          supplier={editing === 'new' ? undefined : editing}
          onClose={() => setEditing(null)}
          onUseExisting={(existing) => {
            // The register is searched for the one it already holds.
            setSearch(existing.name);
            setStatus('');
            setEditing(null);
          }}
        />
      ) : null}
    </div>
  );
}

interface FormValues {
  name: string;
  kra_pin: string;
  contact_name: string;
  phone: string;
  email: string;
  address: string;
  bank_name: string;
  account_number: string;
  mpesa_type: '' | 'PAYBILL' | 'TILL';
  mpesa_number: string;
  mpesa_account: string;
}

function defaults(s?: Supplier): FormValues {
  return {
    name: s?.name ?? '',
    kra_pin: s?.kra_pin ?? '',
    contact_name: s?.contact_name ?? '',
    phone: s?.phone ?? '',
    email: s?.email ?? '',
    address: s?.address ?? '',
    bank_name: s?.bank_name ?? '',
    account_number: s?.account_number ?? '',
    mpesa_type: s?.mpesa_type ?? '',
    mpesa_number: s?.mpesa_number ?? '',
    mpesa_account: s?.mpesa_account ?? '',
  };
}

/**
 * Add or edit a supplier. With `onCreated` (the quick-create path from a
 * gate-in) it saves and hands the record back at once; otherwise a newly added
 * supplier stays open so its documents can be attached, since an attachment
 * needs the record to exist first.
 */
export function SupplierSheet({
  open,
  onClose,
  onCreated,
  supplier,
  onUseExisting,
}: {
  open: boolean;
  onClose: () => void;
  onCreated?: (record: { id: number }) => void;
  supplier?: Supplier;
  /** Panel only: what "Use <existing> instead" does. Quick-create selects it. */
  onUseExisting?: (existing: ExistingSupplier) => void;
}) {
  const { has } = useSession();
  const canApprove = has(PERM.FINANCE_APPROVE);
  const form = useForm<FormValues>({ defaultValues: defaults(supplier) });
  const [saved, setSaved] = useState<Supplier | undefined>(supplier);
  const [banner, setBanner] = useState<string | null>(null);
  const [duplicate, setDuplicate] = useState<ReturnType<typeof duplicateOf>>(null);
  const [kind, setKind] = useState<string>(SUPPLIER_DOCUMENT_KINDS[0]);
  const [linked, setLinked] = useState<number | null>(null);

  const create = useCreateSupplier();
  const update = useUpdateSupplier();
  const resubmit = useResubmitSupplier();
  const setActive = useSetSupplierActive();
  const link = useLinkSupplierHistory();

  const submit = form.handleSubmit(async (values) => {
    setBanner(null);
    setDuplicate(null);
    try {
      if (saved) {
        const next = await update.mutateAsync({ id: saved.id, ...values });
        setSaved(next);
        setBanner('Saved.');
      } else {
        const created = await create.mutateAsync(values);
        if (onCreated) {
          onClose();
          onCreated(created);
        } else {
          setSaved(created);
          setBanner('Added. Finance will approve it. You can attach its documents now.');
        }
      }
    } catch (error) {
      // R15: a repeated PIN or name is refused naming the supplier already
      // there, and the answer is nearly always "use that one".
      const dup = duplicateOf(error);
      if (dup) {
        setDuplicate(dup);
        setBanner(null);
        return;
      }
      setBanner(applyFieldErrors(error, form.setError));
    }
  });

  function chooseExisting(existing: ExistingSupplier) {
    if (onCreated) {
      onClose();
      onCreated({ id: existing.id });
    } else {
      onUseExisting?.(existing);
    }
  }

  async function act<T>(run: () => Promise<T>, after?: (result: T) => void) {
    setBanner(null);
    try {
      after?.(await run());
    } catch (error) {
      setBanner(errorMessage(error));
    }
  }

  const busy = create.isPending || update.isPending;
  const err = form.formState.errors;

  return (
    <Sheet
      open={open}
      title={saved ? saved.name : 'New supplier'}
      onClose={onClose}
      footer={
        <>
          <Button block loading={busy} onClick={submit}>
            {saved ? 'Save changes' : 'Add supplier'}
          </Button>
        </>
      }
    >
      <form className="flex flex-col gap-3" onSubmit={submit}>
        {saved ? (
          <div className="flex items-center gap-2">
            <SupplierChipBadge supplier={saved} />
            {saved.registered_by_name ? (
              <span className="text-sm text-slate-500">Added by {saved.registered_by_name}</span>
            ) : null}
          </div>
        ) : null}
        {saved?.status === 'REJECTED' && saved.decision_reason ? (
          <Banner tone="error">Rejected: {saved.decision_reason}</Banner>
        ) : null}
        {banner ? (
          <Banner tone={banner === 'Saved.' || banner.startsWith('Added') ? 'info' : 'error'}>
            {banner}
          </Banner>
        ) : null}
        {duplicate ? (
          <Banner tone="error">
            <span className="flex flex-col items-start gap-2">
              <span>
                {duplicate.kind === 'pin'
                  ? `That KRA PIN is already on the register, for ${duplicate.existing.name}.`
                  : `A supplier called ${duplicate.existing.name} is already on the register.`}
              </span>
              <Button variant="secondary" onClick={() => chooseExisting(duplicate.existing)}>
                Use {duplicate.existing.name} instead
              </Button>
            </span>
          </Banner>
        ) : null}

        <Field label="Name" htmlFor="sup-name" error={err.name?.message}>
          <Input id="sup-name" {...form.register('name', { required: 'What are they called?' })} />
        </Field>

        <Field
          label="KRA PIN"
          htmlFor="sup-pin"
          error={err.kra_pin?.message}
          hint="Not needed to add a supplier, but Finance needs it to approve."
        >
          <Input id="sup-pin" autoCapitalize="characters" {...form.register('kra_pin')} />
        </Field>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Contact person" htmlFor="sup-contact" error={err.contact_name?.message}>
            <Input id="sup-contact" {...form.register('contact_name')} />
          </Field>
          <Field label="Phone" htmlFor="sup-phone" error={err.phone?.message}>
            <Input id="sup-phone" inputMode="tel" {...form.register('phone')} />
          </Field>
        </div>

        <Field label="Email" htmlFor="sup-email" error={err.email?.message}>
          <Input id="sup-email" type="email" {...form.register('email')} />
        </Field>

        <Field label="Address" htmlFor="sup-address" error={err.address?.message}>
          <Textarea id="sup-address" {...form.register('address')} />
        </Field>

        <p className="pt-1 text-sm font-semibold text-slate-900">Payment details</p>
        <p className="-mt-2 text-sm text-slate-600">
          Finance needs at least one way to pay. Changing these later is recorded and Finance is
          told.
        </p>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Bank" htmlFor="sup-bank" error={err.bank_name?.message}>
            <Input id="sup-bank" {...form.register('bank_name')} />
          </Field>
          <Field label="Account number" htmlFor="sup-acct" error={err.account_number?.message}>
            <Input id="sup-acct" {...form.register('account_number')} />
          </Field>
        </div>

        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="M-Pesa" htmlFor="sup-mp-type" error={err.mpesa_type?.message}>
            <Select id="sup-mp-type" {...form.register('mpesa_type')}>
              <option value="">None</option>
              <option value="PAYBILL">Paybill</option>
              <option value="TILL">Till</option>
            </Select>
          </Field>
          <Field label="Number" htmlFor="sup-mp-num" error={err.mpesa_number?.message}>
            <Input id="sup-mp-num" inputMode="numeric" {...form.register('mpesa_number')} />
          </Field>
          <Field label="Account" htmlFor="sup-mp-acct" error={err.mpesa_account?.message}>
            <Input id="sup-mp-acct" {...form.register('mpesa_account')} />
          </Field>
        </div>
      </form>

      {saved ? (
        <div className="mt-4 flex flex-col gap-3">
          <Field label="Document kind" htmlFor="sup-doc-kind">
            <Select id="sup-doc-kind" value={kind} onChange={(e) => setKind(e.target.value)}>
              {SUPPLIER_DOCUMENT_KINDS.map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </Select>
          </Field>
          <PhotoCapture
            targetType="network.Supplier"
            targetId={saved.id}
            kind="DOCUMENT"
            label="Documents"
            caption={kind}
          />

          {saved.status === 'REJECTED' ? (
            <Button
              variant="secondary"
              loading={resubmit.isPending}
              onClick={() => void act(() => resubmit.mutateAsync({ id: saved.id }), setSaved)}
            >
              Resubmit for approval
            </Button>
          ) : null}

          {canApprove ? (
            <>
              <Button
                variant="secondary"
                loading={setActive.isPending}
                onClick={() =>
                  void act(
                    () => setActive.mutateAsync({ id: saved.id, active: !saved.is_active }),
                    (next) => setSaved({ ...saved, ...next }),
                  )
                }
              >
                {saved.is_active ? 'Deactivate' : 'Reactivate'}
              </Button>
              <Button
                variant="secondary"
                loading={link.isPending}
                onClick={() =>
                  void act(() => link.mutateAsync({ id: saved.id }), (r) => setLinked(r.linked))
                }
              >
                Link past deliveries
              </Button>
              {linked !== null ? (
                <p className="text-sm text-slate-600">
                  {linked === 0
                    ? 'No unlinked deliveries match this name.'
                    : `${linked} past ${linked === 1 ? 'delivery' : 'deliveries'} linked.`}
                </p>
              ) : null}
            </>
          ) : null}
        </div>
      ) : null}
    </Sheet>
  );
}
