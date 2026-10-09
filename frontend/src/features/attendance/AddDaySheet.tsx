/**
 * "Add a day" for Director-role holders (design §4.18.6a; R13).
 *
 * For someone who was on site but whose phone placed them elsewhere. The
 * server refuses anyone but the Director and the Director's own day
 * (WORK_DAY_ADD_NOT_ALLOWED); the sheet states the same up front. The session
 * is shown everywhere as "Added by the Director".
 */

import { useState } from 'react';

import { useList } from '../../api/hooks';
import { useSession } from '../../auth/session';
import { Banner, Button, Field, Input, Select, Textarea } from '../../components/ui';
import { Sheet } from '../../components/ui/data';
import type { Project } from '../projects/types';
import { useAddDay } from './api';
import { buildAddDay, type AddDayDraft } from './approvals';
import { usePlaces } from './places';
import { clockError } from './rules';

const EMPTY: AddDayDraft = {
  person: '',
  date: '',
  place: '',
  project: '',
  start: '08:00',
  end: '17:00',
  reason: '',
};

export function AddDaySheet({ onClose }: { onClose: () => void }) {
  const { user } = useSession();
  const add = useAddDay();
  const { places } = usePlaces();
  const people = useList<{ id: number; full_name: string }>('users', { page_size: 200 });
  const projects = useList<Project>('projects', { status: 'OPEN', page_size: 200 });
  const [draft, setDraft] = useState<AddDayDraft>(EMPTY);
  const [error, setError] = useState('');
  const set = (patch: Partial<AddDayDraft>) => setDraft((d) => ({ ...d, ...patch }));

  function submit() {
    const built = buildAddDay(draft, user?.id);
    if ('error' in built) {
      setError(built.error);
      return;
    }
    setError('');
    add.mutate(built.body, { onSuccess: onClose, onError: (e) => setError(clockError(e)) });
  }

  return (
    <Sheet
      open
      title="Add a day"
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" className="flex-1" onClick={onClose}>
            Cancel
          </Button>
          <Button className="flex-1" loading={add.isPending} onClick={submit}>
            Add day
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <Banner tone="info">
          Shown everywhere as added by the Director, with no position. Someone else approves it.
        </Banner>
        <Field label="Who" htmlFor="add-person">
          <Select id="add-person" value={draft.person} onChange={(e) => set({ person: e.target.value })}>
            <option value="">Choose</option>
            {(people.data?.results ?? [])
              .filter((p) => p.id !== user?.id)
              .map((p) => (
                <option key={p.id} value={p.id}>
                  {p.full_name}
                </option>
              ))}
          </Select>
        </Field>
        <Field label="Date" htmlFor="add-date">
          <Input id="add-date" type="date" value={draft.date} onChange={(e) => set({ date: e.target.value })} />
        </Field>
        <Field label="Where" htmlFor="add-place">
          <Select id="add-place" value={draft.place} onChange={(e) => set({ place: e.target.value })}>
            <option value="">Choose</option>
            {places.map((p) => (
              <option key={`${p.kind}:${p.id}`} value={`${p.kind}:${p.id}`}>
                {p.name} ({p.label})
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Project" htmlFor="add-project" hint="Optional. Without one the day is approved by a Director-role holder.">
          <Select id="add-project" value={draft.project} onChange={(e) => set({ project: e.target.value })}>
            <option value="">None</option>
            {(projects.data?.results ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.reference}
              </option>
            ))}
          </Select>
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="From" htmlFor="add-start">
            <Input id="add-start" type="time" value={draft.start} onChange={(e) => set({ start: e.target.value })} />
          </Field>
          <Field label="To" htmlFor="add-end">
            <Input id="add-end" type="time" value={draft.end} onChange={(e) => set({ end: e.target.value })} />
          </Field>
        </div>
        <Field label="Why" htmlFor="add-reason" hint="Kept on the record with your name.">
          <Textarea id="add-reason" value={draft.reason} onChange={(e) => set({ reason: e.target.value })} />
        </Field>
        {error ? <Banner tone="error">{error}</Banner> : null}
      </div>
    </Sheet>
  );
}
