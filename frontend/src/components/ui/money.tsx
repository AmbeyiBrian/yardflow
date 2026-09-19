/**
 * Money, rendered so a column of it can be read and a field so it can be typed
 * (§7.3, C8).
 *
 * Four things, and none of them is decoration:
 *
 * **The tenant's currency, shown.** `OrganizationSettings.currency` has been
 * settable since C8 and was displayed nowhere — so a figure was a bare number
 * and the reader supplied the currency from memory. On a system sold to more
 * than one contractor that is a guess waiting to be wrong.
 *
 * **Tabular numerals.** In a proportional font `1` is narrower than `8`, so
 * `1,250.00` and `999,000.00` do not line up and the eye cannot compare them
 * down a column.
 *
 * **Grouped digits in inputs too.** `40000000` typed into a box is not a figure
 * anybody can check; `40,000,000.00` is. The grouping appears when the field
 * loses focus and disappears while it has it, because separators that shift
 * under the cursor make typing worse, not better.
 *
 * **Absent is not zero.** A figure withheld by O14 arrives as `undefined` and
 * renders as nothing at all. A dash or a `0.00` would be a claim about the
 * project when the truth is a fact about the reader.
 */

import { forwardRef, useState, type InputHTMLAttributes, type ReactNode } from 'react';

import { useSession } from '../../auth/session';
import { cn } from './cn';
import { Input } from './index';

/** Digits that line up. Applied anywhere a figure sits in a column. */
export const TABULAR = 'tabular-nums';

/** The tenant's currency code, falling back to the server's own default. */
export function useCurrency(): string {
  const { user } = useSession();
  return user?.organization?.settings?.currency || 'KES';
}

/**
 * Format a decimal for display, in a currency.
 *
 * Returns `null` for a withheld or missing value so callers can tell "you were
 * not told" from "this is zero" — collapsing the two is how a restriction turns
 * into a false figure.
 */
export function formatMoney(
  value?: string | number | null,
  currency?: string,
): string | null {
  if (value === undefined || value === null || value === '') return null;
  const parsed = Number(value);
  if (Number.isNaN(parsed)) return String(value);

  if (!currency) {
    return parsed.toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  try {
    return parsed.toLocaleString(undefined, {
      style: 'currency',
      currency,
      currencyDisplay: 'code',
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  } catch {
    // An unknown or mistyped code must not blank the figure — the number is
    // the part that matters, and a missing symbol is the lesser problem.
    return `${currency} ${parsed.toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
  }
}

/**
 * A money figure.
 *
 * `withCurrency` is on by default. Turn it off inside a table whose header
 * already says the currency, where repeating it on every row is noise.
 */
export function Money({
  value,
  className,
  placeholder = '',
  tone,
  withCurrency = true,
}: {
  value?: string | number | null;
  className?: string;
  placeholder?: ReactNode;
  /** `bad` for a negative margin or an overrun — the one figure worth colour. */
  tone?: 'neutral' | 'good' | 'bad';
  withCurrency?: boolean;
}) {
  const currency = useCurrency();
  const text = formatMoney(value, withCurrency ? currency : undefined);
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
 * An input that takes money.
 *
 * Carries the tenant's currency inside the field, and echoes the grouped figure
 * underneath as it is typed — `40000000` becomes a legible
 * `KES 40,000,000.00` a person can actually check.
 *
 * The echo sits **below** the field rather than replacing what is in it. The
 * obvious thing is to reformat the value in place, and it is the wrong thing
 * twice over: the input has to stay uncontrolled for `register` to drive it, and
 * separators inserted mid-typing move the caret under the user's fingers. So the
 * field holds the plain number the API wants, and the echo does the reading.
 */
export const MoneyInput = forwardRef<
  HTMLInputElement,
  Omit<InputHTMLAttributes<HTMLInputElement>, 'type'>
>(function MoneyInput({ className, onChange, defaultValue, ...props }, ref) {
  const currency = useCurrency();
  const [typed, setTyped] = useState(
    defaultValue === undefined || defaultValue === null ? '' : String(defaultValue),
  );

  const echo = formatMoney(typed, currency);

  return (
    <div className="flex flex-col gap-1">
      <div className="relative">
        <span
          aria-hidden
          className="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-sm font-medium text-slate-500"
        >
          {currency}
        </span>
        <Input
          ref={ref}
          inputMode="decimal"
          // Padded past the currency so a long figure never runs under it.
          className={cn(TABULAR, 'pl-14 text-right', className)}
          defaultValue={defaultValue}
          onChange={(event) => {
            setTyped(event.target.value);
            onChange?.(event);
          }}
          {...props}
        />
      </div>
      {echo ? (
        <p className={cn(TABULAR, 'text-right text-xs text-slate-500')}>{echo}</p>
      ) : null}
    </div>
  );
});

/**
 * Classes for a money input wired directly to a form register.
 *
 * Prefer `MoneyInput`. This exists for the cases where a caller needs the bare
 * input element and only wants the alignment.
 */
export const MONEY_INPUT = cn(TABULAR, 'text-right');
