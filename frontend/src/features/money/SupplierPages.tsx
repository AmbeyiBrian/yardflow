/**
 * Adding a supplier with no signal (design §4.20.8; R15, R6).
 *
 * Online, "Add new supplier" is the full register sheet (`quickCreate`). Offline
 * that sheet cannot save, so this small form queues a `SUPPLIER` operation with
 * just what a clerk at the gate knows: name, contact, phone, KRA PIN. The
 * server's `add_supplier` runs the duplicate checks on replay; a refusal comes
 * back as "Fix and resend" on the Purchases tab.
 */

import { useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';

import { Banner, Button, Card, Field, Input, Spinner } from '../../components/ui';
import { PageHeader, Sheet } from '../../components/ui/data';
import { newUuid } from '../../offline/db';
import { SAVED_ON_PHONE } from './drafts';
import { queueSupplier, resendCorrected } from './offline';
import { useQueuedEntry } from './queued';
import { supplierBody, type SupplierDraft, type SupplierOption } from './suppliersOffline';

export function OfflineSupplierForm({
  onDone,
  resendOf,
  initial,
}: {
  onDone: (option: SupplierOption) => void;
  /** "Fix and resend": the refused supplier this one replaces (R6). */
  resendOf?: string;
  initial?: Partial<SupplierDraft>;
}) {
  const [name, setName] = useState(initial?.name ?? '');
  const [contact, setContact] = useState(initial?.contact_name ?? '');
  const [phone, setPhone] = useState(initial?.phone ?? '');
  const [pin, setPin] = useState(initial?.kra_pin ?? '');
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<SupplierOption | null>(null);
  const [busy, setBusy] = useState(false);
  // One uuid per form, so a double tap cannot add the supplier twice.
  const uuid = useRef(newUuid());

  async function submit() {
    setError(null);
    if (!name.trim()) {
      setError('What is the supplier called?');
      return;
    }
    const draft = { name, contact_name: contact, phone, kra_pin: pin };
    setBusy(true);
    try {
      const client_uuid = resendOf
        ? await resendCorrected(resendOf, supplierBody(draft))
        : await queueSupplier(draft, uuid.current);
      setSaved({
        value: `q:${client_uuid}`,
        label: `${name.trim()} (waiting to send)`,
        name: name.trim(),
        queued: true,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : 'That could not be saved.');
    } finally {
      setBusy(false);
    }
  }

  if (saved) {
    return (
      <div className="flex flex-col gap-3">
        <Banner tone="success">{SAVED_ON_PHONE}</Banner>
        <Button block onClick={() => onDone(saved)}>
          Done
        </Button>
      </div>
    );
  }

  return (
    <form className="flex flex-col gap-3" onSubmit={(e) => e.preventDefault()}>
      {error ? <Banner tone="error">{error}</Banner> : null}
      <Field label="Name" htmlFor="sup-name">
        <Input id="sup-name" value={name} onChange={(e) => setName(e.target.value)} />
      </Field>
      <Field label="Contact person" htmlFor="sup-contact" hint="Optional.">
        <Input id="sup-contact" value={contact} onChange={(e) => setContact(e.target.value)} />
      </Field>
      <Field label="Phone" htmlFor="sup-phone" hint="Optional.">
        <Input id="sup-phone" type="tel" value={phone} onChange={(e) => setPhone(e.target.value)} />
      </Field>
      <Field label="KRA PIN" htmlFor="sup-pin" hint="Optional. Finance completes the rest.">
        <Input
          id="sup-pin"
          autoCapitalize="characters"
          value={pin}
          onChange={(e) => setPin(e.target.value)}
        />
      </Field>
      <Button block disabled={busy} onClick={() => void submit()}>
        {busy ? <Spinner /> : resendOf ? 'Fix and resend' : 'Add supplier'}
      </Button>
    </form>
  );
}

/** "Add a supplier" in a sheet, for the pickers; shown by the caller only when offline. */
export function OfflineSupplierSheet({
  open,
  onClose,
  onAdded,
}: {
  open: boolean;
  onClose: () => void;
  onAdded: (option: SupplierOption) => void;
}) {
  return (
    <Sheet open={open} title="Add a supplier" onClose={onClose}>
      <OfflineSupplierForm
        onDone={(option) => {
          onAdded(option);
          onClose();
        }}
      />
    </Sheet>
  );
}

/** The "Fix and resend" target for a refused supplier (§4.20.8). */
export default function AddSupplierPage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const resend = params.get('resend');
  const { entry, settled } = useQueuedEntry(resend);

  if (resend && !entry) {
    return (
      <div className="flex flex-col gap-4">
        <PageHeader title="Add a supplier" />
        {settled ? (
          <Banner tone="info">That entry is no longer on this phone.</Banner>
        ) : (
          <Spinner className="text-slate-400" />
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <PageHeader title="Add a supplier" subtitle="Saved on this phone and sent when there is signal." />
      <Card>
        <OfflineSupplierForm
          key={entry?.client_uuid}
          resendOf={entry?.client_uuid}
          initial={
            entry
              ? {
                  name: String(entry.payload.name ?? ''),
                  contact_name: String(entry.payload.contact_name ?? ''),
                  phone: String(entry.payload.phone ?? ''),
                  kra_pin: String(entry.payload.kra_pin ?? ''),
                }
              : undefined
          }
          onDone={() => navigate('/money?tab=purchases')}
        />
      </Card>
    </div>
  );
}
