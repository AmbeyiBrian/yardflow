import { describe, expect, it } from 'vitest';

import { describeFieldErrors } from './hooks';

describe('describeFieldErrors', () => {
  it('uses a message that explains itself as it is', () => {
    expect(
      describeFieldErrors({ uom: ["A reel is measured, not counted. Set the unit to a length such as 'm'."] }),
    ).toBe("A reel is measured, not counted. Set the unit to a length such as 'm'.");
  });

  it('names the field in front of a stock phrase', () => {
    expect(describeFieldErrors({ 'lines.0.quantity': ['This field is required.'] })).toBe(
      'Quantity: This field is required.',
    );
    expect(describeFieldErrors({ serial_number: ['Ensure this field has no more than 150 characters.'] })).toBe(
      'Serial number: Ensure this field has no more than 150 characters.',
    );
  });

  it('joins several fields into one sentence', () => {
    expect(
      describeFieldErrors({ name: ['A name is required.'], code: ['This field may not be blank.'] }),
    ).toBe('A name is required. Code: This field may not be blank.');
  });
});
