// The shared vectors (§4.15.6): the backend's pytest reads the same file, so the
// phone and the server cannot drift apart on what a label says.
import { describe, expect, it } from 'vitest';
import vectors from '../../../../backend/stock/tests/data/label_vectors.json';
import { readLabel } from './readLabel';

interface Vector {
  name: string;
  raw: string;
  serials: string[];
  box_code: string | null;
  document_token: string | null;
}

describe('readLabel against the shared vectors', () => {
  for (const v of vectors as Vector[]) {
    it(v.name, () => {
      const got = readLabel(v.raw);
      expect(got.serials).toEqual(v.serials);
      expect(got.boxCode).toBe(v.box_code);
      expect(got.documentToken).toBe(v.document_token);
      expect(got.raw).toBe(v.raw);
    });
  }
});

describe('readLabel direct cases', () => {
  it('keeps the raw value untrimmed', () => {
    expect(readLabel('  A1 \n').raw).toBe('  A1 \n');
  });

  it('does not trim a non-breaking space off the outside of a serial', () => {
    // Only the six ASCII blanks are trimmed; the fallback keeps the NBSP.
    expect(readLabel(' AB').serials).toEqual([' AB']);
  });

  it('never reads a boolean, null or nested value as a serial', () => {
    expect(readLabel('{"sn": null}').serials).toEqual(['{"sn": null}']);
    expect(readLabel('{"sn": {"a": 1}}').serials).toEqual(['{"sn": {"a": 1}}']);
    expect(readLabel('{"sn": 42}').serials).toEqual(['42']);
  });

  it('treats a scan link with an empty token as no gate pass', () => {
    expect(readLabel('https://y.example/qr/scan?token=').documentToken).toBeNull();
  });

  it('decodes + and percent escapes in query values, and keeps bad escapes', () => {
    expect(readLabel('https://e.com/p?sn=A+B%2F1').serials).toEqual(['A B/1']);
    expect(readLabel('https://e.com/units/100%zz').serials).toEqual(['100%zz']);
  });

  it('takes the highest-priority URL key and all of its values', () => {
    expect(readLabel('https://e.com/p?s=3&sn=1&sn=2').serials).toEqual(['1', '2']);
  });

  it('is case-sensitive when de-duplicating', () => {
    expect(readLabel('a1,A1').serials).toEqual(['a1', 'A1']);
  });

  it('returns nothing for null and undefined', () => {
    expect(readLabel(null).serials).toEqual([]);
    expect(readLabel(undefined).raw).toBe('');
  });
});
