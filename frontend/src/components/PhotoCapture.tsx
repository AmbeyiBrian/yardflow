/**
 * Photo capture (design §7.3, §4.13; D6, G3, H2, N-7).
 *
 * T5.10's criterion is that a technician "closes out a job from a site on a
 * phone, **including two site photos**". That is this component, and it is
 * shared rather than local to the closeout screen because gate-in (D6) and
 * release (G3) attach photos in exactly the same way.
 *
 * Two decisions worth stating:
 *
 * **`capture="environment"` on a file input, not `getUserMedia`.** The scanner
 * needs a live video frame because it is decoding; a photo does not. Handing the
 * job to the platform camera app gets autofocus, flash, HDR and the user's own
 * familiar shutter for free, and it still degrades to the gallery when the
 * camera is unavailable — the same "completable with the camera denied" rule the
 * scanner follows.
 *
 * **Uploads are immediate, one request per photo.** A technician on a site with
 * one bar of signal should not lose four photos because the fifth failed, and a
 * closeout that batched them would have to hold megabytes in memory while the
 * form is filled in. Each upload either lands or is retryable on its own.
 *
 * The target must exist before a photo can hang off it, so a screen creating a
 * document saves it first and then shows this. `disabledReason` is what it says
 * in the meantime.
 */

import { useRef, useState } from 'react';

import { ApiError, api } from '../api/client';
import { errorMessage } from '../api/hooks';
import { Banner, Button, Card, Spinner } from './ui';

export interface Attachment {
  id: number;
  filename: string;
  content_type: string;
  size: number;
  kind: string;
  uploaded_by: number | null;
  uploaded_by_name: string;
  download_url: string;
  created_at: string;
}

async function upload(
  targetType: string,
  targetId: string | number,
  file: File,
  kind: string,
): Promise<Attachment> {
  const form = new FormData();
  form.set('target_type', targetType);
  form.set('target_id', String(targetId));
  form.set('kind', kind);
  form.set('file', file);
  // The client passes FormData through untouched, so the browser sets the
  // multipart boundary itself.
  return api.post<Attachment>('/attachments', form);
}

export function PhotoCapture({
  targetType,
  targetId,
  kind = 'PHOTO',
  label = 'Photos',
  hint,
  minimum = 0,
  disabledReason,
  onChange,
}: {
  /** A model label, e.g. `jobs.JobCloseout`. The API enforces the same list. */
  targetType: string;
  targetId: string | number | undefined;
  kind?: 'PHOTO' | 'DOCUMENT' | 'SIGNATURE';
  label?: string;
  hint?: string;
  /** Shown as "n of m taken" while short. Advisory — the server decides. */
  minimum?: number;
  /** Why the camera is unavailable, when the target does not exist yet. */
  disabledReason?: string;
  onChange?: (attachments: Attachment[]) => void;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [items, setItems] = useState<Attachment[]>([]);
  const [busy, setBusy] = useState(0);
  const [error, setError] = useState<string | null>(null);

  function publish(next: Attachment[]) {
    setItems(next);
    onChange?.(next);
  }

  async function onPicked(files: FileList | null) {
    if (!files || !targetId) return;
    setError(null);

    // Sequential rather than parallel: a phone uploading four photos at once
    // over a weak connection is slower than one at a time, and a failure part
    // way through leaves a clearer picture of what landed.
    for (const file of Array.from(files)) {
      setBusy((count) => count + 1);
      try {
        const attachment = await upload(targetType, targetId, file, kind);
        publish([...items, attachment].slice());
        setItems((current) => {
          const next = current.includes(attachment) ? current : [...current, attachment];
          onChange?.(next);
          return next;
        });
      } catch (failure) {
        setError(
          failure instanceof ApiError && failure.fieldErrors.file
            ? failure.fieldErrors.file.join(' ')
            : errorMessage(failure),
        );
      } finally {
        setBusy((count) => count - 1);
      }
    }

    // Let the same file be picked twice — a retaken photo often has the same
    // name, and the input would otherwise fire no change event.
    if (inputRef.current) inputRef.current.value = '';
  }

  async function remove(attachment: Attachment) {
    setError(null);
    try {
      await api.delete(`/attachments/${attachment.id}`);
      setItems((current) => {
        const next = current.filter((item) => item.id !== attachment.id);
        onChange?.(next);
        return next;
      });
    } catch (failure) {
      setError(errorMessage(failure));
    }
  }

  const short = minimum > 0 && items.length < minimum;

  return (
    <Card className="flex flex-col gap-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-semibold text-slate-900">{label}</p>
          {hint ? <p className="text-sm text-slate-600">{hint}</p> : null}
          {minimum > 0 ? (
            <p className={short ? 'text-sm text-amber-700' : 'text-sm text-emerald-700'}>
              {items.length} of {minimum} taken
            </p>
          ) : null}
        </div>
        {busy > 0 ? <Spinner className="text-slate-400" /> : null}
      </div>

      {error ? <Banner tone="error">{error}</Banner> : null}
      {disabledReason ? <Banner tone="info">{disabledReason}</Banner> : null}

      <input
        ref={inputRef}
        type="file"
        // `capture` asks for the rear camera and is ignored on a desktop, where
        // this is a file picker. Both are correct.
        accept="image/*,application/pdf"
        capture={kind === 'PHOTO' ? 'environment' : undefined}
        multiple
        className="hidden"
        onChange={(event) => void onPicked(event.target.files)}
      />

      <Button
        variant="secondary"
        block
        disabled={!targetId}
        onClick={() => inputRef.current?.click()}
      >
        {kind === 'PHOTO' ? 'Take a photo' : 'Attach a file'}
      </Button>

      {items.length > 0 ? (
        <ul className="grid grid-cols-3 gap-2">
          {items.map((attachment) => (
            <li key={attachment.id} className="flex flex-col gap-1">
              {attachment.content_type.startsWith('image/') ? (
                // N-7: even the thumbnail is a signed, expiring URL. There is no
                // public path to an attachment in either environment.
                <img
                  src={attachment.download_url}
                  alt={attachment.filename}
                  className="aspect-square w-full rounded-lg object-cover"
                />
              ) : (
                <a
                  href={attachment.download_url}
                  target="_blank"
                  rel="noreferrer"
                  className="flex aspect-square w-full items-center justify-center rounded-lg border border-slate-200 bg-slate-50 p-1 text-center text-xs break-all text-slate-600"
                >
                  {attachment.filename}
                </a>
              )}
              <Button
                variant="ghost"
                className="min-h-0 px-1 py-0.5 text-xs"
                onClick={() => void remove(attachment)}
              >
                Remove
              </Button>
            </li>
          ))}
        </ul>
      ) : null}
    </Card>
  );
}
