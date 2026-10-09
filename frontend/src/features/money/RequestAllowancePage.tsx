/**
 * Request an allowance or float (Epic R, R2, R5; design §4.17.5, §4.17.10).
 *
 * Asked for before the money is spent, so nobody pays the company's costs out
 * of their own pocket. The form warns early — above the daily limit, or dates
 * that touch an earlier request of the same type — using the same pure rules
 * the server enforces (`rules.ts`). They are warnings only: the server decides,
 * and its refusal (ALLOWANCE_LIMIT, ALLOWANCE_OVERLAP) is shown as it words it.
 */

import { useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import { useNavigate } from 'react-router-dom';

import { Banner, Button, Card, Field, Select, Spinner, Textarea, Input } from '../../components/ui';
import { PageHeader } from '../../components/ui/data';
import { MoneyInput } from '../../components/ui/money';
import { newUuid } from '../../offline/db';
import { useAllowanceRequests, useCreateAllowanceRequest, useFinanceSettings } from './api';
import { moneyError } from './errors';
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
  const navigate = useNavigate();
  const place = useSiteProject();
  const [banner, setBanner] = useState<string | null>(null);
  const [localErrors, setLocalErrors] = useState<Record<string, string>>({});
  const uuid = useRef(newUuid());

  const settings = useFinanceSettings();
  const mine = useAllowanceRequests({ mine: true, page_size: 200 });
  const create = useCreateAllowanceRequest();

  const form = useForm<Values>({
    defaultValues: {
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

    try {
      const saved = await create.mutateAsync({
        type: values.type,
        transport_scope: values.type === 'TRANSPORT' ? (values.transport_scope as TransportScope) : null,
        amount: values.amount,
        from_date: values.from_date,
        to_date: values.to_date,
        site: place.site && !place.direct ? Number(place.site) : null,
        project: place.project ? Number(place.project) : null,
        reason: values.reason,
        client_uuid: uuid.current,
      });
      navigate(`/money/requests/${saved.id}`);
    } catch (error) {
      setBanner(moneyError(error, form.setError));
    }
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
            disabled={create.isPending || place.blocked || place.loading}
            onClick={form.handleSubmit(submit)}
          >
            {create.isPending ? <Spinner /> : 'Send request'}
          </Button>
        </form>
      </Card>
    </div>
  );
}
