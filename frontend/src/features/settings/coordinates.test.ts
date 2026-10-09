import { describe, expect, it } from 'vitest';

import {
  coordinatePayload,
  coordinatesRequired,
  formatCoordinate,
  hasArea,
  parseCoordinate,
  validateCoordinates,
} from './coordinates';

describe('parseCoordinate', () => {
  it('reads plain, signed and comma-decimal values', () => {
    expect(parseCoordinate(' -1.286389 ', 'lat')).toBe(-1.286389);
    expect(parseCoordinate('36,817223', 'lng')).toBe(36.817223);
  });
  it('refuses text and out-of-range values', () => {
    expect(parseCoordinate('abc', 'lat')).toBeNull();
    expect(parseCoordinate('', 'lat')).toBeNull();
    expect(parseCoordinate('91', 'lat')).toBeNull();
    expect(parseCoordinate('181', 'lng')).toBeNull();
    expect(parseCoordinate('100', 'lng')).toBe(100);
  });
});

describe('required rule (4.18.8)', () => {
  it('requires sites, YARD and OFFICE; not stores, vehicles or system rows', () => {
    expect(coordinatesRequired('site')).toBe(true);
    expect(coordinatesRequired('location', 'YARD')).toBe(true);
    expect(coordinatesRequired('location', 'OFFICE')).toBe(true);
    expect(coordinatesRequired('location', 'STORE')).toBe(false);
    expect(coordinatesRequired('location', 'VEHICLE')).toBe(false);
    expect(coordinatesRequired('location', 'YARD', true)).toBe(false);
  });
  it('shows the area block only where one exists', () => {
    expect(hasArea('location', 'STORE')).toBe(false);
    expect(hasArea('location', 'OFFICE')).toBe(true);
    expect(hasArea('site')).toBe(true);
  });
});

describe('validateCoordinates', () => {
  const ok = { latitude: '-1.29', longitude: '36.82', radius_m: '200' };
  it('accepts a good set', () => {
    expect(validateCoordinates(ok, true)).toEqual({});
  });
  it('flags blanks only when required', () => {
    const blank = { ...ok, latitude: '', longitude: '' };
    expect(validateCoordinates(blank, true).latitude).toBeDefined();
    expect(validateCoordinates(blank, false)).toEqual({});
  });
  it('flags one without the other and bad ranges', () => {
    expect(validateCoordinates({ ...ok, longitude: '' }, false).longitude).toBeDefined();
    expect(validateCoordinates({ ...ok, latitude: '95' }, true).latitude).toBeDefined();
  });
  it('keeps the radius within 20-2000 whole metres', () => {
    expect(validateCoordinates({ ...ok, radius_m: '10' }, true).radius_m).toBeDefined();
    expect(validateCoordinates({ ...ok, radius_m: '2001' }, true).radius_m).toBeDefined();
    expect(validateCoordinates({ ...ok, radius_m: '20.5' }, true).radius_m).toBeDefined();
    expect(validateCoordinates({ ...ok, radius_m: '' }, true).radius_m).toBeDefined();
  });
});

describe('coordinatePayload', () => {
  it('rounds to six places and sends the radius as a number', () => {
    expect(coordinatePayload({ latitude: '-1.2863891234', longitude: '36,8', radius_m: '150' })).toEqual({
      latitude: '-1.286389',
      longitude: '36.800000',
      radius_m: 150,
    });
    expect(formatCoordinate(1)).toBe('1.000000');
  });
  it('sends null for blanks', () => {
    expect(coordinatePayload({ latitude: '', longitude: '', radius_m: '200' }).latitude).toBeNull();
  });
});
