import { describe, expect, it } from 'vitest';

import { lineSiteText } from './lineSite';

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
