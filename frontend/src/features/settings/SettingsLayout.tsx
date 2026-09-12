/**
 * The settings section's own navigation (design §7.2, §7.4).
 *
 * Each pane is behind the permission its feature needs, and the tab is hidden
 * when the user lacks it — so `catalogue.manage` without `users.manage` lands on
 * the catalogue and never sees the people list. The server re-checks every call
 * regardless (§7.2).
 */

import { NavLink, Navigate, Outlet } from 'react-router-dom';

import { PERM, type Permission } from '../../auth/permissions';
import { useSession } from '../../auth/session';
import { cn } from '../../components/ui/cn';

const PANES: { to: string; label: string; anyOf: Permission[] }[] = [
  // Everyone's own, so no permission gates it: the person who owns the phone is
  // the person who enrols it (B5, T8.10).
  { to: 'security', label: 'Security', anyOf: [] },
  { to: 'catalogue', label: 'Catalogue', anyOf: [PERM.CATALOGUE_MANAGE] },
  { to: 'network', label: 'Network', anyOf: [PERM.CATALOGUE_MANAGE] },
  { to: 'people', label: 'People', anyOf: [PERM.USERS_MANAGE] },
  { to: 'organization', label: 'Company', anyOf: [PERM.SETTINGS_MANAGE] },
  // "Approvals" is who signs for what; "Rules" is the switches and timings.
  // One tab called "Rules" covering only the second was read as the first.
  { to: 'approvals', label: 'Approvals', anyOf: [PERM.SETTINGS_MANAGE] },
  { to: 'rules', label: 'Rules', anyOf: [PERM.SETTINGS_MANAGE] },
  // Readable by any member — a notification nobody expected reads as spam, so
  // seeing what you will be told is open. Editing is gated in the pane itself.
  { to: 'notifications', label: 'Notifications', anyOf: [] },
];

export default function SettingsLayout() {
  const { hasAny } = useSession();
  // An empty `anyOf` means everybody — `hasAny()` with no arguments is false,
  // so the check has to allow for it explicitly.
  const visible = PANES.filter(
    (pane) => pane.anyOf.length === 0 || hasAny(...pane.anyOf),
  );

  return (
    <div className="flex flex-col gap-4">
      <nav className="flex gap-1 overflow-x-auto border-b border-slate-200 pb-2">
        {visible.map((pane) => (
          <NavLink
            key={pane.to}
            to={pane.to}
            className={({ isActive }) =>
              cn(
                'flex min-h-[44px] items-center rounded-lg px-3 text-sm font-medium whitespace-nowrap',
                isActive ? 'bg-slate-900 text-white' : 'text-slate-700 hover:bg-slate-100',
              )
            }
          >
            {pane.label}
          </NavLink>
        ))}
      </nav>

      <Outlet />
    </div>
  );
}

/** Land on the first pane the user can actually see. */
export function SettingsIndexRedirect() {
  const { hasAny } = useSession();
  const first = PANES.find(
    (pane) => pane.anyOf.length === 0 || hasAny(...pane.anyOf),
  );
  return <Navigate to={first ? first.to : '/'} replace />;
}
