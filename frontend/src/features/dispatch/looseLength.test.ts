import { describe, expect, it } from 'vitest';

import { drumOrLooseMessage, lengthLabel, lineTrackingMode } from './looseLength';

const none = { scanned: null, looseLength: false, untagged: false } as const;

describe('gate-out cable line', () => {
  it('is bulk when loose length is chosen for a cable item', () => {
    expect(lineTrackingMode({ ...none, itemMode: 'REEL', looseLength: true })).toBe('BULK');
  });

  it('stays a drum line when a drum is scanned, even if loose was left on', () => {
    expect(
      lineTrackingMode({ ...none, itemMode: 'REEL', scanned: 'reel', looseLength: true }),
    ).toBe('REEL');
  });

  it('ignores loose length for other items', () => {
    expect(lineTrackingMode({ ...none, itemMode: 'SERIALIZED', looseLength: true })).toBe(
      'SERIALIZED',
    );
    expect(lineTrackingMode({ ...none, itemMode: 'BULK' })).toBe('BULK');
  });

  it('keeps the other rules', () => {
    expect(lineTrackingMode({ ...none, itemMode: 'SERIALIZED', scanned: 'unit' })).toBe(
      'SERIALIZED',
    );
    expect(lineTrackingMode({ ...none, itemMode: 'SERIALIZED', untagged: true })).toBe('BULK');
  });

  it('words the field and the prompt', () => {
    expect(lengthLabel('m')).toBe('Length (m)');
    expect(lengthLabel(undefined)).toBe('Length');
    expect(drumOrLooseMessage('Fibre')).toContain('Loose length');
  });
});
