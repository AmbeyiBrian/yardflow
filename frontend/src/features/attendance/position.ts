/**
 * One position read for clock-in (design §4.18.4; R13).
 *
 * Asked once, on demand, never watched: high accuracy, no cached position
 * (`maximumAge: 0` — a fix from an hour ago would say where the phone was, not
 * where it is) and a 15 s timeout. Failures are typed so the card can say
 * "Turn location on to clock in" instead of a browser's wording.
 */

import type { Fix } from './types';

export type PositionErrorCode = 'DENIED' | 'UNAVAILABLE' | 'TIMEOUT' | 'UNSUPPORTED';

export class PositionError extends Error {
  readonly code: PositionErrorCode;
  constructor(code: PositionErrorCode) {
    super(code);
    this.name = 'PositionError';
    this.code = code;
  }
}

export const POSITION_OPTIONS: PositionOptions = {
  enableHighAccuracy: true,
  maximumAge: 0,
  timeout: 15_000,
};

/** The browser's numeric codes: 1 denied, 2 unavailable, 3 timeout. */
export function toPositionError(error: { code?: number } | null | undefined): PositionError {
  switch (error?.code) {
    case 1:
      return new PositionError('DENIED');
    case 3:
      return new PositionError('TIMEOUT');
    default:
      return new PositionError('UNAVAILABLE');
  }
}

export function readPosition(): Promise<Fix> {
  return new Promise((resolve, reject) => {
    if (typeof navigator === 'undefined' || !navigator.geolocation) {
      reject(new PositionError('UNSUPPORTED'));
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (p) =>
        resolve({
          lat: p.coords.latitude,
          lng: p.coords.longitude,
          accuracy_m: Math.round(p.coords.accuracy),
        }),
      (e) => reject(toPositionError(e)),
      POSITION_OPTIONS,
    );
  });
}

/** What to tell the person when the read failed. */
export function positionMessage(code: PositionErrorCode): string {
  switch (code) {
    case 'DENIED':
      return 'Turn location on to clock in. Allow location for YardFlow in your phone or browser settings.';
    case 'TIMEOUT':
      return 'Your phone could not find its position in time. Step outside if you are indoors, then try again.';
    case 'UNSUPPORTED':
      return 'This device cannot give a position, so it cannot clock in.';
    default:
      return 'Turn location on to clock in. Your phone could not find its position.';
  }
}
