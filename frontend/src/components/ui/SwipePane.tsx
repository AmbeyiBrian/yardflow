/**
 * Where a tab's content lives while `useSwipeTabs` is in charge of it (§7.3).
 * Kept apart from the hook so this file exports only a component, which is
 * what fast refresh wants.
 */

import type { ReactNode } from 'react';

import { cn } from './cn';
import type { SwipePaneProps } from './useSwipeTabs';

/**
 * The box a tab's content lives in. It follows the finger during a swipe and,
 * remounting on each tab, slides the new content in from the side it came from.
 * The entry class is dropped when the animation ends so nothing transformed is
 * left wrapping the pane (see the note on `position: fixed` above).
 */
export function SwipePane<T extends string>({
  tab,
  enteredFrom,
  paneRef,
  className,
  children,
}: SwipePaneProps<T> & { className?: string; children: ReactNode }) {
  return (
    <div
      key={tab}
      ref={paneRef}
      data-tab-pane={tab}
      className={cn(enteredFrom && `yf-tab-enter-${enteredFrom}`, className)}
      onAnimationEnd={(event) => {
        if (event.target !== event.currentTarget) return;
        event.currentTarget.classList.remove('yf-tab-enter-left', 'yf-tab-enter-right');
      }}
    >
      {children}
    </div>
  );
}
