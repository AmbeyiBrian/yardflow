/**
 * Record a site purchase (Epic R, R7, R9; design §4.19.3, §4.19.13; T18.16).
 *
 * Goods bought for a site, from a supplier. Site first and the project follows
 * (`SiteProject`), as for an expense. Each line is a catalogue item or free text,
 * a quantity and a unit price; the total is the sum, kept in cents (R7). The
 * destination decides what happens on approval: USED_AT_SITE is cost at once,
 * INTO_YARD makes a draft delivery to be received into stock, which is why it
 * needs `receive_into` and a catalogue item on every line (§4.19.3).
 *
 * R9: where the project has a budget the form asks the server "would this go
 * over?" before sending (`budget-check`) and, if so, asks for the reason. The
 * reason is also asked when the server itself answers
 * `OVER_BUDGET_REASON_REQUIRED`. It warns, it never blocks: a reason lets it
 * through (§4.19.5).
 *
 * Receipt photos are held as drafts and sent after the purchase exists. With no
 * signal the purchase and its photos go to the phone's queue together as a
 * `SITE_PURCHASE` (§4.19.11, R6), as `queueExpense` does on the expense form;
 * replay never refuses for over-budget, it only flags. A refused one comes back
 * here through "Fix and resend". A supplier added on this phone is named by
 * `supplier_client_uuid` (§4.20.8), and then the purchase is queued even online,
 * because the server cannot know that supplier yet.
 */

import { useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';

import { ApiError } from '../../api/client';
import { ItemPicker } from '../../components/ItemPicker';
import { Banner, Button, Card, Field, Input, Select, Spinner, Textarea } from '../../components/ui';
import { PageHeader } from '../../components/ui/data';
import { ControlledReferenceSelect } from '../../components/ui/ReferenceSelect';
import { newUuid } from '../../offline/db';
import { useOffline } from '../../offline/OfflineProvider';
import { DraftPhotos } from './DraftPhotos';
import { SAVED_ON_PHONE, isNetworkError, sendTo, toUploadItems, uploadItems } from './drafts';
import { moneyError } from './errors';
import { queuePurchase, resendCorrected, type PhotoDraft } from './offline';
import {
  isBlankLine,
  lineTotal,
  totalText,
  validatePurchase,
  type PurchaseDestination,
  type PurchaseLineDraft,
} from './purchaseRules';
import { useBudgetCheck, useCreateSitePurchase } from './purchasesApi';
import { needsQueue, purchaseBody } from './purchasesOffline';
import { useQueuedEntry } from './queued';
import { useBundleHeadroom, useReceivableLocations, useSupplierOptions } from './reference';
import { overHeadroom } from './bundle';
import { fromCents } from './rules';
import { SiteProjectFields, useSiteProject } from './SiteProject';
import { OfflineSupplierSheet } from './SupplierPages';
import { supplierValue } from './suppliersOffline';

interface Line extends PurchaseLineDraft {
  key: number;
}

const blank = (key: number): Line => ({
  key,
  item_type: '',
  description: '',
  quantity: '1',
  unit_price: '',
});

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

const text = (value: unknown): string =>
  value === undefined || value === null ? '' : String(value);

export default function RecordPurchasePage() {
  const [params] = useSearchParams();
  const resend = params.get('resend');
  const { entry, settled } = useQueuedEntry(resend);

  // "Fix and resend" starts from the queued payload, so wait for it (R6).
  if (resend && !entry) {
    return (
      <div className="flex flex-col gap-4">
        <PageHeader title="Record a purchase" />
        {settled ? (
          <Banner tone="info">That entry is no longer on this phone.</Banner>
        ) : (
          <Spinner className="text-slate-400" />
        )}
      </div>
    );
  }
  return (
    <PurchaseForm
      key={entry?.client_uuid ?? 'new'}
      resendOf={entry?.client_uuid}
      payload={entry?.payload}
    />
  );
}

function PurchaseForm({
  resendOf,
  payload,
}: {
  resendOf?: string;
  payload?: Record<string, unknown>;
}) {
  const navigate = useNavigate();
  const { online } = useOffline();
  const place = useSiteProject(
    '',
    payload ? { site: text(payload.site), project: text(payload.project) } : undefined,
  );

  const [supplier, setSupplier] = useState(payload ? supplierValue(payload) : '');
  const [date, setDate] = useState(text(payload?.purchase_date) || today());
  const [destination, setDestination] = useState<PurchaseDestination>(
    payload?.destination === 'INTO_YARD' ? 'INTO_YARD' : 'USED_AT_SITE',
  );
  const [receiveInto, setReceiveInto] = useState(text(payload?.receive_into));
  const [lines, setLines] = useState<Line[]>(() => {
    const queued = Array.isArray(payload?.lines)
      ? (payload.lines as Record<string, unknown>[])
      : [];
    return queued.length
      ? queued.map((l, i) => ({
          key: i + 1,
          item_type: text(l.item_type),
          description: text(l.description),
          quantity: text(l.quantity) || '1',
          unit_price: text(l.unit_price),
        }))
      : [blank(1)];
  });
  const [reason, setReason] = useState(text(payload?.over_budget_reason));
  // R9: shown once the server says it would pass the budget.
  const [needReason, setNeedReason] = useState(Boolean(text(payload?.over_budget_reason)));
  const [addingSupplier, setAddingSupplier] = useState(false);
  const [drafts, setDrafts] = useState<PhotoDraft[]>([]);
  const [banner, setBanner] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  // One uuid per form, so a retry after a dropped response cannot record twice.
  const uuid = useRef(newUuid());
  const nextKey = useRef(lines.length + 1);

  // Each reads the network online and the offline bundle otherwise (R6, §4.19.11).
  const suppliers = useSupplierOptions();
  const receivable = useReceivableLocations().locations;
  const create = useCreateSitePurchase();
  const budgetCheck = useBudgetCheck();
  const headroom = useBundleHeadroom();

  // §4.19.3: supplier active and not REJECTED (PENDING is fine to buy from).
  const supplierOptions = suppliers.options;

  const patchLine = (key: number, patch: Partial<Line>) =>
    setLines((rows) => rows.map((row) => (row.key === key ? { ...row, ...patch } : row)));

  async function submit() {
    setBanner(null);
    const found = validatePurchase({ destination, receiveInto, lines });
    if (!place.project && !place.blocked) found.project = 'Which project is this for?';
    if (!place.site) found.site = 'Which site is it for?';
    if (!supplier) found.supplier = 'Who was it bought from?';
    if (needReason && !reason.trim()) found.reason = 'Say why it goes over budget.';
    setErrors(found);
    if (place.blocked || Object.keys(found).length) return;

    const used = lines.filter((l) => !isBlankLine(l));
    const input = {
      project: Number(place.project),
      site: Number(place.site),
      purchase_date: date,
      destination,
      receive_into: destination === 'INTO_YARD' ? Number(receiveInto) : null,
      lines: used.map((l) => ({
        item_type: l.item_type ? Number(l.item_type) : null,
        description: l.description.trim(),
        quantity: l.quantity,
        unit_price: l.unit_price,
      })),
      over_budget_reason: reason.trim() || undefined,
      // A resent entry keeps the photos it already has.
      photos_expected: resendOf ? (Number(payload?.photos_expected) || 0) : drafts.length,
    };
    const body = purchaseBody(input, supplier);

    setBusy(true);
    try {
      // R9: ask before sending, so the reason is not discovered after a round trip.
      if (online && !resendOf && !reason.trim()) {
        try {
          const check = await budgetCheck.mutateAsync({
            project: body.project,
            amount: totalText(used),
          });
          if (check.over) {
            setNeedReason(true);
            return;
          }
        } catch {
          // No answer is no reason to stop; the server asks again if it must.
        }
      }

      // Offline: the headroom the last bundle stored. A warning only; the
      // server decides on replay and flags (R9).
      if (!online && !resendOf && !reason.trim()) {
        if (overHeadroom(headroom, Number(body.project), totalText(used))) {
          setNeedReason(true);
          return;
        }
      }

      if (resendOf) {
        await resendCorrected(resendOf, body as Record<string, unknown>);
        return leaveQueued();
      }
      // A supplier still on this phone cannot be named to the server yet, so
      // the purchase waits behind it in the queue (§4.20.8).
      if (online && !needsQueue(body)) {
        try {
          const saved = await create.mutateAsync({
            ...body,
            supplier: Number(body.supplier),
            client_uuid: uuid.current,
          });
          const failed = await uploadItems(
            toUploadItems(drafts, newUuid),
            sendTo('commercials.SitePurchase', saved.id),
          );
          navigate(`/money/purchases/${saved.id}`, {
            state: failed.length ? { failedPhotos: failed } : undefined,
          });
          return;
        } catch (error) {
          if (error instanceof ApiError && error.code === 'OVER_BUDGET_REASON_REQUIRED') {
            setNeedReason(true);
            return;
          }
          if (!isNetworkError(error)) {
            setBanner(moneyError(error, () => undefined) ?? 'That could not be saved.');
            return;
          }
        }
      }
      await queuePurchase(body, drafts, uuid.current);
      leaveQueued();
    } catch (error) {
      setBanner(error instanceof Error ? error.message : 'That could not be saved.');
    } finally {
      setBusy(false);
    }
  }

  function leaveQueued() {
    navigate('/money?tab=purchases', { state: { notice: SAVED_ON_PHONE } });
  }

  const total = totalText(lines.filter((l) => !isBlankLine(l)));

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Record a purchase"
        subtitle="Goods bought from a supplier for a site, used there or taken into the yard."
      />

      <Card>
        <form className="flex flex-col gap-3" onSubmit={(e) => e.preventDefault()}>
          {banner ? <Banner tone="error">{banner}</Banner> : null}

          <SiteProjectFields
            state={place}
            idPrefix="sp"
            siteError={errors.site}
            projectError={errors.project}
          />

          <Field label="Supplier" htmlFor="sp-supplier" error={errors.supplier}>
            <Select id="sp-supplier" value={supplier} onChange={(e) => setSupplier(e.target.value)}>
              <option value="">Choose…</option>
              {supplierOptions.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </Select>
          </Field>
          {!online ? (
            <Button variant="ghost" onClick={() => setAddingSupplier(true)}>
              Not on the list? Add a supplier
            </Button>
          ) : null}

          <Field label="Bought on" htmlFor="sp-date">
            <Input id="sp-date" type="date" value={date} onChange={(e) => setDate(e.target.value)} />
          </Field>

          <Field
            label="Where does it go?"
            hint={
              destination === 'INTO_YARD'
                ? 'Approval will create a delivery for the storekeeper to receive.'
                : 'Used at the site: counted as project cost once approved.'
            }
          >
            <div className="grid grid-cols-2 gap-2" role="group" aria-label="Destination">
              {(
                [
                  ['USED_AT_SITE', 'Used at the site'],
                  ['INTO_YARD', 'Into the yard'],
                ] as const
              ).map(([value, label]) => (
                <Button
                  key={value}
                  variant={destination === value ? 'primary' : 'secondary'}
                  aria-pressed={destination === value}
                  onClick={() => setDestination(value)}
                >
                  {label}
                </Button>
              ))}
            </div>
          </Field>

          {destination === 'INTO_YARD' ? (
            <Field label="Receive into" htmlFor="sp-into" error={errors.receive_into}>
              <ControlledReferenceSelect
                resource="locations"
                id="sp-into"
                value={receiveInto}
                onChange={(e) => setReceiveInto(e.target.value)}
              >
                <option value="">Choose…</option>
                {receivable.map((l) => (
                  <option key={l.id} value={l.id}>
                    {l.label}
                  </option>
                ))}
              </ControlledReferenceSelect>
            </Field>
          ) : null}

          <fieldset className="flex flex-col gap-3 rounded-lg border border-slate-200 p-3">
            <legend className="px-1 text-sm font-medium text-slate-700">What was bought</legend>
            {destination === 'INTO_YARD' ? (
              <p className="text-sm text-slate-500">
                Goods going into the yard must be catalogue items.
              </p>
            ) : null}
            {lines.map((line, index) => {
              const t = lineTotal(line.quantity, line.unit_price);
              return (
                <div key={line.key} className="flex flex-col gap-2 rounded-lg bg-slate-50 p-2">
                  <ItemPicker
                    id={`sp-item-${line.key}`}
                    value={line.item_type}
                    onChange={(item) =>
                      patchLine(line.key, {
                        item_type: item ? String(item.id) : '',
                        description: item ? '' : line.description,
                      })
                    }
                    placeholder="Catalogue item (type to search)"
                  />
                  {destination === 'USED_AT_SITE' && !line.item_type ? (
                    <Input
                      aria-label={`Description ${index + 1}`}
                      placeholder="Or describe it (free text)"
                      value={line.description}
                      onChange={(e) => patchLine(line.key, { description: e.target.value })}
                    />
                  ) : null}
                  <div className="grid grid-cols-2 gap-2">
                    <Input
                      aria-label={`Quantity ${index + 1}`}
                      inputMode="decimal"
                      placeholder="Quantity"
                      value={line.quantity}
                      onChange={(e) => patchLine(line.key, { quantity: e.target.value })}
                    />
                    <Input
                      aria-label={`Unit price ${index + 1}`}
                      inputMode="decimal"
                      placeholder="Unit price"
                      value={line.unit_price}
                      onChange={(e) => patchLine(line.key, { unit_price: e.target.value })}
                    />
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-sm text-slate-600">
                      {Number.isNaN(t) ? '' : `Line total ${fromCents(t)}`}
                    </span>
                    {lines.length > 1 ? (
                      <Button
                        variant="ghost"
                        onClick={() => setLines((rows) => rows.filter((r) => r.key !== line.key))}
                      >
                        Remove
                      </Button>
                    ) : null}
                  </div>
                </div>
              );
            })}
            {errors.lines ? (
              <p role="alert" className="text-sm text-red-600">
                {errors.lines}
              </p>
            ) : null}
            <Button
              variant="secondary"
              onClick={() => setLines((rows) => [...rows, blank(nextKey.current++)])}
            >
              Another line
            </Button>
            <p className="text-base font-semibold text-slate-900">Total {total}</p>
          </fieldset>

          {needReason ? (
            <>
              <Banner tone="warning">
                This would take the project over its budget. It can still go for approval; say why.
              </Banner>
              <Field label="Why over budget" htmlFor="sp-reason" error={errors.reason}>
                <Textarea
                  id="sp-reason"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
              </Field>
            </>
          ) : null}

          <DraftPhotos
            hint="The receipt or invoice. Taken now, sent with the purchase."
            drafts={drafts}
            onChange={setDrafts}
          />

          <Button block disabled={busy || place.blocked || place.loading} onClick={() => void submit()}>
            {busy ? (
              <Spinner />
            ) : resendOf ? (
              'Fix and resend'
            ) : needReason ? (
              'Record it with this reason'
            ) : (
              'Record it'
            )}
          </Button>
        </form>
      </Card>

      <OfflineSupplierSheet
        open={addingSupplier}
        onClose={() => setAddingSupplier(false)}
        onAdded={(option) => setSupplier(option.value)}
      />
    </div>
  );
}
