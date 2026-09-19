/**
 * T5.10 — job detail and closeout (design §7.4, §4.9; H2, H5, I1).
 *
 * The criterion: "a technician **closes out a job from a site on a phone**,
 * including two site photos."
 *
 * Four decisions that criterion forces:
 *
 * **Lines are picked from what the technician is actually carrying.** Not an
 * item search. On a phone, at a site, the question is never "which of the four
 * hundred catalogue items is this" — it is "what did I do with the things in my
 * van". Reading custody (§4.10: a balance at their PERSON node) means the picker
 * is three rows long and cannot name something they do not have.
 *
 * **Save, then photograph, then submit.** A photo has to hang off a record that
 * exists, so the closeout is saved as a draft first. That is not a workaround —
 * it is also what makes a closeout survive a phone dying half way through, which
 * on a site is a normal Tuesday.
 *
 * **The action per line is the whole of §4.9.** Installed and consumed post
 * immediately; returning and recovered create an expectation the yard confirms.
 * So the four options are spelled out in the technician's own words rather than
 * hidden behind a toggle, because picking the wrong one is what makes stock
 * appear in a yard while it is still in a van.
 *
 * **Two photos are asked for, not enforced here.** H2 wants site photos; the
 * count is advisory in the client because the server is what decides, and a
 * technician in a basement with no signal and a cracked camera must still be
 * able to report what they did (the same rule the scanner follows).
 */

import { useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useCrumb } from '../../components/ui/breadcrumbs';

import { errorMessage, useAction, useDetail, useList, useResource } from '../../api/hooks';
import { useSession } from '../../auth/session';
import { PhotoCapture } from '../../components/PhotoCapture';
import {
  ActionBar,
  Banner,
  Button,
  Card,
  Field,
  Input,
  OwnershipBadge,
  Select,
  Spinner,
  Textarea,
} from '../../components/ui';
import { EmptyState, PageHeader, Stat, StatusBadge } from '../../components/ui/data';
import { type Carried, carriedLabel, mergeCarried } from './carried';
import type {
  CloseoutAction,
  CloseoutLineInput,
  CustodyBalance,
  Job,
  JobCloseout,
  LabourInput,
  Reconciliation,
  Reel,
  SerialUnit,
} from './types';

/** §4.9, in the words a technician would use. */
const ACTIONS: { value: CloseoutAction; label: string; note: string }[] = [
  { value: 'INSTALLED', label: 'Installed at the site', note: 'Posts now — it is in the ground.' },
  { value: 'CONSUMED', label: 'Used up on site', note: 'Posts now — cable, ties, consumables.' },
  {
    value: 'RETURNING',
    label: 'Bringing it back',
    note: 'Stays on your record until the yard receives it.',
  },
  {
    value: 'RECOVERED',
    label: 'Recovered from the site',
    note: 'Stays on your record until the yard receives it.',
  },
];

/** Today, as the date input wants it. */
function today(): string {
  return new Date().toISOString().slice(0, 10);
}

export default function JobCloseoutPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { user } = useSession();

  const job = useDetail<Job>('jobs', id);
  useCrumb(job.data?.reference);
  const reconciliation = useResource<Reconciliation>(
    `jobs/${id}/reconciliation`,
    undefined,
    { enabled: Boolean(id) },
  );

  // What this technician holds. Three reads because the ledger tracks the three
  // kinds differently (§3.1) — merged into one picker below, because to the
  // person holding them they are just "my stuff".
  const bulk = useList<CustodyBalance>('stock/custody', { holder: user?.id });
  const people = useList<{ id: number; full_name: string }>('users', { page_size: 200 });
  const serials = useList<SerialUnit>('serials', { holder: 'me', page_size: 100 });
  const drums = useList<Reel>('drums', { holder: 'me', page_size: 100 });

  const [lines, setLines] = useState<CloseoutLineInput[]>([]);
  // O15: days worked, per person, on the form that already has to be filled in.
  // Optional — a delivery dropped at a site has no days to report, and
  // requiring them would produce invented ones.
  const [labour, setLabour] = useState<LabourInput[]>([]);
  const [notes, setNotes] = useState('');
  const [draft, setDraft] = useState<JobCloseout | null>(null);
  const [photos, setPhotos] = useState(0);
  const [banner, setBanner] = useState<string | null>(null);

  const save = useAction<Record<string, unknown>, JobCloseout>({
    resource: 'job-closeouts',
    invalidates: ['job-closeouts'],
  });
  const submit = useAction<{ id: number }, JobCloseout>({
    resource: 'job-closeouts',
    path: (body) => `${body.id}/submit`,
    // A submitted closeout moves material, so every screen reading stock,
    // custody or reconciliation is stale the moment it succeeds.
    invalidates: [
      'job-closeouts',
      'jobs',
      'stock',
      'stock/custody',
      'serials',
      'drums',
      'custody-expectations',
      'exceptions',
      'notifications',
    ],
  });

  const carried = useMemo<Carried[]>(
    () =>
      mergeCarried({
        balances: bulk.data?.results ?? [],
        serials: serials.data?.results ?? [],
        drums: drums.data?.results ?? [],
      }),
    [bulk.data, serials.data, drums.data],
  );

  /** A serialized unit is one line; everything else carries a quantity. */
  function add(item: Carried) {
    setLines((current) => [
      ...current,
      {
        action: 'INSTALLED',
        item_type: item.itemType,
        item_name: carriedLabel(item),
        serial_unit: item.serialUnit ?? null,
        reel: item.reel ?? null,
        // A serialized unit goes whole (Q5), so its quantity is not a question.
        quantity: item.serialUnit ? '1' : '',
        uom: item.uom,
        condition: item.condition,
        notes: '',
      },
    ]);
  }

  function update(index: number, patch: Partial<CloseoutLineInput>) {
    setLines((current) =>
      current.map((line, position) => (position === index ? { ...line, ...patch } : line)),
    );
  }

  function remove(index: number) {
    setLines((current) => current.filter((_, position) => position !== index));
  }

  async function saveDraft() {
    setBanner(null);
    try {
      const created = await save.mutateAsync({
        job: Number(id),
        notes,
        lines: lines.map((line) => ({
          action: line.action,
          item_type: line.item_type,
          serial_unit: line.serial_unit || null,
          reel: line.reel || null,
          quantity: line.quantity,
          uom: line.uom,
          condition: line.condition || '',
          notes: line.notes || '',
        })),
        labour: labour
          .filter((entry) => entry.person && entry.days)
          .map((entry) => ({
            person: Number(entry.person),
            work_date: entry.work_date,
            days: entry.days,
          })),
      });
      setDraft(created);
    } catch (error) {
      setBanner(errorMessage(error));
    }
  }

  async function submitCloseout() {
    if (!draft) return;
    setBanner(null);
    try {
      await submit.mutateAsync({ id: draft.id });
      navigate(`/jobs/${id}`, { replace: true });
      setDraft(null);
      setLines([]);
      setNotes('');
      void reconciliation.refetch();
      void job.refetch();
    } catch (error) {
      setBanner(errorMessage(error));
    }
  }

  if (job.isLoading) return <Spinner className="text-slate-400" />;
  if (job.isError) return <Banner tone="error">{errorMessage(job.error)}</Banner>;

  const data = job.data!;
  const totals = reconciliation.data?.totals;

  return (
    <div className="flex flex-col gap-4 pb-28">
      <PageHeader
        title={data.site_name || data.reference}
        subtitle={
          <span className="flex flex-wrap items-center gap-2">
            <StatusBadge status={data.status} />
            {data.site_ref ? <span>{data.site_ref}</span> : null}
            <span>{data.reference}</span>
            {data.client_name ? <span>· {data.client_name}</span> : null}
          </span>
        }
        actions={
          <Link
            to="/jobs"
            className="flex min-h-[44px] items-center px-2 text-sm text-slate-700"
          >
            Back
          </Link>
        }
      />

      {banner ? <Banner tone="error">{banner}</Banner> : null}

      {/* H4, and the reason a technician sees it too: the figure they are about
          to change is the one an owner will ask them about. */}
      {totals ? (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Stat label="Issued" value={totals.issued} />
          <Stat label="Installed" value={totals.installed} tone="good" />
          <Stat label="Consumed" value={totals.consumed} />
          <Stat
            label="Unaccounted"
            value={totals.unaccounted}
            tone={Number(totals.unaccounted) === 0 ? 'good' : 'warn'}
            hint={
              Number(totals.unaccounted) === 0
                ? 'Everything is explained.'
                : 'H5 blocks closing while this is not zero.'
            }
          />
        </div>
      ) : null}

      {data.is_closed ? (
        <Banner tone="info">
          This job is closed
          {data.closed_with_variance ? ' with a variance' : ''}
          {data.close_reason ? `: ${data.close_reason}` : '.'}
        </Banner>
      ) : null}

      {!draft ? (
        <>
          <Card className="flex flex-col gap-3">
            <div>
              <h2 className="text-sm font-semibold text-slate-900">What did you do with it?</h2>
              <p className="text-sm text-slate-600">
                Tap something you are carrying, then say what happened to it.
              </p>
            </div>

            {bulk.isLoading || serials.isLoading || drums.isLoading ? (
              <Spinner className="text-slate-400" />
            ) : carried.length === 0 ? (
              <EmptyState
                title="You are not holding anything."
                hint="Material appears here once a gate pass is released to you."
              />
            ) : (
              <ul className="flex flex-col gap-2">
                {carried.map((item) => (
                  <li key={item.key}>
                    <button
                      type="button"
                      onClick={() => add(item)}
                      className="flex w-full items-center justify-between gap-3 rounded-lg border border-slate-200 p-2 text-left active:bg-slate-50"
                    >
                      <span className="min-w-0">
                        <span className="block truncate text-sm font-medium text-slate-900">
                          {carriedLabel(item)}
                        </span>
                        <span className="block text-xs text-slate-500">
                          {item.available} {item.uom}
                          {item.condition ? ` · ${item.condition.toLowerCase()}` : ''}
                        </span>
                      </span>
                      {/* E1: whose it is, wherever it appears. */}
                      <OwnershipBadge client={item.ownerClient || null} />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          {lines.length > 0 ? (
            <Card className="flex flex-col gap-4">
              <h2 className="text-sm font-semibold text-slate-900">
                {lines.length} line{lines.length === 1 ? '' : 's'}
              </h2>
              {lines.map((line, index) => {
                const action = ACTIONS.find((option) => option.value === line.action);
                return (
                  <div
                    key={`${line.item_type}-${index}`}
                    className="flex flex-col gap-2 border-t border-slate-100 pt-3 first:border-0 first:pt-0"
                  >
                    <div className="flex items-start justify-between gap-2">
                      <p className="text-sm font-medium text-slate-900">{line.item_name}</p>
                      <Button
                        variant="ghost"
                        className="min-h-0 px-2 py-0.5 text-xs"
                        onClick={() => remove(index)}
                      >
                        Remove
                      </Button>
                    </div>

                    <Field label="What happened" htmlFor={`action-${index}`} hint={action?.note}>
                      <Select
                        id={`action-${index}`}
                        value={line.action}
                        onChange={(event) =>
                          update(index, { action: event.target.value as CloseoutAction })
                        }
                      >
                        {ACTIONS.map((option) => (
                          <option key={option.value} value={option.value}>
                            {option.label}
                          </option>
                        ))}
                      </Select>
                    </Field>

                    {line.serial_unit ? (
                      <p className="text-sm text-slate-600">
                        One unit — a serialized item goes whole (Q5).
                      </p>
                    ) : (
                      <Field label={`How much (${line.uom})`} htmlFor={`qty-${index}`}>
                        <Input
                          id={`qty-${index}`}
                          type="number"
                          inputMode="decimal"
                          min="0"
                          step="0.001"
                          value={line.quantity}
                          onChange={(event) => update(index, { quantity: event.target.value })}
                        />
                      </Field>
                    )}

                    <Field label="Note" htmlFor={`note-${index}`}>
                      <Input
                        id={`note-${index}`}
                        value={line.notes ?? ''}
                        onChange={(event) => update(index, { notes: event.target.value })}
                      />
                    </Field>
                  </div>
                );
              })}

              {/*
                O15: days worked, on the form that already has to be filled in.
                A separate timesheet would be a new habit, and a habit nobody
                has is where a missing figure comes from.
              */}
              <div className="flex flex-col gap-2 border-t border-slate-200 pt-3">
                <div className="flex items-center justify-between">
                  <p className="text-sm font-medium text-slate-700">Days worked</p>
                  <Button
                    variant="ghost"
                    onClick={() =>
                      setLabour((current) => [
                        ...current,
                        {
                          person: String(user?.id ?? ''),
                          work_date: today(),
                          days: '1.0',
                        },
                      ])
                    }
                  >
                    Add a day
                  </Button>
                </div>

                {labour.length === 0 ? (
                  <p className="text-xs text-slate-500">
                    Optional. Leave it empty if there is nothing to report — an
                    invented figure is worse than none.
                  </p>
                ) : null}

                {labour.map((entry, index) => {
                  const total = labour
                    .filter((other) => other.person === entry.person &&
                      other.work_date === entry.work_date)
                    .reduce((sum, other) => sum + Number(other.days || 0), 0);
                  return (
                    <div key={index} className="flex flex-col gap-2 rounded-xl bg-slate-50 p-3">
                      <Field label="Who" htmlFor={`labour-person-${index}`}>
                        <Select
                          id={`labour-person-${index}`}
                          value={entry.person}
                          onChange={(event) =>
                            setLabour((current) =>
                              current.map((row, position) =>
                                position === index
                                  ? { ...row, person: event.target.value }
                                  : row,
                              ),
                            )
                          }
                        >
                          <option value="">Choose…</option>
                          {(people.data?.results ?? []).map((person) => (
                            <option key={person.id} value={person.id}>
                              {person.full_name}
                            </option>
                          ))}
                        </Select>
                      </Field>

                      <div className="grid grid-cols-2 gap-2">
                        <Field label="Date" htmlFor={`labour-date-${index}`}>
                          <Input
                            id={`labour-date-${index}`}
                            type="date"
                            value={entry.work_date}
                            onChange={(event) =>
                              setLabour((current) =>
                                current.map((row, position) =>
                                  position === index
                                    ? { ...row, work_date: event.target.value }
                                    : row,
                                ),
                              )
                            }
                          />
                        </Field>
                        <Field label="Days" htmlFor={`labour-days-${index}`}>
                          <Input
                            id={`labour-days-${index}`}
                            inputMode="decimal"
                            value={entry.days}
                            onChange={(event) =>
                              setLabour((current) =>
                                current.map((row, position) =>
                                  position === index
                                    ? { ...row, days: event.target.value }
                                    : row,
                                ),
                              )
                            }
                          />
                        </Field>
                      </div>

                      {/*
                        O15: warns, never refuses. Someone may genuinely have
                        split a day across two jobs, and blocking the second
                        closeout because of the first is not the system's call.
                      */}
                      {total > 1 ? (
                        <Banner tone="warning">
                          That is {total} days for one person on one date. Fine if
                          it is right — it will be flagged for the owner either way.
                        </Banner>
                      ) : null}

                      <Button
                        variant="ghost"
                        onClick={() =>
                          setLabour((current) =>
                            current.filter((_, position) => position !== index),
                          )
                        }
                      >
                        Remove
                      </Button>
                    </div>
                  );
                })}
              </div>

              <Field label="Anything else about this job" htmlFor="closeout-notes">
                <Textarea
                  id="closeout-notes"
                  value={notes}
                  onChange={(event) => setNotes(event.target.value)}
                />
              </Field>
            </Card>
          ) : null}

          <ActionBar>
            <Button
              block
              disabled={lines.length === 0}
              loading={save.isPending}
              onClick={() => void saveDraft()}
            >
              Save and add photos
            </Button>
          </ActionBar>
        </>
      ) : (
        <>
          <Banner tone="success">
            Saved. Nothing has moved yet — take the site photos, then submit.
            {/* A saved closeout reads SUBMITTED from birth (§4.9's status has no
                draft), so the screen states the truth rather than echoing the
                field: what makes it real is submitting, and that can only
                happen once. */}
          </Banner>

          <Card className="flex flex-col gap-2">
            <h2 className="text-sm font-semibold text-slate-900">What you are reporting</h2>
            <ul className="flex flex-col gap-1 text-sm">
              {draft.lines.map((line, index) => (
                <li key={line.id ?? index} className="flex justify-between gap-3">
                  <span className="text-slate-700">
                    {ACTIONS.find((option) => option.value === line.action)?.label ?? line.action}
                    {' — '}
                    {line.item_name ?? ''}
                    {line.serial_number ? ` ${line.serial_number}` : ''}
                    {line.drum_number ? ` ${line.drum_number}` : ''}
                  </span>
                  <span className="shrink-0 font-medium text-slate-900">
                    {line.quantity} {line.uom}
                  </span>
                </li>
              ))}
            </ul>
          </Card>

          {/* H2: the site photos. Two is what T5.10 asks for. */}
          <PhotoCapture
            targetType="jobs.JobCloseout"
            targetId={draft.id}
            label="Site photos"
            hint="What it looks like now. Two is the norm — one wide, one close."
            minimum={2}
            onChange={(items) => setPhotos(items.length)}
          />

          <ActionBar>
            <Button variant="secondary" block onClick={() => setDraft(null)}>
              Back
            </Button>
            <Button block loading={submit.isPending} onClick={() => void submitCloseout()}>
              {photos < 2 ? 'Submit anyway' : 'Submit closeout'}
            </Button>
          </ActionBar>

          {photos < 2 ? (
            <p className="text-sm text-amber-700">
              {photos === 0 ? 'No photos yet' : 'One photo so far'} — a closeout with
              photos is the one nobody has to come back and ask about.
            </p>
          ) : null}
        </>
      )}
    </div>
  );
}
