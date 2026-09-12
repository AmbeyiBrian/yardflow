/**
 * Session and permission gates (design §7.2; requirements B1, B4).
 *
 * `GET /me` returns the user, their roles and their **resolved** permissions.
 * Resolution happens once, on the server, so the client never reimplements the
 * rules and then disagrees with them.
 *
 * The gates here are UX only. §7.2: "Permissions are re-checked server-side on
 * every call — the frontend gate is UX, not security."
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

import {
  api,
  clearTokens,
  getRefreshToken,
  restoreSession,
  setUnauthorizedHandler,
  storeTokens,
} from '../api/client';
import type { Permission } from './permissions';

export interface OrganizationSettings {
  money_tracking_enabled: boolean;
  min_stock_enabled: boolean;
  qr_labels_enabled: boolean;
  attachments_enabled: boolean;
  attachments_required_gate_in: boolean;
  attachments_required_gate_out: boolean;
  signature_required_on_release: boolean;
  client_waybill_enabled: boolean;
  timezone: string;
  currency: string;
}

export interface Organization {
  id: string;
  name: string;
  slug: string;
  status: 'ACTIVE' | 'SUSPENDED';
  settings: OrganizationSettings | null;
}

export interface Role {
  id: number;
  name: string;
  is_system: boolean;
}

export interface CurrentUser {
  id: number;
  email: string | null;
  phone: string | null;
  full_name: string;
  is_active: boolean;
  is_platform_admin: boolean;
  organization: Organization | null;
  roles: Role[];
  permissions: string[];
}

interface LoginResponse {
  access: string;
  refresh: string;
  user: CurrentUser;
}

interface SessionValue {
  user: CurrentUser | null;
  /** True until the initial `/me` hydration settles, so guards do not flash. */
  loading: boolean;
  login: (identifier: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refreshUser: () => Promise<void>;
  has: (permission: Permission) => boolean;
  hasAny: (...permissions: Permission[]) => boolean;
  isSuspended: boolean;
}

const SessionContext = createContext<SessionValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [loading, setLoading] = useState(true);

  const loadCurrentUser = useCallback(async () => {
    const me = await api.get<CurrentUser>('/me');
    setUser(me);
  }, []);

  // Boot: if a refresh token survived the reload, turn it into a session.
  useEffect(() => {
    let cancelled = false;

    (async () => {
      if (!getRefreshToken()) {
        setLoading(false);
        return;
      }
      try {
        const restored = await restoreSession();
        if (restored && !cancelled) {
          await loadCurrentUser();
        }
      } catch {
        clearTokens();
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [loadCurrentUser]);

  // A refresh that cannot be renewed means the session is over. Clearing the
  // user here is what sends the route guards back to the login screen.
  useEffect(() => {
    setUnauthorizedHandler(() => setUser(null));
    return () => setUnauthorizedHandler(() => {});
  }, []);

  const login = useCallback(async (identifier: string, password: string) => {
    const result = await api.post<LoginResponse>(
      '/auth/login',
      { identifier, password },
      { anonymous: true },
    );
    storeTokens({ access: result.access, refresh: result.refresh });
    setUser(result.user);
  }, []);

  const logout = useCallback(async () => {
    const refresh = getRefreshToken();
    try {
      if (refresh) {
        // Revokes the refresh token server-side (B1). Without this, logging out
        // would only forget the token locally.
        await api.post('/auth/logout', { refresh });
      }
    } catch {
      // Already invalid, or offline. Either way the local session ends.
    } finally {
      clearTokens();
      setUser(null);
    }
  }, []);

  const value = useMemo<SessionValue>(() => {
    const granted = new Set(user?.permissions ?? []);
    return {
      user,
      loading,
      login,
      logout,
      refreshUser: loadCurrentUser,
      has: (permission) => granted.has(permission),
      hasAny: (...permissions) => permissions.some((p) => granted.has(p)),
      isSuspended: user?.organization?.status === 'SUSPENDED',
    };
  }, [user, loading, login, logout, loadCurrentUser]);

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionValue {
  const value = useContext(SessionContext);
  if (!value) {
    throw new Error('useSession must be used inside <SessionProvider>.');
  }
  return value;
}

/** `usePermission(PERM.GATE_OUT_APPROVE)` — §7.2. */
export function usePermission(permission: Permission): boolean {
  return useSession().has(permission);
}

/**
 * Show children only if the user holds the permission (§7.2).
 *
 *     <Can permission={PERM.GATE_OUT_APPROVE}>
 *       <ApproveButton />
 *     </Can>
 */
export function Can({
  permission,
  anyOf,
  fallback = null,
  children,
}: {
  permission?: Permission;
  anyOf?: Permission[];
  fallback?: ReactNode;
  children: ReactNode;
}) {
  const { has, hasAny } = useSession();

  const allowed = permission
    ? has(permission)
    : anyOf && anyOf.length > 0
      ? hasAny(...anyOf)
      : true;

  return <>{allowed ? children : fallback}</>;
}
