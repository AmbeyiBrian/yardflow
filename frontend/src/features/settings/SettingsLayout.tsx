/**
 * The settings section's own navigation (design §7.2, §7.4).
 *
 * Each pane is behind the permission its feature needs, and the tab is hidden
 * when the user lacks it — so `catalogue.manage` without `users.manage` lands on
 * the catalogue and never sees the people list. The server re-checks every call
 * regardless (§7.2).
 */

import { Navigate, Outlet, useLocation, useNavigate } from 'react-router-dom';

import { TabStrip } from '../../components/ui/TabStrip';
import { SwipePane } from '../../components/ui/SwipePane';
import { useSwipeTabs } from '../../components/ui/useSwipeTabs';

import { PERM, type Permission } from '../../auth/permissions';
import { useSession } from '../../auth/session';

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
  // M6: what each document type is called and what the next one will be.
  { to: 'numbering', label: 'Numbering', anyOf: [PERM.SETTINGS_MANAGE] },
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

  // Nine panes are a long strip on a phone. A thumb across the pane moves to
  // the next one this person may see; the strip still works. The current pane
  // is the last path segment, which is exactly what each NavLink points at.
  const location = useLocation();
  const navigate = useNavigate();
  const current = location.pathname.replace(/\/+$/, '').split('/').pop() ?? '';
  const swipe = useSwipeTabs(
    visible.map((pane) => pane.to),
    current,
    (next) => navigate(next),
  );

  return (
    <div className="flex flex-1 flex-col gap-4" {...swipe.handlers}>
      <TabStrip
        tabs={visible.map((pane) => ({ key: pane.to, label: pane.label }))}
        current={current}
        linkTo={(to) => to}
        className="border-b border-slate-200 pb-2"
        aria-label="Settings"
      />

      {/* A column that grows, so a nested tabbed pane can grow with it. */}
      <SwipePane {...swipe.pane} className="flex flex-1 flex-col">
        <Outlet />
      </SwipePane>
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
