import { describe, expect, it } from 'vitest';

import { ApiError } from '../../api/client';
import type { QueuedFinanceEntry } from '../../offline/financeQueue';
import {
  allowancePrefill,
  casualRef,
  casualValue,
  expensePrefill,
  isNetworkError,
  mergeCasualOptions,
  queuedCasualOptions,
  shouldQueue,
  toUploadItems,
  uploadItems,
} from './drafts';
import { queuedExpenseCards, queuedRequestCards } from './queued';

const draft = (caption: string, filename: string) => ({
  caption,
  filename,
  blob: new Blob(['x']),
});

function queued(over: Partial<QueuedFinanceEntry>): QueuedFinanceEntry {
  return {
    client_uuid: 'u1',
    kind: 'EXPENSE',
    summary: '',
    state: 'WAITING',
    captured_at: '2026-10-09T08:00:00Z',
    payload: {},
    photos: 0,
    ...over,
  };
}

describe('photo drafts', () => {
  it('gives each draft its own client_uuid, kept for retries', () => {
    let n = 0;
    const items = toUploadItems([draft('Receipt', 'a.jpg'), draft('Other', 'b.jpg')], () => `id${++n}`);
    expect(items.map((i) => i.client_uuid)).toEqual(['id1', 'id2']);
    expect(items.map((i) => i.caption)).toEqual(['Receipt', 'Other']);
  });

  it('returns only the uploads that failed, and carries on past one', async () => {
    const items = toUploadItems(
      [draft('Receipt', 'a.jpg'), draft('Fuel pump', 'b.jpg'), draft('Other', 'c.jpg')],
      () => 'x',
    );
    const sent: string[] = [];
    const failed = await uploadItems(items, async (item) => {
      if (item.filename === 'b.jpg') throw new Error('boom');
      sent.push(item.filename);
    });
    expect(sent).toEqual(['a.jpg', 'c.jpg']);
    expect(failed.map((i) => i.filename)).toEqual(['b.jpg']);
  });
});

describe('where a save goes', () => {
  it('treats a thrown fetch as a network error and a server answer as not', () => {
    expect(isNetworkError(new TypeError('Failed to fetch'))).toBe(true);
    expect(isNetworkError(new ApiError(400, { code: 'X', message: 'no' }))).toBe(false);
    expect(isNetworkError(new ApiError(503, { code: 'X', message: 'gateway' }))).toBe(true);
  });

  it('queues offline, or when a line names a casual still on the phone', () => {
    expect(shouldQueue(false)).toBe(true);
    expect(shouldQueue(true)).toBe(false);
    expect(shouldQueue(true, [{ casual: 3, days: 1 }])).toBe(false);
    expect(shouldQueue(true, [{ casual_client_uuid: 'c1', days: 1 }])).toBe(true);
  });
});

describe('casual picker', () => {
  it('offers waiting casuals, not refused ones, and round-trips the reference', () => {
    const options = queuedCasualOptions([
      queued({ client_uuid: 'c1', kind: 'CASUAL', payload: { name: 'Jane', id_number: '123' } }),
      queued({ client_uuid: 'c2', kind: 'CASUAL', state: 'REFUSED', payload: { name: 'Bad' } }),
      queued({ client_uuid: 'e1', kind: 'EXPENSE' }),
    ]);
    expect(options).toHaveLength(1);
    expect(casualRef(options[0].value)).toEqual({ casual_client_uuid: 'c1' });
    expect(casualRef('7')).toEqual({ casual: 7 });
    expect(casualValue({ casual_client_uuid: 'c1', days: 1 })).toBe(options[0].value);
    expect(casualValue({ casual: 7, days: 1 })).toBe('7');
  });

  it('does not repeat an option', () => {
    const merged = mergeCasualOptions(
      [{ value: '1', label: 'a' }],
      [{ value: '1', label: 'b' }, { value: 'q:x', label: 'c' }],
    );
    expect(merged.map((o) => o.label)).toEqual(['a', 'c']);
  });
});

describe('fix and resend prefill', () => {
  it('fills the expense form from the queued payload', () => {
    const prefill = expensePrefill({
      project: 4,
      site: 2,
      category: 9,
      amount: '1200.00',
      incurred_on: '2026-10-08',
      description: 'Diesel',
      litres: null,
      photos_expected: 2,
      casual_lines: [{ casual_client_uuid: 'c1', days: 2, amount: null }],
    });
    expect(prefill.values.amount).toBe('1200.00');
    expect(prefill.values.litres).toBe('');
    expect(prefill.site).toBe('2');
    expect(prefill.project).toBe('4');
    expect(prefill.photos_expected).toBe(2);
    expect(prefill.lines).toEqual([{ casual: 'q:c1', days: '2', amount: '' }]);
  });

  it('fills the allowance form, defaulting the type', () => {
    expect(allowancePrefill({ amount: '500', reason: 'Trip' }).values.type).toBe('FLOAT');
    expect(allowancePrefill({ type: 'TRANSPORT', site: null }).site).toBe('');
  });
});

describe('queued cards', () => {
  it('marks refused entries and points Fix and resend at the right form', () => {
    const [waiting, refused] = queuedExpenseCards([
      queued({ client_uuid: 'a', payload: { amount: '10', description: 'Fuel' } }),
      queued({ client_uuid: 'b', state: 'REFUSED', reason: 'FLOAT_NOT_OPEN' }),
    ]);
    expect(waiting.refused).toBe(false);
    expect(waiting.title).toBe('Fuel');
    expect(refused.refused).toBe(true);
    expect(refused.reason).toBe('FLOAT_NOT_OPEN');
    expect(refused.fixTo).toBe('/money/expenses/new?resend=b');
  });

  it('keeps each kind on its own tab', () => {
    const entries = [
      queued({ client_uuid: 'a', kind: 'EXPENSE' }),
      queued({ client_uuid: 'b', kind: 'ALLOWANCE_REQUEST', payload: { type: 'TRANSPORT' } }),
    ];
    expect(queuedExpenseCards(entries)).toHaveLength(1);
    const requests = queuedRequestCards(entries);
    expect(requests).toHaveLength(1);
    expect(requests[0].title).toBe('Transport allowance');
  });
});
