import { describe, expect, it } from 'vitest';

import { deliverySiteText, lineSiteText } from './lineSite';

const a = { site: 1, name: 'Atlantis' };
const b = { site: 2, name: 'Baobab' };

describe('lineSiteText', () => {
  it('uses the line site, then the delivery site', () => {
    expect(lineSiteText({ for_site_name: 'X' }, 'Y')).toBe('X');
    expect(lineSiteText({}, 'Y')).toBe('Y');
  });
  it('says earmarked later when the delivery named no site', () => {
    expect(lineSiteText({ earmarked_now: [a, b] })).toBe('Atlantis, Baobab (earmarked later)');
  });
  it('shows a dash when nothing is known', () => {
    expect(lineSiteText({ earmarked_now: [] })).toBe('—');
    expect(lineSiteText({})).toBe('—');
  });
  it('shows the change when the earmark differs from what was said', () => {
    expect(lineSiteText({ earmarked_now: [b] }, 'Atlantis')).toBe('Atlantis · now Baobab');
    expect(lineSiteText({ earmarked_now: [a, b] }, 'Atlantis')).toBe(
      'Atlantis · now Atlantis, Baobab',
    );
  });
  it('shows just the site when the earmark matches', () => {
    expect(lineSiteText({ earmarked_now: [a] }, 'Atlantis')).toBe('Atlantis');
  });
});

describe('deliverySiteText', () => {
  const atlantis = { site: 1, name: 'Atlantis Business Park' };
  it('keeps what the delivery said', () => {
    expect(deliverySiteText('Karen', [{ earmarked_now: [atlantis] }])).toBe('Karen');
  });
  it('follows its lines when it said nothing', () => {
    expect(deliverySiteText(undefined, [{ earmarked_now: [atlantis] }, { earmarked_now: [atlantis] }])).toBe(
      'Atlantis Business Park (earmarked later)',
    );
  });
  it('names a site its lines said themselves without "later"', () => {
    expect(deliverySiteText('', [{ for_site_name: 'Karen' }])).toBe('Karen');
  });
  it('says several when the lines differ', () => {
    expect(deliverySiteText(undefined, [{ for_site_name: 'Karen' }, { earmarked_now: [atlantis] }])).toBe(
      'Several sites — see each line',
    );
  });
  it('says nothing particular when nothing is known', () => {
    expect(deliverySiteText(undefined, [{}])).toBe('Not for a particular site');
  });
});
