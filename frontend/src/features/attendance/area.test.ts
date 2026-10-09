import { describe, expect, it } from 'vitest';

import cases from '../../../../shared/area-cases.json';
import { checkArea, haversineM, type Fix, type Place } from './area';

interface Case {
  name: string;
  fix: Fix | null;
  place: Place;
  cap_m: number;
  expected: { distance_m: number | null; inside: boolean; problem: string | null };
}

// The same file backend/core/tests/test_geo.py reads (design §4.18.4).
describe('checkArea against shared/area-cases.json', () => {
  for (const c of cases.cases as Case[]) {
    it(c.name, () => {
      const result = checkArea(c.fix, c.place, c.cap_m);
      expect(result.inside).toBe(c.expected.inside);
      expect(result.problem).toBe(c.expected.problem);
      if (c.expected.distance_m === null) {
        expect(result.distance_m).toBeNull();
      } else {
        expect(result.distance_m).not.toBeNull();
        expect(Math.abs((result.distance_m as number) - c.expected.distance_m)).toBeLessThan(0.01);
      }
    });
  }
});

describe('haversineM', () => {
  it('is about 111.2 km for a degree of latitude', () => {
    expect(Math.abs(haversineM(0, 0, 1, 0) - 111_195)).toBeLessThan(5);
  });

  it('is zero at a point', () => {
    expect(haversineM(10, 20, 10, 20)).toBe(0);
  });
});
