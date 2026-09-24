/**
 * A horizontal strip of tabs that keeps the selected one in view (§7.3).
 *
 * Nine settings panes or five network tabs do not fit a phone's width. Before
 * this, a swipe moved to the next pane while the strip stayed where it was, so
 * the highlighted tab was often off the edge and the screen looked as if
 * nothing had it selected. Now whenever the selection changes — by swipe or by
 * tap — the strip scrolls just enough, sideways only, to show it. The page is
 * never scrolled up or down to do so.
 *
 * Tabs are buttons, or links when `linkTo` is given, so the settings panes keep
 * real URLs. The strip is a sideways scroller, which `useSwipeTabs` already
 * leaves alone: a finger on the strip scrolls the strip.
 */

import { useEffect, useRef, type ReactNode } from 'react';
import { Link } from 'react-router-dom';

import { cn } from './cn';

export interface TabItem<T extends string> {
  key: T;
  label: ReactNode;
}

/** Room left between the selected tab and the strip's edge, so it is not cut. */
const MARGIN = 16;

export function TabStrip<T extends string>({
  tabs,
  current,
  onSelect,
  linkTo,
  className,
  'aria-label': ariaLabel = 'Sections',
}: {
  tabs: readonly TabItem<T>[];
  current: T;
  onSelect?: (key: T) => void;
  /** Render each tab as a link to this path instead of a button. */
  linkTo?: (key: T) => string;
  className?: string;
  'aria-label'?: string;
}) {
  const strip = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const container = strip.current;
    const active = container?.querySelector<HTMLElement>('[aria-current="page"]');
    if (!container || !active) return;
    const left = active.offsetLeft - container.offsetLeft;
    const right = left + active.offsetWidth;
    const viewStart = container.scrollLeft;
    const viewEnd = viewStart + container.clientWidth;
    let target: number | null = null;
    if (left - MARGIN < viewStart) target = left - MARGIN;
    else if (right + MARGIN > viewEnd) target = right + MARGIN - container.clientWidth;
    if (target === null) return;
    const reduced =
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    container.scrollTo({ left: Math.max(0, target), behavior: reduced ? 'auto' : 'smooth' });
  }, [current]);

  return (
    <nav ref={strip} aria-label={ariaLabel} className={cn('flex gap-1 overflow-x-auto', className)}>
      {tabs.map((tab) => {
        const active = tab.key === current;
        const classes = cn(
          'flex min-h-[44px] shrink-0 items-center rounded-lg px-3 text-sm font-medium whitespace-nowrap',
          active ? 'bg-slate-900 text-white' : 'text-slate-700 hover:bg-slate-100',
        );
        return linkTo ? (
          <Link
            key={tab.key}
            to={linkTo(tab.key)}
            aria-current={active ? 'page' : undefined}
            className={classes}
          >
            {tab.label}
          </Link>
        ) : (
          <button
            key={tab.key}
            type="button"
            aria-current={active ? 'page' : undefined}
            onClick={() => onSelect?.(tab.key)}
            className={classes}
          >
            {tab.label}
          </button>
        );
      })}
    </nav>
  );
}
