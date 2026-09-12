/**
 * Data-display primitives (design §7.3).
 *
 * Separate from `index.tsx` only for size. The rules encoded here are §7.3's:
 * cards on a phone that become tables at `md`, bottom-anchored dialogs, and an
 * empty state that cannot be mistaken for a broken screen.
 */

import type { ReactNode } from 'react';

import { Button } from './index';
import { cn } from './cn';

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">{title}</h1>
        {subtitle ? <p className="text-sm text-slate-600">{subtitle}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap gap-2">{actions}</div> : null}
    </div>
  );
}

/**
 * What to show when a list is empty (§7.3).
 *
 * An empty list and a broken screen look identical without this, and the
 * difference matters most to the person who has just arrived at the yard.
 */
export function EmptyState({
  title,
  hint,
  action,
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-slate-300 px-6 py-10 text-center">
      <p className="text-sm font-medium text-slate-800">{title}</p>
      {hint ? <p className="max-w-sm text-sm text-slate-500">{hint}</p> : null}
      {action}
    </div>
  );
}

const STATUS_TONES: Record<string, string> = {
  DRAFT: 'bg-slate-100 text-slate-700',
  PENDING: 'bg-amber-100 text-amber-900',
  PENDING_APPROVAL: 'bg-amber-100 text-amber-900',
  SUBMITTED: 'bg-amber-100 text-amber-900',
  AWAITING_CLOSEOUT: 'bg-amber-100 text-amber-900',
  INVESTIGATING: 'bg-amber-100 text-amber-900',
  OVERDUE: 'bg-red-100 text-red-900',
  OPEN: 'bg-sky-100 text-sky-900',
  IN_PROGRESS: 'bg-sky-100 text-sky-900',
  APPROVED: 'bg-emerald-100 text-emerald-900',
  ACKNOWLEDGED: 'bg-emerald-100 text-emerald-900',
  RELEASED: 'bg-emerald-100 text-emerald-900',
  POSTED: 'bg-emerald-100 text-emerald-900',
  RETURNED: 'bg-emerald-100 text-emerald-900',
  RESOLVED: 'bg-emerald-100 text-emerald-900',
  CONFIRMED: 'bg-emerald-100 text-emerald-900',
  CLOSED: 'bg-slate-200 text-slate-700',
  CANCELLED: 'bg-slate-200 text-slate-600',
  DECLINED: 'bg-red-100 text-red-900',
  REJECTED: 'bg-red-100 text-red-900',
  EXPIRED: 'bg-red-100 text-red-900',
  WRITTEN_OFF: 'bg-purple-100 text-purple-900',
};

export function StatusBadge({ status }: { status?: string | null }) {
  if (!status) return null;
  return (
    <span
      className={cn(
        'inline-block rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap',
        STATUS_TONES[status] ?? 'bg-slate-100 text-slate-700',
      )}
    >
      {status.replaceAll('_', ' ').toLowerCase()}
    </span>
  );
}

export interface Column<T> {
  header: string;
  cell: (row: T) => ReactNode;
  /** Hidden on the phone card. Use for things only useful on a wide screen. */
  wideOnly?: boolean;
  className?: string;
}

/**
 * A list that is cards on a phone and a table from `md` (§7.3).
 *
 * One component rather than two markups per screen, because the two drift: a
 * column added to the table and forgotten on the card is a field the storekeeper
 * cannot see on the device they actually use.
 */
export function DataList<T>({
  rows,
  columns,
  rowKey,
  onRowClick,
  empty,
}: {
  rows: T[];
  columns: Column<T>[];
  rowKey: (row: T) => string | number;
  onRowClick?: (row: T) => void;
  empty?: ReactNode;
}) {
  if (rows.length === 0) {
    return <>{empty ?? <EmptyState title="Nothing here yet." />}</>;
  }

  return (
    <>
      <ul className="flex flex-col gap-2 md:hidden">
        {rows.map((row) => (
          <li key={rowKey(row)}>
            <button
              type="button"
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              className={cn(
                'w-full rounded-xl border border-slate-200 bg-white p-3 text-left',
                onRowClick && 'active:bg-slate-50',
              )}
            >
              <dl className="flex flex-col gap-1">
                {columns
                  .filter((column) => !column.wideOnly)
                  .map((column) => (
                    <div key={column.header} className="flex justify-between gap-3 text-sm">
                      <dt className="text-slate-500">{column.header}</dt>
                      <dd className="text-right font-medium text-slate-900">
                        {column.cell(row)}
                      </dd>
                    </div>
                  ))}
              </dl>
            </button>
          </li>
        ))}
      </ul>

      {/* Scrolls inside itself, so the page never scrolls sideways. */}
      <div className="hidden overflow-x-auto rounded-xl border border-slate-200 bg-white md:block">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-slate-200 text-left text-slate-500">
              {columns.map((column) => (
                <th key={column.header} className="px-3 py-2 font-medium whitespace-nowrap">
                  {column.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={rowKey(row)}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                className={cn(
                  'border-b border-slate-100 last:border-0',
                  onRowClick && 'cursor-pointer hover:bg-slate-50',
                )}
              >
                {columns.map((column) => (
                  <td key={column.header} className={cn('px-3 py-2', column.className)}>
                    {column.cell(row)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

/**
 * A modal that is a bottom sheet on a phone (§7.3).
 *
 * Bottom-anchored because that is where a thumb is: a dialog whose buttons sit
 * at the top of a six-inch screen cannot be dismissed one-handed.
 */
export function Sheet({
  open,
  title,
  onClose,
  children,
  footer,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-slate-900/40 md:items-center md:p-6"
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <button
        type="button"
        aria-label="Close"
        className="absolute inset-0 cursor-default"
        onClick={onClose}
      />
      <div className="relative flex max-h-[90vh] w-full max-w-lg flex-col rounded-t-2xl bg-white md:rounded-2xl">
        <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
          <h2 className="text-base font-semibold text-slate-900">{title}</h2>
          <Button variant="ghost" onClick={onClose} className="min-h-0 px-2 py-1">
            Close
          </Button>
        </div>
        <div className="flex-1 overflow-y-auto p-4">{children}</div>
        {footer ? (
          <div className="flex gap-3 border-t border-slate-200 p-4">{footer}</div>
        ) : null}
      </div>
    </div>
  );
}

/** A labelled figure, for the reconciliation and dashboard tiles. */
export function Stat({
  label,
  value,
  tone = 'neutral',
  hint,
}: {
  label: string;
  value: ReactNode;
  tone?: 'neutral' | 'good' | 'warn' | 'bad';
  hint?: string;
}) {
  const tones = {
    neutral: 'text-slate-900',
    good: 'text-emerald-700',
    warn: 'text-amber-700',
    bad: 'text-red-700',
  } as const;

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-3">
      <p className="text-xs font-medium tracking-wide text-slate-500 uppercase">{label}</p>
      <p className={cn('text-2xl font-semibold', tones[tone])}>{value}</p>
      {hint ? <p className="text-xs text-slate-500">{hint}</p> : null}
    </div>
  );
}
