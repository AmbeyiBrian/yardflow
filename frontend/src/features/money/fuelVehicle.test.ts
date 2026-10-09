import { describe, expect, it } from 'vitest';

import { fuelAssets, fuelVehicleBody, fuelVehicleError, vehicleLabel } from './fuelVehicle';

describe('fuel vehicle (§4.20.10)', () => {
  it('offers only active vehicles and generators', () => {
    const rows = [
      { type: 'VEHICLE', status: 'ACTIVE' },
      { type: 'GENERATOR', status: 'ACTIVE' },
      { type: 'TOOL', status: 'ACTIVE' },
      { type: 'VEHICLE', status: 'CLOSED' },
    ] as const;
    expect(fuelAssets([...rows])).toHaveLength(2);
  });

  it('labels with the tag when there is one', () => {
    expect(vehicleLabel({ name: 'Hilux', tag: 'KDA 123A' })).toBe('KDA 123A · Hilux');
    expect(vehicleLabel({ name: 'Genset', tag: '' })).toBe('Genset');
  });

  it('asks for a vehicle, or a registration when not ours', () => {
    expect(fuelVehicleError({ notOurs: false, vehicle: '', reg: 'KAA 1A' })).toMatch(/Pick/);
    expect(fuelVehicleError({ notOurs: false, vehicle: '4', reg: '' })).toBeNull();
    expect(fuelVehicleError({ notOurs: true, vehicle: '4', reg: ' ' })).toMatch(/registration/);
    expect(fuelVehicleError({ notOurs: true, vehicle: '', reg: 'KAA 1A' })).toBeNull();
  });

  it('sends one of vehicle or vehicle_reg', () => {
    expect(fuelVehicleBody({ notOurs: false, vehicle: '4', reg: 'stale' })).toEqual({
      vehicle: 4,
      vehicle_reg: '',
    });
    expect(fuelVehicleBody({ notOurs: true, vehicle: '4', reg: ' KAA 1A ' })).toEqual({
      vehicle: null,
      vehicle_reg: 'KAA 1A',
    });
  });
});
