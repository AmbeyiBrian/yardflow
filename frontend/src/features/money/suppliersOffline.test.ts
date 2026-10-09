import { describe, expect, it } from 'vitest';

import type { QueuedMutation, QueuedPhoto } from '../../offline/db';
import {
  describeEntry,
  financeEntries,
  heldBehindRefusedSupplier,
  inCaptureOrder,
  photosReadyToUpload,
} from '../../offline/financeQueue';
import { bundleReceivable, bundleVehicles } from './bundle';
import { purchaseBody, needsQueue } from './purchasesOffline';
import { queuedPurchaseCards, queuedSupplierCards } from './queued';
import {
  gateInSupplierFields,
  mergeSupplierOptions,
  queuedSupplierOptions,
  supplierBody,
  supplierOptions,
  supplierRef,
  supplierValue,
} from './suppliersOffline';

function row(over: Partial<QueuedMutation>): QueuedMutation {
  return {
    id: 1,
    client_uuid: 'q1',
    operation: 'SUPPLIER',
    payload: {},
    captured_at: '2026-10-09T08:00:00Z',
    status: 'PENDING',
    attempts: 0,
    ...over,
  };
}

describe('supplier options (R15, §4.20.8)', () => {
  it('never offers a REJECTED supplier and labels a PENDING one', () => {
    const options = supplierOptions([
      { id: 1, name: 'Acme', status: 'APPROVED' },
      { id: 2, name: 'Newco', status: 'PENDING' },
      { id: 3, name: 'Bad', status: 'REJECTED' },
    ]);
    expect(options.map((o) => o.label)).toEqual(['Acme', 'Newco (awaiting approval)']);
  });

  it('lists a supplier waiting on this phone, but not a refused one', () => {
    const entries = financeEntries(
      [
        row({ id: 1, client_uuid: 'a', payload: { name: 'Hardware Ltd' } }),
        row({ id: 2, client_uuid: 'b', status: 'REJECTED', payload: { name: 'Dup' } }),
      ],
      [],
    );
    const options = queuedSupplierOptions(entries);
    expect(options).toEqual([
      { value: 'q:a', label: 'Hardware Ltd (waiting to send)', name: 'Hardware Ltd', queued: true },
    ]);
  });

  it('merges without repeating a value', () => {
    const a = supplierOptions([{ id: 1, name: 'Acme', status: 'APPROVED' }]);
    expect(mergeSupplierOptions(a, a)).toHaveLength(1);
  });

  it('names a server supplier by id and a queued one by supplier_client_uuid', () => {
    expect(supplierRef('12')).toEqual({ supplier: 12 });
    expect(supplierRef('q:abc')).toEqual({ supplier: null, supplier_client_uuid: 'abc' });
    expect(supplierRef('')).toEqual({ supplier: null });
    expect(gateInSupplierFields('q:abc', 'Hardware Ltd')).toEqual({
      supplier: null,
      supplier_client_uuid: 'abc',
      supplier_name: 'Hardware Ltd',
    });
    expect(supplierValue({ supplier_client_uuid: 'abc' })).toBe('q:abc');
    expect(supplierValue({ supplier: 7 })).toBe('7');
  });

  it('builds the SUPPLIER payload trimmed, with blanks dropped and the PIN upper-cased', () => {
    expect(supplierBody({ name: ' Hardware Ltd ', phone: ' ', kra_pin: 'p051234567z' })).toEqual({
      name: 'Hardware Ltd',
      kra_pin: 'P051234567Z',
    });
  });
});

describe('queue ordering and the supplier dependency (§4.20.8)', () => {
  const supplier = row({ id: 2, client_uuid: 's', captured_at: '2026-10-09T08:01:00Z', payload: { name: 'X' } });
  const gateIn = row({
    id: 1,
    client_uuid: 'g',
    operation: 'GATE_IN',
    captured_at: '2026-10-09T08:05:00Z',
    payload: { supplier: null, supplier_client_uuid: 's' },
  });
  const purchase = row({
    id: 3,
    client_uuid: 'p',
    operation: 'SITE_PURCHASE',
    captured_at: '2026-10-09T08:06:00Z',
    payload: { supplier_client_uuid: 's' },
  });

  it('replays the supplier before the gate-in and purchase that name it', () => {
    expect(inCaptureOrder([purchase, gateIn, supplier]).map((r) => r.client_uuid)).toEqual([
      's',
      'g',
      'p',
    ]);
  });

  it('holds dependents while their supplier is refused, and releases them once it is not', () => {
    const refused = { ...supplier, status: 'REJECTED' as const };
    const held = heldBehindRefusedSupplier([gateIn, purchase], [refused, gateIn, purchase]);
    expect([...held].map((r) => r.client_uuid)).toEqual(['g', 'p']);
    expect(heldBehindRefusedSupplier([gateIn], [supplier, gateIn]).size).toBe(0);
  });

  it('does not hold a row that names a server supplier', () => {
    const plain = row({ client_uuid: 'x', operation: 'SITE_PURCHASE', payload: { supplier: 4 } });
    expect(heldBehindRefusedSupplier([plain], [{ ...supplier, status: 'REJECTED' }, plain]).size).toBe(0);
  });
});

describe('queued purchases (§4.19.11, R6)', () => {
  const body = purchaseBody(
    {
      project: 1,
      site: 2,
      purchase_date: '2026-10-09',
      destination: 'USED_AT_SITE',
      receive_into: null,
      lines: [
        { item_type: null, description: 'Cement', quantity: '10', unit_price: '750.50' },
        { item_type: 5, description: '', quantity: '2', unit_price: '100' },
      ],
      photos_expected: 1,
    },
    'q:s',
  );

  it('carries the supplier by uuid and must be queued even online', () => {
    expect(body.supplier).toBeNull();
    expect(body.supplier_client_uuid).toBe('s');
    expect(needsQueue(body)).toBe(true);
    const { supplier_client_uuid: _gone, ...rest } = body;
    expect(needsQueue(purchaseBody(rest, '9'))).toBe(false);
  });

  it('is described and listed with its lines total, waiting or refused', () => {
    const queue = [
      row({ id: 1, client_uuid: 'p', operation: 'SITE_PURCHASE', payload: body }),
      row({
        id: 2,
        client_uuid: 'r',
        operation: 'SITE_PURCHASE',
        status: 'REJECTED',
        exception_reason: 'SUPPLIER_REJECTED: no',
        payload: body,
        captured_at: '2026-10-09T08:10:00Z',
      }),
    ];
    expect(describeEntry('SITE_PURCHASE', body)).toBe('Purchase KES 7,705');
    const cards = queuedPurchaseCards(financeEntries(queue, []));
    expect(cards.map((c) => [c.refused, c.amount])).toEqual([
      [false, '7705'],
      [true, '7705'],
    ]);
    expect(cards[1].fixTo).toBe('/money/purchases/new?resend=r');
    expect(cards[1].reason).toBe('SUPPLIER_REJECTED: no');
  });

  it('lists a refused supplier with a way to fix it', () => {
    const cards = queuedSupplierCards(
      financeEntries(
        [row({ client_uuid: 's', status: 'REJECTED', exception_reason: 'SUPPLIER_DUPLICATE: x', payload: { name: 'Dup' } })],
        [],
      ),
    );
    expect(cards[0]).toMatchObject({ title: 'Supplier Dup', refused: true, fixTo: '/money/suppliers/new?resend=s' });
  });

  it('uploads receipt photos to the purchase once it has landed, and not before', () => {
    const photo: QueuedPhoto = {
      id: 1,
      queue_client_uuid: 'p',
      caption: 'Receipt',
      blob: new Blob(['x']),
      filename: 'r.jpg',
      client_uuid: 'ph1',
      status: 'QUEUED',
      attempts: 0,
    };
    const waiting = row({ client_uuid: 'p', operation: 'SITE_PURCHASE' });
    expect(photosReadyToUpload([waiting], [photo])).toEqual([]);
    const landed = { ...waiting, status: 'APPLIED' as const, document_id: '77' };
    expect(photosReadyToUpload([landed], [photo])).toEqual([
      { photo, targetType: 'commercials.SitePurchase', targetId: '77' },
    ]);
  });
});

describe('the bundle (§4.20.8)', () => {
  it('keeps vehicles and generators only', () => {
    expect(
      bundleVehicles([
        { id: 1, tag: 'KDA 1A', name: 'Hilux', type: 'VEHICLE' },
        { id: 2, tag: 'G1', name: 'Genset', type: 'GENERATOR' },
        { id: 3, tag: 'T1', name: 'Drill', type: 'TOOL' },
      ]).map((v) => v.id),
    ).toEqual([1, 2]);
  });

  it('offers yards and stores to receive into', () => {
    expect(
      bundleReceivable([
        { id: 1, name: 'Main yard', type: 'YARD' },
        { id: 2, name: 'Site cabin', type: 'SITE' },
        { id: 3, name: 'Store', type: 'STORE' },
      ]),
    ).toEqual([
      { id: 1, label: 'Main yard' },
      { id: 3, label: 'Store' },
    ]);
  });
});
