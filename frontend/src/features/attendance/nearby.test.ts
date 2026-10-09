import { describe, expect, it } from 'vitest';

import { distanceM, formatDistance, formatElapsed, sortNearby, suggest } from './nearby';
import { toPositionError } from './position';
import type { ClockPlace } from './types';

const place = (id: number, name: string, lat: number, lng: number, radius_m = 200): ClockPlace => ({
  kind: 'site',
  id,
  name,
  label: 'Site',
  lat,
  lng,
  radius_m,
});

const fix = { lat: -1.2921, lng: 36.8219, accuracy_m: 10 };

describe('nearby', () => {
  it('measures about 111 m per 0.001 degree of latitude', () => {
    const d = distanceM({ lat: 0, lng: 0 }, { lat: 0.001, lng: 0 });
    expect(d).toBeGreaterThan(110);
    expect(d).toBeLessThan(112);
  });

  it('sorts nearest first and keeps name order on a tie', () => {
    const sorted = sortNearby(fix, [
      place(1, 'Far', -1.3, 36.9),
      place(3, 'B', -1.2921, 36.8229),
      place(2, 'A', -1.2921, 36.8229),
      place(4, 'Here', -1.2921, 36.8219),
    ]);
    expect(sorted.map((p) => p.name)).toEqual(['Here', 'A', 'B', 'Far']);
  });

  it('suggests the nearest only when inside its area', () => {
    expect(suggest(sortNearby(fix, [place(1, 'Here', -1.2921, 36.8225)]))?.name).toBe('Here');
    expect(suggest(sortNearby(fix, [place(1, 'Away', -1.3, 36.9)]))).toBeUndefined();
    expect(suggest([])).toBeUndefined();
  });

  it('words distances and elapsed time', () => {
    expect(formatDistance(80)).toBe('80 m');
    expect(formatDistance(1500)).toBe('1.5 km');
    expect(formatElapsed(65 * 60_000)).toBe('1 h 05 min');
    expect(formatElapsed(-5)).toBe('0 min');
  });

  it('types geolocation failures', () => {
    expect(toPositionError({ code: 1 }).code).toBe('DENIED');
    expect(toPositionError({ code: 2 }).code).toBe('UNAVAILABLE');
    expect(toPositionError({ code: 3 }).code).toBe('TIMEOUT');
    expect(toPositionError(undefined).code).toBe('UNAVAILABLE');
  });
});
