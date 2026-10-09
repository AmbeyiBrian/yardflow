/**
 * Registering a casual worker (Epic R, R3; design §4.17.2, §4.17.8, §4.17.10).
 *
 * A casual is paid cash and is not a user, so the register is the only record
 * of who they are: name, ID number, phone, and a photo of the ID. The ID number
 * is the identity — the server refuses a second casual with the same one
 * (CASUAL_ID_DUPLICATE) and names the existing person, which is shown as sent.
 *
 * The ID photo is taken in the form, before saving, so offline it is queued with
 * the casual (R6); online it is sent right after the casual exists, and a
 * failure leaves a retry rather than losing it.
 *
 * `CasualForm` is shared: this page uses it standalone, and the expense form
 * opens it in a sheet so a clerk does not leave a half-filled expense.
 */

import { useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import { useNavigate, useSearchParams } from 'react-router-dom';

import { PhotoCapture } from '../../components/PhotoCapture';
import { Banner, Button, Card, Field, Input, Spinner } from '../../components/ui';
import { PageHeader } from '../../components/ui/data';
import { newUuid } from '../../offline/db';
import { useOffline } from '../../offline/OfflineProvider';
import { useCreateCasual } from './api';
import { DraftPhotos } from './DraftPhotos';
import {
  SAVED_ON_PHONE,
  isNetworkError,
  sendTo,
  toUploadItems,
  uploadItems,
  type UploadItem,
} from './drafts';
import { moneyError } from './errors';
import { queueCasual, resendCorrected, type PhotoDraft } from './offline';
import { useQueuedEntry } from './queued';
import { normaliseIdNumber } from './rules';

interface Values {
  name: string;
  id_number: string;
  phone: string;
}

/** What the caller needs to name the casual: a server id, or `q:<uuid>` while it is on the phone. */
export interface SavedCasual {
  value: string;
  name: string;
  id_number: string;
  queued: boolean;
}

export function CasualForm({
  onDone,
  resendOf,
  initial,
}: {
  onDone: (casual: SavedCasual) => void;
  /** "Fix and resend": the refused entry this one replaces (R6). */
  resendOf?: string;
  initial?: Partial<Values>;
}) {
  const { online } = useOffline();
  const [saved, setSaved] = useState<SavedCasual | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<PhotoDraft[]>([]);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<{ id: number; items: UploadItem[] } | null>(null);
  const create = useCreateCasual();
  // One uuid per form, so a retry after a dropped response cannot register twice.
  const uuid = useRef(newUuid());
  const form = useForm<Values>({
    defaultValues: { name: '', id_number: '', phone: '', ...initial },
  });

  async function submit(values: Values) {
    setBanner(null);
    const body = {
      name: values.name.trim(),
      id_number: normaliseIdNumber(values.id_number),
      phone: values.phone.trim(),
    };
    const idDraft = drafts[0];
    setBusy(true);
    try {
      if (resendOf) {
        const client_uuid = await resendCorrected(resendOf, body);
        setSaved({ value: `q:${client_uuid}`, ...body, queued: true });
        return;
      }
      if (online) {
        try {
          const casual = await create.mutateAsync({ ...body, client_uuid: uuid.current });
          setSaved({
            value: String(casual.id),
            name: casual.name,
            id_number: casual.id_number,
            queued: false,
          });
          if (idDraft) {
            // The casual exists now; an ID photo that fails is retried from the next screen.
            const left = await uploadItems(
              toUploadItems([idDraft], newUuid),
              sendTo('commercials.Casual', casual.id),
            );
            if (left.length) setFailed({ id: casual.id, items: left });
          }
          return;
        } catch (error) {
          if (!isNetworkError(error)) {
            setBanner(moneyError(error, form.setError));
            return;
          }
          // No signal after all: fall through to the phone's queue, with the same uuid.
        }
      }
      const client_uuid = await queueCasual(body, idDraft, uuid.current);
      setSaved({ value: `q:${client_uuid}`, ...body, queued: true });
    } catch (error) {
      setBanner(error instanceof Error ? error.message : 'That could not be saved.');
    } finally {
      setBusy(false);
    }
  }

  async function retry() {
    if (!failed) return;
    setBusy(true);
    const left = await uploadItems(failed.items, sendTo('commercials.Casual', failed.id));
    setFailed(left.length ? { id: failed.id, items: left } : null);
    setBusy(false);
  }

  if (saved) {
    return (
      <div className="flex flex-col gap-3">
        <Banner tone="success">
          {saved.queued ? SAVED_ON_PHONE : `${saved.name} is on the register.`}
        </Banner>
        {failed ? (
          <>
            <Banner tone="error">
              The ID photo did not send. The casual is registered; try the photo again.
            </Banner>
            <Button variant="secondary" loading={busy} onClick={() => void retry()}>
              Send the ID photo again
            </Button>
            <PhotoCapture
              targetType="commercials.Casual"
              targetId={String(failed.id)}
              label="Or take it again"
              caption="ID"
            />
          </>
        ) : null}
        <Button block onClick={() => onDone(saved)}>
          Done
        </Button>
      </div>
    );
  }

  return (
    <form className="flex flex-col gap-3" onSubmit={(e) => e.preventDefault()}>
      {banner ? <Banner tone="error">{banner}</Banner> : null}
      <Field label="Name" htmlFor="casual-name" error={form.formState.errors.name?.message}>
        <Input id="casual-name" {...form.register('name', { required: 'What is their name?' })} />
      </Field>
      <Field
        label="ID number"
        htmlFor="casual-id"
        hint="Spaces and dashes do not matter."
        error={form.formState.errors.id_number?.message}
      >
        <Input
          id="casual-id"
          autoCapitalize="characters"
          {...form.register('id_number', { required: 'The ID number is needed.' })}
        />
      </Field>
      <Field label="Phone" htmlFor="casual-phone" hint="Optional.">
        <Input id="casual-phone" type="tel" {...form.register('phone')} />
      </Field>
      {/* Taken here, before saving: sent with the casual, or queued with it offline. */}
      {resendOf ? (
        <p className="text-sm text-slate-600">The ID photo already taken stays with this entry.</p>
      ) : (
        <DraftPhotos
          label="Photograph the ID"
          fixedCaption="ID"
          single
          drafts={drafts}
          onChange={setDrafts}
        />
      )}
      <Button block disabled={busy} onClick={form.handleSubmit(submit)}>
        {busy ? <Spinner /> : resendOf ? 'Fix and resend' : 'Add casual'}
      </Button>
    </form>
  );
}

export default function AddCasualPage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const resend = params.get('resend');
  const { entry, settled } = useQueuedEntry(resend);

  if (resend && !entry) {
    return (
      <div className="flex flex-col gap-4">
        <PageHeader title="Add a casual" />
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
      <PageHeader title="Add a casual" subtitle="Register someone paid by the day, once." />
      <Card>
        <CasualForm
          key={entry?.client_uuid}
          resendOf={entry?.client_uuid}
          initial={
            entry
              ? {
                  name: String(entry.payload.name ?? ''),
                  id_number: String(entry.payload.id_number ?? ''),
                  phone: String(entry.payload.phone ?? ''),
                }
              : undefined
          }
          onDone={() => navigate('/money?tab=casuals')}
        />
      </Card>
    </div>
  );
}
