/**
 * T4.24 — the notification centre (design §7.4, §9.1; L1).
 *
 * The criterion: "an approval request **appears without a page reload**."
 *
 * That is a polling interval, not a WebSocket. §12 puts this on ECS behind an
 * ALB with a Celery worker, and a socket layer would mean sticky sessions and a
 * second failure mode for a feature whose whole job is a number in a badge. A
 * thirty-second poll of one cheap count endpoint is honest about what it is, and
 * it degrades to "a bit late" rather than "silently broken".
 *
 * The badge lives in `NotificationBell`, which the shell imports on every screen.
 * This module is the list, and it is lazily loaded — which only works while the
 * two stay apart (N-1).
 */

import { useNavigate } from 'react-router-dom';

import { errorMessage, useAction, useList } from '../../api/hooks';
import { Banner, Button, Spinner } from '../../components/ui';
import { EmptyState, PageHeader } from '../../components/ui/data';
import type { Notification } from '../dispatch/types';
import { UNREAD_POLL_MS } from './NotificationBell';

export default function NotificationCentre() {
  const navigate = useNavigate();
  const notifications = useList<Notification>('notifications', { page_size: 50 }, {
    refetchInterval: UNREAD_POLL_MS,
  });
  const markRead = useAction<{ id: number }>({
    resource: 'notifications',
    path: (body) => `${body.id}/read`,
    invalidates: ['notifications', 'notifications/unread'],
  });
  const markAllRead = useAction<Record<string, never>>({
    resource: 'notifications',
    path: () => 'read-all',
    invalidates: ['notifications', 'notifications/unread'],
  });

  const rows = notifications.data?.results ?? [];
  const unreadCount = rows.filter((row) => row.is_unread).length;

  async function open(row: Notification) {
    if (row.is_unread) {
      // Fire and forget: navigating matters more than the read receipt, and the
      // list refetches anyway.
      markRead.mutate({ id: row.id });
    }
    if (row.resource) navigate(row.resource);
  }

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Notifications"
        subtitle="In-app messages. L1 keeps this channel always on."
        actions={
          unreadCount > 0 ? (
            <Button
              variant="secondary"
              loading={markAllRead.isPending}
              onClick={() => markAllRead.mutate({} as Record<string, never>)}
            >
              Mark all read
            </Button>
          ) : undefined
        }
      />

      {notifications.isError ? (
        <Banner tone="error">{errorMessage(notifications.error)}</Banner>
      ) : null}

      {notifications.isLoading ? (
        <Spinner className="text-slate-400" />
      ) : rows.length === 0 ? (
        <EmptyState
          title="Nothing to tell you."
          hint="Approvals, overdue returns and variances land here as they happen."
        />
      ) : (
        <ul className="flex flex-col gap-2">
          {rows.map((row) => (
            <li key={row.id}>
              <button
                type="button"
                onClick={() => open(row)}
                className={[
                  'w-full rounded-xl border p-3 text-left',
                  row.is_unread
                    ? 'border-slate-300 bg-white'
                    : 'border-slate-200 bg-slate-50 text-slate-600',
                ].join(' ')}
              >
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p
                      className={[
                        'text-sm',
                        row.is_unread ? 'font-semibold text-slate-900' : 'font-medium',
                      ].join(' ')}
                    >
                      {row.subject || row.event_key.replaceAll('.', ' ')}
                    </p>
                    {row.body ? <p className="text-sm">{row.body}</p> : null}
                    {row.target_label ? (
                      <p className="text-xs text-slate-500">{row.target_label}</p>
                    ) : null}
                  </div>
                  <div className="flex shrink-0 flex-col items-end gap-1">
                    {row.is_unread ? (
                      <span className="size-2 rounded-full bg-sky-600" aria-label="Unread" />
                    ) : null}
                    <span className="text-xs text-slate-400">
                      {row.occurred_at?.slice(0, 16).replace('T', ' ')}
                    </span>
                  </div>
                </div>
                {row.resource ? (
                  // §9.2: every message is tappable. A notification with nowhere
                  // to go is a notification that gets ignored.
                  <p className="mt-1 text-xs text-slate-500">Tap to open</p>
                ) : null}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
