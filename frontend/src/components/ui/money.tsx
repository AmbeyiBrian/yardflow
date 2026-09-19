/**
 * Money, rendered so a column of it can actually be read (§7.3).
 *
 * Three things, and none of them is decoration:
 *
 * **Tabular numerals.** In a proportional font `1` is narrower than `8`, so
 * `1,250.00` and `999,000.00` do not line up and the eye cannot compare them
 * down a column. `tabular-nums` fixes every digit to the same width, which is
 * what makes a list of figures scannable rather than merely present.
 *
 * **Right alignment.** Decimal points stack, so the magnitude of a number is
 * its shape before anybody has read a digit — which is the difference between
 * spotting a 10× error and not.
 *
 * **Absent is not zero.** A figure withheld by O14 arrives as `undefined` and
 * renders as nothing at all. A dash or a `0.00` would be a claim about the
 * project when the truth is a fact about the reader.
 */

import type { ReactNode } from 'react';

import { cn } from './cn';

/** Digits that line up. Applied anywhere a figure sits in a column. */
export const TABULAR = 'tabular-nums';

/**
 * Format a decimal string for display.
 *
 * Returns `null` for a withheld or missing value so callers can decide between
 * rendering nothing and rendering a placeholder — the two are different, and
 * collapsing them is how "you may not see this" turns into "this is zero".
 */
export function formatMoney(value?: string | number | null): string | null {
  if (value === undefined || value === null || value === '') return null;
  const parsed = Number(value);
  if (Number.isNaN(parsed)) return String(value);
  return parsed.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

/**
 * A money figure.
 *
 * `withheld` renders nothing; pass `placeholder` only where a blank would read
 * as a broken layout rather than as an answer.
 */
export function Money({
  value,
  className,
  placeholder = '',
  tone,
}: {
  value?: string | number | null;
  className?: string;
  placeholder?: ReactNode;
  /** `bad` for a negative margin or an overrun — the one figure worth colour. */
  tone?: 'neutral' | 'good' | 'bad';
}) {
  const text = formatMoney(value);
  if (text === null) return <>{placeholder}</>;

  const negative = Number(value) < 0;
  const resolved = tone ?? (negative ? 'bad' : 'neutral');

  return (
    <span
      className={cn(
        TABULAR,
        'whitespace-nowrap',
        resolved === 'bad' && 'text-red-700',
        resolved === 'good' && 'text-emerald-700',
        className,
      )}
    >
      {text}
    </span>
  );
}

/**
 * Classes for an input that takes money.
 *
 * Right-aligned and tabular for the same reasons as the display above: a figure
 * being typed should look like the figure it will become, and a decimal point
 * that moves as you type is how a zero gets miscounted.
 */
export const MONEY_INPUT = cn(TABULAR, 'text-right');
