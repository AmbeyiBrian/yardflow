/**
 * A search box for a list screen (§7.3).
 *
 * The API has supported `?search=` on nearly every collection since phase 2;
 * only four screens ever offered a box for it. This is that box, in one place,
 * so the next list does not have to decide again what the placeholder says or
 * whether it is labelled for a screen reader.
 *
 * **Debounced.** The screens that already search fire a request per keystroke.
 * React Query cancels the ones it can, but on a phone at the edge of a signal
 * every keystroke is a round trip somebody is waiting on — so the value shown
 * updates immediately and the query follows a beat later.
 *
 * The clear button is not decoration either: on a touch keyboard, emptying a
 * field means holding backspace and watching results flicker back.
 */

import { useEffect, useState } from 'react';

import { Input } from './index';
import { cn } from './cn';

export function SearchField({
  value,
  onChange,
  label,
  placeholder,
  className,
  delay = 250,
}: {
  /** The committed value — what the query uses. */
  value: string;
  onChange: (value: string) => void;
  /** For a screen reader, and for a test to find it by. */
  label: string;
  placeholder?: string;
  className?: string;
  delay?: number;
}) {
  const [typed, setTyped] = useState(value);

  // Follow the outside if it changes underneath — a filter reset, say.
  useEffect(() => {
    setTyped(value);
  }, [value]);

  useEffect(() => {
    if (typed === value) return;
    const timer = setTimeout(() => onChange(typed), delay);
    return () => clearTimeout(timer);
    // `onChange` is intentionally not a dependency: callers pass an inline
    // setter, and including it would restart the timer on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [typed, value, delay]);

  return (
    <div className={cn('relative max-w-sm flex-1', className)}>
      <Input
        type="search"
        aria-label={label}
        placeholder={placeholder ?? 'Search'}
        value={typed}
        onChange={(event) => setTyped(event.target.value)}
        className={typed ? 'pr-16' : undefined}
      />
      {typed ? (
        <button
          type="button"
          onClick={() => {
            setTyped('');
            onChange('');
          }}
          className="absolute top-1/2 right-2 -translate-y-1/2 rounded px-2 py-1 text-sm text-slate-500"
        >
          Clear
        </button>
      ) : null}
    </div>
  );
}
