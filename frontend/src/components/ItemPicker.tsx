/**
 * Find an item by typing (design §7.3b, C9).
 *
 * A combobox: a text input with a listbox under it. Online it asks the API
 * (`?search=`, 20 at a time, ordered by relevance); offline — or when the
 * request cannot reach the server — it searches the item list the sync bundle
 * saved on the phone with `rankItems`, which orders results the same way.
 * With nothing typed it offers the last 8 items picked on this phone.
 *
 * The visible label belongs to the caller's `<Field htmlFor={id}>`.
 */

import {
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from 'react';
import { useQueryClient } from '@tanstack/react-query';

import { ApiError, api } from '../api/client';
import { useDetail, useList } from '../api/hooks';
import { useSession } from '../auth/session';
import { QUICK_CREATE } from '../features/quickCreate';
import type { ItemType } from '../features/settings/types';
import { readReference } from '../offline/db';
import { useOffline } from '../offline/OfflineProvider';
import {
  PAGE_SIZE,
  pushRecent,
  rankItems,
  readRecent,
  shownText,
  type RecentItem,
} from './itemPicker';
import { Input } from './ui';

/** What a line sheet needs to know about the chosen item. */
export interface PickedItem {
  id: number;
  name: string;
  code: string;
  uom: string;
  /** The bundle's name for it. */
  tracking_mode: ItemType['default_tracking_mode'];
  /** The API's name for the same value, so screens that read an `ItemType` keep working. */
  default_tracking_mode: ItemType['default_tracking_mode'];
  /** False when unknown (an older offline bundle does not carry it). */
  is_returnable: boolean;
  category_name: string;
  description: string;
}

interface BundleItem {
  id: number;
  name: string;
  code?: string;
  uom?: string;
  tracking_mode?: ItemType['default_tracking_mode'];
  category_name?: string;
  description?: string;
  is_returnable?: boolean;
}

interface ApiItem {
  id: number;
  name: string;
  code?: string;
  uom?: string;
  default_tracking_mode?: ItemType['default_tracking_mode'];
  category_name?: string;
  description?: string;
  is_returnable?: boolean;
}

function fromApi(item: ApiItem): PickedItem {
  const mode = item.default_tracking_mode ?? 'BULK';
  return {
    id: item.id,
    name: item.name,
    code: item.code ?? '',
    uom: item.uom ?? '',
    tracking_mode: mode,
    default_tracking_mode: mode,
    is_returnable: item.is_returnable ?? false,
    category_name: item.category_name ?? '',
    description: item.description ?? '',
  };
}

function fromBundle(item: BundleItem): PickedItem {
  return fromApi({ ...item, default_tracking_mode: item.tracking_mode });
}

function fromRecent(item: RecentItem): PickedItem {
  return fromApi({
    ...item,
    default_tracking_mode: (item.tracking_mode || 'BULK') as ItemType['default_tracking_mode'],
  });
}

const DEBOUNCE_MS = 250;
const ADD_NEW_ENTRY = QUICK_CREATE['item-types'];

interface Props {
  /** For the caller's `<Field htmlFor>`. */
  id: string;
  label?: string;
  value: number | string | '';
  onChange: (item: PickedItem | null) => void;
  invalid?: boolean;
  disabled?: boolean;
  placeholder?: string;
  allowCreate?: boolean;
}

export function ItemPicker({
  id,
  label,
  value,
  onChange,
  invalid,
  disabled,
  placeholder = 'Type a name, code or description',
  allowCreate = true,
}: Props) {
  const { user, has } = useSession();
  const { online } = useOffline();
  const queryClient = useQueryClient();
  const orgSlug = user?.organization?.slug ?? '';
  const listId = `${id}-listbox`;

  // `null` means "not typing": the input shows the chosen item's name.
  const [query, setQuery] = useState<string | null>(null);
  const [debounced, setDebounced] = useState('');
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [picked, setPicked] = useState<PickedItem | null>(null);
  const [recent, setRecent] = useState<RecentItem[]>([]);
  const [bundle, setBundle] = useState<BundleItem[] | null>(null);
  const [adding, setAdding] = useState(false);
  const wrapper = useRef<HTMLDivElement>(null);

  const text = debounced.trim();
  const typed = (query ?? '').trim();
  const settling = typed !== text;

  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(query ?? ''), DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [query]);

  // Online: let React Query run the latest search; its key carries the text.
  const search = useList<ApiItem>(
    'item-types',
    { search: text, page_size: PAGE_SIZE, is_archived: false },
    { enabled: online && text !== '', retry: false },
  );
  // Online but unreachable (a dropped connection is a TypeError, not an ApiError).
  const unreachable = Boolean(search.error) && !(search.error instanceof ApiError);
  const offlineMode = !online || unreachable;

  const valueId = value === '' ? null : Number(value);
  const known = picked && picked.id === valueId ? picked : null;
  const needsLookup = valueId !== null && !known;
  const detail = useDetail<ApiItem>('item-types', valueId ?? undefined, {
    enabled: online && needsLookup,
    retry: false,
  });

  useEffect(() => {
    if (bundle !== null || !(offlineMode || needsLookup)) return;
    let live = true;
    void readReference<BundleItem>('item_types').then(({ rows }) => {
      if (live) setBundle(rows);
    });
    return () => {
      live = false;
    };
  }, [bundle, offlineMode, needsLookup]);

  const selected: PickedItem | null = useMemo(() => {
    if (valueId === null) return null;
    if (known) return known;
    if (detail.data) return fromApi(detail.data);
    const row = bundle?.find((item) => item.id === valueId);
    return row ? fromBundle(row) : null;
  }, [valueId, known, detail.data, bundle]);

  const offlineResult = useMemo(
    () => (offlineMode && text ? rankItems(bundle ?? [], text) : null),
    [offlineMode, bundle, text],
  );

  const matches: PickedItem[] = offlineResult
    ? offlineResult.matches.map(fromBundle)
    : (search.data?.results ?? []).map(fromApi);
  const total = offlineResult ? offlineResult.total : (search.data?.count ?? 0);
  const loading =
    text !== '' &&
    (settling ||
      (offlineMode ? bundle === null : search.isPending || (search.isFetching && !search.data)));

  const showRecent = typed === '';
  const rows: PickedItem[] = showRecent ? recent.map(fromRecent) : loading ? [] : matches;
  const canAdd = allowCreate && Boolean(ADD_NEW_ENTRY) && has(ADD_NEW_ENTRY.permission);
  const addIndex = canAdd ? rows.length : -1;
  const optionCount = rows.length + (canAdd ? 1 : 0);
  const activeIndex = Math.min(active, Math.max(optionCount - 1, 0));
  const optionId = (index: number) => `${id}-opt-${index}`;

  const openList = () => {
    if (disabled) return;
    setRecent(readRecent(orgSlug));
    setOpen(true);
  };

  const finish = useCallback(
    (item: PickedItem) => {
      setPicked(item);
      setQuery(null);
      setDebounced('');
      setOpen(false);
      setRecent(pushRecent(orgSlug, item));
      onChange(item);
    },
    [onChange, orgSlug],
  );

  /** A recent entry is a summary; fetch the whole item so nothing a line sheet reads is missing. */
  const choose = async (item: PickedItem, fromRecents: boolean) => {
    if (!fromRecents) {
      finish(item);
      return;
    }
    let full = item;
    try {
      if (online) {
        full = fromApi(
          await queryClient.fetchQuery({
            queryKey: ['item-types', 'detail', String(item.id)],
            queryFn: () => api.get<ApiItem>(`/item-types/${item.id}`),
            staleTime: 30_000,
          }),
        );
      } else {
        const { rows: all } = await readReference<BundleItem>('item_types');
        const row = all.find((r) => r.id === item.id);
        if (row) full = fromBundle(row);
      }
    } catch {
      // Keep what the recent list remembered.
    }
    finish(full);
  };

  const clear = () => {
    setPicked(null);
    setQuery(null);
    setDebounced('');
    setOpen(false);
    onChange(null);
  };

  const activate = (index: number) => {
    if (index === addIndex) {
      setOpen(false);
      setAdding(true);
    } else if (rows[index]) {
      void choose(rows[index], showRecent);
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      if (!open) openList();
      setActive(optionCount ? (activeIndex + 1) % optionCount : 0);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      if (!open) openList();
      setActive(optionCount ? (activeIndex - 1 + optionCount) % optionCount : 0);
    } else if (event.key === 'Enter') {
      if (open && optionCount > 0) {
        event.preventDefault();
        activate(activeIndex);
      }
    } else if (event.key === 'Escape') {
      if (open) {
        event.preventDefault();
        setOpen(false);
        setQuery(null);
      }
    }
  };

  const status = showRecent
    ? rows.length === 0
      ? 'Type a name, code or description'
      : ''
    : loading
      ? 'Searching…'
      : matches.length === 0
        ? `No item matches “${typed}”.`
        : '';
  const truncated = !showRecent && !loading ? shownText(matches.length, total) : '';

  const Sheet = ADD_NEW_ENTRY?.Sheet;

  return (
    <div
      ref={wrapper}
      className="relative"
      onBlur={(event) => {
        if (!wrapper.current?.contains(event.relatedTarget as Node | null)) {
          setOpen(false);
          setQuery(null);
        }
      }}
    >
      <Input
        id={id}
        type="text"
        role="combobox"
        aria-label={label}
        aria-expanded={open}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={open && optionCount ? optionId(activeIndex) : undefined}
        autoComplete="off"
        invalid={invalid}
        disabled={disabled}
        placeholder={placeholder}
        className={valueId !== null && !disabled ? 'pr-11' : undefined}
        value={query ?? selected?.name ?? ''}
        onFocus={openList}
        onChange={(event) => {
          setQuery(event.target.value);
          setActive(0);
          if (!open) openList();
        }}
        onKeyDown={onKeyDown}
      />
      {valueId !== null && !disabled ? (
        <button
          type="button"
          aria-label="Clear item"
          onClick={clear}
          className="absolute right-0 top-0 flex h-11 w-11 items-center justify-center text-xl text-slate-500 hover:text-slate-900"
        >
          ×
        </button>
      ) : null}

      {open ? (
        <div className="absolute left-0 right-0 z-30 mt-1 max-h-80 overflow-y-auto overflow-x-hidden rounded-lg border border-slate-300 bg-white shadow-lg">
          {showRecent && rows.length > 0 ? (
            <p className="px-3 pt-2 text-xs font-medium uppercase tracking-wide text-slate-500">
              Recently used
            </p>
          ) : null}
          {status ? (
            <p className="px-3 py-2 text-sm text-slate-500" role="status">
              {status}
            </p>
          ) : null}
          <ul id={listId} role="listbox" aria-label={label ?? 'Items'} className="m-0 list-none p-0">
            {rows.map((item, index) => (
              <li
                key={item.id}
                id={optionId(index)}
                role="option"
                aria-selected={index === activeIndex}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => activate(index)}
                onMouseEnter={() => setActive(index)}
                className={`flex min-h-[44px] cursor-pointer flex-wrap items-center gap-x-2 px-3 py-1.5 ${
                  index === activeIndex ? 'bg-slate-100' : ''
                }`}
              >
                <span className="break-words font-semibold text-slate-900">{item.name}</span>
                <span className="break-words text-sm text-slate-500">
                  {[item.code, item.category_name].filter(Boolean).join(' · ')}
                </span>
              </li>
            ))}
            {canAdd ? (
              <li
                id={optionId(addIndex)}
                role="option"
                aria-selected={addIndex === activeIndex}
                data-add-new
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => activate(addIndex)}
                onMouseEnter={() => setActive(addIndex)}
                className={`flex min-h-[44px] cursor-pointer items-center border-t border-slate-200 px-3 text-sm font-medium text-slate-900 ${
                  addIndex === activeIndex ? 'bg-slate-100' : ''
                }`}
              >
                ＋ Add new {ADD_NEW_ENTRY.noun}…
              </li>
            ) : null}
          </ul>
          {truncated ? <p className="px-3 py-2 text-sm text-slate-500">{truncated}</p> : null}
        </div>
      ) : null}

      {canAdd && adding && Sheet ? (
        <Suspense fallback={null}>
          <Sheet
            open
            onClose={() => setAdding(false)}
            onCreated={async (record) => {
              setAdding(false);
              await queryClient.invalidateQueries({ queryKey: ['item-types'] });
              try {
                const created = await api.get<ApiItem>(`/item-types/${record.id}`);
                finish(fromApi(created));
              } catch {
                finish(fromApi({ id: record.id, name: '' }));
              }
            }}
          />
        </Suspense>
      ) : null}
    </div>
  );
}
