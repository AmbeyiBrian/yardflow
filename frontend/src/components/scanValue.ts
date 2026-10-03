/**
 * What a scanner hands to `onScan` (P3, design §4.15.6).
 *
 * Screens that know nothing about boxes or passes keep receiving one string, so
 * a supplier's GS1 or URL label arrives as the serial inside it. Anything the
 * reader found beyond a single serial (a box code, a pass token, a list) is
 * passed as the trimmed raw text: the screen cannot act on it, and the backend
 * lookup runs the same reader, so it still resolves. Screens that can act on it
 * listen to `onRead` instead.
 */

import type { LabelReading } from '../features/boxes/readLabel';

export function valueToPass(reading: LabelReading): string {
  if (reading.serials.length === 1 && !reading.boxCode && !reading.documentToken) {
    return reading.serials[0];
  }
  return reading.raw.trim();
}
