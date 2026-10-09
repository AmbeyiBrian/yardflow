/**
 * Coordinate parsing and validation for site and location forms
 * (R13; design §4.18.2, §4.18.8).
 *
 * Pure, so the rules the form shows match the rules the server enforces and can
 * be tested without a browser: latitude -90..90, longitude -180..180, a radius
 * of 20..2000 m (the Site CHECK, §4.18.2), and the "required" rule of §4.18.8.
 */

export const DEFAULT_RADIUS_M = 200;
export const MIN_RADIUS_M = 20;
export const MAX_RADIUS_M = 2000;

/** Matches the server's decimal(9,6): six decimal places. */
const DECIMAL_PLACES = 6;

/**
 * Read one coordinate typed or pasted by a person. Accepts a comma as the
 * decimal mark and surrounding spaces; returns null for anything else.
 */
export function parseCoordinate(raw: string, axis: 'lat' | 'lng'): number | null {
  const text = raw.trim().replace(',', '.');
  if (!/^[+-]?\d+(\.\d+)?$/.test(text)) return null;
  const value = Number(text);
  const limit = axis === 'lat' ? 90 : 180;
  return Math.abs(value) <= limit ? value : null;
}

/** The string sent to the API: six decimal places, as the column stores them. */
export function formatCoordinate(value: number): string {
  return value.toFixed(DECIMAL_PLACES);
}

/** Which places must have coordinates (§4.18.8). `is_system` rows are exempt. */
export function coordinatesRequired(
  kind: 'site' | 'location',
  locationType?: string,
  isSystem = false,
): boolean {
  if (isSystem) return false;
  if (kind === 'site') return true;
  return locationType === 'YARD' || locationType === 'OFFICE';
}

/** Does a place of this kind and type carry an area at all? */
export function hasArea(kind: 'site' | 'location', locationType?: string): boolean {
  return kind === 'site' || locationType === 'YARD' || locationType === 'OFFICE';
}

export interface CoordinateErrors {
  latitude?: string;
  longitude?: string;
  radius_m?: string;
}

export interface CoordinateInput {
  latitude: string;
  longitude: string;
  radius_m: string;
}

/** Messages for the form. An empty object means the three boxes are acceptable. */
export function validateCoordinates(input: CoordinateInput, required: boolean): CoordinateErrors {
  const errors: CoordinateErrors = {};
  const lat = input.latitude.trim();
  const lng = input.longitude.trim();

  if (lat === '' && lng === '') {
    if (required) {
      errors.latitude = 'Latitude is required.';
      errors.longitude = 'Longitude is required.';
    }
  } else {
    if (lat === '') errors.latitude = 'Enter the latitude too.';
    else if (parseCoordinate(lat, 'lat') === null) errors.latitude = 'Latitude is a number from -90 to 90.';
    if (lng === '') errors.longitude = 'Enter the longitude too.';
    else if (parseCoordinate(lng, 'lng') === null) errors.longitude = 'Longitude is a number from -180 to 180.';
  }

  const radius = Number(input.radius_m.trim());
  if (input.radius_m.trim() === '' || !Number.isInteger(radius) || radius < MIN_RADIUS_M || radius > MAX_RADIUS_M) {
    errors.radius_m = `Radius is a whole number of metres from ${MIN_RADIUS_M} to ${MAX_RADIUS_M}.`;
  }
  return errors;
}

/**
 * The API fields for a valid input. Blank coordinates go as null (both or
 * neither, §4.18.2); callers validate first.
 */
export function coordinatePayload(input: CoordinateInput): {
  latitude: string | null;
  longitude: string | null;
  radius_m: number;
} {
  const lat = parseCoordinate(input.latitude, 'lat');
  const lng = parseCoordinate(input.longitude, 'lng');
  const both = lat !== null && lng !== null;
  return {
    latitude: both ? formatCoordinate(lat) : null,
    longitude: both ? formatCoordinate(lng) : null,
    radius_m: Number(input.radius_m),
  };
}
