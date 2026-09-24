/**
 * Swipe between tabs on a touch screen (design §7.3).
 *
 * A yard runs this app on phones. Tapping a tab strip that has scrolled off the
 * right edge means finding it first; a horizontal swipe across the content is
 * what a thumb already wants to do. This turns that gesture into "next tab" or
 * "previous tab" on any screen with a tab strip — Approvals, Network, People,
 * Settings — without each of them re-deriving what counts as a swipe.
 *
 * It has to *feel* like a swipe, not a tap that happened to be sideways. So:
 *
 * * While the finger is down the pane follows it (`SwipePane`, its own file), with a little
 *   resistance past the first or last tab so the edge is felt rather than read.
 * * Let go short of the threshold and the pane springs back.
 * * Let go past it and the next pane slides in from the side the finger was
 *   heading, whether the change came from the swipe or from a tap on the strip.
 *
 * What counts: a single touch that travels at least `threshold` pixels
 * sideways and clearly more sideways than up or down. Anything else is a
 * scroll and is left alone. Touches that begin on something that scrolls
 * sideways itself — the tab strip, a wide table — are ignored, because the
 * swipe there means "scroll this", and taking it over would break the thing
 * people were actually doing.
 *
 * The pane is moved by writing to its style directly, not through React state:
 * a touch reports at up to 120 times a second and re-rendering a queue of
 * approval cards at that rate is exactly the stutter this is meant to remove.
 * The transform is removed the moment the gesture or the entry ends, because a
 * transformed ancestor becomes the containing block for `position: fixed`
 * descendants and every sheet in this app is one.
 *
 * Two tabbed screens can nest: Network and People are panes of Settings, and
 * both have tabs of their own. A touch bubbles through both wrappers, and
 * without a rule both would act — the inner moving to the next Network tab, the
 * outer to the next settings pane, and the outer's navigation winning. The
 * innermost wrapper owns the gesture: it claims the touch as it starts, and an
 * outer wrapper that sees a claimed touch leaves it alone.
 *
 * Mouse users are unaffected: only touch events are read. Somebody who has
 * asked for reduced motion gets the tab change with neither the drag nor the
 * slide.
 */

import { useCallback, useRef, useState, type RefObject, type TouchEvent } from 'react';

/** Mark an element whose own horizontal scrolling must win over a tab swipe. */
export const NO_SWIPE = 'data-no-swipe';

/** How far a finger may wander before the gesture is judged sideways or not. */
const SLOP = 10;

/** How far a clearly vertical movement must go before the touch is written off as a scroll. */
const SCROLL_LOCK = 24;

/** Touches an inner tabbed screen has already taken; an outer one must not act on them. */
const claimed = new WeakSet<Event>();

/** How much of the finger's travel the pane follows past the last tab. */
const EDGE_RESISTANCE = 0.25;

interface Gesture {
  x: number;
  y: number;
  /** Began on something that scrolls sideways itself, or turned into a scroll. */
  ignore: boolean;
  /** Decided as a sideways drag; the pane is following the finger. */
  dragging: boolean;
}

export type Direction = 'left' | 'right';

export interface SwipeTabs<T extends string> {
  /** Spread onto the element that wraps the strip and the pane. */
  handlers: {
    onTouchStart: (event: TouchEvent<HTMLElement>) => void;
    onTouchMove: (event: TouchEvent<HTMLElement>) => void;
    onTouchEnd: (event: TouchEvent<HTMLElement>) => void;
    onTouchCancel: (event: TouchEvent<HTMLElement>) => void;
  };
  /** Spread onto `SwipePane`. Kept as one bag so the screens need not know its parts. */
  pane: SwipePaneProps<T>;
}

export interface SwipePaneProps<T extends string> {
  /** The tab whose pane is showing; `SwipePane` remounts on it. */
  tab: T;
  /** Which side the current pane arrived from, or none on first paint. */
  enteredFrom: Direction | null;
  paneRef: RefObject<HTMLDivElement | null>;
}

function reducedMotion() {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  );
}

/** Put the pane back where it was, animated or not. */
function release(element: HTMLDivElement | null, animate: boolean) {
  if (!element) return;
  element.style.transition = animate ? 'transform 200ms ease-out, opacity 200ms ease-out' : '';
  element.style.transform = '';
  element.style.opacity = '';
  if (animate) {
    element.addEventListener(
      'transitionend',
      () => {
        element.style.transition = '';
      },
      { once: true },
    );
  }
}

export function useSwipeTabs<T extends string>(
  keys: readonly T[],
  current: T,
  onChange: (next: T) => void,
  { threshold = 56 }: { threshold?: number } = {},
): SwipeTabs<T> {
  const gesture = useRef<Gesture | null>(null);
  const pane = useRef<HTMLDivElement | null>(null);

  // Which way the pane just arrived from, judged by where the previous tab sat
  // in the strip. A tap two tabs to the right still slides in from the right.
  // Tracked as state derived during render, so no ref is read while rendering.
  const index = keys.indexOf(current);
  const [arrival, setArrival] = useState<{ tab: T; from: Direction | null }>({
    tab: current,
    from: null,
  });
  if (arrival.tab !== current) {
    const previous = keys.indexOf(arrival.tab);
    setArrival({
      tab: current,
      from: previous === -1 || index === -1 ? null : index > previous ? 'right' : 'left',
    });
  }
  const enteredFrom = arrival.tab === current ? arrival.from : null;

  const onTouchStart = useCallback((event: TouchEvent<HTMLElement>) => {
    if (event.touches.length !== 1 || claimed.has(event.nativeEvent)) {
      // Two fingers, or a tabbed screen nested inside this one already has it.
      gesture.current = null;
      return;
    }
    claimed.add(event.nativeEvent);
    const touch = event.touches[0];
    const target = event.target as HTMLElement | null;
    // Started on something that scrolls sideways itself: let it scroll.
    const ignore = Boolean(
      target?.closest(`[${NO_SWIPE}], .overflow-x-auto, input, textarea, select`),
    );
    gesture.current = { x: touch.clientX, y: touch.clientY, ignore, dragging: false };
  }, []);

  const onTouchMove = useCallback(
    (event: TouchEvent<HTMLElement>) => {
      const began = gesture.current;
      if (!began || began.ignore || event.touches.length !== 1) return;
      const touch = event.touches[0];
      const dx = touch.clientX - began.x;
      const dy = touch.clientY - began.y;

      if (!began.dragging) {
        // Clearly up or down: it is a scroll, and stays one for this touch. A
        // real thumb rarely starts dead straight, so this waits for a
        // decisive vertical run rather than judging the first wobble.
        if (Math.abs(dy) >= SCROLL_LOCK && Math.abs(dy) > Math.abs(dx) * 1.5) {
          began.ignore = true;
          return;
        }
        // Sideways, and more sideways than not: start following the finger.
        if (Math.abs(dx) < SLOP || Math.abs(dx) <= Math.abs(dy)) return;
        began.dragging = true;
      }

      if (reducedMotion()) return;
      const element = pane.current;
      if (!element) return;
      const hasNext = dx < 0 ? index + 1 < keys.length : index - 1 >= 0;
      const offset = hasNext ? dx : dx * EDGE_RESISTANCE;
      element.style.transition = '';
      element.style.transform = `translateX(${offset}px)`;
      // Fades a little as it goes, so the drag reads as "leaving".
      element.style.opacity = String(Math.max(0.6, 1 - Math.abs(offset) / 480));
    },
    [index, keys.length],
  );

  const onTouchEnd = useCallback(
    (event: TouchEvent<HTMLElement>) => {
      const began = gesture.current;
      gesture.current = null;
      if (!began || began.ignore || event.changedTouches.length !== 1) {
        release(pane.current, began?.dragging ?? false);
        return;
      }

      const touch = event.changedTouches[0];
      const dx = touch.clientX - began.x;
      const dy = touch.clientY - began.y;
      // Far enough, and sideways: a diagonal scroll must not change tabs. A
      // touch already judged as a drag (the pane has been following it) is
      // held to the distance alone; one with no moves to judge by — a very
      // quick flick — must be decisively sideways in total.
      const decisive =
        Math.abs(dx) >= threshold &&
        (began.dragging ? Math.abs(dx) > Math.abs(dy) : Math.abs(dx) >= Math.abs(dy) * 1.5);
      // Swipe left (dx < 0) moves to the tab on the right, as in every mobile
      // app people already use.
      const next =
        decisive && index !== -1 ? keys[dx < 0 ? index + 1 : index - 1] : undefined;

      if (next === undefined) {
        // Not far enough, or nothing that way: spring back.
        release(pane.current, began.dragging);
        return;
      }
      // The dragged pane is about to be replaced; the new one slides in
      // through `SwipePane`. Clear the transform first so a sheet opened at
      // once is not trapped inside a transformed box.
      release(pane.current, false);
      onChange(next);
    },
    [keys, index, onChange, threshold],
  );

  const onTouchCancel = useCallback(() => {
    const began = gesture.current;
    gesture.current = null;
    release(pane.current, began?.dragging ?? false);
  }, []);

  return {
    handlers: { onTouchStart, onTouchMove, onTouchEnd, onTouchCancel },
    pane: { tab: current, enteredFrom, paneRef: pane },
  };
}
