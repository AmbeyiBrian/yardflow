/**
 * T2.14 — the master settings screen (design §7.4; C8).
 *
 * The criterion: "toggling `money_tracking_enabled` changes whether cost fields
 * appear elsewhere in the app within one reload."
 *
 * That works because the switches are part of the session (`/me` returns
 * `organization.settings`), and saving here refetches it. Every screen that
 * shows a cost field reads the same object, so there is one source for "does
 * this tenant track money" rather than a copy per screen that can disagree.
 *
 * Each switch says what it *does*, not what it is called. C8's switches change
 * what the system enforces — whether an approval can be self-approved, whether a
 * photo is required — and an administrator flipping one deserves to know that.
 */

import { useEffect, useState } from 'react';
import { useForm } from 'react-hook-form';

import { api } from '../../api/client';
import { applyFieldErrors, errorMessage, useResource } from '../../api/hooks';
import { useSession } from '../../auth/session';
import { Banner, Button, Card, Checkbox, Field, Input, Select, Spinner } from '../../components/ui';
import { PageHeader } from '../../components/ui/data';
import type { OrganizationSettingsPayload } from './types';

interface Switch {
  key: keyof OrganizationSettingsPayload;
  label: string;
  hint: string;
}

const GROUPS: { title: string; blurb: string; switches: Switch[] }[] = [
  {
    title: 'What we track',
    blurb: 'Turning these off removes the fields rather than hiding them.',
    switches: [
      {
        key: 'money_tracking_enabled',
        label: 'Track cost and value',
        hint: 'Off by default. Unit costs and stock value appear on receiving, reports and approvals only when this is on.',
      },
      {
        key: 'min_stock_enabled',
        label: 'Watch reorder levels',
        hint: 'Items below their reorder level appear on a low-stock list.',
      },
      {
        key: 'qr_labels_enabled',
        label: 'Print QR labels',
        hint: 'Labels for bins and gate passes, scannable at the gate.',
      },
      {
        key: 'asset_tag_enabled',
        label: 'Give equipment asset tags',
        hint: 'Our own tag alongside the manufacturer serial, generated on receipt.',
      },
      {
        key: 'client_waybill_enabled',
        label: 'Produce client waybills',
        hint: 'A document the client signs when their material goes back to them.',
      },
    ],
  },
  {
    title: 'Evidence',
    blurb:
      'A photo is the difference between a dispute and a record. Required means the document cannot be posted without one.',
    switches: [
      {
        key: 'attachments_enabled',
        label: 'Allow photos and documents',
        hint: 'Attachments are never publicly readable, however they are linked.',
      },
      {
        key: 'attachments_required_gate_in',
        label: 'Require a photo on gate-in',
        hint: 'A delivery nobody photographed is a delivery nobody can dispute later.',
      },
      {
        key: 'attachments_required_gate_out',
        label: 'Require a photo on gate-out',
        hint: 'What actually left, as loaded.',
      },
      {
        key: 'signature_required_on_release',
        label: 'Require a signature at the gate',
        hint: 'The driver signs for what they took.',
      },
    ],
  },
  {
    title: 'Approvals',
    blurb: 'These change what the approval engine enforces, not just what it shows.',
    switches: [
      {
        key: 'allow_self_approval',
        label: 'Allow approving your own request',
        hint: 'Off means a requester can never approve their own gate-out. Turning it on removes that control, and every self-approval is recorded as one.',
      },
      {
        key: 'allow_document_amendment',
        label: 'Allow amending an approved document',
        hint: 'An amendment voids the approval and routes again. Nothing goes out on an approval given for different contents.',
      },
    ],
  },
];

export default function SettingsPage() {
  const { refreshUser } = useSession();
  const settings = useResource<OrganizationSettingsPayload>('settings');
  const [banner, setBanner] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);

  const form = useForm<OrganizationSettingsPayload>();

  // Populated once the server answers. `reset` rather than `defaultValues`
  // because the values arrive after the first render.
  useEffect(() => {
    if (settings.data) form.reset(settings.data);
  }, [settings.data, form]);

  const submit = form.handleSubmit(async (values) => {
    setBanner(null);
    setSaved(false);
    setSaving(true);
    try {
      await api.patch('/settings', {
        ...values,
        gate_pass_expiry_hours: Number(values.gate_pass_expiry_hours),
        approval_escalation_hours: Number(values.approval_escalation_hours),
        retention_months: Number(values.retention_months),
      });
      // The switches live on the session, so every screen that reads them picks
      // the change up without a reload (C8's criterion).
      await refreshUser();
      setSaved(true);
    } catch (error) {
      setBanner(applyFieldErrors(error, form.setError));
    } finally {
      setSaving(false);
    }
  });

  if (settings.isLoading) return <Spinner className="text-slate-400" />;
  if (settings.isError) return <Banner tone="error">{errorMessage(settings.error)}</Banner>;

  return (
    <form className="flex flex-col gap-4" onSubmit={submit}>
      <PageHeader
        title="Settings"
        subtitle="What this organization tracks, requires and enforces."
      />

      {banner ? <Banner tone="error">{banner}</Banner> : null}
      {saved ? <Banner tone="info">Saved. Every screen now reads the new values.</Banner> : null}

      {GROUPS.map((group) => (
        <Card key={group.title} className="flex flex-col gap-1">
          <h2 className="text-sm font-semibold text-slate-900">{group.title}</h2>
          <p className="mb-1 text-sm text-slate-600">{group.blurb}</p>
          {group.switches.map((entry) => (
            <Checkbox
              key={entry.key}
              id={`setting-${entry.key}`}
              label={entry.label}
              hint={entry.hint}
              {...form.register(entry.key as 'money_tracking_enabled')}
            />
          ))}
        </Card>
      ))}

      <Card className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold text-slate-900">Timings</h2>

        <Field
          label="A gate pass expires after"
          htmlFor="setting-expiry"
          hint="Hours. An unreleased pass stops being releasable, so an approval from three weeks ago cannot be walked out on today."
        >
          <Input
            id="setting-expiry"
            type="number"
            min={1}
            inputMode="numeric"
            {...form.register('gate_pass_expiry_hours')}
          />
        </Field>

        <Field
          label="An approval escalates after"
          htmlFor="setting-escalation"
          hint="Hours. An unanswered request goes up rather than sitting in a queue nobody is watching."
        >
          <Input
            id="setting-escalation"
            type="number"
            min={1}
            inputMode="numeric"
            {...form.register('approval_escalation_hours')}
          />
        </Field>

        <Field
          label="Keep records for"
          htmlFor="setting-retention"
          hint="Months. Documents and the ledger are kept at least this long. 84 months is seven years."
        >
          <Input
            id="setting-retention"
            type="number"
            min={12}
            inputMode="numeric"
            {...form.register('retention_months')}
          />
        </Field>

        <Field
          label="Asset tag format"
          htmlFor="setting-tag"
          hint="Used when asset tags are on. {category} and {seq} are filled in."
        >
          <Input id="setting-tag" {...form.register('asset_tag_prefix_format')} />
        </Field>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Time zone" htmlFor="setting-tz">
            <Input id="setting-tz" {...form.register('timezone')} />
          </Field>
          <Field label="Currency" htmlFor="setting-currency">
            <Select id="setting-currency" {...form.register('currency')}>
              <option value="KES">KES</option>
              <option value="USD">USD</option>
              <option value="EUR">EUR</option>
              <option value="TZS">TZS</option>
              <option value="UGX">UGX</option>
            </Select>
          </Field>
        </div>
      </Card>

      <div className="flex justify-end">
        <Button type="submit" loading={saving}>
          Save settings
        </Button>
      </div>
    </form>
  );
}
