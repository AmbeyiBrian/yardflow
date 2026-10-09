/**
 * Places nearest first, for the clock-in card (design §4.18.11; R13).
 *
 * The distance shown here is for choosing; the server's check at clock-in is
 * the one that counts (4.18.4).
 */

import type { ClockPlace, Fix } from './types';

const EARTH_M = 6_371_008.8;

export function distanceM(a: { lat: number; lng: number }, b: { lat: number; lng: number }): number {
  const rad = Math.PI / 180;
  const dLat = (b.lat - a.lat) * rad;
  const dLng = (b.lng - a.lng) * rad;
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLng / 2) ** 2;
  return 2 * EARTH_M * Math.asin(Math.min(1, Math.sqrt(h)));
}

export interface NearbyPlace extends ClockPlace {
  distance_m: number;
}

/** Nearest first; ties keep name order so the list does not jump around. */
export function sortNearby(fix: Fix, places: readonly ClockPlace[]): NearbyPlace[] {
  return places
    .map((p) => ({ ...p, distance_m: Math.round(distanceM(fix, p)) }))
    .sort((a, b) => a.distance_m - b.distance_m || a.name.localeCompare(b.name));
}

/** The nearest place, when the person is inside its area; otherwise none. */
export function suggest(sorted: readonly NearbyPlace[]): NearbyPlace | undefined {
  const first = sorted[0];
  return first && first.distance_m <= first.radius_m ? first : undefined;
}

export function formatDistance(metres: number): string {
  if (metres < 1000) return `${Math.round(metres)} m`;
  return `${(metres / 1000).toFixed(metres < 10_000 ? 1 : 0)} km`;
}

/** "1 h 05 min" for a span in milliseconds. */
export function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 60_000));
  const h = Math.floor(total / 60);
  const m = total % 60;
  return h > 0 ? `${h} h ${String(m).padStart(2, '0')} min` : `${m} min`;
}
