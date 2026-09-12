/**
 * Routes (design §7.1, §7.2).
 *
 * Code-split per feature, so a technician on a cellular connection downloads the
 * job closeout screen and not the reporting suite (N-1).
 *
 * `RequireAuth` guards everything inside the shell; `RequirePermission` guards
 * individual features. Both are UX — the server re-checks every call (§7.2).
 */

import { Suspense, type ReactNode } from 'react';
import { Navigate, Route, Routes, useLocation } from 'react-router-dom';

import { AppShell } from '../components/AppShell';
import { Banner } from '../components/ui';
import { PERM, type Permission } from '../auth/permissions';
import { useSession } from '../auth/session';
import { ErrorBoundary } from '../components/ErrorBoundary';
import { lazyRoute } from './lazyRoute';
import { LogoActivity } from '../components/Logo';

const LoginPage = lazyRoute(() => import('../features/auth/LoginPage'));
const ForgotPasswordPage = lazyRoute(() =>
  import('../features/auth/PasswordResetPages').then((m) => ({ default: m.ForgotPasswordPage })),
);
const ResetPasswordPage = lazyRoute(() =>
  import('../features/auth/PasswordResetPages').then((m) => ({ default: m.ResetPasswordPage })),
);
const DashboardPage = lazyRoute(() => import('../features/dashboard/DashboardPage'));

// Settings (T2.12–T2.15). Split per pane, so an administrator's catalogue screen
// is not in the bundle a technician downloads (N-1).
const SettingsLayout = lazyRoute(() => import('../features/settings/SettingsLayout'));
const CataloguePage = lazyRoute(() => import('../features/settings/CataloguePage'));
const NetworkPage = lazyRoute(() => import('../features/settings/NetworkPage'));
const UsersPage = lazyRoute(() => import('../features/settings/UsersPage'));
const SettingsPage = lazyRoute(() => import('../features/settings/SettingsPage'));
// A4: the company's own details and logo, which print on every document.
const OrganizationPage = lazyRoute(() => import('../features/settings/OrganizationPage'));
const ApprovalRulesPage = lazyRoute(() => import('../features/settings/ApprovalRulesPage'));
// T8.10: everyone's own pane — enrolling the phone in their hand (B5).
const SecurityPage = lazyRoute(() => import('../features/settings/SecurityPage'));
// L2: who hears what. Readable by everyone, editable behind `settings.manage`.
const NotificationSettingsPage = lazyRoute(
  () => import('../features/settings/NotificationsPage'),
);
// Receiving and stock (T3.19–T3.21). The gate-in bundle is one of the two the
// service worker precaches for offline use (T8.1), so it stays self-contained.
const GateInCapturePage = lazyRoute(() => import('../features/receiving/GateInCapturePage'));
const GateInListPage = lazyRoute(() => import('../features/receiving/GateInListPage'));
const GateInDetailPage = lazyRoute(() =>
  import('../features/receiving/GateInListPage').then((m) => ({ default: m.GateInDetailPage })),
);
const StockPage = lazyRoute(() => import('../features/stock/StockPages'));
const SerialHistoryPage = lazyRoute(() =>
  import('../features/stock/StockPages').then((m) => ({ default: m.SerialHistoryPage })),
);
const DrumHistoryPage = lazyRoute(() =>
  import('../features/stock/StockPages').then((m) => ({ default: m.DrumHistoryPage })),
);
const DrumsPage = lazyRoute(() =>
  import('../features/stock/StockPages').then((m) => ({ default: m.DrumsPage })),
);
const TransfersPage = lazyRoute(() =>
  import('../features/stock/StockPages').then((m) => ({ default: m.TransfersPage })),
);
const CountsPage = lazyRoute(() =>
  import('../features/stock/StockPages').then((m) => ({ default: m.CountsPage })),
);
const CountDetailPage = lazyRoute(() =>
  import('../features/stock/StockPages').then((m) => ({ default: m.CountDetailPage })),
);

// Dispatch and approvals (T4.21–T4.24). The gate-out bundle is the second one
// the service worker precaches for offline use (T8.1).
const GateOutRequestPage = lazyRoute(() => import('../features/dispatch/GateOutRequestPage'));
const GateOutListPage = lazyRoute(() => import('../features/dispatch/GateOutPages'));
const GateOutDetailPage = lazyRoute(() =>
  import('../features/dispatch/GateOutPages').then((m) => ({ default: m.GateOutDetailPage })),
);
const ApprovalsPage = lazyRoute(() => import('../features/dispatch/ApprovalsPage'));
const ApprovalDetailPage = lazyRoute(() =>
  import('../features/dispatch/ApprovalsPage').then((m) => ({ default: m.ApprovalDetailPage })),
);
// Jobs, custody and exceptions (T5.10–T5.12). The closeout screen is what a
// technician downloads on a cellular connection, so it is its own chunk (N-1).
const MyJobsPage = lazyRoute(() => import('../features/jobs/MyJobsPage'));
const JobCloseoutPage = lazyRoute(() => import('../features/jobs/JobCloseoutPage'));
const MyCustodyPage = lazyRoute(() => import('../features/jobs/MyCustodyPage'));
const CustodyOverviewPage = lazyRoute(() => import('../features/jobs/CustodyOverviewPage'));
const ReconciliationPage = lazyRoute(() =>
  import('../features/jobs/CustodyOverviewPage').then((m) => ({
    default: m.ReconciliationPage,
  })),
);
const ExceptionsPage = lazyRoute(() => import('../features/jobs/ExceptionsPage'));

// Disposition, disposal and client returns (T6.6). Split apart: a storekeeper
// deciding about quarantine does not download the disposal approval screens.
const QuarantinePage = lazyRoute(() => import('../features/disposition/QuarantinePage'));
const DispositionsPage = lazyRoute(() =>
  import('../features/disposition/QuarantinePage').then((m) => ({
    default: m.DispositionsPage,
  })),
);
const DisposalsPage = lazyRoute(() => import('../features/disposition/DisposalPages'));
const ClientReturnsPage = lazyRoute(() => import('../features/disposition/ClientReturnsPage'));

// Reporting (T7.7, T7.6). Its own chunk: the reporting suite is the thing a
// technician on a cellular connection should never download (N-1).
const ReportsPage = lazyRoute(() => import('../features/reports/ReportsPage'));
const ReportPage = lazyRoute(() =>
  import('../features/reports/ReportsPage').then((m) => ({ default: m.ReportPage })),
);
const RetentionReviewPage = lazyRoute(() =>
  import('../features/reports/ReportsPage').then((m) => ({
    default: m.RetentionReviewPage,
  })),
);

// N1, N3: what this device has captured and what the yard refused (§8.1, §8.4).
const SyncPage = lazyRoute(() => import('../offline/SyncPage'));
// T8.5, §8.3: releasing at the gate with no signal — and only what was already
// approved and downloaded.
const OfflineReleasePage = lazyRoute(() => import('../offline/OfflineReleasePage'));

const NotificationCentre = lazyRoute(() => import('../features/notifications/NotificationCentre'));

const SettingsIndexRedirect = lazyRoute(() =>
  import('../features/settings/SettingsLayout').then((m) => ({
    default: m.SettingsIndexRedirect,
  })),
);

function FullPageSpinner() {
  return (
    <div className="flex min-h-full items-center justify-center p-10">
      {/* The whole name here rather than the bare Y: this is the wait before a
          screen exists at all — first load, or a route arriving — and it is the
          one place with room for it. */}
      <LogoActivity className="h-7 w-auto text-slate-700" shape="wordmark" label="Loading" />
    </div>
  );
}

function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useSession();
  const location = useLocation();

  // Waiting on the initial /me hydration. Redirecting now would bounce a
  // signed-in user to the login screen on every reload.
  if (loading) return <FullPageSpinner />;

  if (!user) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return <>{children}</>;
}

function RequireAnonymous({ children }: { children: ReactNode }) {
  const { user, loading } = useSession();
  if (loading) return <FullPageSpinner />;
  if (user) return <Navigate to="/" replace />;
  return <>{children}</>;
}

function RequirePermission({ anyOf, children }: { anyOf: Permission[]; children: ReactNode }) {
  const { hasAny } = useSession();

  if (!hasAny(...anyOf)) {
    return (
      <Banner tone="warning">
        You do not have permission to view this. If you need it, ask an administrator to add it to
        your role.
      </Banner>
    );
  }
  return <>{children}</>;
}

export function AppRoutes() {
  return (
    // Outside `Suspense`, so it catches the lazy import failing as well as
    // anything a screen throws while rendering. Without it, either one unmounts
    // the tree and leaves a blank page (§7.1).
    <ErrorBoundary>
      <Suspense fallback={<FullPageSpinner />}>
        <Routes>
          <Route
            path="/login"
            element={
              <RequireAnonymous>
                <LoginPage />
              </RequireAnonymous>
            }
          />
          <Route path="/forgot-password" element={<ForgotPasswordPage />} />
          {/* The link in a reset or invitation email lands here. */}
          <Route path="/reset-password" element={<ResetPasswordPage />} />

          <Route
            element={
              <RequireAuth>
                <AppShell />
              </RequireAuth>
            }
          >
            <Route index element={<DashboardPage />} />

            {/* Phase 4 onward replaces the remaining placeholders. Each is
              already behind the permission its feature will require, so the
              navigation and guards are exercised from now. */}
            <Route
              path="gate-in"
              element={
                <RequirePermission anyOf={[PERM.GATE_IN_POST]}>
                  <GateInListPage />
                </RequirePermission>
              }
            />
            <Route
              path="gate-in/new"
              element={
                <RequirePermission anyOf={[PERM.GATE_IN_POST]}>
                  <GateInCapturePage />
                </RequirePermission>
              }
            />
            <Route
              path="gate-in/:id"
              element={
                <RequirePermission anyOf={[PERM.GATE_IN_POST]}>
                  <GateInDetailPage />
                </RequirePermission>
              }
            />
            {/* Correcting a draft. The same screen that captures one, because a
              correction is the same work as the entry and a second screen would
              drift from it. */}
            <Route
              path="gate-in/:id/edit"
              element={
                <RequirePermission anyOf={[PERM.GATE_IN_POST]}>
                  <GateInCapturePage />
                </RequirePermission>
              }
            />
            <Route
              path="gate-out"
              element={
                <RequirePermission anyOf={[PERM.GATE_OUT_REQUEST, PERM.GATE_OUT_RELEASE]}>
                  <GateOutListPage />
                </RequirePermission>
              }
            />
            <Route
              path="gate-out/new"
              element={
                <RequirePermission anyOf={[PERM.GATE_OUT_REQUEST]}>
                  <GateOutRequestPage />
                </RequirePermission>
              }
            />
            <Route
              path="gate-out/:id/edit"
              element={
                <RequirePermission anyOf={[PERM.GATE_OUT_REQUEST]}>
                  <GateOutRequestPage />
                </RequirePermission>
              }
            />
            {/* T8.5: before `gate-out/:id`, so "offline" is never read as an id. */}
            <Route
              path="gate-out/offline"
              element={
                <RequirePermission anyOf={[PERM.GATE_OUT_RELEASE]}>
                  <OfflineReleasePage />
                </RequirePermission>
              }
            />
            <Route
              path="gate-out/:id"
              element={
                <RequirePermission anyOf={[PERM.GATE_OUT_REQUEST, PERM.GATE_OUT_RELEASE]}>
                  <GateOutDetailPage />
                </RequirePermission>
              }
            />
            <Route
              path="approvals"
              element={
                <RequirePermission anyOf={[PERM.GATE_OUT_APPROVE, PERM.DISPOSAL_APPROVE]}>
                  <ApprovalsPage />
                </RequirePermission>
              }
            />
            <Route
              path="approvals/:id"
              element={
                <RequirePermission anyOf={[PERM.GATE_OUT_APPROVE, PERM.DISPOSAL_APPROVE]}>
                  <ApprovalDetailPage />
                </RequirePermission>
              }
            />
            {/* L1: in-app notifications are for everyone, so no permission gate. */}
            <Route path="notifications" element={<NotificationCentre />} />
            {/* §8.1: anybody who can capture offline needs to see what is waiting,
              so this is gated no more tightly than the flows that fill it. */}
            <Route path="sync" element={<SyncPage />} />
            {/* Stock needs no permission of its own: everyone who touches
              material has to be able to see what is there. */}
            <Route path="stock" element={<StockPage />} />
            <Route path="stock/serials/:serialNumber" element={<SerialHistoryPage />} />
            <Route path="stock/drums" element={<DrumsPage />} />
            <Route path="stock/drums/:drumNumber" element={<DrumHistoryPage />} />
            <Route
              path="stock/transfers"
              element={
                <RequirePermission anyOf={[PERM.STOCK_ADJUST, PERM.GATE_IN_POST]}>
                  <TransfersPage />
                </RequirePermission>
              }
            />
            <Route
              path="stock/counts"
              element={
                <RequirePermission anyOf={[PERM.STOCK_ADJUST]}>
                  <CountsPage />
                </RequirePermission>
              }
            />
            <Route
              path="stock/counts/:id"
              element={
                <RequirePermission anyOf={[PERM.STOCK_ADJUST]}>
                  <CountDetailPage />
                </RequirePermission>
              }
            />
            {/* T5.10: the technician's own screens. `jobs/custody` before
              `jobs/:id`, or "custody" would be read as a job id. */}
            <Route
              path="jobs"
              element={
                <RequirePermission anyOf={[PERM.JOB_CLOSEOUT, PERM.REPORT_VIEW_ALL]}>
                  <MyJobsPage />
                </RequirePermission>
              }
            />
            <Route path="jobs/custody" element={<MyCustodyPage />} />
            <Route
              path="jobs/reconciliation"
              element={
                <RequirePermission anyOf={[PERM.REPORT_VIEW_ALL, PERM.GATE_OUT_APPROVE]}>
                  <ReconciliationPage />
                </RequirePermission>
              }
            />
            <Route
              path="jobs/:id"
              element={
                <RequirePermission anyOf={[PERM.JOB_CLOSEOUT, PERM.REPORT_VIEW_ALL]}>
                  <JobCloseoutPage />
                </RequirePermission>
              }
            />
            {/* T5.11: the owner's view of the same material. */}
            <Route
              path="custody"
              element={
                <RequirePermission anyOf={[PERM.REPORT_VIEW_ALL, PERM.GATE_OUT_APPROVE]}>
                  <CustodyOverviewPage />
                </RequirePermission>
              }
            />
            {/* T6.6, J1/J2: quarantine and the decisions taken about it. Gated on
              stock.adjust, which is what a disposition needs. */}
            <Route
              path="quarantine"
              element={
                <RequirePermission anyOf={[PERM.STOCK_ADJUST, PERM.GATE_IN_POST]}>
                  <QuarantinePage />
                </RequirePermission>
              }
            />
            <Route
              path="quarantine/decisions"
              element={
                <RequirePermission anyOf={[PERM.STOCK_ADJUST, PERM.GATE_OUT_APPROVE]}>
                  <DispositionsPage />
                </RequirePermission>
              }
            />
            {/* J3: raising a write-off and approving one are different rights, and
              both reach this screen — the buttons inside differ. */}
            <Route
              path="disposals"
              element={
                <RequirePermission anyOf={[PERM.STOCK_ADJUST, PERM.DISPOSAL_APPROVE]}>
                  <DisposalsPage />
                </RequirePermission>
              }
            />
            {/* K1, K3: what a client owns, what is going back, what they signed. */}
            <Route
              path="client-material"
              element={
                <RequirePermission
                  anyOf={[PERM.GATE_OUT_RELEASE, PERM.REPORT_VIEW_ALL, PERM.GATE_OUT_APPROVE]}
                >
                  <ClientReturnsPage />
                </RequirePermission>
              }
            />
            {/* T5.12, M1: one register. Anyone who can approve or count can work
              through it — the actions inside are gated individually. */}
            <Route
              path="exceptions"
              element={
                <RequirePermission
                  anyOf={[PERM.GATE_OUT_APPROVE, PERM.STOCK_ADJUST, PERM.REPORT_VIEW_ALL]}
                >
                  <ExceptionsPage />
                </RequirePermission>
              }
            />
            {/* T7.7: the catalogue, then one screen that renders any report from
              what the server declares about it. */}
            <Route
              path="reports"
              element={
                <RequirePermission anyOf={[PERM.REPORT_VIEW_ALL]}>
                  <ReportsPage />
                </RequirePermission>
              }
            />
            {/* M5: before `reports/:slug`, or "retention" reads as a report slug. */}
            <Route
              path="reports/retention"
              element={
                <RequirePermission anyOf={[PERM.SETTINGS_MANAGE, PERM.REPORT_VIEW_ALL]}>
                  <RetentionReviewPage />
                </RequirePermission>
              }
            />
            <Route
              path="reports/:slug"
              element={
                <RequirePermission anyOf={[PERM.REPORT_VIEW_ALL]}>
                  <ReportPage />
                </RequirePermission>
              }
            />
            <Route
              path="settings"
              // No permission gate on the section itself: the security pane is
              // everybody's, and gating the parent on admin rights would hide a
              // technician's own fingerprint enrolment from them (B5, T8.10).
              // Each pane inside keeps its own guard.
              element={<SettingsLayout />}
            >
              <Route index element={<SettingsIndexRedirect />} />
              <Route
                path="catalogue"
                element={
                  <RequirePermission anyOf={[PERM.CATALOGUE_MANAGE]}>
                    <CataloguePage />
                  </RequirePermission>
                }
              />
              <Route
                path="network"
                element={
                  <RequirePermission anyOf={[PERM.CATALOGUE_MANAGE]}>
                    <NetworkPage />
                  </RequirePermission>
                }
              />
              <Route
                path="people"
                element={
                  <RequirePermission anyOf={[PERM.USERS_MANAGE]}>
                    <UsersPage />
                  </RequirePermission>
                }
              />
              {/* No permission gate: your own devices are yours to manage. */}
              <Route path="security" element={<SecurityPage />} />
          <Route path="notifications" element={<NotificationSettingsPage />} />
              <Route
                path="rules"
                element={
                  <RequirePermission anyOf={[PERM.SETTINGS_MANAGE]}>
                    <SettingsPage />
                  </RequirePermission>
                }
              />
              <Route
                path="organization"
                element={
                  <RequirePermission anyOf={[PERM.SETTINGS_MANAGE]}>
                    <OrganizationPage />
                  </RequirePermission>
                }
              />
              {/* Who signs for what (F3). Until this existed the only way to
                  write an approval rule was the Django admin. */}
              <Route
                path="approvals"
                element={
                  <RequirePermission anyOf={[PERM.SETTINGS_MANAGE]}>
                    <ApprovalRulesPage />
                  </RequirePermission>
                }
              />
            </Route>
          </Route>

          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Suspense>
    </ErrorBoundary>
  );
}
