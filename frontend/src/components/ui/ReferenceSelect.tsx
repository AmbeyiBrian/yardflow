/**
 * A select over a reference list that ends with "＋ Add new …" (design §7.3).
 *
 * Choosing that option opens the entity's own create sheet in place. When the
 * record is saved, the list is refetched and the select lands on the new row;
 * the surrounding form keeps everything else that was typed. The person who
 * had to leave a gate-out half-filled to go and create the site it was for no
 * longer has to (see `features/quickCreate`).
 *
 * Two flavours, one behaviour:
 *
 * * `ReferenceSelect` for react-hook-form fields (`form` + `name`).
 * * `ControlledReferenceSelect` for `value` / `onChange` selects. The created
 *   id is delivered through the same `onChange` the screen already wired, as a
 *   change event whose target value is the new id — every handler in this
 *   codebase reads `event.target.value` and nothing else.
 *
 * Two details carry the correctness:
 *
 * * **The sentinel never reaches the caller.** Its value is intercepted before
 *   the form or the handler sees it, so a cancelled "add new" leaves no trace
 *   and a submitted form never carries `__add_new__` as a foreign key.
 * * **Select after the options exist.** The new id is handed over only once
 *   the list query has refetched. Handing it over earlier would ask a
 *   `<select>` for a value it has no `<option>` for, which falls back to the
 *   first option and makes the new record look unselected. That exact failure
 *   has been made once already in this codebase.
 *
 * Only shown to somebody who may create the thing. An "add new site" that 403s
 * on save is worse than no option.
 */

import {
  Suspense,
  useState,
  type ChangeEvent,
  type ReactNode,
  type SelectHTMLAttributes,
} from 'react';
import type { FieldValues, Path, RegisterOptions, UseFormReturn } from 'react-hook-form';
import { useQueryClient } from '@tanstack/react-query';

import { useSession } from '../../auth/session';
import { QUICK_CREATE } from '../../features/quickCreate';
import { Select } from './index';

export const ADD_NEW = '__add_new__';

/** The option and the sheet, shared by both flavours. */
function useQuickCreate(resource: string, onCreated: (id: number) => void) {
  const { has } = useSession();
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);

  const entry = QUICK_CREATE[resource];
  const enabled = Boolean(entry) && has(entry.permission);

  const option = enabled ? (
    <option value={ADD_NEW} data-add-new>
      ＋ Add new {entry.noun}…
    </option>
  ) : null;

  const sheet =
    enabled && adding ? (
      <Suspense fallback={null}>
        <entry.Sheet
          open
          onClose={() => setAdding(false)}
          onCreated={async (record) => {
            setAdding(false);
            // Refetch first, so the option exists before it is chosen.
            await queryClient.invalidateQueries({ queryKey: [resource] });
            onCreated(record.id);
          }}
        />
      </Suspense>
    ) : null;

  return { enabled, option, sheet, open: () => setAdding(true) };
}

type BaseProps = Omit<
  SelectHTMLAttributes<HTMLSelectElement>,
  'form' | 'name' | 'onChange' | 'value' | 'defaultValue'
> & {
  /** The API resource the options come from — also the registry key. */
  resource: string;
  /** The `<option>`s. The "add new" option is appended after them. */
  children: ReactNode;
  /** `Select`'s own error styling, passed straight through. */
  invalid?: boolean;
};

type FormProps<TForm extends FieldValues> = BaseProps & {
  form: UseFormReturn<TForm>;
  name: Path<TForm>;
  rules?: RegisterOptions<TForm, Path<TForm>>;
  /** Called after the new record is selected, for anything else that must follow. */
  onCreated?: (id: number) => void;
};

export function ReferenceSelect<TForm extends FieldValues>({
  resource,
  form,
  name,
  rules,
  children,
  onCreated,
  ...rest
}: FormProps<TForm>) {
  const registration = form.register(name, rules);
  const quick = useQuickCreate(resource, (id) => {
    form.setValue(name, String(id) as TForm[Path<TForm>], {
      shouldDirty: true,
      shouldValidate: true,
    });
    onCreated?.(id);
  });

  return (
    <>
      <Select
        {...rest}
        {...registration}
        onChange={(event) => {
          if (event.target.value === ADD_NEW) {
            // Put the control back before anything reads it, then open the
            // sheet. The form never sees the sentinel.
            const current = form.getValues(name);
            event.target.value = current === undefined || current === null ? '' : String(current);
            quick.open();
            return;
          }
          void registration.onChange(event);
        }}
      >
        {children}
        {quick.option}
      </Select>
      {quick.sheet}
    </>
  );
}

type ControlledProps = BaseProps & {
  value: string | number | readonly string[] | undefined;
  onChange?: (event: ChangeEvent<HTMLSelectElement>) => void;
};

export function ControlledReferenceSelect({
  resource,
  value,
  onChange,
  children,
  ...rest
}: ControlledProps) {
  const quick = useQuickCreate(resource, (id) => {
    // The screen's own handler, given exactly what it reads: a target whose
    // value is the new id. Nothing here knows or cares which setter it calls.
    onChange?.({ target: { value: String(id) } } as unknown as ChangeEvent<HTMLSelectElement>);
  });

  return (
    <>
      <Select
        {...rest}
        value={value}
        onChange={(event) => {
          if (event.target.value === ADD_NEW) {
            // Controlled: React will re-render with `value`, but the sheet is
            // opened without the handler ever seeing the sentinel.
            quick.open();
            return;
          }
          onChange?.(event);
        }}
      >
        {children}
        {quick.option}
      </Select>
      {quick.sheet}
    </>
  );
}
