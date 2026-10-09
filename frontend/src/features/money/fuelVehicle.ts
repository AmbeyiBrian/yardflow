/**
 * Pure helpers for the fuel expense's vehicle (design §4.20.10; R14).
 *
 * Fuel is bought for a registered asset (a vehicle or a generator) so spend
 * per litre can be read off the asset (§4.20.4). A fill for somebody else's
 * vehicle is ticked "Not ours" and keeps the typed registration, as before.
 */

import type { Asset } from '../assets/api';

/** Assets that burn fuel: active VEHICLE and GENERATOR only (§4.20.1). */
export function fuelAssets<T extends Pick<Asset, 'type' | 'status'>>(assets: T[]): T[] {
  return assets.filter(
    (a) => (a.type === 'VEHICLE' || a.type === 'GENERATOR') && a.status === 'ACTIVE',
  );
}

/** "KDA 123A · Hilux", or the name alone when the tag is blank. */
export function vehicleLabel(a: Pick<Asset, 'name' | 'tag'>): string {
  return a.tag ? `${a.tag} · ${a.name}` : a.name;
}

export interface FuelVehicleInput {
  /** True when the fill is for a vehicle that is not in the asset register. */
  notOurs: boolean;
  vehicle: string;
  reg: string;
}

/** The message to show, or null when the vehicle answer is complete. */
export function fuelVehicleError(i: FuelVehicleInput): string | null {
  if (i.notOurs) return i.reg.trim() ? null : 'Fuel needs the vehicle registration.';
  return i.vehicle ? null : 'Pick the vehicle, or tick Not ours.';
}

/**
 * What goes on the wire: exactly one of `vehicle` or `vehicle_reg` (§4.20.10).
 * Offline (T17.16's bundle has no vehicles yet) the typed registration is the
 * only way to answer, so the caller passes `notOurs` as true there.
 */
export function fuelVehicleBody(i: FuelVehicleInput): {
  vehicle: number | null;
  vehicle_reg: string;
} {
  if (i.notOurs) return { vehicle: null, vehicle_reg: i.reg.trim() };
  return { vehicle: i.vehicle ? Number(i.vehicle) : null, vehicle_reg: '' };
}
