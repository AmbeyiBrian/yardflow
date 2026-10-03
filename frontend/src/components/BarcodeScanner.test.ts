import { describe, expect, it } from 'vitest';

import { readLabel } from '../features/boxes/readLabel';
import { valueToPass } from './scanValue';

describe('valueToPass', () => {
  it('passes a plain serial trimmed', () => {
    expect(valueToPass(readLabel('  ABC123 '))).toBe('ABC123');
  });

  it('passes the serial found in a labelled line', () => {
    expect(valueToPass(readLabel('SN: ABC123'))).toBe('ABC123');
  });

  it('passes the serial found in a product URL', () => {
    expect(valueToPass(readLabel('https://example.com/p?sn=ABC123'))).toBe('ABC123');
  });

  it('passes the trimmed raw text when there are several serials', () => {
    expect(valueToPass(readLabel(' A1, B2 '))).toBe('A1, B2');
  });

  it('passes the raw text when the label names a box', () => {
    const raw = '{"box":"BX-1","serial":"S1"}';
    expect(valueToPass(readLabel(raw))).toBe(raw);
  });

  it('passes the raw text for a gate-pass address', () => {
    const raw = 'https://yard.example/qr/scan?token=abc';
    expect(valueToPass(readLabel(raw))).toBe(raw);
  });
});
