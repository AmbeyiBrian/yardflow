/**
 * The unread badge (design §7.4, §9.1; L1, T4.24).
 *
 * Its own module because the app shell imports it on every screen. Leaving it in
 * `NotificationCentre` would drag the whole notification list into the main
 * bundle and undo its code splitting (N-1).
 *
 * T4.24 asks that "an approval request appears without a page reload", and this
 * is where that happens: a poll of one cheap count endpoint. §12 puts the app on
 * ECS behind a load balancer, where a socket layer would mean sticky sessions and
 * a second failure mode for a feature whose whole job is a number in a badge. A
 * poll degrades to "a bit late" rather than "silently broken".
 */

import { Link } from 'react-router-dom';

import { useResource } from '../../api/hooks';

/** How often the badge asks. One indexed count, scoped to the caller. */
export const UNREAD_POLL_MS = 30_000;

export function useUnreadCount() {
  return useResource<{ unread: number }>('notifications/unread', undefined, {
    refetchInterval: UNREAD_POLL_MS,
    // Not while the tab is hidden: a phone in a pocket should not poll all day.
    refetchIntervalInBackground: false,
  });
}

export function NotificationBell() {
  const unread = useUnreadCount();
  const count = unread.data?.unread ?? 0;

  return (
    <Link
      to="/notifications"
      className="relative flex min-h-[44px] min-w-[44px] items-center justify-center rounded-lg text-slate-700 hover:bg-slate-100"
      aria-label={count > 0 ? `Notifications, ${count} unread` : 'Notifications'}
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" className="size-5">
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth="1.8"
          d="M15 17h5l-1.4-2.1A2 2 0 0 1 18 13.8V11a6 6 0 0 0-5-5.9V4a1 1 0 1 0-2 0v1.1A6 6 0 0 0 6 11v2.8a2 2 0 0 1-.6 1.1L4 17h5m6 0a3 3 0 1 1-6 0"
        />
      </svg>
      {count > 0 ? (
        <span className="absolute top-1 right-1 min-w-[18px] rounded-full bg-red-600 px-1 text-center text-xs font-semibold text-white">
          {count > 99 ? '99+' : count}
        </span>
      ) : null}
    </Link>
  );
}
