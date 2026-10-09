/**
 * What the Money forms' pickers read, from the network or from the bundle
 * (design §4.17.8; R6).
 *
 * Online, nothing changes: the lists come from the API. With no network
 * (`useOffline().online === false`) the same pickers are filled from the
 * reference tables the last bundle stored, the way the gate-in and gate-out
 * forms already work. One small hook per kind keeps the forms from knowing
 * which side they are reading; each query is enabled on one side only, so
 * offline there is no request to hang or retry.
 */

import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';

import { useList } from '../../api/hooks';
import { readReference } from '../../offline/db';
import { useOffline } from '../../offline/OfflineProvider';
import type { Project } from '../projects/types';
import type { Location, Site } from '../settings/types';
import { useAssets, type Asset } from '../assets/api';
import { fuelAssets } from './fuelVehicle';
import { selectableSuppliers } from '../receiving/supplierPick';
import type { Supplier as RegisterSupplier } from '../settings/suppliersApi';
import { useQueuedFinance } from './offline';
import {
  mergeSupplierOptions,
  queuedSupplierOptions,
  supplierOptions,
  type BundleSupplier,
  type SupplierOption,
} from './suppliersOffline';
import { useAllowanceRequests, useCasuals, useFinanceSettings } from './api';
import {
  bundleCategories,
  bundleFloats,
  bundleLimits,
  bundleOpenProjects,
  bundleSiteProjects,
  bundleReceivable,
  bundleVehicles,
  type BundleHeadroom,
  type BundleLocation,
  type BundleSite,
  type BundleVehicle,
  type CategoryChoice,
  type FloatChoice,
  type ProjectChoice,
  type SiteChoice,
} from './bundle';
import { bundleCasualOptions, type BundleCasual, type CasualOption } from './drafts';
import { candidateProjects } from './rules';
import type { AllowanceLimits, ExpenseCategory } from './types';

/**
 * A stored reference table, read only when there is no network. `networkMode:
 * 'always'` because this is a local read: react-query would otherwise pause it
 * for the very reason it is being used.
 */
function useBundleRows<T>(key: string, offline: boolean) {
  return useQuery({
    queryKey: ['offline-reference', key],
    queryFn: async () => (await readReference<T>(key)).rows,
    enabled: offline,
    networkMode: 'always',
    // A bundle refresh writes new rows without telling react-query.
    staleTime: 0,
  });
}

/** Sites for the picker (R1). */
export function useMoneySites(enabled = true): { sites: SiteChoice[]; loading: boolean } {
  const { online } = useOffline();
  const network = useList<Site>('sites', { page_size: 300 }, { enabled: enabled && online });
  const stored = useBundleRows<BundleSite>('sites', enabled && !online);
  const sites = online ? (network.data?.results ?? []) : (stored.data ?? []);
  return { sites, loading: online ? network.isLoading : stored.isLoading };
}

export interface SiteProjects {
  /** Open projects on the chosen site (empty in direct mode). */
  candidates: ProjectChoice[];
  /** All open projects, for direct mode. */
  allOpen: ProjectChoice[];
  loading: boolean;
  /** The lookup failed, so "no open project" would be a guess. */
  failed: boolean;
}

/**
 * Site → open projects, and the direct "pick a project" list (R1).
 *
 * Offline both come from each site's `open_projects` in the bundle.
 */
export function useSiteProjects(site: string, direct: boolean): SiteProjects {
  const { online } = useOffline();
  const forSite = useList<Project>(
    'projects',
    { site, status: 'OPEN', page_size: 100 },
    { enabled: online && Boolean(site) && !direct },
  );
  const open = useList<Project>(
    'projects',
    { status: 'OPEN', page_size: 200 },
    { enabled: online && direct },
  );
  const stored = useBundleRows<BundleSite>('sites', !online && (direct || Boolean(site)));

  return useMemo(() => {
    if (online) {
      return {
        candidates:
          site && !direct ? candidateProjects(Number(site), forSite.data?.results ?? []) : [],
        allOpen: open.data?.results ?? [],
        loading: direct ? open.isLoading : Boolean(site) && forSite.isLoading,
        failed: !direct && forSite.isError,
      };
    }
    const rows = stored.data ?? [];
    return {
      candidates: site && !direct ? bundleSiteProjects(rows, Number(site)) : [],
      allOpen: direct ? bundleOpenProjects(rows) : [],
      loading: stored.isLoading,
      failed: false,
    };
  }, [online, site, direct, forSite.data, forSite.isLoading, forSite.isError, open.data, open.isLoading, stored.data, stored.isLoading]);
}

/** Expense categories, each with its `kind` (R3). */
export function useMoneyCategories(): { categories: CategoryChoice[]; loading: boolean } {
  const { online } = useOffline();
  const network = useList<ExpenseCategory>(
    'expense-categories',
    { is_active: true, page_size: 100 },
    { enabled: online },
  );
  const stored = useBundleRows<CategoryChoice>('expense_categories', !online);
  return {
    categories: online ? (network.data?.results ?? []) : bundleCategories(stored.data ?? []),
    loading: online ? network.isLoading : stored.isLoading,
  };
}

/**
 * Casuals for the picker, narrowed by what was typed. Offline: the bundle's
 * list, with the ID masked. Casuals queued on this phone are merged in by the
 * caller (`mergeCasualOptions`).
 */
export function useMoneyCasuals(search: string): CasualOption[] {
  const { online } = useOffline();
  const network = useCasuals(search, online);
  const stored = useBundleRows<BundleCasual>('casuals', !online);
  return useMemo(
    () =>
      online
        ? (network.data?.results ?? []).map((c) => ({
            value: String(c.id),
            label: `${c.name} ${c.id_number}`,
          }))
        : bundleCasualOptions(stored.data ?? [], search),
    [online, network.data, stored.data, search],
  );
}

/** The caller's own PAID, unclosed floats with their balance, for "Paid from float". */
export function useMoneyFloats(): FloatChoice[] {
  const { online } = useOffline();
  const network = useAllowanceRequests({ mine: true, type: 'FLOAT', page_size: 100 }, online);
  const stored = useBundleRows<FloatChoice & { amount?: string }>('my_floats', !online);
  return useMemo(
    () =>
      online
        ? (network.data?.results ?? [])
            .filter((f) => f.type === 'FLOAT' && f.status === 'PAID' && !f.closed_at)
            .map((f) => ({ id: f.id, number: f.number, balance: f.balance ?? f.amount }))
        : bundleFloats(stored.data ?? []),
    [online, network.data, stored.data],
  );
}

/** Allowance limits for the early warning; the server still decides (R5). */
export function useMoneyLimits(): AllowanceLimits | undefined {
  const { online } = useOffline();
  const network = useFinanceSettings(online);
  const stored = useBundleRows<Record<string, unknown>>('finance_limits', !online);
  return online ? network.data?.allowance_limits : bundleLimits(stored.data ?? []);
}

/** A vehicle for the fuel picker: the register's row, or the bundle's (R14, §4.20.8). */
export type VehicleChoice = Pick<Asset, 'id' | 'name' | 'tag'>;

/**
 * Vehicles and generators for the fuel picker (R14; design 4.20.10). Online
 * reads the register. Offline reads the bundle's `vehicles` (§4.20.8); a phone
 * whose bundle has none falls back to the typed registration, so offline fuel
 * still queues.
 */
export function useVehicles(): {
  vehicles: VehicleChoice[];
  loading: boolean;
  /** False when this side has no list to offer. */
  available: boolean;
} {
  const { online } = useOffline();
  const network = useAssets({ status: 'ACTIVE', page_size: 300 });
  const stored = useBundleRows<BundleVehicle>('vehicles', !online);
  if (!online) {
    const vehicles = bundleVehicles(stored.data ?? []);
    return { vehicles, loading: stored.isLoading, available: vehicles.length > 0 };
  }
  return {
    vehicles: fuelAssets(network.data?.results ?? []),
    loading: network.isLoading,
    available: true,
  };
}

/**
 * Suppliers for the gate-in and purchase pickers (R15, §4.20.8): the register
 * online, the bundle's `suppliers` offline, plus any added on this phone and
 * still waiting (`q:<uuid>`), so the gate-in can name them before they land.
 */
export function useSupplierOptions(): { options: SupplierOption[]; loading: boolean } {
  const { online } = useOffline();
  const network = useList<RegisterSupplier>(
    'suppliers',
    { page_size: 300, is_active: true },
    { enabled: online },
  );
  const stored = useBundleRows<BundleSupplier>('suppliers', !online);
  const queued = useQueuedFinance();
  const options = useMemo(
    () =>
      mergeSupplierOptions(
        supplierOptions(
          online ? selectableSuppliers(network.data?.results ?? []) : (stored.data ?? []),
        ),
        queuedSupplierOptions(queued),
      ),
    [online, network.data, stored.data, queued],
  );
  return { options, loading: online ? network.isLoading : stored.isLoading };
}

/**
 * "Receive into" for an INTO_YARD purchase (§4.19.3): yards and stores, from
 * the network online and from the bundle's locations offline (§4.19.11).
 */
export function useReceivableLocations(): {
  locations: { id: number; label: string }[];
  loading: boolean;
} {
  const { online } = useOffline();
  const network = useList<Location>('locations', { page_size: 200 }, { enabled: online });
  const stored = useBundleRows<BundleLocation>('locations', !online);
  return online
    ? {
        locations: (network.data?.results ?? [])
          .filter((l) => l.is_active && (l.type === 'YARD' || l.type === 'STORE'))
          .map((l) => ({ id: l.id, label: `${l.code} · ${l.name}` })),
        loading: network.isLoading,
      }
    : { locations: bundleReceivable(stored.data ?? []), loading: stored.isLoading };
}

/**
 * Per-project budget headroom from the bundle, for the offline over-budget
 * warning (R9, §4.19.11). Empty when online (the server answers `budget-check`)
 * and for a caller who may not see cost.
 */
export function useBundleHeadroom(): BundleHeadroom[] {
  const { online } = useOffline();
  return useBundleRows<BundleHeadroom>('project_headroom', !online).data ?? [];
}
