import { describe, expect, it } from 'vitest';

import {
  boxErrorMessages,
  boxLabel,
  boxOfSerial,
  boxPath,
  buildTree,
  canBeParent,
  depthOf,
  describeCounts,
  findByCode,
  linesForPayload,
  normaliseBoxes,
  parentChoices,
  removalBlock,
  serialsInBox,
  withoutBox,
  type DraftBox,
} from './gateInBoxes';
import type { GateInLineInput } from './types';

const box = (key: string, code = '', parent_key = ''): DraftBox => ({
  key,
  code,
  parent_key,
  label_text: '',
});

function line(partial: Partial<GateInLineInput>): GateInLineInput {
  return {
    item_type: 1,
    item_name: 'RRU',
    tracking_mode: 'SERIALIZED',
    quantity: '0',
    uom: 'ea',
    condition: 'NEW',
    owner_type: 'OWN',
    owner_client: null,
    ...partial,
  };
}

const units = (...pairs: [string, string][]) =>
  pairs.map(([serial_number, box_key]) => ({ serial_number, box_key }));

describe('normaliseBoxes', () => {
  it('tolerates a draft stored before boxes existed', () => {
    expect(normaliseBoxes(undefined)).toEqual([]);
    expect(normaliseBoxes('nope')).toEqual([]);
  });
  it('fills blanks, drops junk and repeated keys', () => {
    expect(
      normaliseBoxes([{ key: 'a', code: null, parent_key: null }, null, { key: 'a' }, {}]),
    ).toEqual([{ key: 'a', code: '', parent_key: '', label_text: '' }]);
  });
});

describe('labels', () => {
  const boxes = [box('a', 'PAL-7'), box('b'), box('c', '  '), box('d', 'CTN-1', 'a')];
  it('uses the code, else counts the unlabelled ones in order', () => {
    expect(boxLabel(boxes, 'a')).toBe('PAL-7');
    expect(boxLabel(boxes, 'b')).toBe('Unlabelled box 1');
    expect(boxLabel(boxes, 'c')).toBe('Unlabelled box 2');
  });
  it('names a box that is not there without throwing', () => {
    expect(boxLabel(boxes, 'zzz')).toBe('A box');
  });
  it('shows the path through parents', () => {
    expect(boxPath(boxes, 'd')).toBe('PAL-7 › CTN-1');
  });
  it('finds a code regardless of case and spaces', () => {
    expect(findByCode(boxes, ' pal-7 ')?.key).toBe('a');
    expect(findByCode(boxes, '')).toBeUndefined();
  });
});

describe('depth and the parent picker', () => {
  const boxes = [box('p', 'P'), box('c', 'C', 'p'), box('u', 'U', 'c'), box('solo', 'S')];
  it('counts levels', () => {
    expect(depthOf(boxes, 'p')).toBe(1);
    expect(depthOf(boxes, 'u')).toBe(3);
  });
  it('treats a loop as too deep rather than looping', () => {
    const loop = [box('x', 'X', 'y'), box('y', 'Y', 'x')];
    expect(depthOf(loop, 'x')).toBeGreaterThan(3);
  });
  it('offers only boxes with room for one more level', () => {
    expect(parentChoices(boxes).map((b) => b.key)).toEqual(['p', 'c', 'solo']);
  });
  it('never offers a box itself or anything inside it', () => {
    expect(parentChoices(boxes, 'p').map((b) => b.key)).toEqual([]);
    expect(parentChoices(boxes, 'u').map((b) => b.key)).toEqual(['p', 'c', 'solo']);
  });
  it('keeps the whole subtree within three levels when moving a box', () => {
    // c carries u below it (height 2), so it fits under a root but not under u or itself.
    expect(parentChoices(boxes, 'c').map((b) => b.key)).toEqual(['p', 'solo']);
    const tall = [...boxes, box('d', 'D', 'solo')];
    expect(canBeParent(tall, 'd', 'c')).toBe(false);
    expect(canBeParent(tall, 'solo', 'c')).toBe(true);
  });
});

describe('serial to box mapping', () => {
  const lines = [
    line({ serials: units(['S1', 'a'], ['S2', 'a'], ['S3', '']) }),
    line({ serials: units(['S4', 'b']) }),
  ];
  it('finds the box a unit went into', () => {
    expect(boxOfSerial(lines, 'S1')).toBe('a');
    expect(boxOfSerial(lines, 'S3')).toBe('');
    expect(boxOfSerial(lines, 'nope')).toBe('');
  });
  it('lists units in a box across lines', () => {
    expect(serialsInBox(lines, 'a')).toEqual(['S1', 'S2']);
  });
});

describe('the tree and its counts', () => {
  const boxes = [box('pal', 'PAL-7'), box('c1', 'CTN-1', 'pal'), box('c2', 'CTN-2', 'pal'), box('b')];
  const lines = [
    line({ serials: units(['S1', 'c1'], ['S2', 'c1'], ['S3', 'c2'], ['S4', '']) }),
    line({ tracking_mode: 'BULK', item_name: 'Clamp', quantity: '10', box_key: 'c2' }),
    line({ tracking_mode: 'BULK', item_name: 'Tie', quantity: '5' }),
    line({ tracking_mode: 'REEL', item_name: 'Cable', quantity: '250' }),
  ];
  const tree = buildTree(boxes, lines);

  it('nests boxes and counts units and boxes below', () => {
    expect(tree.roots.map((n) => n.label)).toEqual(['PAL-7', 'Unlabelled box 1']);
    const pallet = tree.roots[0];
    expect(pallet.children.map((n) => n.label)).toEqual(['CTN-1', 'CTN-2']);
    expect(pallet.boxCount).toBe(2);
    expect(pallet.totalUnits).toBe(13);
    expect(describeCounts(pallet)).toBe('2 boxes, 13 units');
    expect(describeCounts(pallet.children[0])).toBe('2 units');
    expect(describeCounts(pallet.children[1])).toBe('11 units');
  });
  it('says empty for an empty box', () => {
    expect(describeCounts(tree.roots[1])).toBe('empty');
  });
  it('keeps loose units, bulk and drums apart from boxes', () => {
    expect(tree.loose.map((e) => `${e.line.item_name}:${e.units}`)).toEqual([
      'RRU:1',
      'Tie:5',
      'Cable:0',
    ]);
  });
  it('splits one serialized line across the boxes its units went into', () => {
    expect(tree.roots[0].children[0].entries[0].serials).toEqual(['S1', 'S2']);
    expect(tree.roots[0].children[1].entries[0].lineIndex).toBe(0);
  });
  it('shows a unit whose box vanished as loose instead of dropping it', () => {
    const t = buildTree([], [line({ serials: units(['S1', 'gone']) })]);
    expect(t.loose[0].serials).toEqual(['S1']);
  });
  it('survives a loop in a stored draft', () => {
    const t = buildTree([box('x', 'X', 'y'), box('y', 'Y', 'x')], []);
    expect(t.roots.length).toBeGreaterThan(0);
  });
  it('says "1 unit" in the singular', () => {
    const t = buildTree([box('a', 'A')], [line({ serials: units(['S1', 'a']) })]);
    expect(describeCounts(t.roots[0])).toBe('1 unit');
  });
});

describe('removing a box', () => {
  const boxes = [box('pal', 'PAL-7'), box('c1', 'CTN-1', 'pal'), box('e')];
  const lines = [line({ serials: units(['S1', 'c1']) })];
  it('refuses a parent with boxes inside', () => {
    expect(removalBlock(boxes, lines, 'pal')).toMatch(/PAL-7 has 1 box inside it/);
  });
  it('refuses a box with units in it, and says how many', () => {
    expect(removalBlock(boxes, lines, 'c1')).toMatch(/CTN-1 has 1 unit in it/);
  });
  it('allows an empty box', () => {
    expect(removalBlock(boxes, lines, 'e')).toBeNull();
    expect(withoutBox(boxes, 'e').map((b) => b.key)).toEqual(['pal', 'c1']);
  });
});

describe('payload lines', () => {
  it('leaves blank box keys off and never boxes a serialized line itself', () => {
    const out = linesForPayload([
      line({
        box_key: '',
        serials: [
          { serial_number: 'S1', box_key: '' },
          { serial_number: 'S2', box_key: 'a' },
        ],
      }),
      line({ tracking_mode: 'SERIALIZED', box_key: 'a', serials: [{ serial_number: 'S3' }] }),
      line({ tracking_mode: 'BULK', quantity: '4', box_key: 'a' }),
      line({ tracking_mode: 'BULK', quantity: '4', box_key: '' }),
      line({ tracking_mode: 'REEL', box_key: 'a' }),
    ]);
    expect(out[0]).not.toHaveProperty('box_key');
    expect(out[0].serials).toEqual([
      { serial_number: 'S1' },
      { serial_number: 'S2', box_key: 'a' },
    ]);
    expect(out[1]).not.toHaveProperty('box_key');
    expect(out[2].box_key).toBe('a');
    expect(out[3]).not.toHaveProperty('box_key');
    expect(out[4]).not.toHaveProperty('box_key');
  });
});

describe('server refusals in words', () => {
  const boxes = [box('a', 'PAL-7'), box('b')];
  const lines = [line({ item_name: 'RRU', serials: units(['S9', 'a']) })];
  it('names boxes by code or as unlabelled', () => {
    const out = boxErrorMessages(
      {
        'boxes.0.code': ['Box PAL-7 appears twice (BOX_CODE_IN_USE).'],
        'boxes.1.code': ['Box x holds nothing (BOX_EMPTY).'],
      },
      boxes,
      lines,
    );
    expect(out[0]).toMatch(/^PAL-7: that code is already taken/);
    expect(out[1]).toBe('Unlabelled box 1 holds nothing. Put something in it or remove it.');
  });
  it('maps the other codes', () => {
    const one = (code: string) =>
      boxErrorMessages({ 'boxes.0.parent_key': [`x (${code})`] }, boxes, lines)[0];
    expect(one('BOX_TOO_DEEP')).toMatch(/three levels/);
    expect(one('BOX_CYCLE')).toMatch(/inside itself/);
    expect(one('BOX_MIXED_DESTINATIONS')).toMatch(/separate boxes/);
  });
  it('explains a line or unit pointing at a bad box, and keeps unknown text', () => {
    const out = boxErrorMessages(
      {
        'lines.0.serials.0.box_key': ['Not on this delivery.'],
        'lines.0.box_key': ['Serialized lines take no box.'],
      },
      boxes,
      lines,
    );
    expect(out).toEqual([
      'Unit S9: Not on this delivery.',
      'Line 1 (RRU): Serialized lines take no box.',
    ]);
  });
  it('ignores errors that are not about boxes', () => {
    expect(boxErrorMessages({ to_location: ['Required.'] }, boxes, lines)).toEqual([]);
  });
});
