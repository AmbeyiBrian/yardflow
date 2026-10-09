import { describe, expect, it } from 'vitest';

import {
  buildAddDay,
  canDecide,
  correctionPair,
  holdsRole,
  showDaysTab,
  sliceLabel,
  splitSlices,
  timeScopes,
} from './approvals';
import type { WorkDay, WorkDaySlice } from './types';

const slice = (o: Partial<WorkDaySlice>): WorkDaySlice => ({
  id: 1,
  approver: 1,
  project: 1,
  status: 'PENDING',
  ...o,
});

describe('roles and scopes (§4.18.6a, §4.18.11)', () => {
  it('finds the Director role by id', () => {
    expect(holdsRole([{ id: 3 }], 3)).toBe(true);
    expect(holdsRole([{ id: 3 }], null)).toBe(false);
    expect(holdsRole(undefined, 3)).toBe(false);
  });
  it('gives scopes by what the person can see', () => {
    expect(timeScopes({ managesProject: false, viewAll: false, isDirector: false })).toEqual(['mine']);
    expect(timeScopes({ managesProject: true, viewAll: false, isDirector: false })).toEqual(['mine', 'team']);
    expect(timeScopes({ managesProject: false, viewAll: false, isDirector: true })).toEqual(['mine', 'team', 'all']);
    expect(timeScopes({ managesProject: false, viewAll: true, isDirector: false })).toEqual(['mine', 'team', 'all']);
  });
  it('shows the Days tab for a request or an open project', () => {
    expect(showDaysTab(0, false)).toBe(false);
    expect(showDaysTab(2, false)).toBe(true);
    expect(showDaysTab(0, true)).toBe(true);
  });
});

describe('slices (§4.18.5)', () => {
  const day = {
    slices: [
      slice({ id: 1, is_mine: true }),
      slice({ id: 2, approver_name: 'Wanjiru', project_name: 'Karen' }),
    ],
  } as WorkDay;
  it('splits mine from others', () => {
    const s = splitSlices(day.slices);
    expect(s.mine).toHaveLength(1);
    expect(s.others[0].id).toBe(2);
    expect(splitSlices(undefined).mine).toEqual([]);
  });
  it('decides only while my slice is pending', () => {
    expect(canDecide(day)).toBe(true);
    expect(canDecide({ slices: [slice({ is_mine: true, status: 'APPROVED' })] } as WorkDay)).toBe(false);
    expect(canDecide({ slices: [slice({})] } as WorkDay)).toBe(false);
  });
  it('words a slice', () => {
    expect(sliceLabel(slice({ approver_name: 'Wanjiru', project_name: 'Karen' }))).toBe('Karen: waiting on Wanjiru');
    expect(sliceLabel(slice({ is_mine: true, project: null }))).toBe('No project: yours');
  });
});

describe('corrections', () => {
  it('pairs original and corrected', () => {
    const f = (i: string | null) => i ?? 'open';
    expect(
      correctionPair(
        { original_in_at: 'a', original_out_at: null, corrected_in_at: 'b', corrected_out_at: 'c' },
        f,
      ),
    ).toEqual({ original: 'a – open', corrected: 'b – c' });
  });
});

describe('buildAddDay', () => {
  const ok = { person: '5', date: '2026-10-09', place: 'site:12', project: '', start: '08:00', end: '17:00', reason: ' on site ' };
  it('builds the body', () => {
    const r = buildAddDay(ok, 1);
    expect('body' in r && r.body.place).toEqual({ site: 12 });
    expect('body' in r && r.body.reason).toBe('on site');
    expect('body' in r && 'project' in r.body).toBe(false);
  });
  it('refuses the Director adding for themselves', () => {
    expect(buildAddDay(ok, 5)).toEqual({ error: 'You cannot add a day for yourself.' });
  });
  it('needs a reason, a place and a sane range', () => {
    expect('error' in buildAddDay({ ...ok, reason: ' ' }, 1)).toBe(true);
    expect('error' in buildAddDay({ ...ok, place: '' }, 1)).toBe(true);
    expect('error' in buildAddDay({ ...ok, end: '07:00' }, 1)).toBe(true);
  });
});
