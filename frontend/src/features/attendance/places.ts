/**
 * The places one can clock in at (design §4.18.3; R13).
 *
 * A site of any status but decommissioned, or a YARD / OFFICE location, that
 * is active and has coordinates. Online they come from the sites and locations
 * APIs. T16.15 adds the offline bundle behind this same hook, so callers do
 * not change.
 */

import { useMemo } from 'react';

import { useList } from '../../api/hooks';
import type { Location, Site } from '../settings/types';
import type { ClockPlace } from './types';

const DEFAULT_RADIUS_M = 200;

type SiteRow = Site & { radius_m?: number | null; is_active?: boolean };
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

/** T16.15 seam: swap the source for the bundle when offline. */
export function usePlaces(): { places: ClockPlace[]; loading: boolean; failed: boolean } {
  const sites = useList<SiteRow>('sites', { page_size: 300 });
  const locations = useList<LocationRow>('locations', { is_active: true, page_size: 300 });
  const places = useMemo(
    () => clockablePlaces(sites.data?.results ?? [], locations.data?.results ?? []),
    [sites.data, locations.data],
  );
  return {
    places,
    loading: sites.isLoading || locations.isLoading,
    failed: sites.isError || locations.isError,
  };
}
