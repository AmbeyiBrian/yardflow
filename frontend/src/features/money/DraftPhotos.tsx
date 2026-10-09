/**
 * Photos taken in the form, before the entry is saved (design §4.17.8; R1, R3).
 *
 * Nothing is uploaded here: each photo is held as a draft and sent after the
 * entry exists (online) or queued with it (offline). The same screen therefore
 * works with and without signal, and the count of drafts is what
 * `photos_expected` says. `capture="environment"` for the same reason as
 * `PhotoCapture`: the platform camera app, falling back to the gallery.
 */

import { useEffect, useMemo, useRef, useState } from 'react';

import { Button, Card, Field, Select } from '../../components/ui';
import type { PhotoDraft } from './offline';

export const CAPTIONS = ['Receipt', 'Fuel pump', 'Work done', 'Other'] as const;

export function DraftPhotos({
  drafts,
  onChange,
  label = 'Photos',
  hint,
  /** A fixed caption (a casual's is always "ID"); otherwise the person picks one. */
  fixedCaption,
  single = false,
}: {
  drafts: PhotoDraft[];
  onChange: (drafts: PhotoDraft[]) => void;
  label?: string;
  hint?: string;
  fixedCaption?: string;
  /** One photo only: a new one replaces the old. */
  single?: boolean;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [caption, setCaption] = useState<string>(CAPTIONS[0]);

  // Object URLs hold the blob in memory until revoked.
  const urls = useMemo(() => drafts.map((d) => URL.createObjectURL(d.blob)), [drafts]);
  useEffect(() => () => urls.forEach((url) => URL.revokeObjectURL(url)), [urls]);

  function picked(files: FileList | null) {
    if (!files) return;
    const added = Array.from(files).map((file) => ({
      caption: fixedCaption ?? caption,
      blob: file as Blob,
      filename: file.name,
    }));
    onChange(single ? added.slice(-1) : [...drafts, ...added]);
    // The same file can be picked twice (a retaken photo often has its name).
    if (inputRef.current) inputRef.current.value = '';
  }

  return (
    <Card className="flex flex-col gap-3">
      <div>
        <p className="text-sm font-semibold text-slate-900">{label}</p>
        {hint ? <p className="text-sm text-slate-600">{hint}</p> : null}
      </div>

      {fixedCaption ? null : (
        <Field label="What the next photo shows" htmlFor="draft-caption">
          <Select
            id="draft-caption"
            value={caption}
            onChange={(event) => setCaption(event.target.value)}
          >
            {CAPTIONS.map((c) => (
              <option key={c}>{c}</option>
            ))}
          </Select>
        </Field>
      )}

      <input
        ref={inputRef}
        type="file"
        accept="image/*"
        capture="environment"
        multiple={!single}
        className="hidden"
        onChange={(event) => picked(event.target.files)}
      />
      <Button variant="secondary" block onClick={() => inputRef.current?.click()}>
        Take a photo
      </Button>

      {drafts.length > 0 ? (
        <ul className="grid grid-cols-3 gap-2">
          {drafts.map((draft, index) => (
            <li key={`${draft.filename}-${index}`} className="flex flex-col gap-1">
              <img
                src={urls[index]}
                alt={draft.caption}
                className="aspect-square w-full rounded-lg object-cover"
              />
              <span className="text-center text-xs text-slate-500">{draft.caption}</span>
              <Button
                variant="ghost"
                className="min-h-0 px-1 py-0.5 text-xs"
                onClick={() => onChange(drafts.filter((_, i) => i !== index))}
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
