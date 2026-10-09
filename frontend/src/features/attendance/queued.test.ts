import { describe, expect, it } from 'vitest';

import type { QueuedMutation } from '../../offline/db';
import { bundlePlaces } from './places';
import {
  attendanceEntries,
  clockInPayload,
  clockOutPayload,
  earlyRefusal,
  pendingAttendance,
  resolveSession,
  sessionFromClockIn,
  sessionFromServer,
  type LocalSession,
} from './queued';
import type { ClockPlace, WorkSession } from './types';

const yard: ClockPlace = {
  kind: 'location',
  id: 3,
  name: 'Main yard',
  label: 'Yard',
  lat: -1.3,
  lng: 36.8,
  radius_m: 200,
};
const site: ClockPlace = { ...yard, kind: 'site', id: 9, name: 'Karen Mast', label: 'Site' };

const fixAt = (lat: number, lng: number, accuracy_m = 10) => ({ lat, lng, accuracy_m });

function row(partial: Partial<QueuedMutation>): QueuedMutation {
  return {
    client_uuid: 'u',
    operation: 'CLOCK_IN',
    payload: {},
    captured_at: '2026-10-09T05:00:00.000Z',
    status: 'PENDING',
    attempts: 0,
    ...partial,
  };
}

describe('clock-in payload (R13, 4.18.9)', () => {
  it('names a site and carries the area the phone checked', () => {
    const body = clockInPayload({ place: site, project: 4, fix: fixAt(-1.3, 36.8), client_uuid: 'c1' });
    expect(body).toEqual({
      site: 9,
      project: 4,
      fix: { lat: -1.3, lng: 36.8, accuracy_m: 10 },
      client_uuid: 'c1',
      place_area: { lat: -1.3, lng: 36.8, radius: 200 },
    });
  });

  it('names a location, and leaves the project out when there is none', () => {
    const body = clockInPayload({ place: yard, fix: fixAt(-1.3, 36.8), client_uuid: 'c2' });
    expect(body.location).toBe(3);
    expect(body).not.toHaveProperty('site');
    expect(body).not.toHaveProperty('project');
  });
});

describe('clock-out payload', () => {
  const session = sessionFromClockIn({ place: yard, client_uuid: 'in-1', clock_in_at: 't' });

  it('names the session by its clock-in uuid and goes without a fix', () => {
    const body = clockOutPayload({ session, fix: null, client_uuid: 'out-1' });
    expect(body.session_client_uuid).toBe('in-1');
    expect(body.fix).toBeNull();
    expect(body.place_area).toEqual({ lat: -1.3, lng: 36.8, radius: 200 });
  });

  it('omits the area when the phone does not know it', () => {
    const body = clockOutPayload({ session: { ...session, place_area: null }, fix: null, client_uuid: 'o' });
    expect(body).not.toHaveProperty('place_area');
  });
});

describe('early refusal', () => {
  it('passes inside the area', () => {
    expect(earlyRefusal(fixAt(-1.3, 36.8), yard, 100)).toBeNull();
  });

  it('says how far, in the server words', () => {
    // about 0.01 degrees of latitude is roughly 1.1 km
    const message = earlyRefusal(fixAt(-1.29, 36.8), yard, 100);
    expect(message).toMatch(/^You are .* from Main yard \(limit 200 m\)\.$/);
  });

  it('refuses a read that is too vague for the cached cap', () => {
    expect(earlyRefusal(fixAt(-1.3, 36.8, 500), yard, 100)).toMatch(/could not fix its position/);
    expect(earlyRefusal(fixAt(-1.3, 36.8, 500), yard, 600)).toBeNull();
  });

  it('asks for location when there is no fix', () => {
    expect(earlyRefusal(null, yard, 100)).toBe('Turn location on to clock in.');
  });
});

describe('which session the card shows', () => {
  const local: LocalSession = sessionFromClockIn({ place: yard, client_uuid: 'in-1', clock_in_at: 't' });
  const server: LocalSession = { ...local, session_client_uuid: '7', queued: false };

  it('prefers the local row while something is waiting to send', () => {
    expect(resolveSession({ server: null, local, waiting: true })).toBe(local);
    // a queued clock-out cleared the local row: clocked out, whatever the server still says
    expect(resolveSession({ server, local: null, waiting: true })).toBeNull();
  });

  it('prefers the server once nothing is waiting', () => {
    expect(resolveSession({ server, local, waiting: false })).toBe(server);
    expect(resolveSession({ server: null, local, waiting: false })).toBeNull();
  });

  it('falls back to the local row when the server did not answer', () => {
    expect(resolveSession({ server: undefined, local, waiting: false })).toBe(local);
  });

  it('keeps a server session for an offline clock-out, with its area when the place is cached', () => {
    const ws = {
      id: 7,
      site: null,
      location: 3,
      place_name: 'Main yard',
      project_name: null,
      clock_in_at: 't',
    } as unknown as WorkSession;
    const mapped = sessionFromServer(ws, [yard, site]);
    expect(mapped.session_client_uuid).toBe('7');
    expect(mapped.place_area).toEqual({ lat: -1.3, lng: 36.8, radius: 200 });
    expect(sessionFromServer(ws, []).place_area).toBeNull();
  });
});

describe('queued attendance entries', () => {
  it('lists waiting and refused ones, oldest first, and no clock-ins that landed', () => {
    const rows = [
      row({ client_uuid: 'b', operation: 'CLOCK_OUT', captured_at: '2026-10-09T10:00:00.000Z' }),
      row({ client_uuid: 'a', captured_at: '2026-10-09T05:00:00.000Z' }),
      row({ client_uuid: 'x', status: 'APPLIED' }),
      row({ client_uuid: 'r', status: 'REJECTED', exception_reason: 'CLOCK_OUTSIDE_AREA: 640 m' }),
      row({ client_uuid: 'e', operation: 'EXPENSE' }),
    ];
    const entries = attendanceEntries(rows);
    expect(entries.map((e) => e.client_uuid)).toEqual(['a', 'r', 'b']);
    expect(entries.find((e) => e.client_uuid === 'r')).toMatchObject({
      state: 'REFUSED',
      reason: 'CLOCK_OUTSIDE_AREA: 640 m',
    });
    expect(entries[0].state).toBe('WAITING');
  });

  it('counts only pending attendance as waiting', () => {
    const rows = [
      row({ status: 'APPLIED' }),
      row({ client_uuid: 'p', operation: 'CLOCK_OUT' }),
      row({ client_uuid: 'q', operation: 'EXPENSE' }),
    ];
    expect(pendingAttendance(rows).map((r) => r.client_uuid)).toEqual(['p']);
  });
});

describe('places from the bundle', () => {
  it('offers sites, yards and offices with coordinates, once each', () => {
    const places = bundlePlaces(
      [
        { id: 1, name: 'Karen Mast', latitude: '-1.3', longitude: '36.8', radius_m: 150 },
        { id: 2, name: 'No coords', latitude: null, longitude: null },
      ] as never,
      [{ id: 3, name: 'Main yard', type: 'YARD', latitude: '-1.2', longitude: '36.7', radius_m: 300 }] as never,
      [
        { id: 3, name: 'Main yard', type: 'YARD', latitude: '-1.2', longitude: '36.7', radius_m: 300 },
        { id: 4, name: 'HQ', type: 'OFFICE', latitude: '-1.1', longitude: '36.9', radius_m: null },
      ] as never,
    );
    expect(places.map((p) => `${p.kind}:${p.id}:${p.radius_m}`)).toEqual([
      'site:1:150',
      'location:3:300',
      'location:4:200',
    ]);
  });
});
