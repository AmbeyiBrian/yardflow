/**
 * Earmarks on the stock screens (Q2, Q4; design §4.16.6-7): the line on a unit
 * or drum page with its history, and the sheets that change one without moving
 * the stock.
 */

import { useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { errorMessage, useAction, useList, useResource } from '../../api/hooks';
import { Banner, Button, Card, Field, Input, Select } from '../../components/ui';
import { Sheet } from '../../components/ui/data';
import type { Site } from '../settings/types';
import {
  bulkChangeProblem,
  bulkSourceOptions,
  buildBulkChangeBody,
  earmarkLine,
  type EarmarkRow,
} from './earmarkHelpers';

interface EarmarkEvent {
  id: number;
  occurred_at: string;
  action_label: string;
  site_name: string;
  to_site_name: string;
  quantity: string | null;
  document_number: string;
  actor_name: string;
  reason: string;
}

/** Refresh every stock screen: the keys are whole paths, so match by prefix. */
function useRefreshStock() {
  const queryClient = useQueryClient();
  return () =>
    queryClient.invalidateQueries({
      predicate: (query) => String(query.queryKey[0]).startsWith('stock'),
    });
}

function useSites(enabled: boolean) {
  return useList<Site>('sites', { page_size: 300 }, { enabled });
}

function SiteSelect({
  id,
  value,
  onChange,
  sites,
}: {
  id: string;
  value: string;
  onChange: (next: string) => void;
  sites: Site[];
}) {
  return (
    <Select id={id} value={value} onChange={(event) => onChange(event.target.value)}>
      <option value="">No site</option>
      {sites.map((site) => (
        <option key={site.id} value={site.id}>
          {site.name}
        </option>
      ))}
    </Select>
  );
}

/** A unit's or a drum's earmark, who changed it, and the button to change it. */
export function EarmarkCard({
  subject,
  siteId,
  siteName,
  canChange,
}: {
  subject: { serial_unit: number } | { reel: number };
  siteId: number | null;
  siteName: string | null;
  canChange: boolean;
}) {
  const [open, setOpen] = useState(false);
  const history = useResource<{ results: EarmarkEvent[] }>(
    'stock/earmarks/history',
    subject,
  );
  const events = history.data?.results ?? [];

  return (
    <Card className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-slate-900">{earmarkLine(siteName)}</h2>
        {canChange ? (
          <Button variant="secondary" onClick={() => setOpen(true)}>
            Change earmark
          </Button>
        ) : null}
      </div>
      {events.length > 0 ? (
        <ul className="flex flex-col gap-1 text-sm text-slate-600">
          {events.slice(0, 8).map((event) => (
            <li key={event.id}>
              <span className="text-slate-500">
                {new Date(event.occurred_at).toLocaleString()}
              </span>{' '}
              <span className="text-slate-900">{event.action_label}</span>
              {event.site_name || event.to_site_name
                ? ` · ${event.site_name || 'no site'} → ${event.to_site_name || 'no site'}`
                : ''}
              {event.document_number ? ` · ${event.document_number}` : ''}
              {event.actor_name ? ` · ${event.actor_name}` : ''}
              {event.reason ? ` · ${event.reason}` : ''}
            </li>
          ))}
        </ul>
      ) : null}
      {canChange && open ? (
        <ChangeEarmarkSheet
          open
          onClose={() => setOpen(false)}
          subject={subject}
          currentSite={siteId}
        />
      ) : null}
    </Card>
  );
}

function ChangeEarmarkSheet({
  open,
  onClose,
  subject,
  currentSite,
}: {
  open: boolean;
  onClose: () => void;
  subject: { serial_unit: number } | { reel: number };
  currentSite: number | null;
}) {
  const sites = useSites(open);
  const refresh = useRefreshStock();
  const change = useAction({ resource: 'stock/earmarks/change' });
  const [toSite, setToSite] = useState(currentSite ? String(currentSite) : '');
  const [reason, setReason] = useState('');
  const [banner, setBanner] = useState<string | null>(null);

  async function save() {
    setBanner(null);
    if (!reason.trim()) {
      setBanner('Say why the earmark is changing.');
      return;
    }
    try {
      await change.mutateAsync({
        ...subject,
        to_site: toSite ? Number(toSite) : null,
        reason: reason.trim(),
      });
      await refresh();
      setReason('');
      onClose();
    } catch (error) {
      setBanner(errorMessage(error));
    }
  }

  return (
    <Sheet
      open={open}
      title="Change earmark"
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" block onClick={onClose}>
            Cancel
          </Button>
          <Button block loading={change.isPending} onClick={save}>
            Save
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {banner ? <Banner tone="error">{banner}</Banner> : null}
        <p className="text-sm text-slate-600">The stock stays where it is.</p>
        <Field label="For site" htmlFor="earmark-site">
          <SiteSelect
            id="earmark-site"
            value={toSite}
            onChange={setToSite}
            sites={sites.data?.results ?? []}
          />
        </Field>
        <Field label="Why" htmlFor="earmark-why">
          <Input id="earmark-why" value={reason} onChange={(e) => setReason(e.target.value)} />
        </Field>
      </div>
    </Sheet>
  );
}

/** A stock row's earmarked or free quantity, moved to another site or cleared. */
export function BulkEarmarkSheet({
  open,
  onClose,
  row,
}: {
  open: boolean;
  onClose: () => void;
  row: EarmarkRow;
}) {
  const options = bulkSourceOptions(row);
  const sites = useSites(open);
  const refresh = useRefreshStock();
  const change = useAction({ resource: 'stock/earmarks/change' });
  const [from, setFrom] = useState(options[0]?.key ?? '');
  const [to, setTo] = useState('');
  const [quantity, setQuantity] = useState('');
  const [reason, setReason] = useState('');
  const [banner, setBanner] = useState<string | null>(null);
  const source = options.find((o) => o.key === from);

  async function save() {
    setBanner(null);
    const input = { from, to, quantity, reason };
    const problem = bulkChangeProblem(options, input);
    if (problem) {
      setBanner(problem);
      return;
    }
    try {
      await change.mutateAsync(buildBulkChangeBody(row, input));
      await refresh();
      setQuantity('');
      setReason('');
      onClose();
    } catch (error) {
      setBanner(errorMessage(error));
    }
  }

  return (
    <Sheet
      open={open}
      title="Change earmark"
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" block onClick={onClose}>
            Cancel
          </Button>
          <Button block loading={change.isPending} onClick={save}>
            Save
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {banner ? <Banner tone="error">{banner}</Banner> : null}
        <Field label="Take it from" htmlFor="bulk-earmark-from">
          <Select
            id="bulk-earmark-from"
            value={from}
            onChange={(event) => setFrom(event.target.value)}
          >
            {options.map((option) => (
              <option key={option.key} value={option.key}>
                {option.label} ({option.max})
              </option>
            ))}
          </Select>
        </Field>
        <Field
          label="Quantity"
          htmlFor="bulk-earmark-qty"
          hint={source ? `Up to ${source.max}.` : undefined}
        >
          <Input
            id="bulk-earmark-qty"
            inputMode="decimal"
            value={quantity}
            onChange={(e) => setQuantity(e.target.value)}
          />
        </Field>
        <Field label="Move it to" htmlFor="bulk-earmark-to">
          <SiteSelect
            id="bulk-earmark-to"
            value={to}
            onChange={setTo}
            sites={sites.data?.results ?? []}
          />
        </Field>
        <Field label="Why" htmlFor="bulk-earmark-why">
          <Input
            id="bulk-earmark-why"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
        </Field>
      </div>
    </Sheet>
  );
}
