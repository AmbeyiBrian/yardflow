import { describe, expect, it } from 'vitest';

import type { QueuedMutation, QueuedPhoto } from './db';
import {
  describeEntry,
  financeEntries,
  inCaptureOrder,
  isPermanentUploadFailure,
  photoTarget,
  photosReadyToUpload,
} from './financeQueue';

function entry(over: Partial<QueuedMutation>): QueuedMutation {
  return {
    id: 1,
    client_uuid: 'q1',
    operation: 'EXPENSE',
    payload: {},
    captured_at: '2026-10-09T08:00:00Z',
    status: 'PENDING',
    attempts: 0,
    ...over,
  };
}

function photo(over: Partial<QueuedPhoto>): QueuedPhoto {
  return {
    id: 1,
    queue_client_uuid: 'q1',
    caption: 'Receipt',
    blob: new Blob(['x']),
    filename: 'a.jpg',
    client_uuid: 'p1',
    status: 'QUEUED',
    attempts: 0,
    ...over,
  };
}

describe('inCaptureOrder', () => {
  it('replays the casual before the expense that names it, whatever order rows arrive in', () => {
    const expense = entry({ id: 2, client_uuid: 'e', captured_at: '2026-10-09T08:05:00Z' });
    const casual = entry({
      id: 1,
      client_uuid: 'c',
      operation: 'CASUAL',
      captured_at: '2026-10-09T08:01:00Z',
    });
    expect(inCaptureOrder([expense, casual]).map((r) => r.client_uuid)).toEqual(['c', 'e']);
  });

  it('breaks a timestamp tie by insertion order and does not mutate its input', () => {
    const rows = [entry({ id: 5, client_uuid: 'b' }), entry({ id: 3, client_uuid: 'a' })];
    expect(inCaptureOrder(rows).map((r) => r.client_uuid)).toEqual(['a', 'b']);
    expect(rows[0].client_uuid).toBe('b');
  });
});

describe('photosReadyToUpload', () => {
  it('holds photos until their entry is applied and has a document id', () => {
    const photos = [photo({})];
    expect(photosReadyToUpload([entry({ status: 'PENDING' })], photos)).toEqual([]);
    expect(photosReadyToUpload([entry({ status: 'APPLIED' })], photos)).toEqual([]);
    expect(photosReadyToUpload([entry({ status: 'REJECTED', document_id: '9' })], photos)).toEqual([]);
  });

  it('targets the expense or the casual by label, with the document id', () => {
    const queue = [
      entry({ id: 1, client_uuid: 'q1', status: 'APPLIED', document_id: '41' }),
      entry({
        id: 2,
        client_uuid: 'q2',
        operation: 'CASUAL',
        status: 'APPLIED',
        document_id: '7',
        captured_at: '2026-10-09T09:00:00Z',
      }),
    ];
    const photos = [
      photo({ id: 2, queue_client_uuid: 'q2', client_uuid: 'p2', caption: 'ID' }),
      photo({ id: 1, queue_client_uuid: 'q1', client_uuid: 'p1' }),
    ];
    const ready = photosReadyToUpload(queue, photos);
    expect(ready.map((r) => [r.photo.client_uuid, r.targetType, r.targetId])).toEqual([
      ['p1', 'commercials.ProjectExpense', '41'],
      ['p2', 'commercials.Casual', '7'],
    ]);
  });

  it('skips photos already uploaded or refused, so a failed one is the only one retried', () => {
    const queue = [entry({ status: 'APPLIED', document_id: '41' })];
    const photos = [
      photo({ id: 1, client_uuid: 'done', status: 'UPLOADED' }),
      photo({ id: 2, client_uuid: 'bad', status: 'FAILED' }),
      photo({ id: 3, client_uuid: 'retry', status: 'QUEUED', attempts: 2 }),
    ];
    expect(photosReadyToUpload(queue, photos).map((r) => r.photo.client_uuid)).toEqual(['retry']);
  });

  it('never sends photos for an allowance request', () => {
    const queue = [entry({ operation: 'ALLOWANCE_REQUEST', status: 'APPLIED', document_id: '3' })];
    expect(photosReadyToUpload(queue, [photo({})])).toEqual([]);
    expect(photoTarget('ALLOWANCE_REQUEST')).toBeNull();
  });
});

describe('isPermanentUploadFailure', () => {
  it('retries transport and server trouble, gives up on a refused file', () => {
    expect([500, 502, 503, 401, 408, 429].some(isPermanentUploadFailure)).toBe(false);
    expect([400, 413, 415, 422].every(isPermanentUploadFailure)).toBe(true);
  });
});

describe('describeEntry and financeEntries', () => {
  it('names an entry in plain words', () => {
    expect(describeEntry('EXPENSE', { amount: '1200' })).toBe('Expense KES 1,200');
    expect(describeEntry('ALLOWANCE_REQUEST', { type: 'NIGHT_OUT', amount: '2000.50' })).toBe(
      'Night-out allowance KES 2,000.5',
    );
    expect(describeEntry('CASUAL', { name: 'Jane Wanjiru' })).toBe('Casual Jane Wanjiru');
  });

  it('lists waiting and refused finance entries, not applied or gate ones', () => {
    const rows = [
      entry({ id: 1, client_uuid: 'a', payload: { amount: '100' } }),
      entry({
        id: 2,
        client_uuid: 'b',
        operation: 'ALLOWANCE_REQUEST',
        status: 'REJECTED',
        exception_reason: 'ALLOWANCE_OVERLAP: overlaps AR-000002',
        payload: { type: 'TRANSPORT', amount: '300' },
      }),
      entry({ id: 3, client_uuid: 'c', status: 'APPLIED' }),
      entry({ id: 4, client_uuid: 'd', operation: 'GATE_IN' }),
    ];
    const list = financeEntries(rows, [photo({ queue_client_uuid: 'a' })]);
    expect(list.map((e) => [e.client_uuid, e.state, e.photos])).toEqual([
      ['a', 'WAITING', 1],
      ['b', 'REFUSED', 0],
    ]);
    expect(list[1].reason).toContain('ALLOWANCE_OVERLAP');
  });

  it('hides a refused entry once a corrected one supersedes it', () => {
    const rows = [
      entry({ id: 1, client_uuid: 'old', status: 'REJECTED', exception_reason: 'x' }),
      entry({ id: 2, client_uuid: 'new', payload: { supersedes_client_uuid: 'old' } }),
    ];
    expect(financeEntries(rows, []).map((e) => e.client_uuid)).toEqual(['new']);
  });
});
