/**
 * Record an expense (Epic R, R1, R3; design §4.17.4, §4.17.10; was T10.23).
 *
 * Anyone may record one, because a technician at a fuel station is closer to the
 * fact than anybody back at the yard. Site first and the project follows from
 * it (`SiteProject`); a category's `kind` decides the extra questions — fuel
 * wants the vehicle, casual labour wants the casuals and their days.
 *
 * The expense is created first and the photos attached to it afterwards: an
 * attachment needs something to hang off, and asking for the image first would
 * mean holding it in memory on a phone that may not survive the walk back. The
 * receipt is asked for, not required (O16): a real cost that would not
 * photograph is still a real cost, and the approvers are told it has no
 * evidence. `photos_expected` says how many are coming so "arriving" is not
 * mistaken for "none".
 */

import { useMemo, useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import { useNavigate, useSearchParams } from 'react-router-dom';

import { useDetail, useList } from '../../api/hooks';
import { PhotoCapture } from '../../components/PhotoCapture';
import { Banner, Button, Card, Field, Input, Select, Spinner, Textarea } from '../../components/ui';
import { PageHeader, Sheet } from '../../components/ui/data';
import { MoneyInput } from '../../components/ui/money';
import { newUuid } from '../../offline/db';
import type { Project, ProjectJob } from '../projects/types';
import { useAllowanceRequests, useCasuals, useCreateExpense } from './api';
import { CasualForm } from './CasualPages';
import { moneyError } from './errors';
import { fromCents, toCents } from './rules';
import { SiteProjectFields, useSiteProject } from './SiteProject';
import type { Casual, ExpenseCategory, ProjectExpense } from './types';

export const CAPTIONS = ['Receipt', 'Fuel pump', 'Work done', 'Other'] as const;

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
  photos_expected: string;
}

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

export default function RecordExpensePage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  // Arrived from a project's own screen: that project is the answer, and asking
  // again invites picking the wrong one off a list of two hundred.
  const fixedProject = params.get('project') ?? '';
  const place = useSiteProject(fixedProject);

  const [saved, setSaved] = useState<ProjectExpense | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [localErrors, setLocalErrors] = useState<Record<string, string>>({});
  const [caption, setCaption] = useState<string>('Receipt');
  const [lines, setLines] = useState<Line[]>([{ key: 1, casual: '', days: '1', amount: '' }]);
  const [search, setSearch] = useState('');
  const [adding, setAdding] = useState<number | null>(null);
  // Casuals chosen or just added stay nameable when a later search drops them.
  const [known, setKnown] = useState<Record<string, Casual>>({});
  // One uuid per form, so a retry after a dropped response cannot record twice.
  const uuid = useRef(newUuid());
  const nextKey = useRef(2);

  const categories = useList<ExpenseCategory>('expense-categories', {
    is_active: true,
    page_size: 100,
  });
  const floats = useAllowanceRequests({ mine: true, type: 'FLOAT', page_size: 100 });
  const casuals = useCasuals(search);
  const create = useCreateExpense();

  const form = useForm<Values>({
    defaultValues: {
      job: '',
      category: '',
      amount: '',
      incurred_on: today(),
      description: '',
      scope_of_work: '',
      vehicle_reg: '',
      litres: '',
      float_request: '',
      photos_expected: '1',
    },
  });

  const category = (categories.data?.results ?? []).find(
    (c) => String(c.id) === form.watch('category'),
  );
  const kind = category?.kind ?? 'GENERAL';

  const arrivedFrom = useDetail<Project>('projects', fixedProject || undefined);

  const jobs = useList<ProjectJob>(
    'jobs',
    { project: place.project, page_size: 100 },
    { enabled: Boolean(place.project) },
  );

  const openFloats = (floats.data?.results ?? []).filter(
    (f) => f.type === 'FLOAT' && f.status === 'PAID' && !f.closed_at,
  );

  const casualOptions = useMemo(() => {
    const byId = new Map<string, Casual>(Object.entries(known));
    for (const c of casuals.data?.results ?? []) byId.set(String(c.id), c);
    return [...byId.values()];
  }, [known, casuals.data]);

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

    try {
      setSaved(
        await create.mutateAsync({
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
          photos_expected: Math.max(0, Number(values.photos_expected) || 0),
          client_uuid: uuid.current,
          casual_lines:
            kind === 'CASUAL_LABOUR'
              ? usable.map((l) => ({
                  casual: Number(l.casual),
                  days: Number(l.days),
                  amount: l.amount ? l.amount : null,
                }))
              : undefined,
        }),
      );
    } catch (error) {
      setBanner(moneyError(error, form.setError));
    }
  }

  if (saved) {
    return (
      <div className="flex flex-col gap-4">
        <PageHeader title="Record an expense" />
        <Card>
          <Banner tone="success">
            Recorded. It reaches the project&rsquo;s cost once it has been approved.
          </Banner>
          <div className="mt-3 flex flex-col gap-3">
            <Field label="What the next photo shows" htmlFor="ex-caption">
              <Select
                id="ex-caption"
                value={caption}
                onChange={(event) => setCaption(event.target.value)}
              >
                {CAPTIONS.map((c) => (
                  <option key={c}>{c}</option>
                ))}
              </Select>
            </Field>
            <PhotoCapture
              targetType="commercials.ProjectExpense"
              targetId={String(saved.id)}
              label="Photos"
              caption={caption}
              minimum={saved.photos_expected}
            />
            <Button
              variant="ghost"
              onClick={() => {
                uuid.current = newUuid();
                setSaved(null);
                form.reset({ ...form.getValues(), amount: '', description: '' });
              }}
            >
              Record another
            </Button>
            <Button
              onClick={() =>
                navigate(fixedProject ? `/projects/${fixedProject}` : `/money/expenses/${saved.id}`)
              }
            >
              Done
            </Button>
          </div>
        </Card>
      </div>
    );
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
              {(categories.data?.results ?? []).map((c) => (
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
                      const picked = casualOptions.find((c) => String(c.id) === event.target.value);
                      if (picked) setKnown((k) => ({ ...k, [String(picked.id)]: picked }));
                      patchLine(line.key, { casual: event.target.value });
                    }}
                  >
                    <option value="">Choose…</option>
                    {casualOptions.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name} {c.id_number}
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
                    {f.number} — balance {f.balance ?? f.amount}
                  </option>
                ))}
              </Select>
            </Field>
          ) : null}

          <Field
            label="Photos to attach"
            htmlFor="ex-photos"
            hint="Receipt, fuel pump, work done. You add them on the next screen. 0 if there are none."
          >
            <Input
              id="ex-photos"
              type="number"
              min={0}
              inputMode="numeric"
              {...form.register('photos_expected')}
            />
          </Field>

          <Button
            block
            disabled={create.isPending || place.blocked || place.loading}
            onClick={form.handleSubmit(submit)}
          >
            {create.isPending ? <Spinner /> : 'Record it'}
          </Button>
        </form>
      </Card>

      <Sheet open={adding !== null} title="Add a casual" onClose={() => setAdding(null)}>
        <CasualForm
          onDone={(casual) => {
            setKnown((k) => ({ ...k, [String(casual.id)]: casual }));
            if (adding !== null) patchLine(adding, { casual: String(casual.id) });
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
