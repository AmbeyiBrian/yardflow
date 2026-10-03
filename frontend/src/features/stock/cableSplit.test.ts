import { describe, expect, it } from 'vitest';

import { cableSplitText } from './cableSplit';

describe('cable split', () => {
  it('says drums and loose', () => {
    expect(cableSplitText({ on_drums: '1000.000', loose: '240.000', uom: 'm' })).toBe(
      '1,000 m on drums · 240 m loose',
    );
  });

  it('keeps a fraction and shows zeros', () => {
    expect(cableSplitText({ on_drums: '0', loose: '12.50', uom: 'm' })).toBe(
      '0 m on drums · 12.5 m loose',
    );
  });

  it('says nothing for rows without a split', () => {
    expect(cableSplitText({ uom: 'ea' })).toBeNull();
    expect(cableSplitText({ on_drums: null, loose: null, uom: 'ea' })).toBeNull();
  });
});
