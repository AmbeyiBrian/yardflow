import { describe, expect, it } from 'vitest';

import { aimRegion, pickNearestCentre } from './aimRegion';

describe('aimRegion', () => {
  it('centres a square on a landscape frame', () => {
    expect(aimRegion(1280, 720, 0.4)).toEqual({ sx: 496, sy: 216, size: 288 });
  });

  it('centres a square on a portrait frame', () => {
    expect(aimRegion(720, 1280, 0.4)).toEqual({ sx: 216, sy: 496, size: 288 });
  });

  it('uses the whole short side at fraction 1', () => {
    expect(aimRegion(640, 480, 1)).toEqual({ sx: 80, sy: 0, size: 480 });
  });

  it('stays on whole pixels', () => {
    const r = aimRegion(1001, 777, 0.4);
    expect(Number.isInteger(r.sx) && Number.isInteger(r.sy) && Number.isInteger(r.size)).toBe(true);
  });
});

describe('pickNearestCentre', () => {
  const box = (x: number, y: number) => ({ x, y, width: 20, height: 20 });

  it('chooses the code nearest the centre', () => {
    const found = [
      { rawValue: 'far', boundingBox: box(0, 0) },
      { rawValue: 'near', boundingBox: box(90, 90) },
    ];
    expect(pickNearestCentre(found, 200, 200)?.rawValue).toBe('near');
  });

  it('ignores codes without a box when another can be ranked', () => {
    const found = [{ rawValue: 'blind' }, { rawValue: 'ranked', boundingBox: box(0, 0) }];
    expect(pickNearestCentre(found, 200, 200)?.rawValue).toBe('ranked');
  });

  it('falls back to the first when none has a box', () => {
    expect(pickNearestCentre([{ rawValue: 'a' }, { rawValue: 'b' }], 200, 200)?.rawValue).toBe('a');
  });

  it('returns undefined for no codes', () => {
    expect(pickNearestCentre([], 200, 200)).toBeUndefined();
  });
});
