/**
 * The asset register (design §4.20.4, §4.20.10; R14).
 *
 * Every member reads it (a fuel entry picks a vehicle from it); only
 * `asset.manage` adds or edits. An asset is a thing the company owns that is
 * not stock: a vehicle, a generator, a tool. It is never deleted, only closed.
 */

import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { useNavigate } from 'react-router-dom';

import { applyFieldErrors, errorMessage, useList } from '../../api/hooks';
import { PERM } from '../../auth/permissions';
import { Can } from '../../auth/session';
import { Banner, Button, Field, Input, Select, Spinner, Textarea } from '../../components/ui';
import { DataList, EmptyState, PageHeader, Sheet } from '../../components/ui/data';
import { cn } from '../../components/ui/cn';
import { SearchField } from '../../components/ui/SearchField';
import type { Supplier } from '../settings/suppliersApi';
import {
  ASSET_TYPES,
  useAssets,
  useCreateAsset,
  useUpdateAsset,
  type Asset,
  type AssetInput,
  type AssetType,
} from './api';
import { expiryStatus, type ExpiryStatus } from './rules';

const TYPE_LABEL = Object.fromEntries(ASSET_TYPES.map((t) => [t.value, t.label]));

const TONES: Record<ExpiryStatus['tone'], string> = {
  ok: 'bg-slate-100 text-slate-700',
  soon: 'bg-amber-100 text-amber-900',
  lapsed: 'bg-red-100 text-red-900',
  none: '',
};

/** Amber within 30 days, red once lapsed (§4.20.10). Renders nothing when far off. */
export function ExpiryChip({ label, date }: { label: string; date: string | null }) {
  const status = expiryStatus(date);
  if (status.tone === 'none' || status.tone === 'ok') return null;
  return (
    <span
      className={cn(
        'inline-block rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap',
        TONES[status.tone],
      )}
    >
      {label}: {status.label.toLowerCase()}
    </span>
  );
}

export function AssetStatusChip({ asset }: { asset: Pick<Asset, 'status' | 'closed_reason'> }) {
  if (asset.status === 'ACTIVE') return null;
  return (
    <span className="inline-block rounded-full bg-slate-200 px-2 py-0.5 text-xs font-medium text-slate-700">
      {asset.closed_reason === 'SOLD' ? 'Sold' : 'Written off'}
    </span>
  );
}

export default function AssetsPage() {
  const navigate = useNavigate();
  const [search, setSearch] = useState('');
  const [type, setType] = useState('');
  const [status, setStatus] = useState('ACTIVE');
  const [adding, setAdding] = useState(false);

  const assets = useAssets({
    search: search || undefined,
    type: type || undefined,
    status: status || undefined,
  });

  return (
    <div className="flex flex-1 flex-col gap-4">
      <PageHeader
        title="Assets"
        subtitle="Vehicles, generators and equipment the company owns, and who has each one."
      />

      <div className="flex flex-wrap items-center gap-2">
        <SearchField
          value={search}
          onChange={setSearch}
          label="Search assets"
          placeholder="Name, tag or registration"
        />
        <Select
          aria-label="Filter by type"
          value={type}
          onChange={(e) => setType(e.target.value)}
          className="max-w-40"
        >
          <option value="">All types</option>
          {ASSET_TYPES.map((t) => (
            <option key={t.value} value={t.value}>
              {t.label}
            </option>
          ))}
        </Select>
        <Select
          aria-label="Filter by status"
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          className="max-w-40"
        >
          <option value="ACTIVE">In service</option>
          <option value="CLOSED">Sold or written off</option>
          <option value="">All</option>
        </Select>
        <Can permission={PERM.ASSET_MANAGE}>
          <Button onClick={() => setAdding(true)} className="ml-auto">
            New asset
          </Button>
        </Can>
      </div>

      {assets.isLoading ? (
        <Spinner className="text-slate-400" />
      ) : assets.isError ? (
        <Banner tone="error">{errorMessage(assets.error)}</Banner>
      ) : (
        <DataList
          rows={assets.data?.results ?? []}
          rowKey={(row) => row.id}
          onRowClick={(row) => navigate(`/assets/${row.id}`)}
          empty={<EmptyState title="No assets here." hint="Nothing matches these filters." />}
          columns={[
            {
              header: 'Asset',
              cell: (row) => (
                <span className="flex flex-col items-end gap-1 md:items-start">
                  <span>{row.name}</span>
                  <AssetStatusChip asset={row} />
                </span>
              ),
            },
            { header: 'Type', cell: (row) => TYPE_LABEL[row.type] ?? row.type },
            { header: 'Tag', cell: (row) => row.tag || '—' },
            { header: 'Held by', cell: (row) => row.holder_name || 'In the yard' },
            {
              header: 'Expiry',
              cell: (row) =>
                row.status === 'ACTIVE' ? (
                  <span className="flex flex-wrap justify-end gap-1 md:justify-start">
                    <ExpiryChip label="Insurance" date={row.insurance_expires_on} />
                    <ExpiryChip label="Inspection" date={row.inspection_expires_on} />
                  </span>
                ) : null,
            },
          ]}
        />
      )}

      {adding ? (
        <AssetSheet
          open
          onClose={() => setAdding(false)}
          onSaved={(asset) => navigate(`/assets/${asset.id}`)}
        />
      ) : null}
    </div>
  );
}

interface FormValues {
  type: AssetType;
  name: string;
  tag: string;
  purchase_date: string;
  supplier: string;
  cost: string;
  purchase_terms: string;
  make: string;
  model: string;
  insurance_expires_on: string;
  inspection_expires_on: string;
  holder: string;
}

/** Add (with the first holder) or edit an asset (§4.20.4). */
export function AssetSheet({
  open,
  onClose,
  asset,
  onSaved,
}: {
  open: boolean;
  onClose: () => void;
  asset?: Asset;
  onSaved?: (asset: Asset) => void;
}) {
  const form = useForm<FormValues>({
    defaultValues: {
      type: asset?.type ?? 'VEHICLE',
      name: asset?.name ?? '',
      tag: asset?.tag ?? '',
      purchase_date: asset?.purchase_date ?? '',
      supplier: asset?.supplier ? String(asset.supplier) : '',
      cost: asset?.cost ?? '',
      purchase_terms: asset?.purchase_terms ?? '',
      make: asset?.make ?? '',
      model: asset?.model ?? '',
      insurance_expires_on: asset?.insurance_expires_on ?? '',
      inspection_expires_on: asset?.inspection_expires_on ?? '',
      holder: '',
    },
  });
  const [banner, setBanner] = useState<string | null>(null);
  const create = useCreateAsset();
  const update = useUpdateAsset();
  const people = useList<{ id: number; full_name: string }>('users', { page_size: 200 });
  const suppliers = useList<Supplier>('suppliers', { page_size: 200, is_active: true });

  const type = form.watch('type');
  const isVehicle = type === 'VEHICLE';

  const submit = form.handleSubmit(async (v) => {
    setBanner(null);
    // R14: a vehicle needs its registration.
    if (isVehicle && !v.tag.trim()) {
      form.setError('tag', { message: 'A vehicle needs its registration.' });
      return;
    }
    const body: AssetInput = {
      type: v.type,
      name: v.name,
      tag: v.tag,
      purchase_date: v.purchase_date || null,
      supplier: v.supplier ? Number(v.supplier) : null,
      cost: v.cost || null,
      purchase_terms: v.purchase_terms,
      // Make, model and expiries are for vehicles only (§4.20.2).
      make: isVehicle ? v.make : '',
      model: isVehicle ? v.model : '',
      insurance_expires_on: isVehicle ? v.insurance_expires_on || null : null,
      inspection_expires_on: isVehicle ? v.inspection_expires_on || null : null,
    };
    try {
      const saved = asset
        ? await update.mutateAsync({ id: asset.id, ...body })
        : await create.mutateAsync({ ...body, holder: v.holder ? Number(v.holder) : null });
      onClose();
      onSaved?.(saved);
    } catch (error) {
      setBanner(applyFieldErrors(error, form.setError));
    }
  });

  const err = form.formState.errors;

  return (
    <Sheet
      open={open}
      title={asset ? 'Edit asset' : 'New asset'}
      onClose={onClose}
      footer={
        <Button block loading={create.isPending || update.isPending} onClick={submit}>
          {asset ? 'Save changes' : 'Add asset'}
        </Button>
      }
    >
      <form className="flex flex-col gap-3" onSubmit={submit}>
        {banner ? <Banner tone="error">{banner}</Banner> : null}

        <Field label="Type" htmlFor="as-type">
          <Select id="as-type" {...form.register('type')}>
            {ASSET_TYPES.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </Select>
        </Field>

        <Field label="Name" htmlFor="as-name" error={err.name?.message}>
          <Input id="as-name" {...form.register('name', { required: 'What is it called?' })} />
        </Field>

        <Field
          label={isVehicle ? 'Registration' : 'Tag'}
          htmlFor="as-tag"
          error={err.tag?.message}
          hint={isVehicle ? undefined : 'Optional. A number or label on the item.'}
        >
          <Input id="as-tag" autoCapitalize="characters" {...form.register('tag')} />
        </Field>

        {isVehicle ? (
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Make" htmlFor="as-make" error={err.make?.message}>
              <Input id="as-make" {...form.register('make')} />
            </Field>
            <Field label="Model" htmlFor="as-model" error={err.model?.message}>
              <Input id="as-model" {...form.register('model')} />
            </Field>
            <Field
              label="Insurance expires"
              htmlFor="as-ins"
              error={err.insurance_expires_on?.message}
            >
              <Input id="as-ins" type="date" {...form.register('insurance_expires_on')} />
            </Field>
            <Field
              label="Inspection expires"
              htmlFor="as-insp"
              error={err.inspection_expires_on?.message}
            >
              <Input id="as-insp" type="date" {...form.register('inspection_expires_on')} />
            </Field>
          </div>
        ) : null}

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Bought on" htmlFor="as-bought" error={err.purchase_date?.message}>
            <Input id="as-bought" type="date" {...form.register('purchase_date')} />
          </Field>
          <Field label="Cost" htmlFor="as-cost" error={err.cost?.message}>
            <Input id="as-cost" inputMode="decimal" {...form.register('cost')} />
          </Field>
        </div>

        <Field label="Bought from" htmlFor="as-supplier" error={err.supplier?.message}>
          <Select id="as-supplier" {...form.register('supplier')}>
            <option value="">Not recorded</option>
            {(suppliers.data?.results ?? []).map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </Select>
        </Field>

        <Field label="Purchase terms" htmlFor="as-terms" error={err.purchase_terms?.message}>
          <Textarea id="as-terms" {...form.register('purchase_terms')} />
        </Field>

        {!asset ? (
          <Field
            label="Held by"
            htmlFor="as-holder"
            error={err.holder?.message}
            hint="Who has it now. This writes its first handover."
          >
            <Select id="as-holder" {...form.register('holder')}>
              <option value="">In the yard</option>
              {(people.data?.results ?? []).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.full_name}
                </option>
              ))}
            </Select>
          </Field>
        ) : null}
      </form>
    </Sheet>
  );
}
