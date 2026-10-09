/**
 * Request an allowance or float (Epic R, R2, R5; design §4.17.5, §4.17.10).
 *
 * Asked for before the money is spent, so nobody pays the company's costs out
 * of their own pocket. The form warns early — above the daily limit, or dates
 * that touch an earlier request of the same type — using the same pure rules
 * the server enforces (`rules.ts`). They are warnings only: the server decides,
 * and its refusal (ALLOWANCE_LIMIT, ALLOWANCE_OVERLAP) is shown as it words it.
 *
 * Offline (R6) the request is queued and sent when there is network. The limits
 * come from the cached finance settings if this visit has them; without them the
 * warning is simply skipped, since the server still decides on arrival.
 */

import { useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import { useNavigate, useSearchParams } from 'react-router-dom';

import { Banner, Button, Card, Field, Select, Spinner, Textarea, Input } from '../../components/ui';
import { PageHeader } from '../../components/ui/data';
import { MoneyInput } from '../../components/ui/money';
import { newUuid } from '../../offline/db';
import { useOffline } from '../../offline/OfflineProvider';
import { useAllowanceRequests, useCreateAllowanceRequest, useFinanceSettings } from './api';
import { SAVED_ON_PHONE, allowancePrefill, isNetworkError } from './drafts';
import { moneyError } from './errors';
import { queueAllowanceRequest, resendCorrected, type QueuedAllowanceBody } from './offline';
import { useQueuedEntry } from './queued';
import { checkLimit, daysBetween, findOverlap } from './rules';
import { SiteProjectFields, useSiteProject } from './SiteProject';
import type { AllowanceType, TransportScope } from './types';

export const TYPE_LABELS: Record<AllowanceType, string> = {
  FLOAT: 'Float',
  TRANSPORT: 'Transport',
  NIGHT_OUT: 'Night out',
  TEAM_ALLOWANCE: 'Team allowance',
  OTHER: 'Other',
};

interface Values {
  type: AllowanceType;
  transport_scope: TransportScope | '';
  from_date: string;
  to_date: string;
  amount: string;
  reason: string;
}

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

export default function RequestAllowancePage() {
  const [params] = useSearchParams();
  const resend = params.get('resend');
  const { entry, settled } = useQueuedEntry(resend);

  // "Fix and resend" starts from the queued payload, so wait for it.
  if (resend && !entry) {
    return (
      <div className="flex flex-col gap-4">
        <PageHeader title="Request an allowance" />
        {settled ? (
          <Banner tone="info">That entry is no longer on this phone.</Banner>
        ) : (
          <Spinner className="text-slate-400" />
        )}
      </div>
    );
  }
  return (
    <RequestForm
      key={entry?.client_uuid ?? 'new'}
      resendOf={entry?.client_uuid}
      payload={entry?.payload}
    />
  );
}

function RequestForm({
  resendOf,
  payload,
}: {
  resendOf?: string;
  payload?: Record<string, unknown>;
}) {
  const navigate = useNavigate();
  const { online } = useOffline();
  const prefill = payload ? allowancePrefill(payload) : null;
  const place = useSiteProject(
    '',
    prefill ? { site: prefill.site, project: prefill.project } : undefined,
  );
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const [localErrors, setLocalErrors] = useState<Record<string, string>>({});
  const uuid = useRef(newUuid());

  const settings = useFinanceSettings();
  const mine = useAllowanceRequests({ mine: true, page_size: 200 });
  const create = useCreateAllowanceRequest();

  const form = useForm<Values>({
    defaultValues: (prefill?.values as Partial<Values> | undefined) ?? {
      type: 'FLOAT',
      transport_scope: '',
      from_date: today(),
      to_date: today(),
      amount: '',
      reason: '',
    },
  });

  const [type, scope, from, to, amount] = form.watch([
    'type',
    'transport_scope',
    'from_date',
    'to_date',
    'amount',
  ]);

  const days = from && to ? daysBetween(from, to) : 0;
  const datesBackwards = Boolean(from && to) && days < 1;
  const limit = checkLimit(type, scope || null, amount, days, settings.data?.allowance_limits);
  const overlap =
    from && to && !datesBackwards
      ? findOverlap({ type, from_date: from, to_date: to }, mine.data?.results ?? [])
      : null;

  async function submit(values: Values) {
    setBanner(null);
    const errors: Record<string, string> = {};
    if (values.type === 'TRANSPORT' && !values.transport_scope) {
      errors.transport_scope = 'Within Nairobi or outside it?';
    }
    if (datesBackwards) errors.to_date = 'The end date is before the start.';
    if (place.site && !place.project && !place.blocked) errors.project = 'Which project is this for?';
    setLocalErrors(errors);
    if (place.blocked || Object.keys(errors).length) return;

    const body: QueuedAllowanceBody = {
      type: values.type,
      transport_scope: values.type === 'TRANSPORT' ? (values.transport_scope as TransportScope) : null,
      amount: values.amount,
      from_date: values.from_date,
      to_date: values.to_date,
      site: place.site && !place.direct ? Number(place.site) : null,
      project: place.project ? Number(place.project) : null,
      reason: values.reason,
    };

    setBusy(true);
    try {
      if (resendOf) {
        await resendCorrected(resendOf, body);
        return leaveQueued();
      }
      if (online) {
        try {
          const saved = await create.mutateAsync({ ...body, client_uuid: uuid.current });
          navigate(`/money/requests/${saved.id}`);
          return;
        } catch (error) {
          if (!isNetworkError(error)) {
            setBanner(moneyError(error, form.setError));
            return;
          }
          // No signal after all: queue it under the same uuid, so a request that
          // did land cannot become a second one.
        }
      }
      await queueAllowanceRequest(body, uuid.current);
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
        title="Request an allowance"
        subtitle="Ask before you spend. A float is accounted for as you record expenses against it."
      />

      <Card>
        <form className="flex flex-col gap-3" onSubmit={(e) => e.preventDefault()}>
          {banner ? <Banner tone="error">{banner}</Banner> : null}

          <Field label="What for" htmlFor="rq-type">
            <Select id="rq-type" {...form.register('type')}>
              {(Object.keys(TYPE_LABELS) as AllowanceType[]).map((t) => (
                <option key={t} value={t}>
                  {TYPE_LABELS[t]}
                </option>
              ))}
            </Select>
          </Field>

          {type === 'TRANSPORT' ? (
            <Field label="Where to" htmlFor="rq-scope" error={localErrors.transport_scope}>
              <Select id="rq-scope" {...form.register('transport_scope')}>
                <option value="">Choose…</option>
                <option value="WITHIN_NAIROBI">Within Nairobi</option>
                <option value="OUTSIDE_NAIROBI">Outside Nairobi</option>
              </Select>
            </Field>
          ) : null}

          <div className="grid grid-cols-2 gap-3">
            <Field label="From" htmlFor="rq-from">
              <Input id="rq-from" type="date" {...form.register('from_date')} />
            </Field>
            <Field label="To" htmlFor="rq-to" error={localErrors.to_date}>
              <Input id="rq-to" type="date" {...form.register('to_date')} />
            </Field>
          </div>
          {days > 0 ? (
            <p className="text-sm text-slate-500">
              {days} {days === 1 ? 'day' : 'days'}, counting both ends.
            </p>
          ) : null}

          <Field
            label="Amount"
            htmlFor="rq-amount"
            error={form.formState.errors.amount?.message}
          >
            <MoneyInput
              id="rq-amount"
              {...form.register('amount', { required: 'How much do you need?' })}
            />
          </Field>

          {limit ? <Banner tone="warning">{limit.message} It may be refused.</Banner> : null}
          {overlap ? (
            <Banner tone="warning">
              You already have {overlap.number} for these dates ({overlap.from_date} to{' '}
              {overlap.to_date}). It may be refused.
            </Banner>
          ) : null}

          <SiteProjectFields
            state={place}
            idPrefix="rq"
            siteOptional
            projectError={localErrors.project}
          />

          <Field label="Reason" htmlFor="rq-reason" error={form.formState.errors.reason?.message}>
            <Textarea
              id="rq-reason"
              {...form.register('reason', { required: 'Say what it is for.' })}
            />
          </Field>

          <Button
            block
            disabled={busy || place.blocked || place.loading}
            onClick={form.handleSubmit(submit)}
          >
            {busy ? <Spinner /> : resendOf ? 'Fix and resend' : 'Send request'}
          </Button>
        </form>
      </Card>
    </div>
  );
}
