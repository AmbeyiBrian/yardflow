/**
 * UI primitives (design §7.3).
 *
 * A small hand-rolled set in the shadcn/ui idiom — Tailwind classes, `cn()` for
 * merging, `data-*` for state — rather than the shadcn CLI's generated tree.
 * The reason is scope: the components this build actually needs are few, and
 * each one here encodes a §7.3 rule that a generated component would not:
 * 44px minimum touch targets, bottom-anchored primary actions, and cards on
 * narrow screens that become tables at `md`.
 *
 * Adding the full shadcn/ui set later is a drop-in; nothing here depends on it
 * being absent.
 */

import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from 'react';
import { forwardRef } from 'react';

import { cn } from './cn';
import { LogoActivity } from '../Logo';

/* -------------------------------------------------------------------------- */
/* Button                                                                     */
/* -------------------------------------------------------------------------- */

type ButtonVariant = 'primary' | 'secondary' | 'danger' | 'ghost';

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  /** Fill the width — the default for a bottom-anchored primary action. */
  block?: boolean;
  loading?: boolean;
}

const VARIANTS: Record<ButtonVariant, string> = {
  primary: 'bg-slate-900 text-white hover:bg-slate-800 disabled:bg-slate-400',
  secondary:
    'bg-white text-slate-900 border border-slate-300 hover:bg-slate-50 disabled:text-slate-400',
  danger: 'bg-red-600 text-white hover:bg-red-700 disabled:bg-red-300',
  ghost: 'bg-transparent text-slate-700 hover:bg-slate-100',
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { className, variant = 'primary', block, loading, disabled, children, ...props },
  ref,
) {
  return (
    <button
      ref={ref}
      // 44px minimum height: the §7.3 rule, applied here so no caller has to
      // remember it.
      className={cn(
        'inline-flex min-h-[44px] items-center justify-center gap-2 rounded-lg px-4',
        'text-base font-medium transition-colors',
        'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-900',
        'disabled:cursor-not-allowed',
        VARIANTS[variant],
        block && 'w-full',
        className,
      )}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...props}
    >
      {loading ? <Spinner /> : null}
      {children}
    </button>
  );
});

/**
 * Waiting, as the product mark rather than a spinning ring (§7.3a).
 *
 * The `Y` with a band of light sweeping up through it. Kept behind the name
 * `Spinner` deliberately: it appears in some thirty places and inside every
 * loading button, and the point was to change all of them at once, not to make
 * thirty edits and miss four.
 *
 * `className` still sets the size, so `size-4` and `size-6` behave as before —
 * though the Y is narrower than it is tall, so width follows the letterform
 * rather than filling a square.
 */
export function Spinner({ className }: { className?: string }) {
  return <LogoActivity className={cn('h-4 w-auto', className)} />;
}

/* -------------------------------------------------------------------------- */
/* Field and Input                                                            */
/* -------------------------------------------------------------------------- */

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  invalid?: boolean;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { className, invalid, ...props },
  ref,
) {
  return (
    <input
      ref={ref}
      aria-invalid={invalid || undefined}
      className={cn(
        'min-h-[44px] w-full rounded-lg border px-3 text-base',
        // 16px text stops iOS zooming the viewport on focus, which on a phone
        // reads as the layout breaking.
        'bg-white text-slate-900',
        invalid ? 'border-red-500' : 'border-slate-300',
        'focus:outline-2 focus:outline-offset-0 focus:outline-slate-900',
        className,
      )}
      {...props}
    />
  );
});

export const Select = forwardRef<
  HTMLSelectElement,
  SelectHTMLAttributes<HTMLSelectElement> & { invalid?: boolean }
>(function Select({ className, invalid, children, ...props }, ref) {
  return (
    <select
      ref={ref}
      aria-invalid={invalid || undefined}
      className={cn(
        'min-h-[44px] w-full rounded-lg border bg-white px-3 text-base text-slate-900',
        invalid ? 'border-red-500' : 'border-slate-300',
        'focus:outline-2 focus:outline-offset-0 focus:outline-slate-900',
        className,
      )}
      {...props}
    >
      {children}
    </select>
  );
});

export const Textarea = forwardRef<
  HTMLTextAreaElement,
  TextareaHTMLAttributes<HTMLTextAreaElement> & { invalid?: boolean }
>(function Textarea({ className, invalid, rows, ...props }, ref) {
  return (
    <textarea
      ref={ref}
      aria-invalid={invalid || undefined}
      rows={rows ?? 3}
      className={cn(
        'w-full rounded-lg border bg-white p-3 text-base text-slate-900',
        invalid ? 'border-red-500' : 'border-slate-300',
        'focus:outline-2 focus:outline-offset-0 focus:outline-slate-900',
        className,
      )}
      {...props}
    />
  );
});

/** A checkbox with its label, sized for a 44px target so gloves work (§7.3). */
export const Checkbox = forwardRef<
  HTMLInputElement,
  InputHTMLAttributes<HTMLInputElement> & { label: string; hint?: string }
>(function Checkbox({ className, label, hint, id, ...props }, ref) {
  return (
    <label
      htmlFor={id}
      className={cn(
        'flex min-h-[44px] cursor-pointer items-start gap-3 py-2 text-sm text-slate-800',
        className,
      )}
    >
      <input
        ref={ref}
        id={id}
        type="checkbox"
        className="mt-0.5 size-5 shrink-0 rounded border-slate-400 accent-slate-900"
        {...props}
      />
      <span>
        <span className="font-medium">{label}</span>
        {hint ? <span className="block text-slate-500">{hint}</span> : null}
      </span>
    </label>
  );
});

export function Field({
  label,
  hint,
  error,
  htmlFor,
  children,
}: {
  label: string;
  hint?: string;
  error?: string;
  htmlFor?: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={htmlFor} className="text-sm font-medium text-slate-700">
        {label}
      </label>
      {children}
      {hint && !error ? <p className="text-sm text-slate-500">{hint}</p> : null}
      {/* role="alert" so a screen reader announces a validation failure. */}
      {error ? (
        <p role="alert" className="text-sm text-red-600">
          {error}
        </p>
      ) : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Layout                                                                     */
/* -------------------------------------------------------------------------- */

export function Card({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <div className={cn('rounded-xl border border-slate-200 bg-white p-4', className)}>
      {children}
    </div>
  );
}

/**
 * A bottom-anchored action bar (§7.3).
 *
 * Primary actions are thumb-reachable on a phone and inline on a desktop, so
 * the storekeeper's one-handed flow and the reporting screens share components.
 */
export function ActionBar({ children }: { children: ReactNode }) {
  return (
    <div
      className={cn(
        'sticky bottom-0 z-10 flex gap-3 border-t border-slate-200 bg-white/95 px-4 py-3',
        'pb-safe backdrop-blur md:static md:border-0 md:bg-transparent md:px-0 md:backdrop-blur-none',
      )}
    >
      {children}
    </div>
  );
}

export function Banner({
  tone = 'info',
  children,
}: {
  tone?: 'info' | 'success' | 'warning' | 'error';
  children: ReactNode;
}) {
  const tones = {
    info: 'bg-slate-100 text-slate-800',
    // "Everything is accounted for" is a result, not a neutral note — a
    // reconciliation that balances should look different from one that does not.
    success: 'bg-emerald-100 text-emerald-900',
    warning: 'bg-amber-100 text-amber-900',
    error: 'bg-red-100 text-red-900',
  } as const;

  return (
    <div
      // An error is assertive: it interrupts, because it means the thing you
      // just did did not happen. Everything else is polite and waits its turn.
      // Both were `status` before, so a failed release announced itself with the
      // same urgency as a hint.
      role={tone === 'error' ? 'alert' : 'status'}
      className={cn('rounded-lg px-4 py-3 text-sm', tones[tone])}
    >
      {children}
    </div>
  );
}

/** E1: client-owned stock must be visually distinct wherever it appears. */
export function OwnershipBadge({ client }: { client?: string | null }) {
  return client ? (
    <span className="rounded-full bg-[var(--color-client-owned)]/12 px-2 py-0.5 text-xs font-medium text-[var(--color-client-owned)]">
      {client}
    </span>
  ) : (
    <span className="rounded-full bg-[var(--color-own-stock)]/12 px-2 py-0.5 text-xs font-medium text-[var(--color-own-stock)]">
      Own stock
    </span>
  );
}
