/**
 * One asset (design §4.20.4, §4.20.10; R14): details, photos and documents,
 * who has it and who had it, and for a vehicle what its fuel has cost.
 *
 * Handing over is open to `asset.manage` and to the current holder (giving it
 * on is natural; taking it from someone is not). A closed asset is frozen apart
 * from its attachments, so every write action hides.
 */

import { useState, type ReactNode } from 'react';
import { useForm } from 'react-hook-form';
import { useParams } from 'react-router-dom';

import { applyFieldErrors, errorMessage, useList, useResource } from '../../api/hooks';
import { PERM } from '../../auth/permissions';
import { useSession } from '../../auth/session';
import { PhotoCapture, type Attachment } from '../../components/PhotoCapture';
import { Banner, Button, Card, Field, Input, Select, Spinner, Textarea } from '../../components/ui';
import { EmptyState, ListState, PageHeader, Sheet, Stat } from '../../components/ui/data';
import { Money } from '../../components/ui/money';
import { useCrumb } from '../../components/ui/breadcrumbs';
import {
  ASSET_DOCUMENT_KINDS,
  ASSET_TYPES,
  useAsset,
  useAssetFuel,
  useAssetHandovers,
  useCloseAsset,
  useHandOverAsset,
  useRefreshAssets,
  type Asset,
  type AssetHandover,
} from './api';
import { AssetSheet, AssetStatusChip, ExpiryChip } from './AssetsPage';
import { dateInFuture, monthRange, recentMonths, todayISO } from './rules';

function Rows({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="flex flex-col gap-2">
      {rows
        .filter(([, v]) => v !== null && v !== undefined && v !== '')
        .map(([label, value]) => (
          <div key={label} className="flex justify-between gap-3 text-sm">
            <dt className="text-slate-500">{label}</dt>
            <dd className="text-right font-medium text-slate-900">{value}</dd>
          </div>
        ))}
    </dl>
  );
}

export default function AssetDetailPage() {
  const { id } = useParams();
  const query = useAsset(id);
  const { user, has } = useSession();
  const canManage = has(PERM.ASSET_MANAGE);
  const [sheet, setSheet] = useState<'edit' | 'handover' | 'close' | null>(null);
  const [docKind, setDocKind] = useState<string>(ASSET_DOCUMENT_KINDS[1]);
  useCrumb(query.data?.name);

  if (query.isLoading) return <Spinner className="text-slate-400" />;
  const asset = query.data;
  if (!asset) return <ListState query={query}>{<EmptyState title="Not found." />}</ListState>;

  const active = asset.status === 'ACTIVE';
  const isHolder = user?.id !== undefined && asset.holder === user.id;
  const canHandOver = active && (canManage || isHolder);
  const type = ASSET_TYPES.find((t) => t.value === asset.type)?.label ?? asset.type;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={asset.name}
        subtitle={
          <span className="flex flex-wrap items-center gap-2">
            {type}
            {asset.tag ? ` · ${asset.tag}` : ''}
            <AssetStatusChip asset={asset} />
            {active ? (
              <>
                <ExpiryChip label="Insurance" date={asset.insurance_expires_on} />
                <ExpiryChip label="Inspection" date={asset.inspection_expires_on} />
              </>
            ) : null}
          </span>
        }
      />

      {(canHandOver || (canManage && active)) && (
        <div className="flex flex-wrap gap-2">
          {canHandOver ? <Button onClick={() => setSheet('handover')}>Hand over</Button> : null}
          {canManage && active ? (
            <>
              <Button variant="secondary" onClick={() => setSheet('edit')}>
                Edit
              </Button>
              <Button variant="secondary" onClick={() => setSheet('close')}>
                Sold or written off
              </Button>
            </>
          ) : null}
        </div>
      )}

      <Card className="flex flex-col gap-3">
        <Rows
          rows={[
            ['Held by', asset.holder_name || 'In the yard'],
            ['Make', asset.make],
            ['Model', asset.model],
            ['Insurance expires', asset.insurance_expires_on],
            ['Inspection expires', asset.inspection_expires_on],
            ['Bought on', asset.purchase_date],
            ['Bought from', asset.supplier_name],
            ['Cost', asset.cost ? <Money key="c" value={asset.cost} /> : null],
            ['Purchase terms', asset.purchase_terms],
            [
              'Closed',
              asset.status === 'CLOSED'
                ? `${asset.closed_reason === 'SOLD' ? 'Sold' : 'Written off'} on ${asset.closed_on}`
                : null,
            ],
            ['Reason', asset.closed_note],
          ]}
        />
      </Card>

      {asset.type === 'VEHICLE' || asset.type === 'GENERATOR' ? (
        <FuelPanel assetId={asset.id} />
      ) : null}

      <HandoverHistory assetId={asset.id} />

      {/* §4.20.7: attachments are an office act, so only `asset.manage` adds them. */}
      {canManage ? (
        <div className="flex flex-col gap-3">
          <PhotoCapture
            targetType="assets.Asset"
            targetId={asset.id}
            kind="PHOTO"
            label="Photos"
            caption="Photo"
          />
          <Field label="Document kind" htmlFor="as-doc-kind">
            <Select id="as-doc-kind" value={docKind} onChange={(e) => setDocKind(e.target.value)}>
              {ASSET_DOCUMENT_KINDS.filter((k) => k !== 'Photo').map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </Select>
          </Field>
          <PhotoCapture
            targetType="assets.Asset"
            targetId={asset.id}
            kind="DOCUMENT"
            label="Documents"
            caption={docKind}
          />
        </div>
      ) : (
        <ReadOnlyAttachments assetId={asset.id} />
      )}

      {sheet === 'edit' ? <AssetSheet open asset={asset} onClose={() => setSheet(null)} /> : null}
      {sheet === 'handover' ? <HandOverSheet asset={asset} onClose={() => setSheet(null)} /> : null}
      {sheet === 'close' ? <CloseSheet asset={asset} onClose={() => setSheet(null)} /> : null}
    </div>
  );
}

function ReadOnlyAttachments({ assetId }: { assetId: number }) {
  const query = useResource<Attachment[] | { results: Attachment[] }>('attachments', {
    target_type: 'assets.Asset',
    target_id: String(assetId),
    page_size: 100,
  });
  const items = Array.isArray(query.data) ? query.data : (query.data?.results ?? []);
  if (items.length === 0) return null;
  return (
    <Card className="flex flex-col gap-2">
      <p className="text-sm font-semibold text-slate-900">Photos and documents</p>
      <ul className="grid grid-cols-3 gap-2">
        {items.map((a) => (
          <li key={a.id} className="flex flex-col gap-1">
            {a.content_type.startsWith('image/') ? (
              <img
                src={a.download_url}
                alt={a.caption || a.filename}
                className="aspect-square w-full rounded-lg object-cover"
              />
            ) : (
              <a
                href={a.download_url}
                target="_blank"
                rel="noreferrer"
                className="flex aspect-square w-full items-center justify-center rounded-lg border border-slate-200 bg-slate-50 p-1 text-center text-xs break-all text-slate-600"
              >
                {a.filename}
              </a>
            )}
            {a.caption ? (
              <span className="text-center text-xs text-slate-500">{a.caption}</span>
            ) : null}
          </li>
        ))}
      </ul>
    </Card>
  );
}

function HandoverHistory({ assetId }: { assetId: number }) {
  const query = useAssetHandovers(assetId);
  const rows: AssetHandover[] = Array.isArray(query.data)
    ? query.data
    : (query.data?.results ?? []);
  const yard = 'the yard';

  return (
    <Card className="flex flex-col gap-2">
      <p className="text-sm font-semibold text-slate-900">Who has had it</p>
      <ListState query={query}>
        {rows.length === 0 ? (
          <p className="text-sm text-slate-500">No handovers recorded.</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {rows.map((h) => (
              <li key={h.id} className="flex flex-col text-sm">
                <span className="font-medium text-slate-900">
                  {h.from_holder_name || yard} → {h.to_holder_name || yard}
                </span>
                <span className="text-slate-500">
                  {h.handed_over_on}
                  {h.handed_over_by_name ? ` · by ${h.handed_over_by_name}` : ''}
                  {h.note ? ` · ${h.note}` : ''}
                </span>
              </li>
            ))}
          </ul>
        )}
      </ListState>
    </Card>
  );
}

/** Litres, spend and per-litre for one month (§4.20.4). */
function FuelPanel({ assetId }: { assetId: number }) {
  const months = recentMonths(12);
  const [month, setMonth] = useState(months[0]);
  const range = monthRange(month);
  const fuel = useAssetFuel(assetId, range.from, range.to);
  const f = fuel.data;

  return (
    <Card className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-3">
        <p className="text-sm font-semibold text-slate-900">Fuel</p>
        <Select
          aria-label="Month"
          value={month}
          onChange={(e) => setMonth(e.target.value)}
          className="max-w-40"
        >
          {months.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </Select>
      </div>
      {fuel.isLoading ? (
        <Spinner className="text-slate-400" />
      ) : fuel.isError ? (
        <Banner tone="error">{errorMessage(fuel.error)}</Banner>
      ) : f ? (
        <>
          <div className="grid grid-cols-2 gap-3">
            <Stat label="Spend" value={<Money value={f.spend} />} />
            <Stat label="Litres" value={f.litres ?? '—'} />
            <Stat label="Fills" value={f.fill_count} />
            <Stat
              label="Per litre"
              value={f.spend_per_litre ? <Money value={f.spend_per_litre} /> : '—'}
            />
          </div>
          {f.pending_spend && Number(f.pending_spend) > 0 ? (
            <p className="text-sm text-slate-600">
              <Money value={f.pending_spend} /> awaiting approval, not counted above.
            </p>
          ) : null}
        </>
      ) : null}
    </Card>
  );
}

function HandOverSheet({ asset, onClose }: { asset: Asset; onClose: () => void }) {
  const form = useForm({ defaultValues: { to_holder: '', note: '', handed_over_on: todayISO() } });
  const [banner, setBanner] = useState<string | null>(null);
  const handOver = useHandOverAsset();
  const refresh = useRefreshAssets();
  const people = useList<{ id: number; full_name: string; is_active?: boolean }>('users', {
    page_size: 200,
  });

  const submit = form.handleSubmit(async (v) => {
    setBanner(null);
    if (dateInFuture(v.handed_over_on)) {
      form.setError('handed_over_on', { message: 'A handover cannot be dated in the future.' });
      return;
    }
    try {
      await handOver.mutateAsync({
        id: asset.id,
        to_holder: v.to_holder ? Number(v.to_holder) : null,
        note: v.note,
        handed_over_on: v.handed_over_on,
      });
      await refresh();
      onClose();
    } catch (error) {
      setBanner(applyFieldErrors(error, form.setError));
    }
  });

  return (
    <Sheet
      open
      title={`Hand over ${asset.name}`}
      onClose={onClose}
      footer={
        <Button block loading={handOver.isPending} onClick={submit}>
          Hand over
        </Button>
      }
    >
      <form className="flex flex-col gap-3" onSubmit={submit}>
        {banner ? <Banner tone="error">{banner}</Banner> : null}
        <Field label="Give it to" htmlFor="ho-to" error={form.formState.errors.to_holder?.message}>
          <Select id="ho-to" {...form.register('to_holder')}>
            <option value="">The yard</option>
            {(people.data?.results ?? [])
              .filter((p) => p.is_active !== false && p.id !== asset.holder)
              .map((p) => (
                <option key={p.id} value={p.id}>
                  {p.full_name}
                </option>
              ))}
          </Select>
        </Field>
        <Field label="Date" htmlFor="ho-date" error={form.formState.errors.handed_over_on?.message}>
          <Input id="ho-date" type="date" max={todayISO()} {...form.register('handed_over_on')} />
        </Field>
        <Field label="Note" htmlFor="ho-note">
          <Textarea id="ho-note" {...form.register('note')} />
        </Field>
      </form>
    </Sheet>
  );
}

function CloseSheet({ asset, onClose }: { asset: Asset; onClose: () => void }) {
  const form = useForm({
    defaultValues: { closed_reason: 'SOLD', closed_on: todayISO(), closed_note: '' },
  });
  const [banner, setBanner] = useState<string | null>(null);
  const close = useCloseAsset();
  const refresh = useRefreshAssets();

  const submit = form.handleSubmit(async (v) => {
    setBanner(null);
    if (dateInFuture(v.closed_on)) {
      form.setError('closed_on', { message: 'The date cannot be in the future.' });
      return;
    }
    if (asset.purchase_date && v.closed_on < asset.purchase_date) {
      form.setError('closed_on', { message: 'That is before it was bought.' });
      return;
    }
    try {
      await close.mutateAsync({
        id: asset.id,
        closed_on: v.closed_on,
        closed_reason: v.closed_reason as 'SOLD' | 'WRITTEN_OFF',
        closed_note: v.closed_note,
      });
      await refresh();
      onClose();
    } catch (error) {
      setBanner(applyFieldErrors(error, form.setError));
    }
  });

  return (
    <Sheet
      open
      title={`Close ${asset.name}`}
      onClose={onClose}
      footer={
        <Button block loading={close.isPending} onClick={submit}>
          Close asset
        </Button>
      }
    >
      <form className="flex flex-col gap-3" onSubmit={submit}>
        <Banner tone="info">
          A closed asset cannot be handed over or take fuel, and cannot be reopened.
        </Banner>
        {banner ? <Banner tone="error">{banner}</Banner> : null}
        <Field label="What happened" htmlFor="cl-reason">
          <Select id="cl-reason" {...form.register('closed_reason')}>
            <option value="SOLD">Sold</option>
            <option value="WRITTEN_OFF">Written off</option>
          </Select>
        </Field>
        <Field label="Date" htmlFor="cl-date" error={form.formState.errors.closed_on?.message}>
          <Input id="cl-date" type="date" max={todayISO()} {...form.register('closed_on')} />
        </Field>
        <Field label="Note" htmlFor="cl-note" hint="Who bought it, or why it was written off.">
          <Textarea id="cl-note" {...form.register('closed_note')} />
        </Field>
      </form>
    </Sheet>
  );
}
