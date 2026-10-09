// The area check for clock-in (design §4.18.4; R13). The twin of
// backend/core/geo.py: same semantics, and both are tested against
// shared/area-cases.json so the phone and the server cannot disagree.

export const EARTH_RADIUS_M = 6_371_008.8;

export interface Fix {
  lat?: number | null;
  lng?: number | null;
  accuracy_m?: number | null;
}

export interface Place {
  lat: number;
  lng: number;
  radius_m: number;
}

export type AreaProblem = 'NO_FIX' | 'TOO_VAGUE';

export interface AreaResult {
  distance_m: number | null;
  inside: boolean;
  problem: AreaProblem | null;
}

const rad = (deg: number): number => (deg * Math.PI) / 180;

export function haversineM(lat1: number, lng1: number, lat2: number, lng2: number): number {
  const dPhi = rad(lat2 - lat1);
  const dLambda = rad(lng2 - lng1);
  const a =
    Math.sin(dPhi / 2) ** 2 + Math.cos(rad(lat1)) * Math.cos(rad(lat2)) * Math.sin(dLambda / 2) ** 2;
  return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(a)));
}

const isNumber = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value);

export function checkArea(fix: Fix | null | undefined, place: Place, capM: number): AreaResult {
  if (!fix || !isNumber(fix.lat) || !isNumber(fix.lng) || !isNumber(fix.accuracy_m)) {
    return { distance_m: null, inside: false, problem: 'NO_FIX' };
  }
  if (fix.accuracy_m < 0) return { distance_m: null, inside: false, problem: 'NO_FIX' };

  const distance = haversineM(fix.lat, fix.lng, place.lat, place.lng);
  if (fix.accuracy_m > capM) return { distance_m: distance, inside: false, problem: 'TOO_VAGUE' };
  return { distance_m: distance, inside: distance <= place.radius_m + fix.accuracy_m, problem: null };
}
