/**
 * The application shell (design §7.2, §7.3; requirements B4, N-1, A2).
 *
 * One build serves every role. Navigation is driven by the permissions `/me`
 * resolved, so a technician never sees the storekeeper's screens — and the same
 * bundle needs no per-role variant.
 *
 * Mobile-first: a bottom tab bar within thumb reach on a phone, a sidebar from
 * `md` up. No horizontal page scroll at any width (§7.3).
 */

import { NavLink, Outlet, useLocation } from 'react-router-dom';
import { useEffect, useState, type ReactNode } from 'react';

import { NotificationBell } from '../features/notifications/NotificationBell';
import { OfflineBanner } from '../offline/OfflineBanner';
import { cn } from './ui/cn';
import { LogoMark, Wordmark } from './Logo';
import { Banner, Button } from './ui';
import { PERM, type Permission } from '../auth/permissions';
import { useSession } from '../auth/session';

interface NavItem {
  to: string;
  label: string;
  /** Shown only if the user holds one of these. Absent means always shown. */
  anyOf?: Permission[];
  icon: ReactNode;
}

// Kept in one list so the phone tab bar and the desktop sidebar can never drift
// apart.
const NAV_ITEMS: NavItem[] = [
  { to: '/', label: 'Home', icon: <GlyphHome /> },
  {
    to: '/gate-in',
    label: 'Gate-in',
    anyOf: [PERM.GATE_IN_POST],
    icon: <GlyphInbox />,
  },
  {
    to: '/gate-out',
    label: 'Gate-out',
    anyOf: [PERM.GATE_OUT_REQUEST, PERM.GATE_OUT_RELEASE],
    icon: <GlyphTruck />,
  },
  {
    to: '/approvals',
    label: 'Approvals',
    anyOf: [PERM.GATE_OUT_APPROVE, PERM.DISPOSAL_APPROVE],
    icon: <GlyphCheck />,
  },
  { to: '/stock', label: 'Stock', icon: <GlyphBoxes /> },
  {
    // O12: a manager's standing question is "how is my PO doing", and it is
    // asked often enough to be top-level rather than buried under settings.
    to: '/projects',
    label: 'Projects',
    anyOf: [PERM.PROJECT_VIEW_COST, PERM.CATALOGUE_MANAGE],
    icon: <GlyphClipboard />,
  },
  {
    to: '/jobs',
    label: 'Jobs',
    anyOf: [PERM.JOB_CLOSEOUT, PERM.REPORT_VIEW_ALL],
    icon: <GlyphClipboard />,
  },
  {
    // J1, J2: quarantine has its own entry because material sitting there
    // unnoticed is the failure the requirement exists to prevent.
    to: '/quarantine',
    label: 'Quarantine',
    anyOf: [PERM.STOCK_ADJUST, PERM.GATE_IN_POST],
    icon: <GlyphShield />,
  },
  {
    // K1, K3: an operator audit opens with this screen.
    to: '/client-material',
    label: 'Client stock',
    anyOf: [PERM.GATE_OUT_RELEASE, PERM.REPORT_VIEW_ALL, PERM.GATE_OUT_APPROVE],
    icon: <GlyphHandshake />,
  },
  {
    // T5.11, T5.12: an owner's two standing questions — who has it, and what is
    // still unresolved. Both are top-level because both are checked daily.
    to: '/custody',
    label: 'Custody',
    anyOf: [PERM.REPORT_VIEW_ALL, PERM.GATE_OUT_APPROVE],
    icon: <GlyphHand />,
  },
  {
    to: '/exceptions',
    label: 'Exceptions',
    anyOf: [PERM.GATE_OUT_APPROVE, PERM.STOCK_ADJUST, PERM.REPORT_VIEW_ALL],
    icon: <GlyphAlert />,
  },
  {
    to: '/reports',
    label: 'Reports',
    anyOf: [PERM.REPORT_VIEW_ALL],
    icon: <GlyphChart />,
  },
  {
    to: '/settings',
    label: 'Settings',
    // No `anyOf`: everybody has a security pane of their own (B5, T8.10), and
    // the admin panes inside are gated individually.
    icon: <GlyphCog />,
  },
];

/**
 * What this person is, in the company's own words.
 *
 * A name alone does not tell somebody what they are allowed to do, and "Brian
 * Store Keeper" is a name that happens to read like a job — which is exactly
 * the confusion worth removing. Several roles are listed, because somebody who
 * is both a storekeeper and an approver needs to know they are both.
 */
function roleSummary(roles: { name: string }[] | undefined): string {
  if (!roles?.length) return 'No role yet';
  if (roles.length <= 2) return roles.map((role) => role.name).join(' · ');
  return `${roles[0].name} and ${roles.length - 1} more`;
}

export function AppShell() {
  const { user, logout, hasAny, isSuspended } = useSession();

  const visible = NAV_ITEMS.filter((item) => !item.anyOf || hasAny(...item.anyOf));

  return (
    <div className="flex min-h-full flex-col bg-slate-50 md:flex-row">

      {/* Sidebar from md up.
          `sticky top-0 h-screen`: navigation stays put while the content beside
          it scrolls. A long stock list used to carry the whole sidebar off the
          top of the window, so getting to another screen meant scrolling back up
          first — on the layout where there is most room to keep it. */}
      <aside className="sticky top-0 hidden h-screen w-60 shrink-0 border-r border-slate-200 bg-white md:flex md:flex-col">
        <div className="px-4 py-5">
          <Wordmark className="h-5 w-auto text-slate-900" />
          <p className="truncate text-sm text-slate-500">
            {user?.organization?.name ?? 'Platform administration'}
          </p>
        </div>
        {/* The nav itself scrolls, and only if it has to: a role with twelve
            destinations still fits a laptop, but the sidebar must never be the
            reason something is unreachable. */}
        <nav className="flex flex-1 flex-col gap-1 overflow-y-auto px-2">
          {visible.map((item) => (
            <SidebarLink key={item.to} item={item} />
          ))}
        </nav>
        {/* Pinned to the bottom of the sidebar rather than to the end of a long
            nav list — sign out should be in the same place on every screen. */}
        <div className="shrink-0 border-t border-slate-200 p-3">
          <div className="flex items-center justify-between gap-2 pb-2">
            <div className="min-w-0 px-1">
              <p className="truncate text-sm text-slate-600">{user?.full_name}</p>
              <p className="truncate text-xs text-slate-500">{roleSummary(user?.roles)}</p>
            </div>
            {/* L1: in-app is the channel that is always on, so the badge is
                visible from every screen rather than only on its own page. */}
            <NotificationBell />
          </div>
          <Button variant="secondary" block onClick={() => void logout()}>
            Sign out
          </Button>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        {/* §8.1: unmistakable, and at the top of the content rather than
            floating over it. A fixed overlay would cover the first line of every
            screen — and covering content to announce a problem is its own
            problem. */}
        <div className="sticky top-0 z-20">
          <OfflineBanner />
        </div>

        {/* Phone header. */}
        <header className="flex items-center justify-between border-b border-slate-200 bg-white px-4 py-3 md:hidden">
          <div className="flex min-w-0 items-center gap-2.5">
            {/* The sidebar carries the wordmark, but it is hidden on a phone —
                so the compact mark stands in. Not labelled: the company name
                beside it is what a storekeeper needs read out, and "YardFlow"
                announced first would only delay it. */}
            <LogoMark className="h-6 w-auto shrink-0 text-slate-900" labelled={false} />
            <div className="min-w-0">
              <p className="truncate text-base font-semibold text-slate-900">
                {user?.organization?.name ?? 'Platform administration'}
              </p>
              <p className="truncate text-xs text-slate-500">
                {user?.full_name}
                {user?.roles?.length ? ` · ${roleSummary(user.roles)}` : ''}
              </p>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <NotificationBell />
            <Button variant="ghost" onClick={() => void logout()}>
              Sign out
            </Button>
          </div>
        </header>

        {isSuspended ? (
          <div className="px-4 pt-4">
            {/* A2: the notice the requirement asks for. Reads still work. */}
            <Banner tone="warning">
              This organization is suspended. You can still view records, but new
              movements cannot be posted. Please contact support.
            </Banner>
          </div>
        ) : null}

        <main className="min-w-0 flex-1 px-4 py-4 md:px-6 md:py-6">
          <Outlet />
        </main>

        {/* Bottom tab bar on a phone — thumb-reachable. */}
        <PhoneTabBar items={visible} />
      </div>
    </div>
  );
}

function SidebarLink({ item }: { item: NavItem }) {
  return (
    <NavLink
      to={item.to}
      end={item.to === '/'}
      className={({ isActive }) =>
        cn(
          'flex min-h-[44px] items-center gap-3 rounded-lg px-3 text-sm font-medium',
          isActive ? 'bg-slate-900 text-white' : 'text-slate-700 hover:bg-slate-100',
        )
      }
    >
      <span aria-hidden="true">{item.icon}</span>
      {item.label}
    </NavLink>
  );
}

/**
 * The phone navigation (§7.3; N-1).
 *
 * A bottom bar fits about five targets at 44px on the narrowest phone this has to
 * work on, and an owner has thirteen destinations. The bar used to render
 * `items.slice(0, 5)` — which quietly made the other eight *unreachable on a
 * phone*: no Reports, no Settings, no Exceptions, no Quarantine, from the device
 * this product is built around. The sidebar has them all, so it only showed up
 * for somebody working on a phone, which is everybody in a yard.
 *
 * So: four tabs, then More. The tabs are the four a storekeeper touches all day —
 * whatever order `NAV_ITEMS` puts first — and everything else is one tap away in
 * a sheet, at full width and full height, which is easier to hit than a fifth
 * cramped tab was.
 *
 * More is highlighted while the current screen lives inside it, so the bar never
 * shows nothing as selected.
 */
const PRIMARY_TABS = 4;

function PhoneTabBar({ items }: { items: NavItem[] }) {
  const [open, setOpen] = useState(false);
  const location = useLocation();

  // With one to spare there is no point spending a slot on More.
  const overflows = items.length > PRIMARY_TABS + 1;
  const primary = overflows ? items.slice(0, PRIMARY_TABS) : items;
  const rest = overflows ? items.slice(PRIMARY_TABS) : [];

  // Navigating closes the sheet. Without this it stays open over the screen it
  // just opened, and the first thing you do is dismiss it.
  useEffect(() => {
    setOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open]);

  const restIsActive = rest.some(
    (item) => location.pathname === item.to || location.pathname.startsWith(`${item.to}/`),
  );

  return (
    <>
      {open ? (
        <>
          {/* Tapping away closes it — the cheapest dismissal on a phone, and the
              one people try first. */}
          <button
            type="button"
            aria-label="Close menu"
            className="fixed inset-0 z-30 bg-slate-900/40 md:hidden"
            onClick={() => setOpen(false)}
          />
          <div
            id="more-menu"
            role="dialog"
            aria-modal="true"
            aria-label="More screens"
            // Above the bar, not over it: the way out stays visible and in the
            // same place, so More toggles rather than traps.
            className="fixed inset-x-0 bottom-[calc(56px+env(safe-area-inset-bottom))] z-40 max-h-[70vh] overflow-y-auto border-t border-slate-200 bg-white pb-2 md:hidden"
          >
            <p className="px-4 pt-3 pb-1 text-xs font-medium tracking-wide text-slate-500 uppercase">
              More
            </p>
            {rest.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === '/'}
                onClick={() => setOpen(false)}
                className={({ isActive }) =>
                  cn(
                    'flex min-h-[52px] items-center gap-3 px-4 text-base',
                    isActive ? 'bg-slate-100 font-medium text-slate-900' : 'text-slate-700',
                  )
                }
              >
                <span aria-hidden="true">{item.icon}</span>
                {item.label}
              </NavLink>
            ))}
          </div>
        </>
      ) : null}

      <nav
        className={cn(
          'sticky bottom-0 z-20 flex border-t border-slate-200 bg-white pb-safe md:hidden',
          // Above the backdrop while the sheet is open. Otherwise the dimming
          // falls over the bar too: it reads as disabled, and the More button
          // that closes it is behind the overlay.
          open && 'z-50',
        )}
      >
        {primary.map((item) => (
          <TabLink key={item.to} item={item} />
        ))}
        {overflows ? (
          <button
            type="button"
            aria-expanded={open}
            aria-controls="more-menu"
            onClick={() => setOpen((wasOpen) => !wasOpen)}
            className={cn(
              'flex min-h-[56px] flex-1 flex-col items-center justify-center gap-1 text-xs',
              open || restIsActive ? 'text-slate-900' : 'text-slate-500',
            )}
          >
            <span aria-hidden="true">
              <GlyphMore />
            </span>
            <span className="truncate px-1">More</span>
          </button>
        ) : null}
      </nav>
    </>
  );
}

function TabLink({ item }: { item: NavItem }) {
  return (
    <NavLink
      to={item.to}
      end={item.to === '/'}
      className={({ isActive }) =>
        cn(
          'flex min-h-[56px] flex-1 flex-col items-center justify-center gap-1 text-xs',
          isActive ? 'text-slate-900' : 'text-slate-500',
        )
      }
    >
      <span aria-hidden="true">{item.icon}</span>
      <span className="truncate px-1">{item.label}</span>
    </NavLink>
  );
}

/* Inline SVG rather than an icon package: it keeps the bundle small on a
   cellular connection and needs no network at render time. */

function glyph(path: ReactNode) {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {path}
    </svg>
  );
}

function GlyphHome() {
  return glyph(<><path d="M3 10.5 12 3l9 7.5" /><path d="M5 9.5V21h14V9.5" /></>);
}
function GlyphInbox() {
  return glyph(<><path d="M3 12h5l2 3h4l2-3h5" /><path d="M4 12 6 4h12l2 8v8H4z" /></>);
}
function GlyphTruck() {
  return glyph(<><path d="M2 7h11v9H2z" /><path d="M13 10h4l4 3v3h-8z" /><circle cx="6" cy="18" r="2" /><circle cx="17" cy="18" r="2" /></>);
}
function GlyphCheck() {
  return glyph(<><path d="M20 6 9 17l-5-5" /></>);
}
function GlyphBoxes() {
  return glyph(<><path d="M3 8h8v8H3zM13 8h8v8h-8z" /><path d="M3 8l4-4h8l4 4" /></>);
}
function GlyphClipboard() {
  return glyph(<><path d="M9 4h6v3H9z" /><path d="M6 6h2v0h8V6h2v15H6z" /><path d="M9 12h6M9 16h4" /></>);
}
function GlyphChart() {
  return glyph(<><path d="M4 20V4" /><path d="M4 20h16" /><path d="M8 20v-6M13 20V8M18 20v-9" /></>);
}
function GlyphCog() {
  return glyph(<><circle cx="12" cy="12" r="3" /><path d="M12 3v3M12 18v3M4.5 7.5l2 1M17.5 15.5l2 1M4.5 16.5l2-1M17.5 8.5l2-1" /></>);
}
function GlyphHand() {
  return glyph(<><path d="M8 13V5a1.5 1.5 0 0 1 3 0v6" /><path d="M11 11V4.5a1.5 1.5 0 0 1 3 0V11" /><path d="M14 11V6.5a1.5 1.5 0 0 1 3 0V14a6 6 0 0 1-6 6H9a5 5 0 0 1-5-5v-3a1.5 1.5 0 0 1 3 0" /></>);
}
function GlyphAlert() {
  return glyph(<><path d="M12 4 3 20h18z" /><path d="M12 10v5M12 17.5v.5" /></>);
}
function GlyphShield() {
  return glyph(<><path d="M12 3l7 3v6c0 4-3 7-7 9-4-2-7-5-7-9V6z" /><path d="M9.5 12l1.8 1.8L15 10" /></>);
}
function GlyphHandshake() {
  return glyph(<><path d="M3 12l4-4 5 5-4 4z" /><path d="M21 12l-4-4-5 5 4 4z" /><path d="M8 8l4-3 4 3" /></>);
}

function GlyphMore() {
  // Three dots: the one icon that reads as "the rest of it" without a label,
  // though it gets a label anyway.
  return glyph(
    <>
      <circle cx="5" cy="12" r="1.5" />
      <circle cx="12" cy="12" r="1.5" />
      <circle cx="19" cy="12" r="1.5" />
    </>,
  );
}
