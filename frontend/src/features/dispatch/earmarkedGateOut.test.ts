import { describe, expect, it } from 'vitest';

import {
  allTicked,
  divertErrors,
  diversionText,
  earmarkedBulkProblem,
  earmarkedRows,
  earmarkedToLines,
  groupJobsByProject,
  jobMissing,
  jobRule,
  type EarmarkedJob,
  type EarmarkedLine,
} from './earmarkedGateOut';
import type { GateOutLine } from './types';

const boards: EarmarkedLine = {
  item_type: 3,
  item_name: 'Baseband board',
  tracking_mode: 'SERIALIZED',
  uom: 'EA',
  owner_type: 'OWN',
  owner_client: null,
  condition: 'NEW',
  requested_qty: '2',
  units: [
    { serial_unit: 11, serial_number: 'BB-1' },
    { serial_unit: 12, serial_number: 'BB-2' },
  ],
  reels: [],
};
const cable: EarmarkedLine = {
  item_type: 5,
  item_name: 'Fibre',
  tracking_mode: 'REEL',
  uom: 'M',
  owner_type: 'OWN',
  owner_client: null,
  condition: 'NEW',
  requested_qty: '800.000',
  units: [],
  reels: [
    { reel: 21, drum_number: 'DRM-1', length: '500.000' },
    { reel: 22, drum_number: 'DRM-2', length: '300.000' },
  ],
};
const jumpers: EarmarkedLine = {
  item_type: 4,
  item_name: 'Jumper',
  tracking_mode: 'BULK',
  uom: 'EA',
  owner_type: 'CLIENT',
  owner_client: 9,
  condition: 'GOOD',
  requested_qty: '25.000',
  units: [],
  reels: [],
};
const all = [boards, cable, jumpers];

describe('earmarkedRows and allTicked', () => {
  it('has one row per unit, drum and bulk line, all ticked', () => {
    const rows = earmarkedRows(all);
    expect(rows.map((row) => row.key)).toEqual(['u:11', 'u:12', 'd:21', 'd:22', 'b:2']);
    expect(rows[2].label).toBe('DRM-1 (500 M)');
    expect(rows[4].quantity).toBe('25');
    expect([...allTicked(all)]).toHaveLength(5);
  });
});

describe('earmarkedToLines', () => {
  it('turns everything ticked into three lines', () => {
    const { lines, skipped } = earmarkedToLines(all, allTicked(all), {}, []);
    expect(skipped).toBe(0);
    expect(lines).toHaveLength(3);
    expect(lines[0].serials?.map((s) => s.serial_unit)).toEqual([11, 12]);
    expect(lines[0].requested_qty).toBe('2');
    expect(lines[1].tracking_mode).toBe('REEL');
    expect(lines[1].reels).toEqual([
      { reel: 21, drum_number: 'DRM-1', length_requested: '500' },
      { reel: 22, drum_number: 'DRM-2', length_requested: '300' },
    ]);
    expect(lines[1].requested_qty).toBe('800');
    expect(lines[2].requested_qty).toBe('25');
    expect(lines[2].owner_client).toBe(9);
  });

  it('leaves out what is unticked and a line with nothing left', () => {
    const ticked = new Set(['u:12', 'd:22']);
    const { lines } = earmarkedToLines(all, ticked, {}, []);
    expect(lines).toHaveLength(2);
    expect(lines[0].serials?.map((s) => s.serial_number)).toEqual(['BB-2']);
    expect(lines[0].requested_qty).toBe('1');
    expect(lines[1].requested_qty).toBe('300');
  });

  it('takes a lowered bulk quantity', () => {
    const { lines } = earmarkedToLines([jumpers], allTicked([jumpers]), { 'b:0': '10' }, []);
    expect(lines[0].requested_qty).toBe('10');
  });

  it('does not add what is already on the request', () => {
    const existing: GateOutLine[] = [
      {
        item_type: 3,
        tracking_mode: 'SERIALIZED',
        requested_qty: '1',
        uom: 'EA',
        serials: [{ serial_unit: 11 }],
      },
      {
        item_type: 5,
        tracking_mode: 'REEL',
        requested_qty: '500',
        uom: 'M',
        reels: [{ reel: 21, length_requested: '500' }],
      },
      {
        item_type: 4,
        tracking_mode: 'BULK',
        requested_qty: '5',
        uom: 'EA',
        owner_client: 9,
        condition: 'GOOD',
      },
    ];
    const { lines, skipped } = earmarkedToLines(all, allTicked(all), {}, existing);
    expect(skipped).toBe(3);
    expect(lines).toHaveLength(2);
    expect(lines[0].serials?.map((s) => s.serial_unit)).toEqual([12]);
    expect(lines[1].reels?.map((r) => r.reel)).toEqual([22]);
  });
});

describe('earmarkedBulkProblem', () => {
  it('refuses nothing, zero, and more than is earmarked', () => {
    expect(earmarkedBulkProblem('', '25', 'EA')).toMatch(/how much/);
    expect(earmarkedBulkProblem('0', '25', 'EA')).toMatch(/how much/);
    expect(earmarkedBulkProblem('30', '25.000', 'EA')).toMatch(/Only 25 EA/);
    expect(earmarkedBulkProblem('25', '25.000', 'EA')).toBeNull();
  });
});

const job = (id: number, project: number | null, name = ''): EarmarkedJob => ({
  id,
  reference: `J-${id}`,
  project,
  project_name: name,
});

describe('job rule', () => {
  it('is none, single, required or optional', () => {
    expect(jobRule([]).kind).toBe('none');
    expect(jobRule([job(1, 7)])).toEqual({ kind: 'single', job: job(1, 7) });
    expect(jobRule([job(1, 7), job(2, 8)]).kind).toBe('required');
    expect(jobRule([job(1, 7), job(2, 7)]).kind).toBe('optional');
    expect(jobRule([job(1, 7), job(2, null)]).kind).toBe('required');
  });

  it('is missing only when required and not chosen', () => {
    const rule = jobRule([job(1, 7), job(2, 8)]);
    expect(jobMissing(rule, '')).toBe(true);
    expect(jobMissing(rule, '2')).toBe(false);
    expect(jobMissing(jobRule([]), '')).toBe(false);
  });

  it('groups jobs by project, no project last', () => {
    const groups = groupJobsByProject([
      job(1, null),
      job(2, 8, 'Beta'),
      job(3, 7, 'Alpha'),
      job(4, 8, 'Beta'),
    ]);
    expect(groups.map((g) => g.label)).toEqual(['Beta', 'Alpha', 'No project']);
    expect(groups[0].jobs.map((j) => j.id)).toEqual([2, 4]);
  });
});

describe('diversionText', () => {
  it('names units, drums, or a bulk quantity, with the reason', () => {
    expect(
      diversionText(
        { site: 1, site_name: 'Site A', quantity: '1', serials: ['BB-1'] },
        'Plan changed',
      ),
    ).toBe('Diverted from Site A: 1 (BB-1) — Plan changed');
    expect(
      diversionText(
        { site: 1, site_name: 'Site A', quantity: '800.000', drums: ['DRM-1', 'DRM-2'] },
        'x',
      ),
    ).toBe('Diverted from Site A: 800 (DRM-1, DRM-2) — x');
    expect(diversionText({ site: 1, site_name: 'Site A', quantity: '15.000' }, '')).toBe(
      'Diverted from Site A: 15 — no reason given',
    );
  });
});

describe('divertErrors', () => {
  it('picks the divert_reason errors by line index', () => {
    expect(
      divertErrors({
        'lines.1.divert_reason': ['Site A: 1 would be diverted.'],
        'lines.0.requested_qty': ['short'],
      }),
    ).toEqual({ 1: 'Site A: 1 would be diverted.' });
    expect(divertErrors(undefined)).toEqual({});
  });
});
