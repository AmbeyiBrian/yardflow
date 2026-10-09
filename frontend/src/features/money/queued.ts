/**
 * Entries still on the phone, in the shape the money lists draw (design
 * §4.17.8, R6; T15.10).
 *
 * `useQueuedFinance` reads the outbox; this maps what it returns to what the
 * cards want. A waiting entry reads "Waiting to send". A refused one shows the
 * server's reason and offers "Fix and resend", which reopens the form from the
 * queued payload.
 */

import { useEffect, useState } from 'react';

import { ALLOWANCE_WORDS, type QueuedFinanceEntry } from '../../offline/financeQueue';
import { useQueuedFinance } from './offline';

export interface QueuedCard {
  client_uuid: string;
  title: string;
  meta: string;
  amount: string;
  /** Refused by the server: shown with the reason and a way to fix it. */
  refused: boolean;
  reason?: string;
  /** Where "Fix and resend" goes. */
  fixTo: string;
}

function card(entry: QueuedFinanceEntry, title: string, meta: string, route: string): QueuedCard {
  return {
    client_uuid: entry.client_uuid,
    title,
    meta,
    amount: String(entry.payload.amount ?? '0'),
    refused: entry.state === 'REFUSED',
    reason: entry.reason,
    fixTo: `${route}?resend=${encodeURIComponent(entry.client_uuid)}`,
  };
}

export function queuedExpenseCards(entries: readonly QueuedFinanceEntry[]): QueuedCard[] {
  return entries
    .filter((entry) => entry.kind === 'EXPENSE')
    .map((entry) =>
      card(
        entry,
        String(entry.payload.description || 'Expense'),
        String(entry.payload.incurred_on ?? ''),
        '/money/expenses/new',
      ),
    );
}

export function queuedRequestCards(entries: readonly QueuedFinanceEntry[]): QueuedCard[] {
  return entries
    .filter((entry) => entry.kind === 'ALLOWANCE_REQUEST')
    .map((entry) =>
      card(
        entry,
        ALLOWANCE_WORDS[String(entry.payload.type)] ?? 'Allowance',
        `${String(entry.payload.from_date ?? '')} to ${String(entry.payload.to_date ?? '')}`,
        '/money/requests/new',
      ),
    );
}

/** R7, §4.19.11: purchases waiting to send or refused, listed on the Purchases tab. */
export function queuedPurchaseCards(entries: readonly QueuedFinanceEntry[]): QueuedCard[] {
  return entries
    .filter((entry) => entry.kind === 'SITE_PURCHASE')
    .map((entry) => {
      const lines = Array.isArray(entry.payload.lines) ? entry.payload.lines : [];
      const total = lines.reduce((sum: number, line) => {
        const l = line as { quantity?: unknown; unit_price?: unknown };
        return sum + Math.round(Number(l.quantity) * Number(l.unit_price) * 100);
      }, 0);
      return {
        ...card(
          entry,
          'Purchase',
          String(entry.payload.purchase_date ?? ''),
          '/money/purchases/new',
        ),
        amount: String(Number.isNaN(total) ? 0 : total / 100),
      };
    });
}

/** R15, §4.20.8: suppliers added on this phone, waiting or refused (duplicate name or PIN). */
export function queuedSupplierCards(entries: readonly QueuedFinanceEntry[]): QueuedCard[] {
  return entries
    .filter((entry) => entry.kind === 'SUPPLIER')
    .map((entry) => ({
      ...card(
        entry,
        `Supplier ${String(entry.payload.name ?? '')}`.trim(),
        String(entry.payload.phone ?? ''),
        '/money/suppliers/new',
      ),
      amount: '',
    }));
}

export function queuedCasualCards(entries: readonly QueuedFinanceEntry[]): QueuedCard[] {
  return entries
    .filter((entry) => entry.kind === 'CASUAL')
    .map((entry) =>
      card(
        entry,
        String(entry.payload.name ?? 'Casual'),
        String(entry.payload.id_number ?? ''),
        '/money/casuals/new',
      ),
    );
}

export function useQueuedMoney() {
  const entries = useQueuedFinance();
  return {
    expenses: queuedExpenseCards(entries),
    requests: queuedRequestCards(entries),
    casuals: queuedCasualCards(entries),
    purchases: queuedPurchaseCards(entries),
    suppliers: queuedSupplierCards(entries),
  };
}

/**
 * The queued entry a "Fix and resend" link names, for the forms. `settled`
 * turns true once the live query has had a moment to answer, so a form can say
 * "no longer on this device" instead of waiting forever.
 */
export function useQueuedEntry(uuid: string | null): {
  entry: QueuedFinanceEntry | undefined;
  settled: boolean;
} {
  const entries = useQueuedFinance();
  const [settled, setSettled] = useState(false);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(true), 1500);
    return () => window.clearTimeout(timer);
  }, []);
  // Kept once found: sending the correction drops the old row from the device,
  // and the form must not vanish from under the "saved" message.
  const [kept, setKept] = useState<QueuedFinanceEntry | undefined>();
  const found = uuid ? entries.find((e) => e.client_uuid === uuid) : undefined;
  if (found && found.client_uuid !== kept?.client_uuid) setKept(found);
  return { entry: found ?? kept, settled };
}
