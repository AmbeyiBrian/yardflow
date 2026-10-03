import { describe, expect, it } from 'vitest';

import { decodeTokenPayload, looksLikePassNumber, normalisePassNumber } from './passScan';

// Generated with django.core.signing.dumps(..., salt='dispatch.document.qr').
const GATE_OUT =
  'eyJ0eXBlIjoiZGlzcGF0Y2guR2F0ZU91dCIsImlkIjoiMTIiLCJvcmciOiIzIn0:1xCtIi:g_45XbchY4Mue1mGM5vumijbnZJImWfg7abcO0IFbAw';
const GATE_IN =
  'eyJ0eXBlIjoicmVjZWl2aW5nLkdhdGVJbiIsImlkIjoiNyIsIm9yZyI6IjMifQ:1xCtIi:qqIxpaR53WQALqyQvNEHmDxKlWd7XhWCDZB5MkSyCaM';
// The gate-out payload plus padding, with compress=True (a leading dot).
const COMPRESSED =
  '.eJyrViqpLEhVslJKySwuSCxJztBzTyxJ9S8tUdJRykwBihsaAVn5RelApjGQVZAIEqygMlCqBQAinjXe:1xCtIn:AefBlxk6_QMEfJrJf5e56tjYhLrnMj0jeYGrAPbq-4w';

describe('decodeTokenPayload', () => {
  it('reads a gate-out token', async () => {
    expect(await decodeTokenPayload(GATE_OUT)).toEqual({
      type: 'dispatch.GateOut',
      id: '12',
      org: '3',
    });
  });

  it('reads a delivery note token', async () => {
    expect((await decodeTokenPayload(GATE_IN))?.type).toBe('receiving.GateIn');
  });

  it('inflates a compressed token', async () => {
    expect(await decodeTokenPayload(COMPRESSED)).toMatchObject({
      type: 'dispatch.GateOut',
      id: '12',
    });
  });

  it('gives null for rubbish rather than throwing', async () => {
    expect(await decodeTokenPayload('')).toBeNull();
    expect(await decodeTokenPayload('not a token')).toBeNull();
    expect(await decodeTokenPayload('bm90LWpzb24:x:y')).toBeNull();
    expect(await decodeTokenPayload('.AAAA:x:y')).toBeNull();
  });
});

describe('looksLikePassNumber', () => {
  it('accepts issued numbers, with any tenant prefix', () => {
    expect(looksLikePassNumber('GP-000042')).toBe(true);
    expect(looksLikePassNumber(' gp-42 ')).toBe(true);
    expect(looksLikePassNumber('OUT-0007')).toBe(true);
  });

  it('refuses serials and other text', () => {
    expect(looksLikePassNumber('HW-RRU-88001')).toBe(false);
    expect(looksLikePassNumber('GP000042')).toBe(false);
    expect(looksLikePassNumber('')).toBe(false);
    expect(looksLikePassNumber('hello world')).toBe(false);
  });
});

describe('normalisePassNumber', () => {
  it('trims and upper-cases', () => {
    expect(normalisePassNumber(' gp-000042 ')).toBe('GP-000042');
  });
});
