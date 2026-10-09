/**
 * The places one can clock in at (design §4.18.3; R13).
 *
 * A site of any status but decommissioned, or a YARD / OFFICE location, that
 * is active and has coordinates. Online they come from the sites and locations
 * APIs; with no answer from them (offline, paused) the same list is built from
 * the offline bundle (§4.18.9), so callers do not change.
 */

import { useEffect, useMemo, useState } from 'react';

import { useList } from '../../api/hooks';
import { readReference } from '../../offline/db';
import type { Location, Site } from '../settings/types';
import { DEFAULT_ACCURACY_CAP_M } from './queued';
import type { ClockPlace } from './types';

const DEFAULT_RADIUS_M = 200;

type SiteRow = Site & {
  radius_m?: number | null;
  is_active?: boolean;
  open_projects?: { id: number; reference: string; title: string }[];
};
type LocationRow = Omit<Location, 'type'> & {
  type: Location['type'] | 'OFFICE';
  latitude?: string | null;
  longitude?: string | null;
  radius_m?: number | null;
};

function coords(lat: string | null | undefined, lng: string | null | undefined) {
  if (lat == null || lng == null || lat === '' || lng === '') return null;
  const a = Number(lat);
  const b = Number(lng);
  return Number.isFinite(a) && Number.isFinite(b) ? { lat: a, lng: b } : null;
}

export function clockablePlaces(sites: readonly SiteRow[], locations: readonly LocationRow[]): ClockPlace[] {
  const out: ClockPlace[] = [];
  for (const s of sites) {
    const c = coords(s.latitude, s.longitude);
    if (!c || s.status === 'DECOMMISSIONED' || s.is_active === false) continue;
    out.push({
      kind: 'site',
      id: s.id,
      name: s.name,
      label: 'Site',
      ...c,
      radius_m: s.radius_m ?? DEFAULT_RADIUS_M,
      ...(s.open_projects ? { open_projects: s.open_projects } : {}),
    });
  }
  for (const l of locations) {
    if ((l.type !== 'YARD' && l.type !== 'OFFICE') || !l.is_active || l.is_system) continue;
    const c = coords(l.latitude, l.longitude);
    if (!c) continue;
    out.push({
      kind: 'location',
      id: l.id,
      name: l.name,
      label: l.type === 'YARD' ? 'Yard' : 'Office',
      ...c,
      radius_m: l.radius_m ?? DEFAULT_RADIUS_M,
    });
  }
  return out;
}

/**
 * The bundle's rows are slimmer than the API's: no `is_active` or `is_system`
 * (it only lists active, non-system places), and `offices` holds the OFFICE
 * locations the `locations` list leaves out. Merged by id so a YARD that
 * appears in both is offered once.
 */
export function bundlePlaces(
  sites: readonly SiteRow[],
  locations: readonly LocationRow[],
  offices: readonly LocationRow[],
): ClockPlace[] {
  const seen = new Set<number>();
  const merged: LocationRow[] = [];
  for (const row of [...locations, ...offices]) {
    if (seen.has(row.id)) continue;
    seen.add(row.id);
    merged.push({ ...row, is_active: row.is_active ?? true, is_system: row.is_system ?? false });
  }
  return clockablePlaces(sites, merged);
}

function useBundlePlaces(): ClockPlace[] {
  const [places, setPlaces] = useState<ClockPlace[]>([]);
  useEffect(() => {
    let live = true;
    void Promise.all([
      readReference<SiteRow>('sites'),
      readReference<LocationRow>('locations'),
      readReference<LocationRow>('offices'),
    ]).then(([sites, locations, offices]) => {
      if (live) setPlaces(bundlePlaces(sites.rows, locations.rows, offices.rows));
    });
    return () => {
      live = false;
    };
  }, []);
  return places;
}

/** The accuracy cap (§4.18.3) from the last bundle, so the phone can refuse early offline. */
export function useAccuracyCap(): number {
  const [cap, setCap] = useState(DEFAULT_ACCURACY_CAP_M);
  useEffect(() => {
    let live = true;
    void readReference<{ accuracy_cap_m?: number }>('attendance').then(({ rows }) => {
      const value = rows[0]?.accuracy_cap_m;
      if (live && typeof value === 'number') setCap(value);
    });
    return () => {
      live = false;
    };
  }, []);
  return cap;
}

export function usePlaces(): { places: ClockPlace[]; loading: boolean; failed: boolean } {
  const sites = useList<SiteRow>('sites', { page_size: 300 });
  const locations = useList<LocationRow>('locations', { is_active: true, page_size: 300 });
  const cached = useBundlePlaces();
  const answered = Boolean(sites.data && locations.data);
  const online = useMemo(
    () => clockablePlaces(sites.data?.results ?? [], locations.data?.results ?? []),
    [sites.data, locations.data],
  );
  // Online answers win; without them (no signal, or a gateway error) the
  // bundle is what the phone has, and "failed" only holds when it has nothing.
  const places = answered ? online : cached;
  const failedOnline = sites.isError || locations.isError;
  return {
    places,
    loading: !answered && !failedOnline && (sites.isLoading || locations.isLoading),
    failed: failedOnline && cached.length === 0,
  };
}
