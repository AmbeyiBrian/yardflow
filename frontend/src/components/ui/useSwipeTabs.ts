/**
 * Swipe between tabs on a touch screen (design §7.3).
 *
 * A yard runs this app on phones. Tapping a tab strip that has scrolled off the
 * right edge means finding it first; a horizontal swipe across the content is
 * what a thumb already wants to do. This turns that gesture into "next tab" or
 * "previous tab" on any screen with a tab strip — Approvals, Network, People,
 * Settings — without each of them re-deriving what counts as a swipe.
 *
 * What counts: a single touch that travels at least `threshold` pixels
 * sideways and clearly more sideways than up or down. Anything else is a
 * scroll and is left alone. Touches that begin on something that scrolls
 * sideways itself — the tab strip, a wide table — are ignored, because the
 * swipe there means "scroll this", and taking it over would break the thing
 * people were actually doing.
 *
 * Mouse users are unaffected: only touch events are read.
 */

import { useCallback, useRef } from 'react';

interface SwipeHandlers {
  onTouchStart: (event: React.TouchEvent<HTMLElement>) => void;
  onTouchEnd: (event: React.TouchEvent<HTMLElement>) => void;
}

/** Mark an element whose own horizontal scrolling must win over a tab swipe. */
export const NO_SWIPE = 'data-no-swipe';

export function useSwipeTabs<T extends string>(
  keys: readonly T[],
  current: T,
  onChange: (next: T) => void,
  { threshold = 56 }: { threshold?: number } = {},
): SwipeHandlers {
  const start = useRef<{ x: number; y: number; ignore: boolean } | null>(null);

  const onTouchStart = useCallback((event: React.TouchEvent<HTMLElement>) => {
    if (event.touches.length !== 1) {
      start.current = null;
      return;
    }
    const touch = event.touches[0];
    const target = event.target as HTMLElement | null;
    // Started on something that scrolls sideways itself: let it scroll.
    const ignore = Boolean(
      target?.closest(`[${NO_SWIPE}], .overflow-x-auto, input, textarea, select`),
    );
    start.current = { x: touch.clientX, y: touch.clientY, ignore };
  }, []);

  const onTouchEnd = useCallback(
    (event: React.TouchEvent<HTMLElement>) => {
      const began = start.current;
      start.current = null;
      if (!began || began.ignore || event.changedTouches.length !== 1) return;

      const touch = event.changedTouches[0];
      const dx = touch.clientX - began.x;
      const dy = touch.clientY - began.y;
      // Sideways, decisively: a diagonal scroll must not change tabs.
      if (Math.abs(dx) < threshold || Math.abs(dx) < Math.abs(dy) * 1.5) return;

      const index = keys.indexOf(current);
      if (index === -1) return;
      // Swipe left (dx < 0) moves to the tab on the right, as in every mobile
      // app people already use.
      const next = keys[dx < 0 ? index + 1 : index - 1];
      if (next !== undefined) onChange(next);
    },
    [keys, current, onChange, threshold],
  );

  return { onTouchStart, onTouchEnd };
}
