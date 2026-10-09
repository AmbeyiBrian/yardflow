/**
 * Record an expense (Epic R, R1, R3; design §4.17.4, §4.17.10; was T10.23).
 *
 * Anyone may record one, because a technician at a fuel station is closer to the
 * fact than anybody back at the yard. Site first and the project follows from
 * it (`SiteProject`); a category's `kind` decides the extra questions — fuel
 * wants the vehicle, casual labour wants the casuals and their days.
 *
 * Photos are taken in the form, before saving (§4.17.8): receipt, fuel pump,
 * work done. Online the expense is created and the photos sent right after,
 * since an attachment needs something to hang off; a photo that fails leaves the
 * expense saved and a retry on its page. Offline, the expense and its photos go
 * to the phone's queue together and send when there is network (R6). The receipt
 * is asked for, not required (O16): a real cost that would not photograph is
 * still a real cost, and the approvers are told it has no evidence.
 * `photos_expected` is the number of photos taken, so "arriving" is not mistaken
 * for "none".
 */

import { useMemo, useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import { useNavigate, useSearchParams } from 'react-router-dom';

import { useDetail, useList } from '../../api/hooks';
import { Banner, Button, Card, Field, Input, Select, Spinner, Textarea } from '../../components/ui';
import { PageHeader, Sheet } from '../../components/ui/data';
import { MoneyInput } from '../../components/ui/money';
import { newUuid } from '../../offline/db';
import { useOffline } from '../../offline/OfflineProvider';
import type { Project, ProjectJob } from '../projects/types';
import { useCreateExpense } from './api';
import { CasualForm } from './CasualPages';
import { DraftPhotos } from './DraftPhotos';
import {
  SAVED_ON_PHONE,
  casualRef,
  expensePrefill,
  isNetworkError,
  mergeCasualOptions,
  queuedCasualOptions,
  sendTo,
  shouldQueue,
  toUploadItems,
  uploadItems,
  type CasualOption,
} from './drafts';
import { moneyError } from './errors';
import {
  queueExpense,
  resendCorrected,
  useQueuedFinance,
  type PhotoDraft,
  type QueuedExpenseBody,
} from './offline';
import { useQueuedEntry } from './queued';
import { useMoneyCasuals, useMoneyCategories, useMoneyFloats } from './reference';
import { fromCents, toCents } from './rules';
import { SiteProjectFields, useSiteProject } from './SiteProject';

interface Line {
  key: number;
  casual: string;
  days: string;
  amount: string;
}

interface Values {
  job: string;
  category: string;
  amount: string;
  incurred_on: string;
  description: string;
  scope_of_work: string;
  vehicle_reg: string;
  litres: string;
  float_request: string;
}

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

export default function RecordExpensePage() {
  const [params] = useSearchParams();
  const resend = params.get('resend');
  const { entry, settled } = useQueuedEntry(resend);

  // "Fix and resend" starts from the queued payload, so wait for it.
  if (resend && !entry) {
    return (
      <div className="flex flex-col gap-4">
        <PageHeader title="Record an expense" />
        {settled ? (
          <Banner tone="info">That entry is no longer on this phone.</Banner>
        ) : (
          <Spinner className="text-slate-400" />
        )}
      </div>
    );
  }
  return (
    <ExpenseForm
      key={entry?.client_uuid ?? 'new'}
      resendOf={entry?.client_uuid}
      payload={entry?.payload}
    />
  );
}

function ExpenseForm({
  resendOf,
  payload,
}: {
  resendOf?: string;
  payload?: Record<string, unknown>;
}) {
  const navigate = useNavigate();
  const { online } = useOffline();
  const [params] = useSearchParams();
  const prefill = payload ? expensePrefill(payload) : null;
  // Arrived from a project's own screen: that project is the answer, and asking
  // again invites picking the wrong one off a list of two hundred.
  const fixedProject = params.get('project') ?? '';
  const place = useSiteProject(
    fixedProject,
    prefill ? { site: prefill.site, project: prefill.project } : undefined,
  );

  const [banner, setBanner] = useState<string | null>(null);
  const [localErrors, setLocalErrors] = useState<Record<string, string>>({});
  const [drafts, setDrafts] = useState<PhotoDraft[]>([]);
  const [busy, setBusy] = useState(false);
  const [lines, setLines] = useState<Line[]>(
    prefill?.lines.length
      ? prefill.lines.map((l, i) => ({ key: i + 1, ...l }))
      : [{ key: 1, casual: '', days: '1', amount: '' }],
  );
  const [search, setSearch] = useState('');
  const [adding, setAdding] = useState<number | null>(null);
  // Casuals chosen or just added stay nameable when a later search drops them.
  const [known, setKnown] = useState<Record<string, CasualOption>>({});
  // One uuid per form, so a retry after a dropped response cannot record twice.
  const uuid = useRef(newUuid());
  const nextKey = useRef(prefill ? prefill.lines.length + 1 : 2);

  // Each reads the network online and the offline bundle otherwise (R6, §4.17.8).
  const { categories } = useMoneyCategories();
  const openFloats = useMoneyFloats();
  const serverCasuals = useMoneyCasuals(search);
  const queuedEntries = useQueuedFinance();
  const create = useCreateExpense();

  const form = useForm<Values>({
    defaultValues: prefill?.values ?? {
      job: '',
      category: '',
      amount: '',
      incurred_on: today(),
      description: '',
      scope_of_work: '',
      vehicle_reg: '',
      litres: '',
      float_request: '',
    },
  });

  const category = categories.find((c) => String(c.id) === form.watch('category'));
  const kind = category?.kind ?? 'GENERAL';

  const arrivedFrom = useDetail<Project>('projects', fixedProject || undefined);

  const jobs = useList<ProjectJob>(
    'jobs',
    { project: place.project, page_size: 100 },
    { enabled: Boolean(place.project) },
  );

  // Server casuals (the bundle's list offline), then those still on this phone
  // (§4.17.8).
  const casualOptions = useMemo(
    () =>
      mergeCasualOptions(
        Object.values(known),
        serverCasuals,
        queuedCasualOptions(queuedEntries),
      ),
    [known, serverCasuals, queuedEntries],
  );

  const patchLine = (key: number, patch: Partial<Line>) =>
    setLines((rows) => rows.map((row) => (row.key === key ? { ...row, ...patch } : row)));

  const fixedLabel = arrivedFrom.data
    ? `${arrivedFrom.data.po_number || arrivedFrom.data.reference} ${arrivedFrom.data.title ?? ''}`.trim()
    : '…';

  async function submit(values: Values) {
    setBanner(null);
    const errors: Record<string, string> = {};
    if (!place.project && !place.blocked) errors.project = 'Which project is this for?';
    if (!place.fixed && !place.direct && !place.site) errors.site = 'Which site was it?';
    if (kind === 'FUEL' && !values.vehicle_reg.trim()) {
      errors.vehicle_reg = 'Fuel needs the vehicle registration.';
    }
    const usable = lines.filter((l) => l.casual);
    if (kind === 'CASUAL_LABOUR') {
      if (usable.length === 0) errors.casuals = 'Pick at least one casual.';
      else if (usable.some((l) => !(Number(l.days) > 0))) {
        errors.casuals = 'Each casual needs a number of days above zero.';
      }
    }
    setLocalErrors(errors);
    if (place.blocked || Object.keys(errors).length) return;

    const body: QueuedExpenseBody = {
      project: Number(place.project),
      site: place.site && !place.direct ? Number(place.site) : null,
      job: values.job ? Number(values.job) : null,
      category: Number(values.category),
      amount: values.amount,
      incurred_on: values.incurred_on,
      description: values.description,
      scope_of_work: values.scope_of_work,
      vehicle_reg: kind === 'FUEL' ? values.vehicle_reg.trim() : '',
      litres: kind === 'FUEL' && values.litres ? values.litres : null,
      float_request: values.float_request ? Number(values.float_request) : null,
      // What was taken here is what the approver is told to expect (R1);
      // a resent entry keeps the photos it already has.
      photos_expected: resendOf ? (prefill?.photos_expected ?? 0) : drafts.length,
      casual_lines:
        kind === 'CASUAL_LABOUR'
          ? usable.map((l) => ({
              ...casualRef(l.casual),
              days: Number(l.days),
              amount: l.amount ? l.amount : null,
            }))
          : undefined,
    };

    setBusy(true);
    try {
      if (resendOf) {
        await resendCorrected(resendOf, body as Record<string, unknown>);
        return leaveQueued();
      }
      if (!shouldQueue(online, body.casual_lines)) {
        try {
          const saved = await create.mutateAsync({
            ...body,
            client_uuid: uuid.current,
            casual_lines: body.casual_lines?.map((l) => ({
              casual: Number(l.casual),
              days: l.days,
              amount: l.amount ?? null,
            })),
          });
          // The expense exists; photos that fail do not undo it. They travel to
          // the detail page, which says which are left and offers a retry (R1).
          const failed = await uploadItems(
            toUploadItems(drafts, newUuid),
            sendTo('commercials.ProjectExpense', saved.id),
          );
          navigate(`/money/expenses/${saved.id}`, {
            state: failed.length ? { failedPhotos: failed } : undefined,
          });
          return;
        } catch (error) {
          if (!isNetworkError(error)) {
            setBanner(moneyError(error, form.setError));
            return;
          }
          // No signal after all: queue it under the same uuid, so a request that
          // did land cannot become a second expense.
        }
      }
      await queueExpense(body, drafts, uuid.current);
      leaveQueued();
    } catch (error) {
      setBanner(error instanceof Error ? error.message : 'That could not be saved.');
    } finally {
      setBusy(false);
    }
  }

  function leaveQueued() {
    navigate('/money', { state: { notice: SAVED_ON_PHONE } });
  }

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Record an expense"
        subtitle="Transport, fuel, hire, permits, casual labour — anything paid out on a project."
      />

      <Card>
        <form className="flex flex-col gap-3" onSubmit={(e) => e.preventDefault()}>
          {banner ? <Banner tone="error">{banner}</Banner> : null}

          {place.fixed ? (
            <Field label="Project">
              <p className="text-sm font-medium text-slate-900">{fixedLabel}</p>
            </Field>
          ) : (
            <SiteProjectFields
              state={place}
              idPrefix="ex"
              siteError={localErrors.site}
              projectError={localErrors.project}
            />
          )}

          <Field
            label="Against a job"
            htmlFor="ex-job"
            hint={
              // Optional on purpose: a permit is the project's, a recovery truck is one job's.
              place.project && !jobs.isLoading && !(jobs.data?.results ?? []).length
                ? 'No jobs under this project yet — it will sit against the project as a whole.'
                : 'Optional. Leave it blank for a cost that belongs to the whole project.'
            }
          >
            <Select id="ex-job" disabled={!place.project} {...form.register('job')}>
              <option value="">The project as a whole</option>
              {(jobs.data?.results ?? []).map((job) => (
                <option key={job.id} value={job.id}>
                  {job.reference} {job.site_ref ?? ''}
                </option>
              ))}
            </Select>
          </Field>

          <Field
            label="What kind"
            htmlFor="ex-category"
            error={form.formState.errors.category?.message}
          >
            <Select
              id="ex-category"
              {...form.register('category', { required: 'Pick a category.' })}
            >
              <option value="">Choose…</option>
              {categories.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </Select>
          </Field>

          {kind === 'FUEL' ? (
            <>
              <Field label="Vehicle registration" htmlFor="ex-reg" error={localErrors.vehicle_reg}>
                <Input id="ex-reg" autoCapitalize="characters" {...form.register('vehicle_reg')} />
              </Field>
              <Field label="Litres" htmlFor="ex-litres" hint="Optional.">
                <Input
                  id="ex-litres"
                  inputMode="decimal"
                  {...form.register('litres')}
                />
              </Field>
            </>
          ) : null}

          {kind === 'CASUAL_LABOUR' ? (
            <fieldset className="flex flex-col gap-3 rounded-lg border border-slate-200 p-3">
              <legend className="px-1 text-sm font-medium text-slate-700">Casuals and days</legend>
              <Field label="Find a casual" htmlFor="ex-casual-search" hint="Type a name or ID.">
                <Input
                  id="ex-casual-search"
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                />
              </Field>
              {lines.map((line, index) => (
                <div key={line.key} className="flex flex-col gap-2 rounded-lg bg-slate-50 p-2">
                  <Select
                    aria-label={`Casual ${index + 1}`}
                    value={line.casual}
                    onChange={(event) => {
                      const picked = casualOptions.find((c) => c.value === event.target.value);
                      if (picked) setKnown((k) => ({ ...k, [picked.value]: picked }));
                      patchLine(line.key, { casual: event.target.value });
                    }}
                  >
                    <option value="">Choose…</option>
                    {casualOptions.map((c) => (
                      <option key={c.value} value={c.value}>
                        {c.label}
                      </option>
                    ))}
                  </Select>
                  <div className="grid grid-cols-2 gap-2">
                    <Input
                      aria-label={`Days worked ${index + 1}`}
                      inputMode="decimal"
                      placeholder="Days"
                      value={line.days}
                      onChange={(event) => patchLine(line.key, { days: event.target.value })}
                    />
                    <Input
                      aria-label={`Amount ${index + 1} (optional)`}
                      inputMode="decimal"
                      placeholder="Amount (optional)"
                      value={line.amount}
                      onChange={(event) => patchLine(line.key, { amount: event.target.value })}
                    />
                  </div>
                  <div className="flex gap-2">
                    <Button variant="secondary" onClick={() => setAdding(line.key)}>
                      Add new casual
                    </Button>
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
              ))}
              {localErrors.casuals ? (
                <p role="alert" className="text-sm text-red-600">
                  {localErrors.casuals}
                </p>
              ) : null}
              <Button
                variant="secondary"
                onClick={() =>
                  setLines((rows) => [
                    ...rows,
                    { key: nextKey.current++, casual: '', days: '1', amount: '' },
                  ])
                }
              >
                Another casual
              </Button>
              <LineTotal lines={lines} />
            </fieldset>
          ) : null}

          <Field
            label="Amount"
            htmlFor="ex-amount"
            hint="Excluding VAT."
            error={form.formState.errors.amount?.message}
          >
            <MoneyInput
              id="ex-amount"
              {...form.register('amount', { required: 'How much was it?' })}
            />
          </Field>

          <Field label="When" htmlFor="ex-date">
            <Input id="ex-date" type="date" {...form.register('incurred_on')} />
          </Field>

          <Field label="Scope of work" htmlFor="ex-scope" hint="What the work was.">
            <Textarea id="ex-scope" {...form.register('scope_of_work')} />
          </Field>

          <Field label="What for" htmlFor="ex-description">
            <Textarea id="ex-description" {...form.register('description')} />
          </Field>

          {openFloats.length > 0 ? (
            <Field
              label="Paid from float"
              htmlFor="ex-float"
              hint="Leave blank if you paid it yourself and want it paid back."
            >
              <Select id="ex-float" {...form.register('float_request')}>
                <option value="">Not from a float</option>
                {openFloats.map((f) => (
                  <option key={f.id} value={f.id}>
                    {f.number} — balance {f.balance}
                  </option>
                ))}
              </Select>
            </Field>
          ) : null}

          {resendOf ? (
            <p className="text-sm text-slate-600">
              {prefill?.photos_expected ?? 0} photo(s) already taken stay with this entry.
            </p>
          ) : (
            <DraftPhotos
              hint="Receipt, fuel pump, work done. Taken now, sent with the expense. None is fine if there is nothing to photograph."
              drafts={drafts}
              onChange={setDrafts}
            />
          )}

          <Button
            block
            disabled={busy || place.blocked || place.loading}
            onClick={form.handleSubmit(submit)}
          >
            {busy ? <Spinner /> : resendOf ? 'Fix and resend' : 'Record it'}
          </Button>
        </form>
      </Card>

      <Sheet open={adding !== null} title="Add a casual" onClose={() => setAdding(null)}>
        <CasualForm
          onDone={(casual) => {
            setKnown((k) => ({
              ...k,
              [casual.value]: { value: casual.value, label: `${casual.name} ${casual.id_number}` },
            }));
            if (adding !== null) patchLine(adding, { casual: casual.value });
            setAdding(null);
          }}
        />
      </Sheet>
    </div>
  );
}

/** What the per-line amounts add up to, when any were given. Advisory: the total is the authority. */
function LineTotal({ lines }: { lines: Line[] }) {
  const given = lines.filter((l) => l.amount && !Number.isNaN(toCents(l.amount)));
  if (given.length === 0) return null;
  const sum = given.reduce((acc, l) => acc + toCents(l.amount), 0);
  return <p className="text-sm text-slate-500">Lines given add up to {fromCents(sum)}.</p>;
}
